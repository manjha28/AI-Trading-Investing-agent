"""Peer universe for the valuation engine: Dhan sector/industry classification plus valuation inputs for the tracked NSE equities.

The peer comparison needs at least five comparable peers per industry (or sector). Peers come from the instruments the app already tracks
(verified Dhan NSE_EQ mappings), so this module only needs Dhan Company Info (read-only, throttled to the app's existing >= 1 s spacing):
each response gives the security's SECTOR / INDUSTRY and its P/E, P/B and market cap, which are stored as provider inputs and run
through the same source chain as the subject's. Nothing is invented for instruments Dhan does not cover.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.broker_adapters import BrokerAdapterError
from app.models import Instrument, InstrumentProviderIdentifier, ValuationProviderInput
from app.services import dhan_company_info, valuation_inputs

MAX_CONSECUTIVE_FAILURES = 5


def dhan_equity_targets(db: Session) -> list[tuple[Instrument, str]]:
    """(instrument, Dhan security id) for every tracked non-synthetic NSE equity with a verified Dhan NSE_EQ mapping, one per security id."""
    rows = db.execute(select(Instrument, InstrumentProviderIdentifier.provider_instrument_id).join(
        InstrumentProviderIdentifier, InstrumentProviderIdentifier.instrument_id == Instrument.id).where(
        InstrumentProviderIdentifier.provider == "dhan", InstrumentProviderIdentifier.is_verified.is_(True),
        Instrument.country == "IN", Instrument.asset_class == "equity", Instrument.is_synthetic.is_(False)).order_by(Instrument.symbol)).all()
    seen: set[str] = set()
    out: list[tuple[Instrument, str]] = []
    for instrument, ident in rows:
        security_id, _, segment = ident.partition("|")
        if segment != "NSE_EQ" or not security_id.isdigit() or security_id in seen:
            continue
        seen.add(security_id)
        out.append((instrument, security_id))
    return out


def fresh_instrument_ids(db: Session, max_age: timedelta, now: datetime) -> set[UUID]:
    cutoff = now - max_age
    return set(db.scalars(select(ValuationProviderInput.instrument_id).where(
        ValuationProviderInput.provider == "dhan", ValuationProviderInput.metric == "market_cap", ValuationProviderInput.fetched_at >= cutoff)))


def refresh_dhan_universe(
    db: Session, adapter: Any, access_token: str, tokens: dict[str, str], *, max_age_hours: float = 12, limit: int | None = None,
    only: set[UUID] | None = None, log: Callable[[str], None] = lambda _m: None, now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch Dhan Company Info (CO + RATIOS) for targets whose stored Dhan inputs are older than ``max_age_hours``. ``adapter`` is a DhanAdapter
    (its ``get_company_info`` applies the shared throttle). Stops early after repeated failures so a revoked token cannot hammer the API."""
    now = now or datetime.now(timezone.utc)
    skip = fresh_instrument_ids(db, timedelta(hours=max_age_hours), now)
    summary = {"targets": 0, "fetched": 0, "skipped_fresh": 0, "failed": 0, "no_classification": 0, "stopped_early": False, "failures": []}
    consecutive = 0
    for instrument, security_id in dhan_equity_targets(db):
        if only is not None and instrument.id not in only:
            continue
        summary["targets"] += 1
        if instrument.id in skip:
            summary["skipped_fresh"] += 1
            continue
        if limit is not None and summary["fetched"] >= limit:
            break
        try:
            data = adapter.get_company_info(access_token, tokens, security_id, "NSE_EQ", metrics=("CO", "RATIOS"))
        except BrokerAdapterError as exc:
            consecutive += 1
            summary["failed"] += 1
            summary["failures"].append({"symbol": instrument.symbol, "category": getattr(exc, "category", "error")})
            log(f"{instrument.symbol}: Dhan Company Info failed ({getattr(exc, 'category', 'error')})")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                summary["stopped_early"] = True
                break
            continue
        consecutive = 0
        snapshot = dhan_company_info.normalize(data, security_id, fetched_at=now)
        inputs = (snapshot or {}).get("valuation_inputs") or {}
        if not inputs:
            summary["failed"] += 1
            continue
        valuation_inputs.store(db, instrument.id, "dhan", inputs, now)
        summary["fetched"] += 1
        if "industry" not in inputs and "sector" not in inputs:
            summary["no_classification"] += 1
        if summary["fetched"] % 20 == 0:
            db.commit()
            log(f"{summary['fetched']} fetched…")
    db.commit()
    return summary


def peer_group_sizes(db: Session) -> dict[str, dict[str, int]]:
    """{level: {label: number of distinct symbols}} from the stored Dhan classification (what the peer lookup can draw on)."""
    out: dict[str, dict[str, int]] = {"sector": {}, "industry": {}}
    for level in out:
        rows = db.execute(select(ValuationProviderInput.text_value, func.count(func.distinct(Instrument.symbol))).join(
            Instrument, Instrument.id == ValuationProviderInput.instrument_id).where(
            ValuationProviderInput.provider == "dhan", ValuationProviderInput.metric == level).group_by(ValuationProviderInput.text_value))
        out[level] = {label: count for label, count in rows if label}
    return out
