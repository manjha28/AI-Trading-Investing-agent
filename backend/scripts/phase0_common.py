"""Shared loaders + cost overlay for the Phase 0 measurement scripts (read-only against stored data)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from app.services import indian_charges as ic
from app.services.barrier_study import SLOTS, Session

IST = ZoneInfo("Asia/Kolkata")
OPEN_MIN = 9 * 60 + 15
MIN_SLOTS = 365                     # a session needs >= 365 of 375 stored regular-session minutes (the same bar the VAR preflight uses)
D = Decimal


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return datetime.combine(start, time(0), tzinfo=IST), datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST)


def build_sessions(rows: list, min_slots: int = MIN_SLOTS) -> tuple[list[Session], dict]:
    """rows: (ts, open, high, low, close, volume). One Session per IST day, one slot per minute 09:15..15:29. Missing minutes stay NaN (never filled)."""
    per: dict[date, dict] = defaultdict(lambda: {k: np.full(SLOTS, np.nan) for k in "ohlcv"})
    for ts, o, h, l, c, v in rows:
        loc = ts.astimezone(IST)
        slot = loc.hour * 60 + loc.minute - OPEN_MIN
        if 0 <= slot < SLOTS:
            a = per[loc.date()]
            a["o"][slot], a["h"][slot], a["l"][slot], a["c"][slot], a["v"][slot] = float(o), float(h), float(l), float(c), float(v)
    sessions, integ = [], {"days_stored": len(per), "days_used": 0, "days_excluded_short": 0, "missing_slots_in_used": 0, "zero_volume_bars": 0, "zero_volume_flat_carry": 0,
                           "zero_volume_moved": 0, "bars": 0, "invalid_ohlc": 0, "excluded_examples": []}
    for day in sorted(per):
        a = per[day]
        have = int((~np.isnan(a["o"])).sum())
        if have < min_slots:
            integ["days_excluded_short"] += 1
            if len(integ["excluded_examples"]) < 3:
                integ["excluded_examples"].append({"date": day.isoformat(), "stored_slots": have})
            continue
        valid = ~np.isnan(a["o"])
        bad = valid & ((a["h"] < a["l"]) | (a["o"] > a["h"] + 1e-9) | (a["o"] < a["l"] - 1e-9) | (a["c"] > a["h"] + 1e-9) | (a["c"] < a["l"] - 1e-9) | (a["o"] <= 0))
        integ["invalid_ohlc"] += int(bad.sum())
        zero = valid & (a["v"] == 0)
        integ["zero_volume_bars"] += int(zero.sum())
        prev_close = np.concatenate([[np.nan], np.where(valid, a["c"], np.nan)[:-1]])
        pc = np.where(np.isnan(prev_close), np.nan, prev_close)
        lastvalid = np.maximum.accumulate(np.where(valid, np.arange(SLOTS), -1))
        carried = np.where(np.concatenate([[-1], lastvalid[:-1]]) >= 0, a["c"][np.clip(np.concatenate([[-1], lastvalid[:-1]]), 0, SLOTS - 1)], np.nan)
        flat = zero & (a["o"] == a["h"]) & (a["h"] == a["l"]) & (a["l"] == a["c"]) & (a["c"] == carried)
        integ["zero_volume_flat_carry"] += int(flat.sum())
        integ["zero_volume_moved"] += int((zero & ~flat).sum())
        integ["bars"] += have
        integ["missing_slots_in_used"] += int(SLOTS - have)
        integ["days_used"] += 1
        _ = pc
        sessions.append(Session(day, a["o"], a["h"], a["l"], a["c"], a["v"]))
    return sessions, integ


def equity_rows(db: DbSession, symbol: str, start: date, end: date) -> list:
    lo, hi = _bounds(start, end)
    return db.execute(text("""select b.timestamp, b.open, b.high, b.low, b.close, b.volume from market_bars b join instruments i on i.id = b.instrument_id
        where i.symbol = :s and i.asset_class = 'equity' and i.venue = 'NSE' and b.interval = '1m' and b.source = 'dhan' and b.is_synthetic = false
        and b.timestamp >= :lo and b.timestamp < :hi order by b.timestamp"""), {"s": symbol, "lo": lo, "hi": hi}).all()


def futures_contract(db: DbSession, underlying: str) -> tuple | None:
    return db.execute(text("""select c.id, c.trading_symbol, c.lot_size, c.tick_size, c.instrument_type, count(distinct (f.ts at time zone 'Asia/Kolkata')::date) n
        from derivative_contracts c join futures_candles_1m f on f.contract_id = c.id where c.underlying_symbol = :u and c.exchange = 'NSE' and c.instrument_type like 'FUT%'
        group by 1, 2, 3, 4, 5 order by n desc, c.expiry limit 1"""), {"u": underlying}).first()


def futures_rows(db: DbSession, contract_id, start: date, end: date) -> list:
    lo, hi = _bounds(start, end)
    return db.execute(text("select ts, open, high, low, close, volume from futures_candles_1m where contract_id = :i and ts >= :lo and ts < :hi order by ts"), {"i": contract_id, "lo": lo, "hi": hi}).all()


def futures_rows_by_symbol(db: DbSession, trading_symbol: str, start: date, end: date) -> list:
    lo, hi = _bounds(start, end)
    return db.execute(text("""select f.ts, f.open, f.high, f.low, f.close, f.volume from futures_candles_1m f join derivative_contracts c on c.id = f.contract_id
        where c.trading_symbol = :s and c.exchange = 'NSE' and f.ts >= :lo and f.ts < :hi order by f.ts"""), {"s": trading_symbol, "lo": lo, "hi": hi}).all()


def round_trip_cost_fraction(segment: str, instrument_type: str, price: float, on: date, *, notional: float | None = None, units: int | None = None) -> tuple[float, list[str], dict]:
    """Round-trip cost (entry order + one exit order at the same price) as a FRACTION of notional, from the single authoritative charge service."""
    p = D(repr(round(price, 2)))
    q = D(units) if units is not None else D(int(notional // price))
    kw = dict(exchange="NSE", segment=segment, instrument_type=instrument_type, broker_profile="dhan", trading_date=on)
    buy = ic.calculate_trade_charges(**kw, order_id="entry", side="buy", execution_price=p, executed_quantity=q)
    sell = ic.calculate_trade_charges(**kw, order_id="exit", side="sell", execution_price=p, executed_quantity=q)
    agg = ic.sum_charges([buy, sell])
    return float(agg["total"] / (p * q)), agg["unverified_components"], {k: str(v) for k, v in agg["rounded"].items()}
