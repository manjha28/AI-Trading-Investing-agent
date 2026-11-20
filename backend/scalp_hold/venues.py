"""Venue profiles and cost functions.

Money rule: ``Decimal`` exists ONLY here and in trade-level PnL accounting in ``sim.py``. A float price/qty is converted exactly once, in
``make_fill``, when a Fill is recorded. Nothing vectorised touches Decimal.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Literal
from zoneinfo import ZoneInfo

D = Decimal
CENT = D("0.01")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Fill:
    side: Literal["buy", "sell"]
    notional: Decimal
    is_maker: bool
    ts: datetime
    order_seq: int        # distinct orders; used for flat brokerage


def make_fill(side: Literal["buy", "sell"], price: float, qty: float, is_maker: bool, ts: datetime, order_seq: int) -> Fill:
    """The single float -> Decimal conversion point (via the string form; Decimal(float) is forbidden)."""
    return Fill(side, D(repr(float(price))) * D(repr(float(qty))), is_maker, ts, order_seq)


@dataclass(frozen=True)
class WaiverRule:
    """Delta 'Scalper Offer': closing fills are free when the position was held for less than ``window`` minutes. UNVERIFIED: off by default."""
    enabled: bool = False
    windows: dict[str, int] = field(default_factory=lambda: {"BTCUSD": 30})
    default_window: int = 15

    def window_for(self, symbol: str) -> int:
        return self.windows.get(symbol, self.default_window)


@dataclass(frozen=True)
class CostCtx:
    symbol: str = ""
    hold_minutes: float = 0.0
    tds_pct: Decimal = D("0")           # UNVERIFIED config; applied to gross notional per leg
    trading_date: date | None = None    # NSE venues: execution date for the dated fee schedule (None -> taken from the fills)


@dataclass(frozen=True)
class VenueProfile:
    id: str
    tz: str
    session_open: time | None       # None = 24/7
    session_close: time | None
    flatten_time: time | None
    no_new_entry_after: time | None
    anchor_rule: Literal["SESSION_OPEN", "UTC_MIDNIGHT", "LOCAL_MIDNIGHT", "ROLLING"]
    profile_window: Literal["SESSION", "ROLLING"]
    profile_rolling_bars: int | None
    lot_size: Decimal
    tick_size: Decimal
    has_funding: bool
    waiver: WaiverRule | None
    cost_fn: Callable[[list[Fill], CostCtx], Decimal] = field(compare=False, repr=False, default=lambda fills, ctx: D("0"))
    breakdown_fn: Callable[[list[Fill], CostCtx], dict[str, Decimal]] = field(compare=False, repr=False, default=lambda fills, ctx: {})
    is_india: bool = False
    funding_hours_utc: tuple[int, ...] = ()

    def costs(self, fills: list[Fill], ctx: CostCtx) -> Decimal:
        return self.cost_fn(fills, ctx)


# ───────────────────────── CRYPTO_DELTA ─────────────────────────
GST_CRYPTO = D("1.18")
MAKER, TAKER = D("0.0002"), D("0.0005")


def _crypto_breakdown(fills: list[Fill], ctx: CostCtx, waiver: WaiverRule | None) -> dict[str, Decimal]:
    """Fee per fill = notional x rate x 1.18, plus TDS per leg. Partials are fee-neutral (the rate is proportional to notional).
    Waiver: if enabled and hold < window, ALL closing fills cost 0 (closing = the fills on the side opposite to the first fill)."""
    out = {"fees": D("0"), "gst": D("0"), "tds": D("0")}
    opening = fills[0].side if fills else None
    waived = bool(waiver and waiver.enabled and ctx.hold_minutes < waiver.window_for(ctx.symbol))
    for f in fills:
        if waived and f.side != opening:
            continue
        base = f.notional * (MAKER if f.is_maker else TAKER)
        out["fees"] += base
        out["gst"] += base * D("0.18")
        out["tds"] += f.notional * ctx.tds_pct / D(100)
    return out


def _crypto_cost(waiver: WaiverRule | None) -> Callable[[list[Fill], CostCtx], Decimal]:
    return lambda fills, ctx: q2(sum(_crypto_breakdown(fills, ctx, waiver).values(), D("0")))


def crypto_delta(waiver_enabled: bool = False, lot_size: str = "0.001", tick_size: str = "0.5") -> VenueProfile:
    waiver = WaiverRule(enabled=waiver_enabled)
    return VenueProfile(
        id="CRYPTO_DELTA", tz="UTC", session_open=None, session_close=None, flatten_time=None, no_new_entry_after=None, anchor_rule="UTC_MIDNIGHT",
        profile_window="ROLLING", profile_rolling_bars=288, lot_size=D(lot_size), tick_size=D(tick_size), has_funding=True, waiver=waiver,
        cost_fn=_crypto_cost(waiver), breakdown_fn=lambda fills, ctx: {k: q2(v) for k, v in _crypto_breakdown(fills, ctx, waiver).items()},
        funding_hours_utc=(0, 8, 16))


# ───────────────────────── NSE (index futures and equity MIS) ─────────────────────────
# Charges are NOT calculated here: both NSE venues delegate to the single authoritative calculator
# (app.services.indian_charges), which is effective-dated and per-order. The execution date is taken from the fills.
def _nse_trade_date(fills: list[Fill]) -> date:
    ts = fills[0].ts
    return (ts.astimezone(ZoneInfo("Asia/Kolkata")) if ts.tzinfo else ts).date()


def _nse_breakdown(segment: str, instrument_type: str, fills: list[Fill], on: date | None = None) -> dict[str, Decimal]:
    from app.services import indian_charges as ic
    if not fills:
        return {k: D("0") for k in ("brokerage", "stt", "exchange", "ipft", "sebi", "stamp", "gst")}
    day = on or _nse_trade_date(fills)
    orders: dict[int, list[Fill]] = {}
    for f in fills:
        orders.setdefault(f.order_seq, []).append(f)
    charges = [ic.calculate_trade_charges(exchange="NSE", segment=segment, instrument_type=instrument_type, broker_profile="dhan", trading_date=day,
                                          order_id=str(seq), side=fs[0].side, fills=[(f.notional, D(1)) for f in fs]) for seq, fs in sorted(orders.items())]
    agg = ic.sum_charges(charges)
    return {k: v for k, v in agg["rounded"].items() if k != "dp"}


def _nse_profile(venue_id: str, segment: str, instrument_type: str, lot_size: str, tick_size: str) -> VenueProfile:
    return VenueProfile(
        id=venue_id, tz="Asia/Kolkata", session_open=time(9, 15), session_close=time(15, 30), flatten_time=time(15, 20), no_new_entry_after=time(15, 0),
        anchor_rule="SESSION_OPEN", profile_window="SESSION", profile_rolling_bars=None, lot_size=D(lot_size), tick_size=D(tick_size), has_funding=False, waiver=None,
        cost_fn=lambda fills, ctx: sum(_nse_breakdown(segment, instrument_type, fills, ctx.trading_date).values(), D("0")),
        breakdown_fn=lambda fills, ctx: _nse_breakdown(segment, instrument_type, fills, ctx.trading_date), is_india=True)


def nse_index_fut(lot_size: str = "65", tick_size: str = "0.05") -> VenueProfile:
    return _nse_profile("NSE_INDEX_FUT", "futures", "index_future", lot_size, tick_size)


def nse_stock_fut(lot_size: str, tick_size: str) -> VenueProfile:
    """Individual-stock futures: its own instrument type, lot size and tick (never the NIFTY defaults)."""
    return _nse_profile("NSE_STOCK_FUT", "futures", "stock_future", lot_size, tick_size)


def nse_equity_mis(tick_size: str = "0.05") -> VenueProfile:
    return _nse_profile("NSE_EQUITY_MIS", "equity_cash", "cash", "1", tick_size)


CRYPTO_DELTA = crypto_delta()
NSE_INDEX_FUT = nse_index_fut()
NSE_EQUITY_MIS = nse_equity_mis()
VENUES = {"crypto": CRYPTO_DELTA, "index": NSE_INDEX_FUT, "equity": NSE_EQUITY_MIS}

# Slippage defaults, bps per side. ASSUMED, not measured.
SLIP_BPS = {"BTCUSD": 1.0, "ETHUSD": 1.5, "SOLUSD": 3.0, "XRPUSD": 3.0, "NIFTY_FUT": 0.2, "BANKNIFTY_FUT": 0.3, "EQUITY": 2.0}


def warn_unverified(*, scalper_offer_enabled: bool, tds_pct: Decimal | float, slip_bps: dict[str, float]) -> list[str]:
    """Emit a WARNING to stderr for every unverified assumption in use. Returns the messages (also printed)."""
    msgs = []
    if scalper_offer_enabled:
        msgs.append("WARNING: scalper_offer_enabled=True is UNVERIFIED; closing fills under the hold window are costed at 0.")
    if D(str(tds_pct)) != 0:
        msgs.append(f"WARNING: tds_pct={tds_pct} is UNVERIFIED config applied to gross notional per leg.")
    used = {k: v for k, v in slip_bps.items() if v}
    if used:
        msgs.append("WARNING: slip_bps are ASSUMED, not measured: " + ", ".join(f"{k}={v}" for k, v in used.items()))
    for m in msgs:
        print(m, file=sys.stderr)
    return msgs


def index_end_date(index) -> date:
    """IST date of the last bar: the fee schedule a run is costed at (one schedule per run; a run spanning a rate change is an approximation)."""
    last = index[-1]
    return (last.tz_convert("Asia/Kolkata") if getattr(last, "tzinfo", None) else last).date()


def round_trip_pct(venue: VenueProfile, symbol: str, price: float, notional: float, *, maker_exit: bool = False, hold_minutes: float = 60.0,
                   on: date | None = None) -> float:
    """Round-trip cost as a fraction of notional (entry taker; exit taker unless ``maker_exit``). Decimal stays inside this module.
    ``on`` is the date whose fee schedule applies; it is REQUIRED for NSE venues (a hard-coded date previously priced every run at 2026-01-01 rates)."""
    if venue.is_india and on is None:
        raise ValueError("round_trip_pct needs the execution date (on=) for NSE venues so the dated fee schedule can be selected")
    qty = notional / price
    ts = datetime(on.year, on.month, on.day) if on else datetime(2026, 1, 1)
    fills = [make_fill("buy", price, qty, False, ts, 0), make_fill("sell", price, qty, maker_exit, ts, 1)]
    cost = venue.costs(fills, CostCtx(symbol, hold_minutes, trading_date=on))
    return float(cost / D(repr(float(notional))))
