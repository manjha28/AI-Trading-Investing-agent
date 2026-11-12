"""Coverage reporting and an adapter for an AUTHORISED fundamentals feed.

Access finding (see report): Screener.in publishes no API, data feed or licensing programme, its Terms license
"personal, non-commercial transitory viewing only" and forbid copying/mirroring, and its pages credit CMOTS Internet
Technologies as the data provider. Automated collection from screener.in is therefore NOT implemented. This module
is the integration point for a feed the owner is licensed to use (a written Screener agreement, or a data vendor such
as the underlying provider). It is disabled unless FUNDAMENTALS_FEED_ENABLED, a base URL and an API key are set.

Feed contract (JSON, one company per request, absolute INR, never Crores):
    GET {base}/fundamentals/{symbol}?basis=consolidated   Authorization: Bearer <key>
    {"symbol": "BEL", "basis": "consolidated", "currency": "INR", "unit": "INR",
     "periods": [{"period_type": "annual"|"quarterly", "period_end": "2025-03-31",
                  "fields": {"revenue": "1234.5", ...canonical names...}, "extras": {"interest_expense": "..."}}]}
Anything that does not match is rejected, never repaired. Imports reuse ``screener_import.apply_gap_fill`` so
existing values are never overwritten, gaps are filled per field with provenance, and conflicts are recorded.
"""

import asyncio
import logging
import random
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import Instrument, InstrumentFundamental
from app.services import screener_import as si

logger = logging.getLogger(__name__)
CANONICAL = set(si.CANONICAL_FIELDS)
BASES = {"consolidated", "standalone"}
RETRYABLE = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
_LAST_ERRORS: dict[str, dict[str, str]] = {}  # per-symbol, process-local: the last failure and when


class FeedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def feed_configured() -> bool:
    key = settings.fundamentals_feed_api_key
    return bool(settings.fundamentals_feed_enabled and settings.fundamentals_feed_base_url
                and key is not None and key.get_secret_value())


def parse_feed_payload(payload: Any, symbol: str) -> si.ParsedScreener:
    if not isinstance(payload, dict) or str(payload.get("symbol", "")).upper() != symbol.upper():
        raise FeedError("feed_symbol_mismatch", "The feed response is for a different symbol.")
    if payload.get("currency") != "INR" or payload.get("unit") != "INR":
        raise FeedError("feed_unit_not_inr", "The feed must report absolute INR; no unit or currency is guessed or converted.")
    basis = payload.get("basis")
    if basis not in BASES:
        raise FeedError("feed_basis_not_identified", "The feed did not state consolidated or standalone.")
    periods: dict[tuple[str, date], si.ParsedPeriod] = {}
    try:
        for item in payload.get("periods") or []:
            kind = item["period_type"]
            if kind not in ("annual", "quarterly"):
                raise FeedError("feed_bad_period_type", f"Unsupported period_type {kind!r}.")
            end = date.fromisoformat(item["period_end"])
            period = periods.setdefault((kind, end), si.ParsedPeriod())
            for name, raw in (item.get("fields") or {}).items():
                if name not in CANONICAL:
                    raise FeedError("feed_unknown_field", f"Field {name!r} is not a canonical fundamentals field.")
                if raw is None:
                    continue  # missing stays missing, never zero
                value = Decimal(str(raw))
                low_high = si._PLAUSIBLE.get(name)
                if not value.is_finite() or (low_high and not low_high[0] <= value <= low_high[1]):
                    continue  # implausible: excluded, not repaired
                period.fields[name] = value
            for name, raw in (item.get("extras") or {}).items():
                if raw is not None:
                    period.extras[name] = Decimal(str(raw))
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise FeedError("feed_malformed", f"The feed response is malformed ({type(exc).__name__}).") from exc
    periods = {k: v for k, v in periods.items() if v.fields or v.extras}
    if not periods:
        raise FeedError("feed_no_periods", "The feed returned no usable periods.")
    return si.ParsedScreener(basis=basis, periods=periods)


class FundamentalsFeedClient:
    """Conservative client: minimum spacing between requests, bounded retries with jittered backoff, no auth bypass."""

    def __init__(self, base_url: str, api_key: str, *, min_interval: float, timeout: float = 20.0, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._min_interval = min_interval
        self._client = httpx.Client(timeout=timeout, transport=transport, trust_env=False)
        self._last_request = 0.0
        self._sleep = sleep

    def fetch(self, symbol: str, basis: str = "consolidated") -> dict[str, Any]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self._client.get(f"{self._base}/fundamentals/{symbol}", params={"basis": basis},
                                            headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"})
            except httpx.TimeoutException:
                code, retry_after = "feed_timeout", None
            except httpx.HTTPError:
                code, retry_after = "feed_unreachable", None
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise FeedError("feed_malformed", "The feed returned non-JSON.") from exc
                if response.status_code in (401, 403):
                    raise FeedError("feed_unauthorised", "The feed rejected the credentials or the licence does not cover this request.")
                if response.status_code == 404:
                    raise FeedError("feed_symbol_not_found", "The feed has no data for this symbol.")
                if response.status_code not in RETRYABLE:
                    raise FeedError("feed_provider_error", f"The feed returned HTTP {response.status_code}.")
                code = "feed_rate_limited" if response.status_code == 429 else "feed_unavailable"
                try:
                    retry_after = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
            if attempt == MAX_ATTEMPTS:
                raise FeedError(code, "The feed did not succeed after retries.")
            self._sleep(min(60.0, retry_after if retry_after is not None else 2.0 ** attempt) + random.uniform(0, 1))
        raise FeedError("feed_unavailable", "unreachable")  # pragma: no cover


# ───────────────────────── Coverage ─────────────────────────
def instrument_coverage(db: Session, instrument: Instrument) -> dict[str, Any]:
    gaps = si.fundamentals_gaps(db, instrument.id)
    rows = db.scalars(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == instrument.id)).all()
    cells = len(rows) * len(si.CANONICAL_FIELDS)
    missing_cells = sum(len(fields) for kind in ("annual", "quarterly") for fields in gaps[kind]["missing_fields_by_period"].values())
    history_missing = gaps["annual"]["missing_history_periods"] + gaps["quarterly"]["missing_history_periods"]
    completeness = round(100 * (cells - missing_cells) / cells, 1) if cells else 0.0
    retrieved: dict[str, str] = {}
    for row in rows:
        for source, stamp in [(row.source, row.fetched_at)] + [(v.get("source"), v.get("fetched_at")) for v in (row.field_sources or {}).values()]:
            when = stamp.isoformat() if isinstance(stamp, datetime) else stamp
            if source and when and when > retrieved.get(source, ""):
                retrieved[source] = when
    status = "none" if not rows else "complete" if missing_cells == 0 and history_missing == 0 else "partial"
    error = _LAST_ERRORS.get(instrument.symbol)
    return {
        "instrument_id": str(instrument.id), "symbol": instrument.symbol, "venue": instrument.venue, "status": status,
        "field_completeness_pct": completeness, "annual_periods": gaps["annual"]["stored_periods"],
        "quarterly_periods": gaps["quarterly"]["stored_periods"], "missing_history_periods": history_missing,
        "unresolved_gap_cells": missing_cells, "last_retrieved_by_source": retrieved, "last_error": error,
    }


def coverage_report(db: Session, *, limit: int = 200) -> dict[str, Any]:
    instruments = db.scalars(select(Instrument).where(
        Instrument.country == "IN", Instrument.asset_class == "equity", Instrument.is_synthetic.is_(False),
    ).order_by(Instrument.symbol).limit(limit)).all()
    per = [instrument_coverage(db, i) for i in instruments]
    counts = {s: sum(1 for p in per if p["status"] == s) for s in ("complete", "partial", "none")}
    rows_measured = [p["field_completeness_pct"] for p in per if p["status"] != "none"]
    return {
        "feed": {"enabled": settings.fundamentals_feed_enabled, "configured": feed_configured(), "source_name": settings.fundamentals_feed_source_name,
                 "note": "Screener.in offers no authorised automated access; a licensed feed is required to enable collection."},
        "overall": {"instruments": len(per), **counts, "mean_field_completeness_pct": round(sum(rows_measured) / len(rows_measured), 1) if rows_measured else None},
        "instruments": per,
    }


# ───────────────────────── Collection (only with an authorised feed) ─────────────────────────
def needs_data(db: Session, instrument: Instrument, now: datetime) -> bool:
    cov = instrument_coverage(db, instrument)
    if cov["status"] == "complete":
        return False
    last = cov["last_retrieved_by_source"].get(settings.fundamentals_feed_source_name)
    if last and (now - datetime.fromisoformat(last)).total_seconds() < settings.fundamentals_feed_refresh_hours * 3600:
        return False  # already collected recently: no duplicate request
    return True


def collect_once(db: Session, client: FundamentalsFeedClient, *, now: datetime | None = None, limit: int | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    limit = limit or settings.fundamentals_feed_batch_size
    summary: dict[str, Any] = {"attempted": 0, "imported": 0, "failed": 0, "skipped_complete_or_recent": 0, "errors": {}}
    candidates = db.scalars(select(Instrument).where(
        Instrument.country == "IN", Instrument.asset_class == "equity", Instrument.is_synthetic.is_(False)).order_by(Instrument.symbol)).all()
    for instrument in candidates:
        if summary["attempted"] >= limit:
            break
        if not needs_data(db, instrument, now):
            summary["skipped_complete_or_recent"] += 1
            continue
        summary["attempted"] += 1
        try:
            parsed = parse_feed_payload(client.fetch(instrument.symbol), instrument.symbol)
            si.apply_gap_fill(db, instrument, parsed, now, source=settings.fundamentals_feed_source_name)
            _LAST_ERRORS.pop(instrument.symbol, None)
            summary["imported"] += 1
        except (FeedError, si.ScreenerParseError) as error:
            db.rollback()
            code = getattr(error, "code", str(error))
            _LAST_ERRORS[instrument.symbol] = {"code": code, "message": str(error), "at": now.isoformat()}
            summary["failed"] += 1
            summary["errors"][instrument.symbol] = code
            if code in {"feed_unauthorised", "feed_rate_limited"}:
                break  # stop the cycle: do not push through an access or rate limit
    return summary


async def fundamentals_collection_loop() -> None:
    """Scheduled background collection. Runs only with an authorised, configured feed."""
    if not feed_configured():
        return
    client = FundamentalsFeedClient(settings.fundamentals_feed_base_url or "", settings.fundamentals_feed_api_key.get_secret_value(),  # type: ignore[union-attr]
                                    min_interval=settings.fundamentals_feed_min_interval_seconds)
    while True:
        try:
            def cycle() -> dict[str, Any]:
                with Session(engine) as db:
                    return collect_once(db, client)
            summary = await asyncio.to_thread(cycle)
            logger.info("fundamentals_feed_cycle %s", {k: v for k, v in summary.items() if k != "errors"})
        except Exception as exc:  # noqa: BLE001 - the loop must survive any single failure
            logger.warning("fundamentals_feed_cycle_failed error_class=%s", type(exc).__name__)
        await asyncio.sleep(settings.fundamentals_feed_cycle_minutes * 60)
