"""Load stored futures candles (derivatives platform) as VAR inputs. READ-ONLY; never fabricates or fills missing data.

Source: ``futures_candles_1m`` joined to ``derivative_contracts`` (lot size, tick size in PAISE per the Dhan master; the
conversion is verified against observed price granularity and the load FAILS CLOSED if prices are not on the tick grid).
1m candles are aggregated to 5m only from complete 5-minute groups; incomplete groups are dropped (leaving a visible gap
that downstream ``bars_contiguous`` / feed checks reject). Bars outside [09:15, session close) are excluded: the store holds
candles past 15:30 that are not part of the regular session. Everything here is candle data = level C (no delta)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import text
from sqlalchemy.orm import Session

from .structure import SESSION_OPEN, session_close
from .vtypes import IST, Bar, Contract, DataLevel, D

EXPECTED_5M_BARS = 75
MIN_5M_BARS = EXPECTED_5M_BARS - 2          # a session needs this many complete 5-minute bars to be loaded
RVOL_SESSIONS_REQUIRED = 20                 # default relative-volume window (VarConfig.breakout.rvol_sessions)
LOOKBACK_CALENDAR_DAYS = 45                 # history loaded BEFORE the requested start so ATR / profile / relative-volume baselines are warm


@dataclass
class ContractSeries:
    contract: Contract
    expiry: date
    sessions: dict[date, list[Bar]] = field(default_factory=dict)
    day_volume: dict[date, Decimal] = field(default_factory=dict)
    coverage: dict[date, dict] = field(default_factory=dict)


@dataclass
class UnderlyingData:
    underlying: str
    kind: str                       # "index" | "stock"
    series: list[ContractSeries]
    notes: list[str] = field(default_factory=list)
    exclusions: list[dict] = field(default_factory=list)       # {"date", "reason", ...} for every session that was available but NOT loaded
    sessions_available: int = 0                                # distinct sessions with any stored candle in the load window (incl. lookback)
    stored_minutes: int = 0

    def audit(self, window_start: date | None = None) -> dict:
        """Sessions available vs loaded vs excluded (with reasons) and whether the relative-volume baseline can ever complete."""
        loaded = sorted({d for cs in self.series for d in cs.sessions})
        in_win = [d for d in loaded if window_start is None or d >= window_start]
        reasons: dict[str, int] = {}
        for x in self.exclusions:
            reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
        need = RVOL_SESSIONS_REQUIRED
        first_baseline = loaded[need] if len(loaded) > need else None
        with_baseline = [d for d in in_win if first_baseline is not None and d >= first_baseline]
        return {"underlying": self.underlying, "sessions_available": self.sessions_available, "sessions_loaded": len(loaded), "sessions_loaded_in_window": len(in_win),
                "sessions_excluded": len(self.exclusions), "excluded_by_reason": reasons, "excluded_examples": self.exclusions[:5],
                "first_loaded": loaded[0].isoformat() if loaded else None, "last_loaded": loaded[-1].isoformat() if loaded else None, "notes": self.notes[:5],
                "rvol_baseline": {"prior_sessions_required": need, "first_session_with_baseline": first_baseline.isoformat() if first_baseline else None,
                                  "window_sessions_with_baseline": len(with_baseline), "can_complete": first_baseline is not None}}


def _tick_rupees(raw: Decimal) -> Decimal:
    return raw / Decimal(100)       # master stores paise; verified against data below


def aggregate_5m(rows: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, int]], tick: Decimal, day: date) -> tuple[list[Bar], dict]:
    close_t = session_close(day)
    by: dict[datetime, list] = defaultdict(list)
    for ts, o, h, l, c, v in rows:
        loc = ts.astimezone(IST)
        if not (SESSION_OPEN <= loc.time() < close_t.time()):
            continue
        b = loc.replace(minute=loc.minute - loc.minute % 5, second=0, microsecond=0)
        by[b].append((loc, o, h, l, c, v))
    bars, dropped = [], 0
    for start in sorted(by):
        g = sorted(by[start])
        if len(g) != 5 or len({x[0] for x in g}) != 5:
            dropped += 1
            continue
        vol = sum((D(x[5]) for x in g), Decimal(0))
        bars.append(Bar(start, g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4], vol, None, None, None, DataLevel.C_CANDLE))
    off = [b for b in bars if any((p / tick) != (p / tick).to_integral_value() for p in (b.open, b.high, b.low, b.close))]
    return bars, {"bars": len(bars), "dropped_incomplete_groups": dropped, "off_tick_bars": len(off), "expected": EXPECTED_5M_BARS}


def load_underlying(db: Session, underlying: str, start: date | None = None, end: date | None = None) -> UnderlyingData:
    cons = db.execute(text("""select id, trading_symbol, expiry, lot_size, tick_size, instrument_type from derivative_contracts
        where underlying_symbol = :u and instrument_type like 'FUT%' and exchange = 'NSE' order by expiry"""), {"u": underlying}).all()
    if not cons:
        return UnderlyingData(underlying, "index", [], [f"no futures contracts stored for {underlying}"])
    kind = "index" if cons[0].instrument_type == "FUTIDX" else "stock"
    out = UnderlyingData(underlying, kind, [])
    for c in cons:
        if c.lot_size is None or c.tick_size is None or c.lot_size <= 0 or c.tick_size <= 0:
            out.notes.append(f"{c.trading_symbol}: lot/tick missing in master; excluded (fail closed)")
            continue
        tick = _tick_rupees(Decimal(c.tick_size))
        q = """select ts, open, high, low, close, volume from futures_candles_1m where contract_id = :id"""
        params = {"id": c.id}
        if start:
            q += " and ts >= :s"; params["s"] = datetime.combine(start, time(0), tzinfo=IST)
        if end:
            q += " and ts < :e"; params["e"] = datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST)
        rows = db.execute(text(q + " order by ts"), params).all()
        if not rows:
            continue
        per_day: dict[date, list] = defaultdict(list)
        for r in rows:
            per_day[r.ts.astimezone(IST).date()].append((r.ts, r.open, r.high, r.low, r.close, r.volume))
        cs = ContractSeries(Contract(str(c.id), c.trading_symbol, underlying, kind, None, int(c.lot_size), tick), c.expiry)
        for day, rs in sorted(per_day.items()):
            bars, cov = aggregate_5m(rs, tick, day)
            cs.coverage[day] = cov
            out.sessions_available += 1
            out.stored_minutes += len(rs)
            if cov["off_tick_bars"]:
                # tick size is date-dependent (e.g. INFY trades on a 0.05 grid from 2026-10-01 while the master says 0.10): fail closed PER SESSION
                out.exclusions.append({"date": day.isoformat(), "contract": c.trading_symbol, "reason": "off_tick_grid", "detail": f"{cov['off_tick_bars']} bars off the master tick grid {tick}"})
                continue
            if len(bars) < MIN_5M_BARS:
                out.exclusions.append({"date": day.isoformat(), "contract": c.trading_symbol, "reason": "truncated_or_incomplete_session", "detail": f"{len(bars)} complete 5m bars < {MIN_5M_BARS}"})
                continue
            cs.sessions[day] = bars
            cs.day_volume[day] = sum((b.volume for b in bars), Decimal(0))
        if cs.sessions:
            out.series.append(cs)
    return out


def select_series_for_day(data: UnderlyingData, day: date, prior_day: date | None) -> ContractSeries | None:
    """Causal rollover: among contracts expiring on/after ``day`` that traded on ``day``, pick the one with the greatest
    volume on the PRIOR session (ties -> nearest expiry). Never switches inside a session. None if no prior-session volume."""
    if prior_day is None:
        return None
    live = [s for s in data.series if s.expiry >= day and day in s.sessions and prior_day in s.day_volume]
    if not live:
        return None
    return max(live, key=lambda s: (s.day_volume[prior_day], -s.expiry.toordinal()))


def median_daily_turnover(cs: ContractSeries, lookback: int, upto: date) -> Decimal | None:
    """Median over the previous ``lookback`` sessions (strictly before ``upto``) of sum(typical price * volume), in rupees.
    Volume unit = underlying units (stored volume is always a multiple of the lot size; inferred, not documented)."""
    days = [d for d in sorted(cs.sessions) if d < upto][-lookback:]
    if len(days) < lookback:
        return None
    vals = [sum(((b.high + b.low + b.close) / 3 * b.volume for b in cs.sessions[d]), Decimal(0)) for d in days]
    return Decimal(str(median(vals)))


def load_equity(db: Session, symbol: str, start: date | None = None, end: date | None = None) -> UnderlyingData:
    """NSE equity cash 1-minute bars (market_bars, source 'dhan') as a single always-live 'contract' with lot size 1 (no lot constraint).
    The tick is INFERRED from the data (0.05, else 0.01) and the load fails closed if prices fit neither grid."""
    q = """select b.timestamp as ts, b.open, b.high, b.low, b.close, b.volume from market_bars b join instruments i on i.id = b.instrument_id
           where i.symbol = :s and i.asset_class = 'equity' and i.venue in ('NSE','XNSE') and b.interval = '1m' and b.source = 'dhan'
           and b.is_synthetic = false"""
    params: dict = {"s": symbol}
    if start:
        q += " and b.timestamp >= :a"; params["a"] = datetime.combine(start, time(0), tzinfo=IST)
    if end:
        q += " and b.timestamp < :b"; params["b"] = datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST)
    rows = db.execute(text(q + " order by b.timestamp"), params).all()
    out = UnderlyingData(symbol, "stock", [])
    if not rows:
        out.notes.append(f"{symbol}: no stored NSE 1m equity bars in the window")
        return out
    sample = [r.close for r in rows[:5000]] + [r.open for r in rows[:5000]]
    tick = next((t for t in (Decimal("0.05"), Decimal("0.01")) if all((p / t) == (p / t).to_integral_value() for p in sample)), None)
    if tick is None:
        out.notes.append(f"{symbol}: prices fit neither a 0.05 nor a 0.01 tick grid; excluded (fail closed)")
        return out
    per_day: dict[date, list] = defaultdict(list)
    for r in rows:
        per_day[r.ts.astimezone(IST).date()].append((r.ts, r.open, r.high, r.low, r.close, int(r.volume)))
    cs = ContractSeries(Contract(f"eq-{symbol}", f"{symbol}-EQ", symbol, "stock", None, 1, tick, "equity_cash"), date(9999, 12, 31))
    for day, rs in sorted(per_day.items()):
        bars, cov = aggregate_5m(rs, tick, day)
        cs.coverage[day] = cov
        out.sessions_available += 1
        out.stored_minutes += len(rs)
        if cov["off_tick_bars"]:
            out.exclusions.append({"date": day.isoformat(), "reason": "off_tick_grid", "detail": f"{cov['off_tick_bars']} bars off the {tick} grid"})
            continue
        if len(bars) < MIN_5M_BARS:
            last = max(r[0] for r in rs).astimezone(IST).strftime("%H:%M")
            out.exclusions.append({"date": day.isoformat(), "reason": "truncated_or_incomplete_session", "detail": f"{len(bars)} complete 5m bars < {MIN_5M_BARS}; last stored minute {last}"})
            continue
        cs.sessions[day] = bars
        cs.day_volume[day] = sum((b.volume for b in bars), Decimal(0))
    if cs.sessions:
        out.series.append(cs)
    return out
