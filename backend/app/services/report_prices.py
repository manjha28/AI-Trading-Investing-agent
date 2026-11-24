"""The price line on a Daily report card: the live price during market hours (a fresh stored Dhan quote), otherwise the last stored daily close, with the day change.
Stored data only, one batch of queries, Decimal throughout. Missing data is left out (None), never zero."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketBar, MarketQuote

IST = ZoneInfo("Asia/Kolkata")
LIVE_MAX_AGE = timedelta(minutes=10)
CARD_BUCKETS = ("buy", "setup_waiting", "radar")


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def market_open(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and (9, 15) <= (local.hour, local.minute) < (15, 30)


def _block(value: Decimal, kind: str, as_of: datetime | None, prev: Decimal | None, stale: bool) -> dict[str, Any]:
    change = value - prev if prev else None
    return {"value": str(value), "kind": kind, "as_of": as_of.isoformat() if as_of else None, "change": str(change) if change is not None else None,
            "change_pct": str((change / prev * Decimal(100)).quantize(Decimal("0.01"))) if change is not None and prev else None, "stale": stale}


def build(db: Session, rows: list[dict], now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """{instrument_id: price block} for the given report rows."""
    now = now or datetime.now(timezone.utc)
    ids = [UUID(r["instrument_id"]) for r in rows]
    if not ids:
        return {}
    bars: dict[UUID, list[tuple[datetime, Decimal]]] = {}
    for iid, ts, close in db.execute(select(MarketBar.instrument_id, MarketBar.timestamp, MarketBar.close).where(
            MarketBar.instrument_id.in_(ids), MarketBar.interval == "1d", MarketBar.source == "dhan", MarketBar.is_synthetic.is_(False), MarketBar.timestamp >= now - timedelta(days=20))
            .order_by(MarketBar.instrument_id, MarketBar.timestamp.desc())):
        if len(bars.setdefault(iid, [])) < 2:
            bars[iid].append((_aware(ts), close))
    quotes: dict[UUID, MarketQuote] = {}
    if market_open(now):
        for q in db.scalars(select(MarketQuote).where(MarketQuote.instrument_id.in_(ids), MarketQuote.source == "dhan", MarketQuote.is_synthetic.is_(False),
                                                      MarketQuote.timestamp >= now - LIVE_MAX_AGE)):
            if q.instrument_id not in quotes or _aware(q.timestamp) > _aware(quotes[q.instrument_id].timestamp):
                quotes[q.instrument_id] = q
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        iid = UUID(r["instrument_id"])
        series = bars.get(iid) or []
        if iid in quotes:
            q = quotes[iid]
            out[r["instrument_id"]] = _block(q.close, "ltp", _aware(q.timestamp), series[0][1] if series else None, False)     # the previous session's close is the latest stored daily bar
        elif series:
            out[r["instrument_id"]] = _block(series[0][1], "close", series[0][0], series[1][1] if len(series) > 1 else None, bool(r.get("stale_close") or r.get("stale")))
    return out


def attach(db: Session, payload: dict, now: datetime | None = None) -> dict:
    """A copy of the report payload with a `price` block on every card-bucket row."""
    buckets = payload.get("buckets") or {}
    rows = [r for b in CARD_BUCKETS for r in buckets.get(b, [])]
    blocks = build(db, rows, now)
    new = {b: ([{**r, "price": blocks.get(r["instrument_id"])} for r in rs] if b in CARD_BUCKETS else rs) for b, rs in buckets.items()}
    return {**payload, "buckets": new}
