"""Resumable backfill of Yahoo financial statements for stocks that have none, or only stale ones (Dhan Company Info provides no statements, so Yahoo is the source).

Why it exists: on 22 Nov 2026 171 of 343 research-universe stocks had no annual statements, 150 of them because the Yahoo circuit breaker was open when their nightly slot came up, and the
pipeline only retries a stock once a day. Rules: one request every ``pause`` seconds; exponential backoff on a rate limit; after ``max_consecutive_rate_limits`` in a row stop for
``cooloff_seconds`` (30 minutes) and resume; every attempt is written to ``fundamentals_fetch_log`` so a rate-limited stock (retry queued) is never mistaken for one that has no statements, and the job
is resumable (the queue is recomputed from the stored statements and that log on every start). Uses the same provider and the same Yahoo circuit breaker (``app.yahoo_guard``) as the research pipeline.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import yahoo_guard
from app.models import Instrument, InstrumentFundamental
from app.models.fundamentals_ops import FundamentalsFetchLog
from app.services import fundamentals as fundamentals_service

logger = logging.getLogger(__name__)
RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "rate-limit", "ratelimit", "invalid crumb", "401", "unauthorized", "blocking")
NO_STATEMENTS_RETRY_DAYS = 30


def classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    return "rate_limited" if any(m in text for m in RATE_LIMIT_MARKERS) else "failed"


def record_attempt(db: Session, instrument_id, outcome: str, *, annual: int | None = None, quarterly: int | None = None, detail: str | None = None, origin: str = "research_run",
                   now: datetime | None = None) -> None:
    db.add(FundamentalsFetchLog(instrument_id=instrument_id, attempted_at=now or datetime.now(timezone.utc), outcome=outcome, annual_periods=annual, quarterly_periods=quarterly,
                                detail=(detail or None) and detail[:300], origin=origin))
    db.commit()


def latest_log(db: Session, instrument_id) -> FundamentalsFetchLog | None:
    return db.scalar(select(FundamentalsFetchLog).where(FundamentalsFetchLog.instrument_id == instrument_id).order_by(FundamentalsFetchLog.attempted_at.desc()).limit(1))


def no_statements_recent(db: Session, instrument_id, now: datetime | None = None) -> FundamentalsFetchLog | None:
    """The latest log row if it says Yahoo has NO statements for this stock and is recent enough not to ask again yet."""
    row = latest_log(db, instrument_id)
    now = now or datetime.now(timezone.utc)
    if row is not None and row.outcome == "no_statements" and (now - _aware(row.attempted_at)) < timedelta(days=NO_STATEMENTS_RETRY_DAYS):
        return row
    return None


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def candidates(db: Session, instruments: list[Instrument], now: datetime | None = None, *, limit: int | None = None) -> list[tuple[Instrument, str]]:
    """(instrument, why) for stocks with no annual statements or stale ones, ordered: priority input order first, then rate-limited retries (oldest attempt first), never attempted, failed, stale.
    Stocks whose latest attempt found no statements are skipped for 30 days."""
    now = now or datetime.now(timezone.utc)
    stats = {r[0]: (r[1], r[2]) for r in db.execute(select(InstrumentFundamental.instrument_id, func.count().filter(InstrumentFundamental.period_type == "annual"),
                                                         func.max(InstrumentFundamental.fetched_at)).group_by(InstrumentFundamental.instrument_id))}
    out: list[tuple[int, datetime, int, Instrument, str]] = []
    for pos, inst in enumerate(instruments):
        annual, fetched = stats.get(inst.id, (0, None))
        stale = fetched is not None and fundamentals_service.is_stale(_aware(fetched), now)
        if annual and not stale:
            continue
        if no_statements_recent(db, inst.id, now) is not None:
            continue
        last = latest_log(db, inst.id)
        if not annual:
            kind = ("rate_limited retry" if last is not None and last.outcome == "rate_limited" else "failed retry" if last is not None and last.outcome == "failed" else "never attempted" if last is None else "no annual statements")
        else:
            kind = "stale statements"
        rank = {"rate_limited retry": 0, "never attempted": 1, "no annual statements": 2, "failed retry": 3, "stale statements": 4}[kind]
        out.append((rank, _aware(last.attempted_at) if last is not None else datetime.min.replace(tzinfo=timezone.utc), pos, inst, kind))
    out.sort(key=lambda t: (t[0], t[1], t[2]))               # kind, then oldest attempt, then the caller's priority order
    res = [(i, k) for _, _, _, i, k in out]
    return res[:limit] if limit else res


def fetch_one(db: Session, instrument: Instrument, *, sync: Callable[..., Any] | None = None, origin: str = "backfill") -> str:
    """One Yahoo statements fetch for one stock; writes the log row and returns the outcome (refreshed | rate_limited | no_statements | failed)."""
    from app.services import research_pipeline as rp
    if not yahoo_guard.allowed():
        record_attempt(db, instrument.id, "rate_limited", detail=f"Yahoo circuit breaker open for about {yahoo_guard.seconds_left() // 60} min", origin=origin)
        return "rate_limited"
    try:
        if sync is None:
            providers = rp._providers(rp.create_market_data_provider_chain(("yfinance",)))
            provider = next((p for p in providers if p.name == "yfinance"), None)
            if provider is None:
                record_attempt(db, instrument.id, "failed", detail="Yahoo collector is not configured", origin=origin)
                return "failed"
            candidate, _ = rp._resolve_provider_candidate(db, instrument, provider)
            sync = lambda: fundamentals_service.sync_instrument_fundamentals(db, instrument, candidate.provider_symbol)  # noqa: E731
        rows, _snap = sync()
    except Exception as exc:  # noqa: BLE001 - classified, logged, never raised: one stock must not stop the job
        db.rollback()
        outcome = classify_error(exc)
        if outcome == "rate_limited" or not isinstance(exc, (ImportError, AttributeError, KeyError, TypeError)):
            yahoo_guard.record(False)                  # only a genuine Yahoo/provider failure counts against the breaker, never our own programming errors
        record_attempt(db, instrument.id, outcome, detail=f"{type(exc).__name__}: {str(exc)[:200]}", origin=origin)
        return outcome
    yahoo_guard.record(True)
    annual = sum(1 for r in rows if r.period_type == "annual")
    quarterly = sum(1 for r in rows if r.period_type == "quarterly")
    outcome = "refreshed" if (annual or quarterly) else "no_statements"
    record_attempt(db, instrument.id, outcome, annual=annual, quarterly=quarterly, detail=None if outcome == "refreshed" else "Yahoo returned no annual or quarterly statements for this security", origin=origin)
    return outcome


def run_backfill(session_factory: Callable[[], Session], load_instruments: Callable[[Session], list[Instrument]], *, pause: float = 4.0, backoff_base: float = 8.0, backoff_cap: float = 300.0,
                 max_consecutive_rate_limits: int = 3, cooloff_seconds: float = 1800.0, limit: int | None = None, max_passes: int = 6, fetcher: Callable[..., str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, on_progress: Callable[[dict], None] | None = None) -> dict[str, Any]:
    """Process the queue until it is empty or ``max_passes`` full passes have run. A rate-limited stock goes back into the next pass. Returns counts."""
    fetcher = fetcher or fetch_one
    summary: dict[str, Any] = {"refreshed": 0, "no_statements": 0, "rate_limited": 0, "failed": 0, "cooloffs": 0, "passes": 0, "attempts": 0}
    for _ in range(max_passes):
        with session_factory() as db:
            queue = candidates(db, load_instruments(db), limit=limit)
        if not queue:
            break
        summary["passes"] += 1
        consecutive = 0
        for inst_id, name in [(i.id, i.symbol) for i, _ in queue]:
            with session_factory() as db:
                instrument = db.get(Instrument, inst_id)
                if not yahoo_guard.allowed():                      # this process's breaker is open: rest, then clear OUR breaker and resume
                    sleep(cooloff_seconds)
                    summary["cooloffs"] += 1
                    yahoo_guard.reset()
                    consecutive = 0
                outcome = fetcher(db, instrument)
            summary["attempts"] += 1
            summary[outcome] += 1
            if on_progress:
                on_progress({"symbol": name, "outcome": outcome, **summary})
            if outcome == "rate_limited":
                consecutive += 1
                if consecutive >= max_consecutive_rate_limits:
                    sleep(cooloff_seconds)                           # repeated limits: stop for 30 minutes, then resume
                    summary["cooloffs"] += 1
                    yahoo_guard.reset()
                    consecutive = 0
                else:
                    sleep(min(backoff_cap, backoff_base * 2 ** (consecutive - 1)))      # exponential backoff
            else:
                consecutive = 0
                sleep(pause)
    return summary
