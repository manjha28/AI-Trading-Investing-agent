"""Bar-based execution simulator shared by every family. One position at a time; every order passes risk.risk_gate() and sizes with risk.size_position().

Fills use LAST-price candles; stops and liquidation use MARK-price candles. There are no historical quotes or trades publicly, so slippage is an
assumed cost (declared in CostModel), never a measured one. A maker order fills only when price trades THROUGH the limit. If the stop and the target
are both reachable inside one bar, the stop is assumed to hit first. Hypothetical results; decision support, not investment advice."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal as D

import numpy as np

from . import risk
from .risk import AccountSnapshot, ContractSpec, DayState, MarketState, OrderRequest, RiskConfig

FUNDING_HOURS = (0, 8, 16)
REJECTION_ROWS = 3000


@dataclass(frozen=True)
class CostModel:
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    gst: float = 0.18
    slippage_bps: float = 1.0            # ASSUMED: no L2 or trade history exists to measure it. The tick floor below always applies.
    slippage_ticks_min: float = 1.0
    slippage_mult: float = 1.0           # 2.0 in the stress run
    maker_to_taker_share: float = 0.0    # 0.30 in the stress run: that share of maker fills (chosen by a fixed hash of the trade index) pays taker
    all_taker: bool = False              # taker/taker comparison: resting limits become market orders at the next bar open, target exits pay the taker fee and slippage
    extra_latency_bars: int = 0          # 0 = fill at the next 1-minute bar open (250 ms latency is below the bar); 1 = the 1000 ms stress, one more bar


@dataclass
class Market:
    symbol: str
    spec: ContractSpec
    tick: float
    cv: float
    t: np.ndarray
    o: np.ndarray; h: np.ndarray; l: np.ndarray; c: np.ndarray; v: np.ndarray
    mo: np.ndarray; mh: np.ndarray; ml: np.ndarray; mc: np.ndarray
    funding: dict[int, float]            # funding timestamp (epoch s) -> rate as a fraction per interval, from the FUNDING series
    atr_1h: np.ndarray = field(default=None)   # aligned to the 1m grid, known as of the last completed hour
    funding_df: object = field(default=None)   # the hourly FUNDING series (a DataFrame), for the funding-extreme family


@dataclass
class Leg:
    side: str                 # 'long' | 'short'
    trigger: float            # stop-entry level, limit price, or reference for market
    stop: float
    target: float | None


@dataclass
class Signal:
    idx: int                  # 1m bar index at whose CLOSE the signal is known
    family: str
    variant: str
    legs: list[Leg]
    mode: str                 # 'market' | 'stop_entry' | 'limit'
    valid_bars: int = 1
    max_hold: int = 60        # bars after entry
    tag: str = ""
    reprice: bool = False     # maker entry: the limit is re-set every bar to the previous close (the best bid/ask proxy) until filled or the window ends


def _slip(price: float, m: Market, cost: CostModel) -> float:
    return max(m.tick * cost.slippage_ticks_min, price * cost.slippage_bps / 1e4) * cost.slippage_mult


def _converted(trade_no: int, share: float) -> bool:
    return share > 0 and ((trade_no * 2654435761) % 1000) / 1000.0 < share     # fixed, reproducible choice


def simulate(m: Market, signals, cost: CostModel, cfg: RiskConfig, wallet: float = 1000.0, window: tuple[int, int] | None = None) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Returns (trades, rejection rows, rejection counts by reason). `window` limits signals to [lo, hi) indices of the 1m grid."""
    trades: list[dict] = []
    rejections: list[dict] = []          # the first REJECTION_ROWS rows, for the csv
    counts: dict[str, int] = {}          # every rejection by reason, never truncated
    day = DayState()
    busy_until = -1
    spec = m.spec
    n = len(m.t)
    for sig in signals:
        if window and not (window[0] <= sig.idx < window[1]):
            continue
        if sig.idx <= busy_until:
            counts["skipped_position_open"] = counts.get("skipped_position_open", 0) + 1
            continue
        now = datetime.fromtimestamp(int(m.t[sig.idx]) + 60, tz=timezone.utc)        # the decision is taken at the bar's close
        # --- find the fill ---
        fill = _find_fill(m, sig, cost, n)
        if fill is None:
            counts["unfilled_signal"] = counts.get("unfilled_signal", 0) + 1
            continue
        leg, ei, entry_px, entry_is_maker = fill
        order = OrderRequest(m.symbol, leg.side, D(str(round(entry_px, 8))), D(str(round(leg.stop, 8))), D(str(round(leg.target, 8))) if leg.target is not None else None, sig.family)
        # the gate runs at SIGNAL time for time-based rules (funding window); fills happen later
        gate_market = MarketState(now=now, last_tick_age_s=D(0), spread=None, spread_p95=None)
        acct = AccountSnapshot(wallet=D(str(wallet)), position_margin=D(0), order_margin=D(0), available=D(str(wallet)))
        decision = risk.risk_gate(spec, cfg, acct, gate_market, day, order)
        if decision.action == "reject":
            reason = "+".join(decision.reasons)
            counts[reason] = counts.get(reason, 0) + 1
            if len(rejections) < REJECTION_ROWS:
                rejections.append({"time": int(m.t[sig.idx]), "symbol": m.symbol, "family": sig.family, "variant": sig.variant, "side": leg.side, "reason": reason})
            continue
        qty = decision.qty
        liq = float(decision.liq_price)
        res = _run_trade(m, sig, leg, ei, entry_px, entry_is_maker, qty, liq, cost, len(trades))
        res.update({"family": sig.family, "variant": sig.variant, "symbol": m.symbol, "side": leg.side, "qty": qty, "signal_time": int(m.t[sig.idx]), "tag": sig.tag,
                    "required_margin": float(decision.required_margin)})
        busy_until = res["exit_idx"]
        day.roll(datetime.fromtimestamp(int(m.t[res["exit_idx"]]), tz=timezone.utc))
        day.record_close(D(str(round(res["net"], 8))))
        trades.append(res)
    return trades, rejections, counts


def _find_fill(m: Market, sig: Signal, cost: CostModel, n: int):
    lo = sig.idx + 1 + cost.extra_latency_bars
    if lo >= n:
        return None
    if cost.all_taker and sig.mode == "limit":
        sig = Signal(sig.idx, sig.family, sig.variant, sig.legs[:1], "market", sig.valid_bars, sig.max_hold, sig.tag)
    if sig.mode == "market":
        leg = sig.legs[0]
        px = m.o[lo]
        slip = _slip(px, m, cost)
        return leg, lo, (px + slip if leg.side == "long" else px - slip), False
    hi = min(n, lo + sig.valid_bars)
    if sig.mode == "stop_entry":
        for j in range(lo, hi):
            hits = [leg for leg in sig.legs if (leg.side == "long" and m.h[j] >= leg.trigger) or (leg.side == "short" and m.l[j] <= leg.trigger)]
            if hits:
                if len(hits) > 1:                       # both sides touched in one bar: assume the adverse one (opposite to the bar's close)
                    hits = [h for h in hits if (h.side == "long") == (m.c[j] < m.o[j])] or hits
                leg = hits[0]
                px = max(leg.trigger, m.o[j]) if leg.side == "long" else min(leg.trigger, m.o[j])
                slip = _slip(px, m, cost)
                return leg, j, (px + slip if leg.side == "long" else px - slip), False
        return None
    if sig.mode == "limit":
        leg = sig.legs[0]
        for j in range(lo, hi):
            px = (m.c[j - 1] if sig.reprice else leg.trigger)                         # re-priced each bar to the previous close when the order follows the book
            if (leg.side == "long" and m.l[j] < px) or (leg.side == "short" and m.h[j] > px):   # strictly THROUGH the limit
                if sig.reprice:
                    leg = Leg(leg.side, px, leg.stop, leg.target)
                return leg, j, px, True
        return None
    return None


def _run_trade(m: Market, sig: Signal, leg: Leg, ei: int, entry_px: float, entry_maker: bool, qty: int, liq: float, cost: CostModel, trade_no: int) -> dict:
    long = leg.side == "long"
    sign = 1.0 if long else -1.0
    n = len(m.t)
    last_i = min(n - 1, ei + sig.max_hold)
    exit_px, exit_i, reason, exit_maker = None, last_i, "time", False
    for j in range(ei, last_i + 1):
        ml, mh = m.ml[j], m.mh[j]
        stop_hit = ml <= leg.stop if long else mh >= leg.stop
        if stop_hit:
            base = min(leg.stop, m.mo[j]) if long else max(leg.stop, m.mo[j])           # a gap through the stop fills at the open
            slip = _slip(base, m, cost)
            px = base - slip if long else base + slip
            if (long and ml <= liq) or ((not long) and mh >= liq):                      # the mark also reached liquidation inside the bar
                px = max(px, liq) if long else min(px, liq)
                reason = "liquidated_or_gap"
            else:
                reason = "stop"
            exit_px, exit_i, exit_maker = px, j, False
            break
        if leg.target is not None and (j > ei or sig.mode == "market"):      # a mid-bar fill cannot also have reached the target before it filled
            tgt_hit = (m.h[j] > leg.target) if long else (m.l[j] < leg.target)
            if tgt_hit:
                if cost.all_taker:
                    slip = _slip(leg.target, m, cost)
                    exit_px, exit_i, reason, exit_maker = (leg.target - slip if long else leg.target + slip), j, "target", False
                else:
                    exit_px, exit_i, reason, exit_maker = leg.target, j, "target", True
                break
    if exit_px is None:
        j = last_i + 1 if last_i + 1 < n else last_i
        base = m.o[j]
        slip = _slip(base, m, cost)
        exit_px, exit_i = (base - slip if long else base + slip), j
    entry_taker = (not entry_maker) or _converted(trade_no, cost.maker_to_taker_share)
    exit_taker = (not exit_maker) or _converted(trade_no + 7919, cost.maker_to_taker_share)
    cvq = m.cv * qty
    # reference (frictionless) prices, so slippage is reported separately from the price move
    entry_ref = entry_px - (_slip(entry_px, m, cost) if not entry_maker else 0.0) * sign
    exit_ref = exit_px + (_slip(exit_px, m, cost) * sign if (reason in ("stop", "time", "liquidated_or_gap") or (reason == "target" and not exit_maker)) else 0.0)
    gross = (exit_ref - entry_ref) * sign * cvq
    slippage = ((entry_px - entry_ref) * sign + (exit_ref - exit_px) * sign) * cvq
    if entry_maker and entry_taker:                                  # converted maker: pays the taker fee and the slippage on a market-like fill
        slippage += _slip(entry_px, m, cost) * cvq
    if exit_maker and exit_taker:
        slippage += _slip(exit_px, m, cost) * cvq
    fee_in = entry_px * cvq * (cost.taker_fee if entry_taker else cost.maker_fee)
    fee_out = exit_px * cvq * (cost.taker_fee if exit_taker else cost.maker_fee)
    fees = fee_in + fee_out
    gst = fees * cost.gst
    funding = 0.0
    missing = 0
    t_in, t_out = int(m.t[ei]), int(m.t[exit_i])
    day0 = t_in - t_in % 86400
    for base_day in range(day0, t_out + 86400, 86400):
        for hh in FUNDING_HOURS:
            ts = base_day + hh * 3600
            if t_in < ts <= t_out:
                rate = m.funding.get(ts)
                if rate is None:
                    missing += 1
                    continue
                k = max(0, min(n - 1, int(np.searchsorted(m.t, ts))))
                funding += rate * m.mc[k] * cvq * sign          # long pays a positive rate
    net = gross - slippage - fees - gst - funding
    adverse = {}
    if entry_maker and not entry_taker:                              # how far the price moved against the fill 1/5/30 bars later (bps, positive = against us)
        for k in (1, 5, 30):
            adverse[f"adverse_{k}m_bps"] = -sign * (m.c[min(n - 1, ei + k)] - entry_px) / entry_px * 1e4
    return {**adverse, "entry_ref": entry_ref, "exit_ref": exit_ref, "entry_time": t_in, "exit_time": t_out, "exit_idx": exit_i, "entry_px": entry_px, "exit_px": exit_px, "gross": gross, "slippage": slippage, "fees": fees, "gst": gst,
            "funding": funding, "funding_missing": missing, "net": net, "exit_reason": reason, "entry_maker": entry_maker and not entry_taker, "notional": entry_px * cvq,
            "bps_net": net / (entry_px * cvq) * 1e4 if entry_px * cvq else 0.0}
