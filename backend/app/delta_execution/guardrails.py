"""Fail-closed pre-trade checks. Every check records pass/fail with its reason; any missing input counts as a failure."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.delta_execution import policy
from app.delta_execution.intent import DeltaOrderIntent


@dataclass(frozen=True)
class LiveContext:
    now: datetime
    deployment_enabled: bool
    credentials_present: bool
    environment_block: str | None
    armed: bool
    test_executes_live: bool
    max_daily_loss_usd: Decimal
    max_trades_per_day: int
    max_open_positions: int
    max_notional_usd: Decimal
    contract_value: Decimal | None          # underlying units per contract
    exchange_max_leverage: int | None       # None = the contract's margin data is unknown
    last_candle_close: datetime | None
    last_price: Decimal | None
    orders_today: int
    open_positions: int
    realized_pnl_today: Decimal
    contract_has_open_order: bool
    duplicate_signal: bool
    wallet_balance_usd: Decimal | None = None       # total wallet balance read from Delta just before sending; None = unreadable, so the wallet-based checks are skipped
    realized_pnl_known: bool = True                 # False = today's real realised P&L could not be read from Delta, which fails the daily-loss check closed
    maintenance_margin_pct: Decimal | None = None   # percent, e.g. 0.25; None = unknown, which fails the liquidation-safety check closed
    signal_max_age_seconds: int | None = None       # one candle of the strategy's timeframe (owner decision 2026-11-25); None = unknown timeframe, which fails the freshness check closed


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


def evaluate(intent: DeltaOrderIntent, ctx: LiveContext) -> Decision:
    now = ctx.now.astimezone(timezone.utc)
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name, bool(ok), detail if not ok else "ok"))

    check("deployment_flag", ctx.deployment_enabled, "DELTA_LIVE_EXECUTION_ENABLED is not set on this deployment")
    check("credentials", ctx.credentials_present, "Delta API credentials are not configured")
    check("environment", ctx.environment_block is None, f"live orders are blocked here ({ctx.environment_block})")
    check("armed", ctx.armed, "live execution is not armed by the owner")
    check("test_executes_live", ctx.test_executes_live, "this forward test is paper-only (execute_live is off)")
    check("strategy_allowlist", intent.strategy_key in policy.ALLOWED_STRATEGY_KEYS, f"{intent.strategy_key} is not allowlisted")
    check("size_cap", intent.contracts <= policy.MAX_CONTRACTS, f"{intent.contracts} contracts exceeds the cap of {policy.MAX_CONTRACTS}")
    allowed = min(policy.LEVERAGE_CAP, ctx.exchange_max_leverage) if ctx.exchange_max_leverage else None
    check("leverage_cap", allowed is not None and intent.leverage <= allowed,
          "the contract's margin data is unknown, so its maximum leverage cannot be confirmed" if allowed is None else f"leverage {intent.leverage}x exceeds min({policy.LEVERAGE_CAP}x, exchange max {ctx.exchange_max_leverage}x) = {allowed}x")
    max_age = ctx.signal_max_age_seconds
    fresh = (max_age is not None and max_age > 0 and ctx.last_candle_close is not None
             and timedelta(0) <= now - ctx.last_candle_close.astimezone(timezone.utc) <= timedelta(seconds=max_age))
    check("signal_fresh", fresh, "the strategy timeframe is unknown, so signal freshness cannot be confirmed" if not max_age or max_age <= 0
          else f"no completed candle closed within one candle ({max_age // 60} min); the signal is missing or stale")
    price_ok = ctx.last_price is not None and ctx.last_price > 0
    check("price_known", price_ok, "no last price")
    if price_ok and ctx.contract_value is not None:
        notional = ctx.last_price * ctx.contract_value * intent.contracts
        check("notional_cap", notional <= ctx.max_notional_usd, f"order value {notional:.2f} USD exceeds the cap {ctx.max_notional_usd}")
        long = intent.side == "buy"
        passed_stop = ctx.last_price <= intent.stop_price if long else ctx.last_price >= intent.stop_price
        passed_target = ctx.last_price >= intent.target_price if long else ctx.last_price <= intent.target_price
        check("price_inside_levels", not passed_stop and not passed_target, "price has already moved through the stop or the target")
        risk = abs(intent.reference_price - intent.stop_price) * ctx.contract_value * intent.contracts
        check("risk_per_trade", risk <= ctx.max_daily_loss_usd / 2, f"risk at the stop {risk:.2f} USD exceeds half the daily loss cap")
    else:
        check("notional_cap", False, "contract value or price unknown")
    # wallet-based checks. The balance is NOT a gate: if it cannot cover the order Delta rejects it and the reason is shown in the ledger.
    # The wallet is only used to size the effective leverage and the cross-margin liquidation distance, and when it cannot be read those two are not checked.
    if ctx.wallet_balance_usd is not None and ctx.wallet_balance_usd > 0 and price_ok and ctx.contract_value is not None:
        notional = ctx.last_price * ctx.contract_value * intent.contracts
        cap = policy.effective_leverage_cap()
        check("effective_leverage_cap", notional <= cap * ctx.wallet_balance_usd,
              f"order value {notional:.2f} USD is {notional / ctx.wallet_balance_usd:.1f}x the wallet {ctx.wallet_balance_usd:.2f} USD; the cap is {cap}x")
        if ctx.maintenance_margin_pct is not None and ctx.maintenance_margin_pct >= 0:
            # cross margin: the whole wallet backs the position, so liquidation is (wallet - maintenance margin) / quantity away from entry
            quantity = ctx.contract_value * intent.contracts
            room = (ctx.wallet_balance_usd - notional * ctx.maintenance_margin_pct / 100) / quantity
            gap = abs(intent.reference_price - intent.stop_price)
            check("liquidation_safety_cross", room > 0 and gap <= Decimal("0.5") * room,
                  f"under cross margin the position is liquidated about {max(room, Decimal(0)):.0f} away from entry; the stop is {gap:.0f} away and must be within half of that")
    # the shared liquidation rule (app.delta_lab.risk): the stop must sit before liquidation and within half of the entry-to-liquidation distance
    from app.delta_lab.risk import liquidation_price
    if ctx.maintenance_margin_pct is not None and ctx.maintenance_margin_pct >= 0 and intent.leverage > 0 and intent.reference_price > 0:
        long = intent.side == "buy"
        liq = liquidation_price("long" if long else "short", intent.reference_price, Decimal(intent.leverage), ctx.maintenance_margin_pct / 100)
        before = intent.stop_price > liq if long else intent.stop_price < liq
        half = abs(intent.reference_price - intent.stop_price) <= Decimal("0.5") * abs(intent.reference_price - liq)
        check("liquidation_safety", before and half, f"the stop {intent.stop_price} is not safely before the estimated liquidation price {liq:.2f} (it must be before it and within half the distance)")
    else:
        check("liquidation_safety", False, "maintenance margin unknown, so the liquidation price cannot be estimated")
    check("daily_loss_known", ctx.realized_pnl_known, "today's realised P&L could not be read from Delta")
    check("daily_loss_cap", ctx.realized_pnl_today > -ctx.max_daily_loss_usd, f"realised loss today {ctx.realized_pnl_today} reached the cap {ctx.max_daily_loss_usd}")
    check("daily_trade_cap", ctx.orders_today < ctx.max_trades_per_day, f"{ctx.orders_today} orders already today (cap {ctx.max_trades_per_day})")
    check("open_position_cap", ctx.open_positions < ctx.max_open_positions, f"{ctx.open_positions} positions already open (cap {ctx.max_open_positions})")
    check("one_order_per_contract", not ctx.contract_has_open_order, "an order for this contract is already open")
    check("not_a_duplicate", not ctx.duplicate_signal, "this signal was already acted on")
    return Decision(all(c.passed for c in checks), tuple(checks))
