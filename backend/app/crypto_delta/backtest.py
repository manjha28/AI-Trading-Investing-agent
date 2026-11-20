"""Deterministic backtester for Delta perpetuals (linear USD contracts).

Rules (stated with every result):
  * Completed candles only. A target position is decided at the CLOSE of bar i and executed at the
    OPEN of bar i+1; nothing sees a bar before it has closed (no look-ahead).
  * Every fill pays the exchange's taker fee, plus optional adverse slippage; market and stop exits
    are slipped, take-profit exits are not. Fee rates come from the exchange's own product data.
  * Stops/targets are checked against each bar's high/low (including the entry bar). If both lie
    inside one bar the stop is assumed to hit first. A gap through a level fills at the open.
  * A missing-candle gap closes any open position at the last close before the gap.
  * Margin (when the contract's margin data is supplied, which the API always does): isolated margin at the chosen leverage.
    A trade needs initial margin (notional / leverage) plus its entry fee from the account balance, else the entry is skipped.
    Size is a fixed contract count, a USD notional or a % of equity risked to the stop (whole contracts; capped by margin and
    by the contract's position-size limit). A position is liquidated when its loss reaches (initial - maintenance margin);
    the liquidation outcome is bounded at the margin, so equity can never go negative.
  * NOT modelled: funding payments, margin scaling with position size, cross margin, order-book depth.
All money is Decimal. Results are hypothetical (a replay of real historical candles) and never a position, order or money at risk.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from app.crypto_delta.candles import RESOLUTION_SECONDS, Candle
from app.crypto_delta.indicators import atr_series
from app.crypto_delta.strategies import Strategy

CENT = Decimal("0.01")
ZERO = Decimal("0")
MAX_BARS = 60_000
EQUITY_POINTS = 500


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    contract_value: Decimal  # underlying units per contract
    tick_size: Decimal | None
    taker_fee: Decimal
    maker_fee: Decimal
    # Exchange margin data (percent of notional, as Delta publishes it). None = no margin model (legacy/unit tests only).
    initial_margin_pct: Decimal | None = None
    maintenance_margin_pct: Decimal | None = None
    default_leverage: Decimal | None = None
    liquidation_penalty_factor: Decimal | None = None
    position_size_limit: int | None = None

    @property
    def max_leverage(self) -> Decimal | None:
        """The highest leverage the exchange's initial margin allows (100 / initial margin %)."""
        return Decimal(100) / self.initial_margin_pct if self.initial_margin_pct and self.initial_margin_pct > 0 else None


@dataclass
class _Position:
    direction: int
    entry_price: Decimal
    entry_time: datetime
    entry_index: int
    entry_fee: Decimal
    stop: Decimal | None
    target: Decimal | None
    contracts: Decimal = Decimal("0")
    margin: Decimal = Decimal("0")          # initial margin held for this position (0 without a margin model)
    maintenance: Decimal = Decimal("0")
    liq_price: Decimal | None = None
    notional: Decimal = Decimal("0")


def _plain(value: Decimal) -> str:
    """Decimal as plain text without exponent notation ('20', never '2E+1')."""
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_EVEN))


def run_backtest(
    candles: list[Candle], resolution: str, strategy: Strategy, params: dict[str, Any], spec: ContractSpec, *,
    initial_capital: Decimal, contracts: Decimal, slippage_bps: Decimal,
    leverage: Decimal | None = None, sizing_mode: str = "contracts", sizing_value: Decimal | None = None,
) -> dict[str, Any]:
    if len(candles) > MAX_BARS:
        raise ValueError(f"At most {MAX_BARS} bars per backtest.")
    step = timedelta(seconds=RESOLUTION_SECONDS[resolution])
    n = len(candles)
    targets = strategy.targets(candles, params)
    atr = atr_series([c.high for c in candles], [c.low for c in candles], [c.close for c in candles], params["atr_period"])
    slip = slippage_bps / Decimal(10000)
    fee_rate = spec.taker_fee
    cv = spec.contract_value
    stop_mult, tp_r = params["stop_atr_mult"], params["take_profit_r"]
    margin_model = spec.initial_margin_pct is not None and spec.maintenance_margin_pct is not None
    if margin_model:
        leverage = leverage if leverage is not None else min(Decimal(10), spec.max_leverage or Decimal(10))
        if leverage < 1 or (spec.max_leverage is not None and leverage > spec.max_leverage):
            raise ValueError(f"Leverage must be between 1 and {_plain(spec.max_leverage) if spec.max_leverage else 1} for {spec.symbol}.")
        mm_rate = spec.maintenance_margin_pct / Decimal(100)
        liq_distance = Decimal(1) / leverage - mm_rate            # fraction of entry price the position can lose before liquidation
        if liq_distance <= 0:
            raise ValueError("This leverage leaves no room above the maintenance margin.")
        penalty_factor = spec.liquidation_penalty_factor or ZERO
    if sizing_mode not in ("contracts", "notional_usd", "risk_pct"):
        raise ValueError("sizing_mode must be contracts, notional_usd or risk_pct.")
    if sizing_mode == "risk_pct" and not stop_mult > 0:
        raise ValueError("Sizing by % risked needs a stop (stop_atr_mult > 0).")
    if sizing_mode != "contracts" and (sizing_value is None or sizing_value <= 0):
        raise ValueError("A positive sizing value is required.")

    def tick(price: Decimal) -> Decimal:
        if not spec.tick_size or spec.tick_size <= 0:
            return price
        return (price / spec.tick_size).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * spec.tick_size

    def fee_on(price: Decimal, qty: Decimal | None = None) -> Decimal:
        return (contracts if qty is None else qty) * cv * price * fee_rate

    position: _Position | None = None
    blocked = 0
    pending: int | None = None
    realized = ZERO
    trades: list[dict[str, Any]] = []
    equity: list[tuple[datetime, Decimal]] = []
    bars_in_market = 0
    skipped_no_atr = 0
    skipped_margin = 0
    skipped_size = 0
    liquidations = 0
    max_notional = ZERO
    max_margin = ZERO
    gap_exits = 0

    def close(price: Decimal, when: datetime, index: int, reason: str) -> None:
        nonlocal position, realized, liquidations
        assert position is not None
        qty = position.contracts
        gross = position.direction * (price - position.entry_price) * qty * cv
        fees = position.entry_fee + fee_on(price, qty)
        if reason == "liquidated":
            # Isolated margin: the loss is bounded at the margin. The position loses (initial - maintenance) margin, then the exchange's
            # liquidation penalty is charged on the maintenance margin. (Gaps beyond the liquidation price do not add to the loss.)
            gross = -(position.margin - position.maintenance)
            fees += penalty_factor * position.maintenance
            liquidations += 1
        net = gross - fees
        realized += net
        trades.append({
            "side": "long" if position.direction > 0 else "short",
            "contracts": _plain(qty), "notional_usd": _money(position.notional), "margin_used": _money(position.margin) if margin_model else None,
            "leverage": _plain(leverage) if margin_model else None,
            "entry_time": position.entry_time.isoformat(), "exit_time": when.isoformat(),
            "entry_price": str(tick(position.entry_price)), "exit_price": str(tick(price)),
            "gross_pnl": _money(gross), "fees": _money(fees), "net_pnl": _money(net),
            "exit_reason": reason, "bars_held": max(index - position.entry_index, 0),
            "_net": net,
        })
        position = None

    for i, candle in enumerate(candles):
        if i > 0 and candle.open_time - candles[i - 1].open_time > step:
            if position is not None:
                close(candles[i - 1].close, candles[i - 1].open_time + step, i - 1, "data_gap")
                gap_exits += 1
            pending = None
        if pending is not None:
            if position is not None and pending != position.direction:
                side = -position.direction  # selling a long / buying back a short
                close(candle.open * (Decimal(1) + side * slip), candle.open_time, i, "signal")
            if position is None and pending != 0 and pending != blocked:
                signal_atr = atr[i - 1]
                if stop_mult > 0 and signal_atr is None:
                    skipped_no_atr += 1
                else:
                    price = candle.open * (Decimal(1) + pending * slip)
                    distance = stop_mult * signal_atr if stop_mult > 0 else None
                    qty = contracts
                    margin_blocked = False
                    if sizing_mode == "notional_usd":
                        qty = (sizing_value / (cv * price)).to_integral_value(rounding="ROUND_FLOOR")
                    elif sizing_mode == "risk_pct":
                        risk_usd = (initial_capital + realized) * sizing_value / Decimal(100)
                        qty = (risk_usd / (distance * cv)).to_integral_value(rounding="ROUND_FLOOR") if distance else ZERO
                    if margin_model:
                        balance = initial_capital + realized
                        affordable = (balance / (cv * price * (Decimal(1) / leverage + fee_rate))).to_integral_value(rounding="ROUND_FLOOR")
                        limit = Decimal(spec.position_size_limit) if spec.position_size_limit else None
                        if sizing_mode == "contracts":
                            if qty > affordable:
                                qty, margin_blocked = ZERO, True
                                skipped_margin += 1
                        else:
                            if qty > affordable:
                                qty = affordable
                        if limit is not None and qty > limit:
                            qty = limit
                    if qty < 1:
                        if not margin_blocked:
                            skipped_size += 1     # sizing rounds below one whole contract (or margin leaves less than one)
                        qty = ZERO
                    if qty >= 1:
                        notional = qty * cv * price
                        margin = notional / leverage if margin_model else ZERO
                        maintenance = notional * mm_rate if margin_model else ZERO
                        liq = price * (Decimal(1) - pending * liq_distance) if margin_model else None
                        position = _Position(
                            pending, price, candle.open_time, i, fee_on(price, qty),
                            price - pending * distance if distance else None,
                            price + pending * tp_r * distance if distance and tp_r > 0 else None,
                            qty, margin, maintenance, liq, notional,
                        )
                        max_notional, max_margin = max(max_notional, notional), max(max_margin, margin)
            pending = None
        if position is not None:
            d = position.direction
            stop_hit = position.stop is not None and (candle.low <= position.stop if d > 0 else candle.high >= position.stop)
            target_hit = position.target is not None and (candle.high >= position.target if d > 0 else candle.low <= position.target)
            liq_hit = position.liq_price is not None and (candle.low <= position.liq_price if d > 0 else candle.high >= position.liq_price)
            # Both levels inside one bar: price meets the one nearer the entry first (the stop, unless it lies beyond the liquidation price).
            if liq_hit and stop_hit and position.stop is not None and position.liq_price is not None:
                stop_first = position.stop >= position.liq_price if d > 0 else position.stop <= position.liq_price
                liq_hit, stop_hit = (not stop_first), stop_first
            if liq_hit:
                gapped = candle.open <= position.liq_price if d > 0 else candle.open >= position.liq_price
                blocked = d
                close(candle.open if gapped else position.liq_price, candle.open_time, i, "liquidated")
            elif stop_hit:
                gapped = candle.open <= position.stop if d > 0 else candle.open >= position.stop
                level = candle.open if gapped else position.stop
                blocked = d
                close(level * (Decimal(1) - d * slip), candle.open_time, i, "stop")
            elif target_hit:
                gapped = candle.open >= position.target if d > 0 else candle.open <= position.target
                level = candle.open if gapped else position.target
                blocked = d
                close(level, candle.open_time, i, "target")
        if blocked and targets[i] != blocked:
            blocked = 0
        if position is not None:
            bars_in_market += 1
            unrealized = position.direction * (candle.close - position.entry_price) * position.contracts * cv
            if margin_model:   # isolated margin: an open position can lose at most (initial - maintenance) margin
                unrealized = max(unrealized, -(position.margin - position.maintenance))
            unrealized -= position.entry_fee
        else:
            unrealized = ZERO
        equity.append((candle.open_time, initial_capital + realized + unrealized))
        if i < n - 1:
            current = position.direction if position else 0
            want = targets[i]
            pending = want if want != current and not (position is None and want == blocked) else None
    if position is not None and n:
        close(candles[-1].close, candles[-1].open_time + step, n - 1, "end_of_data")
        equity[-1] = (equity[-1][0], initial_capital + realized)

    nets = [t["_net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    peak, max_dd = initial_capital, ZERO
    for _, value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    net_total = sum(nets, ZERO)
    gross_profit, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    metrics = {
        "closed_trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": str((Decimal(len(wins)) * 100 / len(nets)).quantize(CENT)) if nets else None,
        "net_pnl": _money(net_total) if nets else None,
        "return_pct": str((net_total * 100 / initial_capital).quantize(CENT)) if nets else None,
        "gross_profit": _money(gross_profit), "gross_loss": _money(gross_loss),
        "profit_factor": str((gross_profit / gross_loss).quantize(CENT)) if gross_loss > 0 else None,
        "expectancy": _money(net_total / len(nets)) if nets else None,
        "avg_win": _money(gross_profit / len(wins)) if wins else None,
        "avg_loss": _money(-gross_loss / len(losses)) if losses else None,
        "max_drawdown": _money(max_dd), "max_drawdown_pct": str((max_dd * 100 / peak).quantize(CENT)) if peak > 0 else None,
        "exposure_pct": str((Decimal(bars_in_market) * 100 / n).quantize(CENT)) if n else None,
        "total_fees": _money(sum((Decimal(t["fees"]) for t in trades), ZERO)),
        "skipped_entries_no_atr": skipped_no_atr, "data_gap_exits": gap_exits,
        "liquidations": liquidations, "skipped_entries_margin": skipped_margin, "skipped_entries_size": skipped_size,
        "max_notional_usd": _money(max_notional), "max_margin_used": _money(max_margin) if margin_model else None,
    }
    stride = max(1, len(equity) // EQUITY_POINTS)
    sampled = equity[::stride]
    if equity and sampled[-1][0] != equity[-1][0]:
        sampled.append(equity[-1])
    for trade in trades:
        del trade["_net"]
    return {
        "hypothetical": True,
        "label": "Hypothetical backtest on historical candles. Not a position, an order, or real P&L.",
        "metrics": metrics,
        "trades": trades,
        "equity": [{"time": when.isoformat(), "equity": _money(value)} for when, value in sampled],
        "assumptions": {
            "venue": "Delta Exchange India, linear USD perpetual",
            "contract_value": str(spec.contract_value), "tick_size": str(spec.tick_size) if spec.tick_size else None,
            "fee_rate_per_fill": str(fee_rate), "fee_source": "exchange product data (taker rate applied to every fill)",
            "slippage_bps": str(slippage_bps), "contracts_per_trade": _plain(contracts) if sizing_mode == "contracts" else None,
            "sizing_mode": sizing_mode, "sizing_value": _plain(contracts if sizing_mode == "contracts" else sizing_value),
            "units_per_contract": str(cv),
            "initial_capital_usd": str(initial_capital),
            "margin": {
                "model": "isolated margin at the chosen leverage" if margin_model else "none (unit-test spec without margin data)",
                "leverage": _plain(leverage) if margin_model else None, "max_leverage": _plain(spec.max_leverage) if spec.max_leverage else None,
                "initial_margin_pct": _plain(spec.initial_margin_pct) if margin_model else None,
                "maintenance_margin_pct": _plain(spec.maintenance_margin_pct) if margin_model else None,
                "liquidation_penalty_factor": _plain(penalty_factor) if margin_model else None,
                "position_size_limit": spec.position_size_limit, "source": "Delta Exchange product data",
            },
            "fill_model": "signal at bar close, fill at next bar open; stop before target inside a bar; gaps fill at the open",
            "not_modelled": ["funding payments", "margin scaling with position size", "cross margin", "order-book depth"] if margin_model
            else ["funding payments", "margin and leverage limits", "liquidation", "order-book depth"],
        },
    }
