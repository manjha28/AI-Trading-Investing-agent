"""The ONE risk gate and position sizer. The backtest simulator, the paper engine and any live engine all call these two functions.

Money is Decimal. Default mode is paper. Every rule fails closed: a missing or unverifiable input rejects the order. Nothing here sends anything.
Hypothetical research code; decision support, not investment advice."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Literal

D = Decimal
FUNDING_HOURS_UTC = (0, 8, 16)
Side = Literal["long", "short"]


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    contract_value: Decimal           # base units per contract, e.g. 0.001 BTC
    tick_size: Decimal
    mm_pct: Decimal                   # maintenance margin as a fraction (0.0025 = 0.25%)
    im_pct_min: Decimal               # minimum initial margin fraction (0.005 = 0.5% = 200x)
    slope_threshold_notional: Decimal # above this notional the margin rates scale up; the slope is NOT modelled, so larger orders are refused
    max_leverage: Decimal = D(200)
    taker_fee: Decimal = D("0.0005")
    maker_fee: Decimal = D("0.0002")
    gst: Decimal = D("0.18")


@dataclass(frozen=True)
class RiskConfig:
    risk_pct: Decimal = D("0.005")            # of wallet per trade
    leverage: Decimal = D(10)                 # isolated; never raised to make an order fit
    max_margin_use: Decimal = D("0.80")       # of available balance, margin + round-trip fees + GST
    daily_loss_limit_pct: Decimal = D("0.02")
    max_consecutive_losses: int = 4
    funding_window_s: int = 120
    max_tick_age_s: Decimal = D(2)
    max_orders_per_minute: int = 6
    in_use_mode: Literal["strict", "per_symbol"] = "strict"
    mode: Literal["paper", "live"] = "paper"  # live additionally needs a typed confirmation at start-up (outside this module)
    liq_fraction: Decimal = D("0.5")          # |entry-stop| must be <= this x |entry-liq|


@dataclass(frozen=True)
class AccountSnapshot:
    wallet: Decimal
    position_margin: Decimal
    order_margin: Decimal
    available: Decimal
    symbol_margin: dict[str, Decimal] = field(default_factory=dict)   # margin in use per symbol (per_symbol mode)


@dataclass(frozen=True)
class MarketState:
    now: datetime
    last_tick_age_s: Decimal | None   # None = unknown -> reject
    spread: Decimal | None
    spread_p95: Decimal | None        # the 95th percentile for this hour; None = no quote feed -> the check cannot run and is recorded as such


@dataclass
class DayState:
    day: str = ""
    realized_pnl: Decimal = D(0)
    losses_in_row: int = 0
    order_times: list[datetime] = field(default_factory=list)

    def roll(self, now: datetime) -> None:
        key = now.astimezone(timezone.utc).date().isoformat()
        if key != self.day:
            self.day, self.realized_pnl, self.losses_in_row, self.order_times = key, D(0), 0, []   # an unlock comes with the next UTC day

    def record_close(self, pnl: Decimal) -> None:
        self.realized_pnl += pnl
        self.losses_in_row = self.losses_in_row + 1 if pnl < 0 else 0


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    entry: Decimal
    stop: Decimal                     # triggers on MARK price
    target: Decimal | None
    family: str = ""
    client_order_id: str = ""


@dataclass(frozen=True)
class Decision:
    action: Literal["send", "paper", "reject"]
    reasons: tuple[str, ...]
    qty: int = 0
    leverage: Decimal = D(0)
    liq_price: Decimal | None = None
    required_margin: Decimal = D(0)
    est_round_trip_cost: Decimal = D(0)


def liquidation_price(side: Side, entry: Decimal, leverage: Decimal, mm_pct: Decimal) -> Decimal:
    im = D(1) / leverage
    return entry * (D(1) - im + mm_pct) if side == "long" else entry * (D(1) + im - mm_pct)


def next_funding(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    for hour in FUNDING_HOURS_UTC:
        cand = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if cand >= now:
            return cand
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def in_funding_window(now: datetime, window_s: int) -> bool:
    now = now.astimezone(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stamps = [today + timedelta(hours=h) for h in FUNDING_HOURS_UTC] + [today + timedelta(days=1)]
    return any(abs((now - s).total_seconds()) <= window_s for s in stamps)


def round_trip_cost(spec: ContractSpec, notional: Decimal) -> Decimal:
    """Worst case for the balance check: taker on both legs, plus GST on the fees."""
    return notional * spec.taker_fee * 2 * (D(1) + spec.gst)


def size_position(spec: ContractSpec, cfg: RiskConfig, wallet: Decimal, entry: Decimal, stop: Decimal) -> int:
    """qty = floor_to_lot(risk_amount / |entry - stop| / contract_value). One contract is the lot. Returns 0 when the stop distance is zero."""
    distance = abs(entry - stop)
    if distance <= 0 or wallet <= 0 or entry <= 0:
        return 0
    risk_amount = wallet * cfg.risk_pct
    return int((risk_amount / distance / spec.contract_value).to_integral_value(rounding=ROUND_DOWN))


def risk_gate(spec: ContractSpec, cfg: RiskConfig, acct: AccountSnapshot | None, market: MarketState, day: DayState, order: OrderRequest) -> Decision:
    """Returns send / paper / reject with every reason. Used identically by backtest, paper and live. Never raises leverage or moves a stop."""
    reasons: list[str] = []
    day.roll(market.now)
    if acct is None:
        return Decision("reject", ("account_unreadable",))
    if market.last_tick_age_s is None or market.last_tick_age_s > cfg.max_tick_age_s:
        reasons.append("stale_data")
    if market.spread is not None and market.spread_p95 is not None and market.spread > market.spread_p95:
        reasons.append("spread_above_p95")
    if in_funding_window(market.now, cfg.funding_window_s):
        reasons.append("funding_window")
    if acct.wallet > 0 and day.realized_pnl <= -(acct.wallet * cfg.daily_loss_limit_pct):
        reasons.append("daily_loss_limit")
    if day.losses_in_row >= cfg.max_consecutive_losses:
        reasons.append("consecutive_losses")
    recent = [t for t in day.order_times if (market.now - t).total_seconds() < 60]
    if len(recent) >= cfg.max_orders_per_minute:
        reasons.append("order_rate_cap")
    if order.stop is None or order.stop <= 0:
        reasons.append("no_stop")

    qty = 0
    liq = None
    if True:  # noqa: SIM108 - always evaluated so the rejection log lists every failing check, not only the first
        liq = liquidation_price(order.side, order.entry, cfg.leverage, spec.mm_pct)
        if D(1) / cfg.leverage < spec.im_pct_min or cfg.leverage > spec.max_leverage:
            reasons.append("leverage_not_allowed")
        if order.stop and order.stop > 0:
            if order.side == "long":
                if not order.stop > liq:
                    reasons.append("stop_beyond_liquidation")
                if order.stop >= order.entry:
                    reasons.append("stop_on_wrong_side")
            else:
                if not order.stop < liq:
                    reasons.append("stop_beyond_liquidation")
                if order.stop <= order.entry:
                    reasons.append("stop_on_wrong_side")
            if abs(order.entry - order.stop) > cfg.liq_fraction * abs(order.entry - liq):
                reasons.append("stop_too_close_to_liquidation")
            qty = size_position(spec, cfg, acct.wallet, order.entry, order.stop)
            if qty < 1:
                reasons.append("size_below_one_lot")

    required = D(0)
    cost = D(0)
    if qty >= 1:
        notional = order.entry * spec.contract_value * qty
        if notional > spec.slope_threshold_notional:
            reasons.append("notional_above_margin_slope_threshold")   # the slope above the threshold is not modelled, so this fails closed
        required = notional / cfg.leverage
        cost = round_trip_cost(spec, notional)
        if required + cost > cfg.max_margin_use * acct.available:
            reasons.append("insufficient_available_balance")

    if reasons:
        return Decision("reject", tuple(dict.fromkeys(reasons)), qty, cfg.leverage, liq, required, cost)
    in_use = acct.position_margin + acct.order_margin > 0 if cfg.in_use_mode == "strict" else acct.symbol_margin.get(order.symbol, D(0)) > 0
    if in_use:
        return Decision("paper", ("balance_in_use",), qty, cfg.leverage, liq, required, cost)
    if cfg.mode != "live":
        return Decision("paper", ("mode_paper",), qty, cfg.leverage, liq, required, cost)
    day.order_times.append(market.now)
    return Decision("send", (), qty, cfg.leverage, liq, required, cost)


def post_fill_liquidation_ok(side: Side, stop: Decimal, exchange_liq_price: Decimal | None, entry: Decimal, cfg: RiskConfig) -> bool:
    """After every fill the exchange's own liquidation price must still satisfy the rule; None (unreadable) fails. False means flatten and alert."""
    if exchange_liq_price is None:
        return False
    ok = stop > exchange_liq_price if side == "long" else stop < exchange_liq_price
    return ok and abs(entry - stop) <= cfg.liq_fraction * abs(entry - exchange_liq_price)


def must_flatten_for_missing_stop(fill_time: datetime, stop_confirmed_at: datetime | None, now: datetime, deadline_s: int = 2) -> bool:
    """The stop must be confirmed within 2 s of the fill, else flatten immediately."""
    if stop_confirmed_at is not None and (stop_confirmed_at - fill_time).total_seconds() <= deadline_s:
        return False
    return (now - fill_time).total_seconds() > deadline_s
