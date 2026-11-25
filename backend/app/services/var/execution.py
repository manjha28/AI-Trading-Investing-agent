"""Execution simulator + exit manager for replay/backtest (a fill model behind one interface; no broker, no orders sent).

Candle-only conservative rules: if stop and target are both inside one bar, the STOP is assumed first. The +1R stop move
takes effect from the NEXT bar. Structural/time exits fill at the next bar's open. Stops never widen.
Everything is Decimal. This is a hypothetical-fill model for research and is not a statement about real fills."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from .config import VarConfig
from app.services import indian_charges as ic

from .charges import order_charges, round_trip_estimate
from .strategy import Signal, snap
from .structure import session_close
from .vtypes import IST, Bar, Contract, D


@dataclass
class Position:
    signal: Signal
    contract: Contract
    direction: int
    entry: Decimal
    stop: Decimal
    initial_stop: Decimal
    target: Decimal
    lots: int
    lot_size: int
    entry_ts: datetime
    risk_per_unit: Decimal
    remaining_lots: int = 0
    bars_held: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    partial_done: bool = False
    trail_active: bool = False
    fills: list[tuple[str, Decimal, int, str, datetime]] = field(default_factory=list)  # side, price, lots, reason, ts
    closed: bool = False
    exit_reason: str = ""
    exit_ts: datetime | None = None
    breakeven_pending: bool = False
    expiring_index: bool = False

    def __post_init__(self) -> None:
        self.remaining_lots = self.lots


def simulate_entry(sig: Signal, contract: Contract, nxt: Bar, lots: int, cfg: VarConfig) -> Position | None:
    """Stop-entry order valid for the NEXT bar only. Gap beyond entry fills at the open (+slippage) but is cancelled if the
    resulting stop/target relationship breaks the minimum R. Returns None when not filled/cancelled."""
    d = 1 if sig.direction == "LONG" else -1
    t = contract.tick_size
    slip = t * cfg.execution.entry_slippage_ticks
    if d > 0:
        if nxt.high < sig.entry:
            return None
        fill = max(sig.entry, nxt.open) + slip
    else:
        if nxt.low > sig.entry:
            return None
        fill = min(sig.entry, nxt.open) - slip
    risk = (fill - sig.stop) * d
    target = sig.targets[0]
    if risk <= 0 or (target - fill) * d / risk < D(min(cfg.breakout.minimum_rr, cfg.reversal.minimum_rr)):
        return None   # gap/slippage invalidated the stop/target relationship
    return Position(sig, contract, d, fill, sig.stop, sig.stop, target, lots, contract.lot_size, nxt.ts, risk)


def _slip(contract: Contract, ticks: int) -> Decimal:
    return contract.tick_size * ticks


def _close(pos: Position, price: Decimal, lots: int, reason: str, ts: datetime) -> None:
    side = "sell" if pos.direction > 0 else "buy"
    pos.fills.append((side, price, lots, reason, ts))
    pos.remaining_lots -= lots
    if pos.remaining_lots <= 0:
        pos.closed = True
        pos.exit_reason = reason
        pos.exit_ts = ts


def manage(pos: Position, bars: list[Bar], k: int, cfg: VarConfig, *, struct_exit: bool = False, adverse_only: bool = False) -> None:
    """Advance the position over bar ``k`` (a completed bar after entry). ``struct_exit`` is evaluated by the caller from the
    PREVIOUS completed bars (breakout: two closes back through the level + opposing CVD; reversal: acceptance beyond sweep)."""
    if pos.closed:
        return
    b = bars[k]
    d, c, t = pos.direction, pos.contract, pos.contract.tick_size
    ex = cfg.exits
    r = pos.risk_per_unit
    # next-bar-open exits decided on prior information
    if struct_exit:
        _close(pos, b.open - d * _slip(c, cfg.execution.exit_slippage_ticks), pos.remaining_lots, "structural_invalidation", b.ts)
        return
    if pos.bars_held >= ex.time_stop_bars and pos.mfe_r < ex.minimum_mfe_for_time_stop_r:
        _close(pos, b.open - d * _slip(c, cfg.execution.exit_slippage_ticks), pos.remaining_lots, "time_stop", b.ts)
        return
    sq = session_close(b.ts.astimezone(IST).date(), is_expiring_index_derivative=pos.expiring_index) - timedelta(minutes=cfg.session.square_off_minutes_before_close)
    if b.ts >= sq:
        _close(pos, b.open - d * _slip(c, cfg.execution.exit_slippage_ticks), pos.remaining_lots, "session_square_off", b.ts)
        return
    # apply stop move decided on the previous bar
    if pos.breakeven_pending:
        be = pos.entry + d * _be_offset(pos, cfg)
        be = snap(be, t, up=d > 0)
        if (be - pos.stop) * d > 0:      # only ever tighten
            pos.stop = be
        pos.breakeven_pending = False
    # hard stop (conservative: first)
    hit_stop = (b.low <= pos.stop) if d > 0 else (b.high >= pos.stop)
    if hit_stop:
        gap = (b.open <= pos.stop) if d > 0 else (b.open >= pos.stop)
        px = (b.open if gap else pos.stop) - d * _slip(c, cfg.execution.stop_slippage_ticks)
        _close(pos, px, pos.remaining_lots, "stop" if pos.stop == pos.initial_stop else "stop_after_partial_or_trail", b.ts)
        return
    if adverse_only:   # entry bar: favourable excursion may have preceded the fill, so only adverse moves are credited
        pos.mae_r = max(pos.mae_r, float(((pos.entry - b.low) if d > 0 else (b.high - pos.entry)) / r))
        return
    fav = ((b.high - pos.entry) if d > 0 else (pos.entry - b.low)) / r
    adv = ((pos.entry - b.low) if d > 0 else (b.high - pos.entry)) / r
    pos.mfe_r = max(pos.mfe_r, float(fav))
    pos.mae_r = max(pos.mae_r, float(adv))
    pos.bars_held += 1
    tgt_hit = (b.high >= pos.target) if d > 0 else (b.low <= pos.target)
    p1 = pos.entry + d * r * D(ex.partial_target_r)
    p1_hit = (b.high >= p1) if d > 0 else (b.low <= p1)
    if p1_hit and not pos.partial_done:
        pos.partial_done = True
        pbl = int((Decimal(pos.lots) * D(ex.partial_booking_pct) / 100).to_integral_value(rounding=ROUND_FLOOR))
        if pbl >= 1 and pbl < pos.remaining_lots:
            _close(pos, p1 - d * _slip(c, cfg.execution.exit_slippage_ticks), pbl, "partial_1R", b.ts)
        pos.breakeven_pending = True   # effective from the next bar
    if tgt_hit:
        _close(pos, pos.target - d * _slip(c, cfg.execution.exit_slippage_ticks), pos.remaining_lots, "target", b.ts)
        return
    if ex.exit_model == "structural_trail" and pos.partial_done:
        if pos.mfe_r >= ex.trailing_activation_r:
            pos.trail_active = True
        if pos.trail_active and k >= 2:
            sw = [bars[x].low for x in range(k - 2, k + 1)] if d > 0 else [bars[x].high for x in range(k - 2, k + 1)]
            tr = snap(min(sw) if d > 0 else max(sw), t, up=d < 0)
            if (tr - pos.stop) * d > 0:
                pos.stop = tr


def _be_offset(pos: Position, cfg: VarConfig) -> Decimal:
    """Cost-adjusted breakeven offset per unit: estimated round-trip charges on the REMAINING quantity (authoritative calculator)."""
    q = Decimal(pos.remaining_lots * pos.lot_size)
    if q <= 0:
        return Decimal(0)
    est = round_trip_estimate(pos.contract, cfg.charges, pos.entry_ts.astimezone(IST).date(), "LONG" if pos.direction > 0 else "SHORT", pos.entry, pos.entry, q)
    return est["total"] / q


def finalize(pos: Position, cfg: VarConfig) -> dict:
    """Per-order accounting. Orders: one entry order + EACH distinct exit order (partial, target, stop, ...). Brokerage is per order;
    turnover-based charges use that order's price x underlying units (lots x lot_size applied once). net = gross - total charges."""
    d = pos.direction
    unit = Decimal(pos.lot_size)
    gross = sum((((px - pos.entry) * d) * lots * unit for _s, px, lots, _r, _t in pos.fills), Decimal(0))
    open_side = "buy" if d > 0 else "sell"
    orders = [order_charges(pos.contract, cfg.charges, pos.entry_ts.astimezone(IST).date(), "entry", open_side, pos.entry, Decimal(pos.lots) * unit)]
    for k, (side, px, lots, _reason, ts) in enumerate(pos.fills):
        orders.append(order_charges(pos.contract, cfg.charges, ts.astimezone(IST).date(), f"exit{k + 1}", side, px, Decimal(lots) * unit))
    agg = ic.sum_charges(orders)
    net = gross - agg["total"]
    initial_risk = pos.risk_per_unit * pos.lots * unit
    r = agg["rounded"]
    return {"gross_pnl": gross, "charges": agg["total"], "net_pnl": net, "r_multiple": float(net / initial_risk) if initial_risk else 0.0,
            "brokerage": r["brokerage"], "exchange_charge": r["exchange"], "ipft": r["ipft"], "sebi": r["sebi"], "stt": r["stt"], "stamp_duty": r["stamp"], "gst": r["gst"],
            "other_charges": r["dp"], "charges_raw": {k: str(v) for k, v in agg["raw"].items()}, "turnover": agg["turnover"], "charge_orders": [o.as_dict() for o in orders],
            "fee_schedule_version": agg["schedule_version"], "rounding_method": agg["rounding_method"], "unverified_components": agg["unverified_components"],
            "initial_risk": initial_risk, "mfe_r": pos.mfe_r, "mae_r": pos.mae_r, "costs_verified": not agg["unverified_components"],
            "exit_reason": pos.exit_reason or "open", "entry": pos.entry, "stop": pos.initial_stop, "target": pos.target,
            "lots": pos.lots, "lot_size": pos.lot_size, "entry_ts": pos.entry_ts, "exit_ts": pos.exit_ts, "fills": pos.fills}
