"""Owner-scoped batch 1-minute backfill with per-instrument progress, rate-limit pauses and before/after data quality.

The job record lives in the existing in-memory sync-job store (``market_data._history_sync_jobs``) so the existing
``GET /market-data/history/sync-jobs/{id}`` endpoint reports it. Nothing is ever padded: whatever the provider does not return stays
missing, is re-measured, and is reported with the recorded reason. Windows the provider structurally does not serve are recorded by
``provider_coverage`` and excluded from the refusal share (disclosed, threshold unchanged).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Instrument, User
from app.services.bar_quality import DEFAULT_REFUSE_MISSING_PCT, clean_and_validate
from app.services.provider_coverage import describe, detect_unserved_tail, unserved_ranges
from app.services.timeframes import UnsupportedMarket, profile_for

logger = logging.getLogger(__name__)

PROVIDER = "dhan"
RATE_LIMIT_PAUSES_SECONDS = (20, 45, 75)   # the pauses Dhan needed during the manual run
CHUNK_DAYS = 89
BATCH_KEY = "intraday_1m_batch"


def _measure(db: Session, instrument: Instrument, start: datetime, end: datetime) -> dict[str, Any]:
    from app.services.intraday_runner import load_bars

    try:
        prof = profile_for(instrument)
    except UnsupportedMarket:
        return {"status": "unsupported", "missing_pct": None, "expected_minutes": 0, "missing_minutes": 0, "bars": 0}
    raw = load_bars(db, instrument.id, start, end)
    now = min(datetime.now(timezone.utc), end)
    _, report = clean_and_validate(raw, prof, now=pd.Timestamp(now), unserved=unserved_ranges(db, instrument.id, PROVIDER))
    return {"status": report["status"], "bars": report.get("bars", 0), "expected_minutes": report.get("expected_minutes", 0),
            "missing_minutes": report.get("missing_minutes", 0), "missing_pct": report.get("missing_pct"),
            "provider_unserved_minutes": (report.get("provider_unserved") or {}).get("excluded_minutes", 0),
            "gaps": report.get("gaps", [])[:10], "sessions_no_data": report.get("sessions_no_data", [])[:10]}


def _rate_limited(attempts: list[dict[str, Any]]) -> bool:
    return any(a.get("outcome") == "rate_limited" or a.get("http_status") == 429
               or a.get("reason") in {"rate_limited", "circuit_breaker_open", "circuit_breaker_probe_in_progress"} for a in attempts)


def _attempts_of(result: Any) -> list[dict[str, Any]]:
    return [a.model_dump() if hasattr(a, "model_dump") else dict(a) for a in getattr(result, "provider_attempts", [])]


def new_job(owner_id: UUID, instruments: list[Instrument], start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "id": str(uuid4()), "kind": BATCH_KEY, "owner_id": str(owner_id), "status": "queued", "interval": "1m", "error": None,
        "window": {"start": start.isoformat(), "end": end.isoformat()}, "started_at": datetime.now(timezone.utc).isoformat(),
        "refuse_threshold_pct": DEFAULT_REFUSE_MISSING_PCT, "pause": None, "current": None,
        "instruments_total": len(instruments), "instruments_done": 0,
        "instruments": [{"instrument_id": str(i.id), "symbol": i.symbol, "status": "queued", "before": None, "after": None, "bars_fetched": 0,
                         "attempts": 0, "reason": None, "unrecoverable": [], "provider_coverage": None} for i in instruments],
    }


def run_batch(job_id: str, user_id: UUID, instrument_ids: list[UUID], start: datetime, end: datetime) -> None:
    from app.api import market_data as md
    from app.db import Session as SessionFactory, engine

    def update_row(index: int, **fields: Any) -> None:
        with md._history_sync_jobs_lock:
            job = md._history_sync_jobs.get(job_id)
            if job is not None:
                job["instruments"][index].update(fields)

    md._update_history_sync_job(job_id, status="running")
    try:
        for index, instrument_id in enumerate(instrument_ids):
            with SessionFactory(engine) as db:
                user, instrument = db.get(User, user_id), db.get(Instrument, instrument_id)
                if user is None or instrument is None:
                    update_row(index, status="failed", reason="instrument_or_owner_unavailable")
                    continue
                md._update_history_sync_job(job_id, current={"instrument_id": str(instrument.id), "symbol": instrument.symbol})
                _backfill_one(md, db, job_id, index, user, instrument, start, end, update_row)
            with md._history_sync_jobs_lock:
                md._history_sync_jobs[job_id]["instruments_done"] = index + 1
        md._update_history_sync_job(job_id, status="completed", current=None, pause=None, finished_at=datetime.now(timezone.utc).isoformat())
    except Exception:  # noqa: BLE001 - recorded on the job, never raised into the worker
        logger.exception("intraday_batch_backfill_failed", extra={"job_id": job_id})
        md._update_history_sync_job(job_id, status="failed", error="backfill_failed", current=None, pause=None)
    finally:
        with md._history_sync_jobs_lock:
            if md._history_sync_jobs_in_flight.get((user_id, BATCH_KEY)) == job_id:
                md._history_sync_jobs_in_flight.pop((user_id, BATCH_KEY), None)


def _backfill_one(md: Any, db: Session, job_id: str, index: int, user: User, instrument: Instrument, start: datetime, end: datetime, update_row: Any) -> None:
    before = _measure(db, instrument, start, end)
    update_row(index, status="running", before=before)
    if before["status"] == "unsupported":
        update_row(index, status="failed", reason="unsupported_market: only Indian equities and crypto have 1-minute quality checks", after=before)
        return
    provider = md.market_data_provider_for_user(db, user.id)
    chunks: list[tuple[datetime, datetime]] = []
    chunk_end = end
    while chunk_end > start:
        chunk_start = max(start, chunk_end - timedelta(days=CHUNK_DAYS))
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start
    fetched, failure = 0, None
    if before["missing_minutes"] == 0 and before["status"] != "refuse":
        chunks = []   # nothing missing in served minutes: no provider request is made
    for chunk_start, chunk_end in chunks:
        for attempt in range(len(RATE_LIMIT_PAUSES_SECONDS) + 1):
            update_row(index, status="running", attempts=attempt + 1)
            md._update_history_sync_job(job_id, pause=None)
            attempts: list[dict[str, Any]] = []
            try:
                result = md.sync_history(instrument_id=instrument.id, start=chunk_start, end=chunk_end, interval="1m",
                                         limit=min(md.MAX_DHAN_1M_BACKFILL_BARS, md.MAX_DHAN_1M_CHUNK_BARS), provider=provider, _user=user, db=db,
                                         background_tasks=None)
                attempts = _attempts_of(result)
                unavailable = result.sync_status == "unavailable"
                fetched += sum(a["bars"] for a in attempts if a["provider"] == PROVIDER)
            except HTTPException as error:
                db.rollback()
                detail = error.detail if isinstance(error.detail, dict) else {}
                attempts = list(detail.get("provider_attempts", []))
                unavailable = True
            if not unavailable:
                failure = None
                break
            if _rate_limited(attempts) and attempt < len(RATE_LIMIT_PAUSES_SECONDS):
                seconds = RATE_LIMIT_PAUSES_SECONDS[attempt]
                until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                md._update_history_sync_job(job_id, pause={"until": until.isoformat(), "seconds": seconds, "attempt": attempt + 1,
                                                          "of": len(RATE_LIMIT_PAUSES_SECONDS), "symbol": instrument.symbol,
                                                          "reason": "Dhan is rate-limiting history requests; waiting before retrying"})
                update_row(index, status="waiting_rate_limit")
                time.sleep(seconds)
                continue
            first = next((a for a in attempts if a.get("provider") == PROVIDER), attempts[0] if attempts else {})
            failure = ("provider_rate_limited_after_retries" if _rate_limited(attempts)
                       else ": ".join(p for p in ("provider_history_unavailable", first.get("provider_error_code"), (first.get("provider_message") or first.get("reason") or "")[:180]) if p))
            break
        md._update_history_sync_job(job_id, pause=None)
        if failure:
            break
    detect_unserved_tail(db, instrument, PROVIDER, datetime.now(timezone.utc))
    after = _measure(db, instrument, start, end)
    unrecoverable = []
    if failure is None and after["missing_minutes"] > 0 and chunks:
        # The provider answered every request successfully and these served-minute gaps remain: it returned no candles for them.
        unrecoverable = [{**gap, "reason": "provider_returned_no_candles_after_successful_fetch"} for gap in after["gaps"]]
        unrecoverable += [{"day": day, "missing_minutes": None, "reason": "provider_returned_no_candles_for_session"} for day in after["sessions_no_data"]]
    status = "failed" if failure else "unrecoverable_gaps" if unrecoverable else "completed"
    update_row(index, status=status, after=after, bars_fetched=fetched, reason=failure, unrecoverable=unrecoverable, provider_coverage=describe(db, instrument.id, PROVIDER))
