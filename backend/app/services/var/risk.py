"""Centralised risk: sizing in whole lots, aggregate-risk and daily-loss gates. Fails closed; never rounds up."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from .config import VarConfig
from .charges import round_trip_estimate
from .vtypes import Contract, D


@dataclass(frozen=True)
class Sizing:
    lots: int
    quantity: Decimal
    risk_budget: Decimal
    one_lot_risk: Decimal
    one_lot_costs: Decimal
    initial_risk_rupees: Decimal
    reason: str = ""


def size_position(equity: Decimal, entry: Decimal, stop: Decimal, target: Decimal, direction: str, contract: Contract,
                  cfg: VarConfig, day: date) -> Sizing:
    """Q = floor(risk_budget / (|entry-stop| * lot_size + est round-trip costs)). Zero lots => rejected."""
    if equity <= 0:
        return Sizing(0, Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), "no equity")
    budget = equity * D(cfg.risk.risk_per_trade_pct) / Decimal(100)
    dist = abs(entry - stop)
    if dist <= 0:
        return Sizing(0, Decimal(0), budget, Decimal(0), Decimal(0), Decimal(0), "zero stop distance")
    lot = Decimal(contract.lot_size)
    costs = round_trip_estimate(contract, cfg.charges, day, direction, entry, target, lot)["total"]
    one = dist * lot + costs
    lots = int((budget / one).to_integral_value(rounding=ROUND_FLOOR))
    if lots < 1:
        return Sizing(0, Decimal(0), budget, dist * lot, costs, Decimal(0), "one lot exceeds the risk budget")
    return Sizing(lots, lot * lots, budget, dist * lot, costs, dist * lot * lots)


@dataclass
class DayState:
    start_equity: Decimal
    realized_net: Decimal = Decimal(0)
    entries: int = 0
    consecutive_losses: int = 0
    open_risk: Decimal = Decimal(0)
    open_positions: int = 0
    open_index_positions: int = 0
    locked: str | None = None
    last_stop_out: dict[str, datetime] = field(default_factory=dict)


def can_enter(day: DayState, sz: Sizing, contract: Contract, now: datetime, cfg: VarConfig,
              unrealized: Decimal = Decimal(0)) -> list[str]:
    """Return rejection reasons (empty list = allowed)."""
    r: list[str] = []
    cap = day.start_equity * D(cfg.risk.max_daily_loss_pct) / Decimal(100)
    if day.locked:
        r.append(f"day locked: {day.locked}")
    if day.realized_net + unrealized <= -cap:
        r.append("daily loss limit reached")
    if day.entries >= cfg.risk.max_entries_per_session:
        r.append("maximum entries per session reached")
    if day.consecutive_losses >= cfg.risk.max_consecutive_losses:
        r.append("maximum consecutive losses reached")
    if day.open_positions >= cfg.risk.max_simultaneous_positions:
        r.append("maximum simultaneous positions reached")
    if cfg.risk.one_index_position_at_a_time and contract.kind == "index" and day.open_index_positions >= 1:
        r.append("an index futures position is already open")
    agg = day.start_equity * D(cfg.risk.max_aggregate_open_risk_pct) / Decimal(100)
    if day.open_risk + sz.initial_risk_rupees > agg:
        r.append("aggregate open risk cap would be exceeded")
    last = day.last_stop_out.get(contract.instrument_id)
    if last is not None and (now - last).total_seconds() < cfg.risk.cooldown_minutes_after_stop * 60:
        r.append("cooldown after a stopped-out trade")
    if sz.lots < 1:
        r.append(sz.reason or "zero quantity")
    return r
