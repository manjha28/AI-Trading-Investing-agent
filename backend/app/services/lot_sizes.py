"""Dated lot-size and tick lookup for stored futures. FAILS CLOSED: a lot size is returned only for a contract that has stored candles on
the requested date (evidence it was live), so a date older than the stored history raises instead of silently using today's lot.

The Dhan master holds one snapshot, so the lot/tick on a contract row is the value for THAT contract's whole life (exchanges change lot
sizes between contracts, not inside one). That is correct for the stored window and unavailable before it. Extending history before
2026-08-26 therefore needs the historical master (or the exchange's lot-size circulars) loaded first; this module will not guess.
NOTE tick size is also date-dependent in practice (INFY futures trade on a 0.05 grid from 2026-10-01 while the master says 0.10); callers
must verify prices against the tick, as the loader does."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.derivatives import DerivativeContract, FuturesCandle1m

IST = ZoneInfo("Asia/Kolkata")


class LotSizeUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class LotSize:
    lot_size: int
    tick_size_rupees: Decimal
    contract: str
    expiry: date
    source: str = "derivative_contracts row of a contract with stored candles on this date"


def lot_size_on(db: Session, underlying: str, day: date) -> LotSize:
    start = datetime.combine(day, time(0), tzinfo=IST)
    live = db.execute(
        select(DerivativeContract).where(DerivativeContract.underlying_symbol == underlying, DerivativeContract.instrument_type.like("FUT%"), DerivativeContract.expiry >= day,
                                         select(FuturesCandle1m.contract_id).where(FuturesCandle1m.contract_id == DerivativeContract.id, FuturesCandle1m.ts >= start,
                                                                                    FuturesCandle1m.ts < start + timedelta(days=1)).exists())
        .order_by(DerivativeContract.expiry)).scalars().all()
    if not live:
        raise LotSizeUnavailable(f"no stored contract of {underlying} has candles on {day.isoformat()}: lot size for that date is unavailable (load the historical master first)")
    c = live[0]
    if c.lot_size is None or c.lot_size <= 0 or c.tick_size is None or c.tick_size <= 0:
        raise LotSizeUnavailable(f"{c.trading_symbol}: lot/tick missing in the master")
    return LotSize(int(c.lot_size), Decimal(c.tick_size) / Decimal(100), c.trading_symbol, c.expiry)
