"""Venue profiles and cost functions.

Money rule: ``Decimal`` exists ONLY here and in trade-level PnL accounting in ``sim.py``. A float price/qty is converted exactly once, in
``make_fill``, when a Fill is recorded. Nothing vectorised touches Decimal.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Literal

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
@dataclass(frozen=True)
class _NseRates:
    stt_sell: Decimal
    exchange: Decimal
    stamp_buy: Decimal
    sebi: Decimal
    brokerage_flat: Decimal
    brokerage_pct_cap: Decimal | None      # equity: min(flat, pct x notional) per order


def _nse_breakdown(rates: _NseRates, fills: list[Fill]) -> dict[str, Decimal]:
    pct = D("0.01")
    buy = sum((f.notional for f in fills if f.side == "buy"), D("0"))
    sell = sum((f.notional for f in fills if f.side == "sell"), D("0"))
    # brokerage is per DISTINCT order: partial exits are separate orders and are NOT free in India
    per_order: dict[int, Decimal] = {}
    for f in fills:
        per_order[f.order_seq] = per_order.get(f.order_seq, D("0")) + f.notional
    brokerage = D("0")
    for notional in per_order.values():
        fee = rates.brokerage_flat if rates.brokerage_pct_cap is None else min(rates.brokerage_flat, rates.brokerage_pct_cap * notional / D(100))
        brokerage += fee
    stt = sell * rates.stt_sell / D(100)
    exchange = (buy + sell) * rates.exchange / D(100)
    stamp = buy * rates.stamp_buy / D(100)
    sebi = (buy + sell) * rates.sebi / D(100)
    gst = (brokerage + exchange + sebi) * D("0.18")
    del pct
    return {"stt": stt, "exchange": exchange, "stamp": stamp, "sebi": sebi, "brokerage": brokerage, "gst": gst}


def _nse_profile(venue_id: str, rates: _NseRates, lot_size: str, tick_size: str) -> VenueProfile:
    return VenueProfile(
        id=venue_id, tz="Asia/Kolkata", session_open=time(9, 15), session_close=time(15, 30), flatten_time=time(15, 20), no_new_entry_after=time(15, 0),
        anchor_rule="SESSION_OPEN", profile_window="SESSION", profile_rolling_bars=None, lot_size=D(lot_size), tick_size=D(tick_size), has_funding=False, waiver=None,
        cost_fn=lambda fills, ctx: q2(sum(_nse_breakdown(rates, fills).values(), D("0"))),
        breakdown_fn=lambda fills, ctx: {k: q2(v) for k, v in _nse_breakdown(rates, fills).items()}, is_india=True)


_INDEX_RATES = _NseRates(D("0.05"), D("0.00183"), D("0.002"), D("0.0001"), D("20"), None)
_EQUITY_RATES = _NseRates(D("0.025"), D("0.00307"), D("0.003"), D("0.0001"), D("20"), D("0.03"))


def nse_index_fut(lot_size: str = "65", tick_size: str = "0.05") -> VenueProfile:
    return _nse_profile("NSE_INDEX_FUT", _INDEX_RATES, lot_size, tick_size)


def nse_equity_mis(tick_size: str = "0.05") -> VenueProfile:
    return _nse_profile("NSE_EQUITY_MIS", _EQUITY_RATES, "1", tick_size)


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


def round_trip_pct(venue: VenueProfile, symbol: str, price: float, notional: float, *, maker_exit: bool = False, hold_minutes: float = 60.0) -> float:
    """Round-trip cost as a fraction of notional (entry taker; exit taker unless ``maker_exit``). Decimal stays inside this module."""
    qty = notional / price
    ts = datetime(2026, 1, 1)
    fills = [make_fill("buy", price, qty, False, ts, 0), make_fill("sell", price, qty, maker_exit, ts, 1)]
    cost = venue.costs(fills, CostCtx(symbol, hold_minutes))
    return float(cost / D(repr(float(notional))))
