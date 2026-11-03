"""Walk-forward statistics over completed stored daily bars; never creates live state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.models import Instrument, MarketBar
from app.services.indicator_library import calculate_indicator, minimum_bars_for
from app.services.strategy_contract import FastSlowMAAdapter, SMARSIAdapter, StrategyContext
from app.services.strategy_engine import RSI_PERIOD, StrategyParams
from app.services.trade_metrics import CENT, net_pnl, summarize_net_pnls


BPS = Decimal("10000")
UNIT = Decimal("1")


@dataclass(frozen=True)
class BacktestAssumptions:
    brokerage_bps: Decimal = Decimal("3")
    exchange_charges_bps: Decimal = Decimal("3")
    slippage_bps: Decimal = Decimal("5")
    stop_atr_multiple: Decimal = Decimal("1.5")
    target_r_multiple: Decimal = Decimal("3")


class InsufficientBacktestHistory(ValueError):
    def __init__(self, required: int, available: int):
        self.required = required
        self.available = available
        super().__init__(f"requires {required} completed daily bars; {available} available")


def minimum_history(params: StrategyParams) -> int:
    if params.strategy_id == "sma_rsi":
        strategy_bars = max(params.slow_period + 1, RSI_PERIOD + 1)
    else:
        ma_id = params.ma_type.lower()
        strategy_bars = max(
            minimum_bars_for(ma_id, {"length": params.fast_period}),
            minimum_bars_for(ma_id, {"length": params.slow_period}),
        ) + 1
    return max(strategy_bars, 14)


def _decision_time(bar: MarketBar) -> datetime:
    stamp = bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc) + timedelta(days=1)


def _entry_time(instrument: Instrument, bar: MarketBar) -> datetime:
    """Daily bars are UTC-midnight session-date keys; infer the venue's published open."""
    if instrument.country == "IN":
        opening = time(9, 15)
    elif instrument.country == "US":
        opening = time(9, 30)
    else:
        raise ValueError(f"daily session open is not configured for {instrument.country}")
    zone = ZoneInfo(instrument.timezone)
    session_date = (bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=timezone.utc)).date()
    return datetime.combine(session_date, opening, tzinfo=zone).astimezone(timezone.utc)


def _context(instrument: Instrument, bars: list[MarketBar], source: str) -> StrategyContext:
    # The strategy receives this prefix only. It never receives the future tail of the run.
    rows = [{
        "time": bar.timestamp,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "is_synthetic": bar.is_synthetic,
    } for bar in bars]
    decision_point = _decision_time(bars[-1])
    return StrategyContext(
        instrument=instrument,
        as_of=decision_point,
        frames={"1d": pd.DataFrame(rows)},
        provenance={"1d": source},
    )


def _adapter(params: StrategyParams, *, allow_short: bool):
    if params.strategy_id == "fast_slow_ma":
        return FastSlowMAAdapter(
            fast_period=params.fast_period,
            slow_period=params.slow_period,
            ma_type=params.ma_type,
            allow_short=allow_short,
        )
    return SMARSIAdapter(
        fast_period=params.fast_period,
        slow_period=params.slow_period,
        rsi_max=params.rsi_max,
        max_bar_age_days=params.max_bar_age_days,
    )


def _close_position(position: dict[str, Any], raw_exit: Decimal, exit_time: datetime,
                    reason: str, assumptions: BacktestAssumptions,
                    ambiguous: bool = False) -> dict[str, Any]:
    slip_rate = assumptions.slippage_bps / BPS
    exit_fill = raw_exit * (UNIT - slip_rate)
    entry_fill = position["entry_fill"]
    charge_rate = (assumptions.brokerage_bps + assumptions.exchange_charges_bps) / BPS
    fees = ((entry_fill + exit_fill) * charge_rate).quantize(CENT, rounding=ROUND_HALF_EVEN)
    journal_shape = SimpleNamespace(
        side="long", entry_price=entry_fill, exit_price=exit_fill,
        quantity=UNIT, multiplier=UNIT, fees=fees,
    )
    net = net_pnl(journal_shape)
    assert net is not None
    raw_gross = raw_exit - position["entry_open"]
    fill_gross = exit_fill - entry_fill
    slippage_cost = raw_gross - fill_gross
    total_costs = raw_gross - net
    risk_amount = position["risk_per_unit"]
    r_multiple = (net / risk_amount).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    return {
        "decision_time": position["decision_time"],
        "entry_time": position["entry_time"],
        "entry_bar_timestamp": position["entry_bar_timestamp"],
        "entry_open": position["entry_open"],
        "entry_fill": entry_fill,
        "exit_time": exit_time,
        "exit_bar_timestamp": position.get("exit_bar_timestamp", exit_time),
        "exit_reason": reason,
        "exit_raw_price": raw_exit,
        "exit_fill": exit_fill,
        "quantity_units": UNIT,
        "gross_pnl": raw_gross.quantize(CENT, rounding=ROUND_HALF_EVEN),
        "slippage_cost": slippage_cost.quantize(CENT, rounding=ROUND_HALF_EVEN),
        "brokerage_and_exchange_charges": fees,
        "total_costs": total_costs.quantize(CENT, rounding=ROUND_HALF_EVEN),
        "net_pnl": net,
        "initial_risk": risk_amount,
        "r_multiple": r_multiple,
        "ambiguous_stop_target_same_bar": ambiguous,
        "bars_exposed": position["bars_exposed"],
    }


def run_backtest(
    *,
    instrument: Instrument,
    bars: list[MarketBar],
    source: str,
    params: StrategyParams,
    strategy_version: int,
    assumptions: BacktestAssumptions,
) -> dict[str, Any]:
    if any(bar.is_synthetic for bar in bars):
        raise ValueError("synthetic bars cannot be backtested")
    required = minimum_history(params)
    if len(bars) < required:
        raise InsufficientBacktestHistory(required, len(bars))
    adapter = _adapter(params, allow_short=True)
    slip_rate = assumptions.slippage_bps / BPS
    position: dict[str, Any] | None = None
    pending_entry: dict[str, Any] | None = None
    pending_exit = False
    trades: list[dict[str, Any]] = []
    ambiguous_trades = 0
    unfilled_entry_signals = 0
    exposure_bars = 0

    for index, bar in enumerate(bars):
        exposed_this_bar = position is not None
        if pending_entry is not None and position is None:
            decision_index = pending_entry["decision_index"]
            decision_bar = bars[decision_index]
            entry_time = _entry_time(instrument, bar)
            decision_close = _decision_time(decision_bar)
            if entry_time <= decision_close:
                raise ValueError("next daily session open is not after the decision bar close")
            raw_entry = Decimal(str(bar.open))
            entry_fill = raw_entry * (UNIT + slip_rate)
            atr_value = pending_entry["atr"]
            risk_per_unit = atr_value * assumptions.stop_atr_multiple
            if risk_per_unit > 0:
                stop_price = entry_fill - risk_per_unit
                target_price = entry_fill + risk_per_unit * assumptions.target_r_multiple
                position = {
                    "decision_time": decision_close,
                    "entry_time": entry_time,
                    "entry_bar_timestamp": bar.timestamp,
                    "entry_open": raw_entry,
                    "entry_fill": entry_fill,
                    "risk_per_unit": risk_per_unit,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "bars_exposed": 0,
                }
                exposed_this_bar = True
            pending_entry = None

        if position is not None:
            bar_open = Decimal(str(bar.open))
            bar_high = Decimal(str(bar.high))
            bar_low = Decimal(str(bar.low))
            stop_hit = bar_low <= position["stop_price"]
            target_hit = bar_high >= position["target_price"]
            position["bars_exposed"] += 1
            # OHLC cannot resolve a bar touching both levels; pessimistically stop first.
            if stop_hit and target_hit:
                ambiguous_trades += 1
                raw_exit = bar_open if bar_open <= position["stop_price"] else position["stop_price"]
                position["exit_bar_timestamp"] = bar.timestamp
                trades.append(_close_position(
                    position, raw_exit, _decision_time(bar),
                    "stop_first_ambiguous", assumptions, ambiguous=True,
                ))
                position = None
                pending_exit = False
            elif bar_open <= position["stop_price"]:
                position["exit_bar_timestamp"] = bar.timestamp
                trades.append(_close_position(position, bar_open, _entry_time(instrument, bar),
                                              "stop_gap", assumptions))
                position = None
                pending_exit = False
            elif bar_open >= position["target_price"]:
                position["exit_bar_timestamp"] = bar.timestamp
                trades.append(_close_position(position, bar_open, _entry_time(instrument, bar),
                                              "target_gap", assumptions))
                position = None
                pending_exit = False
            elif pending_exit:
                position["exit_bar_timestamp"] = bar.timestamp
                trades.append(_close_position(position, bar_open, _entry_time(instrument, bar),
                                              "strategy_exit", assumptions))
                position = None
                pending_exit = False
            elif stop_hit:
                position["exit_bar_timestamp"] = bar.timestamp
                trades.append(_close_position(position, position["stop_price"], _decision_time(bar),
                                              "stop", assumptions))
                position = None
            elif target_hit:
                position["exit_bar_timestamp"] = bar.timestamp
                trades.append(_close_position(position, position["target_price"], _decision_time(bar),
                                              "target", assumptions))
                position = None

        if exposed_this_bar:
            exposure_bars += 1

        # Construct a fresh causal context from this prefix only for this decision step.
        prefix = bars[:index + 1]
        context = _context(instrument, prefix, source)
        completed = context.completed_frame("1d")
        if len(completed) != len(prefix):
            raise ValueError("walk-forward prefix includes a bar that is not complete at its decision point")
        signals = adapter.evaluate(context)
        signal = signals[0] if signals else None
        if signal is None or signal.diagnostics.get("status") == "rejected":
            continue

        if signal.direction == "LONG" and position is None and pending_entry is None:
            atr_result = calculate_indicator("atr", completed, parameters={"period": 14})
            atr_value = atr_result.values[-1] if atr_result.available and atr_result.values else None
            if atr_value is not None and atr_value > 0:
                pending_entry = {"decision_index": index, "atr": atr_value}
            elif index == len(bars) - 1:
                unfilled_entry_signals += 1
        elif signal.direction == "SHORT" and position is not None:
            pending_exit = True

    if pending_entry is not None:
        unfilled_entry_signals += 1
    net_results = [trade["net_pnl"] for trade in trades]
    journal_metrics = summarize_net_pnls(net_results)
    gross_total = sum((trade["gross_pnl"] for trade in trades), Decimal("0")).quantize(CENT)
    total_costs = sum((trade["total_costs"] for trade in trades), Decimal("0")).quantize(CENT)
    average_r = (
        (sum((trade["r_multiple"] for trade in trades), Decimal("0")) / Decimal(len(trades)))
        .quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
        if trades else None
    )
    exposure_pct = (Decimal(exposure_bars) / Decimal(len(bars)) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    contract_version = adapter.specification.version
    assumptions_used = {
        "entry": "next daily session open after the completed decision bar",
        "daily_bar_close_boundary": "stored daily timestamp + 24 hours; strategy receives only the prefix completed by that close",
        "daily_session_open": "venue-local open inferred from instrument timezone (IN 09:15, US 09:30)",
        "exit": "first stop, target, or strategy down-cross; long-only exposure",
        "stop": f"ATR(14) x {assumptions.stop_atr_multiple}; ATR uses the decision-bar prefix",
        "target": f"{assumptions.target_r_multiple}R from entry based on the stop distance",
        "same_bar_stop_and_target": "stop first; OHLC does not identify intraday order",
        "brokerage_bps_per_side": assumptions.brokerage_bps,
        "exchange_charges_bps_per_side": assumptions.exchange_charges_bps,
        "slippage_bps_per_side": assumptions.slippage_bps,
        "costs_label": "assumptions, not broker-confirmed charges",
        "quantity_basis": "one analytical unit per trade; no position sizing or portfolio equity",
        "r_definition": "net P&L divided by initial ATR stop risk per one unit",
        "exposure_definition": "bars with exposure divided by completed bars in the data window",
    }
    instrument_evidence = {
        "id": str(instrument.id), "symbol": instrument.symbol,
        "venue": instrument.venue, "currency": instrument.currency,
    }
    return {
        "instrument": instrument_evidence,
        "interval": "1d",
        "source": source,
        "data_window_start": bars[0].timestamp,
        "data_window_end": bars[-1].timestamp,
        "bar_count": len(bars),
        "strategy_id": params.strategy_id,
        "strategy_version": strategy_version,
        "strategy_contract_version": contract_version,
        "parameters": params.model_dump(mode="json"),
        "assumptions": assumptions_used,
        "metrics": {
            **journal_metrics,
            "gross_pnl": gross_total,
            "total_costs": total_costs,
            "ambiguous_stop_target_trades": ambiguous_trades,
            "unfilled_entry_signals": unfilled_entry_signals,
            "unclosed_at_window_end": int(position is not None),
            "exposure_bars": exposure_bars,
            "exposure_pct": exposure_pct,
            "average_r": average_r,
        },
        "trades": trades,
        "evidence": {
            "instrument": instrument_evidence,
            "data_window_start": bars[0].timestamp.isoformat(),
            "data_window_end": bars[-1].timestamp.isoformat(),
            "source": source,
            "interval": "1d",
            "strategy_id": params.strategy_id,
            "strategy_version": strategy_version,
            "strategy_contract_version": contract_version,
            "parameters": params.model_dump(mode="json"),
            "assumptions": assumptions_used,
            "completed_bars_only": True,
            "walk_forward_prefix_built_per_step": True,
            "no_future_bars_passed_to_strategy": True,
        },
    }
