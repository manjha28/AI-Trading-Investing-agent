"""Fail-closed pre-trade checks. Every check records pass/fail with its reason; any missing input counts as a failure."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.dhan_execution import policy
from app.dhan_execution.intent import DhanOrderIntent

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class LiveContext:
    now: datetime
    deployment_enabled: bool
    environment_block: str | None
    armed: bool
    forward_test_execute_live: bool
    max_daily_loss_inr: Decimal
    max_trades_per_day: int
    max_open_positions: int
    max_notional_inr: Decimal
    last_bar_close: datetime | None          # close time of the newest stored 1-minute bar
    last_price: Decimal | None
    orders_today: int
    open_positions: int
    realized_pnl_today: Decimal
    instrument_has_open_order: bool
    duplicate_signal: bool


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str | None:
        failed = [c for c in self.checks if not c.passed]
        return "; ".join(f"{c.name}: {c.detail}" for c in failed) or None

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks]}


def evaluate(intent: DhanOrderIntent, ctx: LiveContext) -> Decision:
    now = ctx.now.astimezone(timezone.utc)
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name, bool(ok), detail if not ok else "ok"))

    check("deployment_flag", ctx.deployment_enabled, "DHAN_LIVE_EXECUTION_ENABLED is not set on this deployment")
    check("environment", ctx.environment_block is None, f"live orders are blocked here ({ctx.environment_block})")
    check("armed", ctx.armed, "live execution is not armed by the owner")
    check("test_executes_live", ctx.forward_test_execute_live, "this forward test is paper-only (execute_live is off)")
    check("strategy_allowlist", intent.strategy_key in policy.ALLOWED_STRATEGY_KEYS, f"{intent.strategy_key} is not allowlisted")
    check("quantity_cap", intent.quantity <= policy.MAX_ORDER_QUANTITY, f"quantity {intent.quantity} exceeds the cap of {policy.MAX_ORDER_QUANTITY}")
    local = now.astimezone(IST)
    lo, hi = policy.ENTRY_WINDOW_IST
    check("market_window", local.weekday() < 5 and lo <= local.time() <= hi, f"{local:%a %H:%M} IST is outside entry hours {lo:%H:%M}-{hi:%H:%M} Mon-Fri")
    fresh = ctx.last_bar_close is not None and timedelta(0) <= now - ctx.last_bar_close.astimezone(timezone.utc) <= timedelta(seconds=policy.PRICE_MAX_AGE_SECONDS + 60)
    check("data_fresh", fresh, "no recent stored 1-minute bar; data is missing or stale" if ctx.last_bar_close is None else f"newest bar closed {ctx.last_bar_close.astimezone(IST):%H:%M} IST, too old")
    price_ok = ctx.last_price is not None and ctx.last_price > 0
    check("price_known", price_ok, "no last price")
    if price_ok:
        notional = ctx.last_price * intent.quantity
        check("notional_cap", notional <= ctx.max_notional_inr, f"order value {notional} exceeds the cap {ctx.max_notional_inr}")
        long = intent.side == "buy"
        passed_stop = ctx.last_price <= intent.stop_price if long else ctx.last_price >= intent.stop_price
        passed_target = ctx.last_price >= intent.target_price if long else ctx.last_price <= intent.target_price
        check("price_inside_levels", not passed_stop and not passed_target, "price has already moved through the stop or the target")
        risk = abs(intent.reference_price - intent.stop_price) * intent.quantity
        check("risk_per_trade", risk <= ctx.max_daily_loss_inr / 2, f"risk at the stop {risk} exceeds half the daily loss cap")
    check("daily_loss_cap", ctx.realized_pnl_today > -ctx.max_daily_loss_inr, f"realised loss today {ctx.realized_pnl_today} reached the cap {ctx.max_daily_loss_inr}")
    check("daily_trade_cap", ctx.orders_today < ctx.max_trades_per_day, f"{ctx.orders_today} orders already today (cap {ctx.max_trades_per_day})")
    check("open_position_cap", ctx.open_positions < ctx.max_open_positions, f"{ctx.open_positions} positions already open (cap {ctx.max_open_positions})")
    check("one_order_per_instrument", not ctx.instrument_has_open_order, "an order for this instrument is already open")
    check("not_a_duplicate", not ctx.duplicate_signal, "this signal was already acted on")
    return Decision(all(c.passed for c in checks), tuple(checks))
