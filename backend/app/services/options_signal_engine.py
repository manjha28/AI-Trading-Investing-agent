"""Deterministic, non-executing intraday long-options signal engine.

This module emits decision-support signals only. It never creates, simulates, or
submits orders. Signals are intentionally suppressed when quotes, history, or
option Greeks fail quality checks.

Expected inputs: recent, closed 5-minute OHLCV bars for one exchange session;
a coherent live option-chain snapshot for one underlying and expiry; quote
source timestamps; and a known lot size when sizing is requested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from statistics import median
from typing import Literal, Sequence
from zoneinfo import ZoneInfo

D = Decimal
ZERO = D("0")
ONE = D("1")
HUNDRED = D("100")

OptionType = Literal["call", "put"]
SignalAction = Literal["BUY_CALL", "BUY_PUT", "EXIT_CALL", "EXIT_PUT", "NO_TRADE"]


@dataclass(frozen=True)
class OHLCVBar:
    """A completed OHLCV bar; timestamp is the bar close time."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_synthetic: bool = False


@dataclass(frozen=True)
class OptionQuote:
    """One option contract's quote and Greeks from a single chain snapshot.

    Greeks are per underlying unit. Theta is assumed to be premium points lost
    per calendar day; if a provider uses another unit, normalize before calling.
    """

    strike: Decimal
    option_type: OptionType
    expiry_at: datetime
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    ltp: Decimal
    volume: Decimal
    open_interest: Decimal
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    implied_volatility: Decimal | None = None
    volume_change: Decimal | None = None
    oi_change: Decimal | None = None
    lot_size: int | None = None


@dataclass(frozen=True)
class ActiveOptionPosition:
    option_type: OptionType
    strike: Decimal
    expiry_at: datetime
    entry_premium: Decimal


@dataclass(frozen=True)
class OptionsSignalInput:
    underlying_symbol: str
    chain_underlying_symbol: str
    as_of: datetime
    spot: Decimal
    spot_timestamp: datetime
    spot_source: str
    chain_timestamp: datetime
    chain_source: str
    chain_spot: Decimal
    expiry_at: datetime
    bars: Sequence[OHLCVBar]
    chain: Sequence[OptionQuote]
    exchange_timezone: str = "Asia/Kolkata"
    iv_rank_percentile: Decimal | None = None
    capital: Decimal | None = None
    risk_fraction: Decimal = D("0.005")
    active_position: ActiveOptionPosition | None = None


@dataclass(frozen=True)
class SignalConfig:
    min_bars: int = 25
    ema_fast: int = 9
    ema_slow: int = 21
    breakout_lookback: int = 10
    volume_lookback: int = 20
    min_volume_multiple: Decimal = D("1.20")
    breakout_atr_buffer: Decimal = D("0.05")
    min_direction_score: int = 5
    min_score_edge: int = 2
    max_spot_quote_age: timedelta = timedelta(seconds=20)
    max_chain_quote_age: timedelta = timedelta(seconds=20)
    max_bar_age: timedelta = timedelta(minutes=10)
    max_chain_spot_difference_pct: Decimal = D("0.20")
    max_bar_spot_difference_pct: Decimal = D("0.75")
    max_relative_spread_pct: Decimal = D("6.0")
    min_abs_delta: Decimal = D("0.40")
    max_abs_delta: Decimal = D("0.65")
    target_abs_delta: Decimal = D("0.55")
    max_daily_theta_to_premium_pct: Decimal = D("20.0")
    max_iv_rank_percentile: Decimal = D("80")
    min_minutes_to_expiry: Decimal = D("30")
    stop_loss_pct: Decimal = D("25")
    take_profit_pct: Decimal = D("50")


@dataclass(frozen=True)
class OptionsSignalResult:
    action: SignalAction
    underlying_symbol: str
    as_of: datetime
    direction_score: int
    opposing_score: int
    selected_option_type: OptionType | None = None
    strike: Decimal | None = None
    reference_entry_premium: Decimal | None = None
    estimated_premium_risk_per_lot: Decimal | None = None
    suggested_lots_by_risk_budget: int | None = None
    option_return_pct_at_bid: Decimal | None = None
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    evidence: dict[str, str | int | None] = field(default_factory=dict)
    underlying_source: str | None = None
    chain_source: str | None = None
    disclaimer: str = "Decision support, not investment advice. No order was placed."


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _age_seconds(now: datetime, observed_at: datetime) -> Decimal:
    return D(str((now - observed_at).total_seconds()))


def _ema(values: Sequence[Decimal], period: int) -> Decimal:
    seed = sum(values[:period], ZERO) / D(period)
    alpha = D(2) / D(period + 1)
    value = seed
    for item in values[period:]:
        value = item * alpha + value * (ONE - alpha)
    return value


def _rsi(values: Sequence[Decimal], period: int = 14) -> Decimal | None:
    if len(values) < period + 1:
        return None
    changes = [right - left for left, right in zip(values, values[1:])]
    gains = [max(change, ZERO) for change in changes]
    losses = [max(-change, ZERO) for change in changes]
    avg_gain = sum(gains[:period], ZERO) / D(period)
    avg_loss = sum(losses[:period], ZERO) / D(period)
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * D(period - 1) + gain) / D(period)
        avg_loss = (avg_loss * D(period - 1) + loss) / D(period)
    if avg_gain == ZERO and avg_loss == ZERO:
        return D("50")
    if avg_loss == ZERO:
        return HUNDRED
    if avg_gain == ZERO:
        return ZERO
    return HUNDRED - HUNDRED / (ONE + avg_gain / avg_loss)


def _atr(bars: Sequence[OHLCVBar], period: int = 14) -> Decimal | None:
    if len(bars) < period + 1:
        return None
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(bars, bars[1:])
    ]
    value = sum(ranges[:period], ZERO) / D(period)
    for item in ranges[period:]:
        value = (value * D(period - 1) + item) / D(period)
    return value


def _session_vwap(
    bars: Sequence[OHLCVBar], now: datetime, exchange_timezone: str
) -> Decimal | None:
    zone = ZoneInfo(exchange_timezone)
    local_date = now.astimezone(zone).date()
    session_bars = [bar for bar in bars if bar.timestamp.astimezone(zone).date() == local_date]
    total_volume = sum((bar.volume for bar in session_bars), ZERO)
    if total_volume <= ZERO or any(bar.volume < ZERO for bar in session_bars):
        return None
    weighted = sum(
        ((bar.high + bar.low + bar.close) / D(3)) * bar.volume
        for bar in session_bars
    )
    return weighted / total_volume


def _freshness_blockers(data: OptionsSignalInput, config: SignalConfig) -> list[str]:
    now = data.as_of
    blockers: list[str] = []
    dates = [now, data.spot_timestamp, data.chain_timestamp, data.expiry_at]
    dates.extend(bar.timestamp for bar in data.bars)
    dates.extend(quote.expiry_at for quote in data.chain)
    if any(not _aware(value) for value in dates):
        return ["timestamps_must_be_timezone_aware"]
    if not data.underlying_symbol.strip() or not data.chain_underlying_symbol.strip():
        blockers.append("missing_underlying_symbol")
    elif data.underlying_symbol.strip().upper() != data.chain_underlying_symbol.strip().upper():
        blockers.append("underlying_symbol_mismatch")
    if not data.spot_source or not data.chain_source:
        blockers.append("missing_data_source_provenance")
    if data.spot <= ZERO or data.chain_spot <= ZERO:
        blockers.append("invalid_spot_price")
    if data.spot_timestamp > now or data.chain_timestamp > now:
        blockers.append("market_timestamp_in_future")
    else:
        if _age_seconds(now, data.spot_timestamp) > D(str(config.max_spot_quote_age.total_seconds())):
            blockers.append("stale_underlying_quote")
        if _age_seconds(now, data.chain_timestamp) > D(str(config.max_chain_quote_age.total_seconds())):
            blockers.append("stale_option_chain")
    if data.spot > ZERO and data.chain_spot > ZERO:
        chain_diff = abs(data.spot - data.chain_spot) * HUNDRED / data.spot
        if chain_diff > config.max_chain_spot_difference_pct:
            blockers.append("chain_spot_mismatch")
    if len(data.bars) < config.min_bars:
        blockers.append("insufficient_5m_history")
    elif any(bar.is_synthetic for bar in data.bars[-config.min_bars :]):
        blockers.append("synthetic_ohlcv_history")
    else:
        ordered = list(data.bars)
        if any(left.timestamp >= right.timestamp for left, right in zip(ordered, ordered[1:])):
            blockers.append("bars_not_strictly_chronological")
        elif any(
            bar.close <= ZERO or bar.low <= ZERO or bar.high < bar.low
            or not bar.low <= bar.open <= bar.high
            or not bar.low <= bar.close <= bar.high
            or bar.volume < ZERO
            for bar in ordered[-config.min_bars :]
        ):
            blockers.append("invalid_ohlcv_bar")
        else:
            latest = ordered[-1]
            if latest.timestamp > now:
                blockers.append("bar_timestamp_in_future")
            elif _age_seconds(now, latest.timestamp) > D(str(config.max_bar_age.total_seconds())):
                blockers.append("stale_ohlcv_history")
            if data.spot > ZERO:
                diff = abs(data.spot - latest.close) * HUNDRED / data.spot
                if diff > config.max_bar_spot_difference_pct:
                    blockers.append("spot_bar_price_mismatch")
    if data.expiry_at <= now + timedelta(minutes=float(config.min_minutes_to_expiry)):
        blockers.append("too_close_to_expiry")
    if data.risk_fraction <= ZERO or data.risk_fraction > D("0.02"):
        blockers.append("risk_fraction_must_be_between_0_and_2_percent")
    if data.capital is not None and data.capital <= ZERO:
        blockers.append("invalid_capital")
    if data.iv_rank_percentile is not None and not ZERO <= data.iv_rank_percentile <= HUNDRED:
        blockers.append("invalid_iv_rank_percentile")
    return blockers


def _option_blockers(quote: OptionQuote, data: OptionsSignalInput, config: SignalConfig) -> list[str]:
    reasons: list[str] = []
    if quote.expiry_at != data.expiry_at:
        reasons.append("expiry_mismatch")
    if quote.bid <= ZERO or quote.ask <= ZERO or quote.ask < quote.bid:
        reasons.append("invalid_two_sided_quote")
    if quote.bid_qty <= ZERO or quote.ask_qty <= ZERO:
        reasons.append("missing_bid_or_ask_depth")
    if quote.volume <= ZERO or quote.open_interest <= ZERO:
        reasons.append("no_volume_or_open_interest")
    if quote.delta is None or not config.min_abs_delta <= abs(quote.delta) <= config.max_abs_delta:
        reasons.append("delta_outside_target_range_or_missing")
    elif (quote.option_type == "call" and quote.delta <= ZERO) or (quote.option_type == "put" and quote.delta >= ZERO):
        reasons.append("delta_sign_inconsistent_with_option_type")
    if (
        quote.gamma is None or quote.gamma <= ZERO
        or quote.theta is None or quote.theta >= ZERO
        or quote.vega is None or quote.vega <= ZERO
    ):
        reasons.append("missing_or_invalid_greeks")
    if quote.bid > ZERO and quote.ask >= quote.bid:
        mid = (quote.bid + quote.ask) / D(2)
        if mid <= ZERO or (quote.ask - quote.bid) * HUNDRED / mid > config.max_relative_spread_pct:
            reasons.append("spread_too_wide")
    if quote.ask > ZERO and quote.theta is not None and quote.theta < ZERO:
        theta_ratio = abs(quote.theta) * HUNDRED / quote.ask
        if theta_ratio > config.max_daily_theta_to_premium_pct:
            reasons.append("theta_decay_too_high_for_premium")
    if data.iv_rank_percentile is not None and data.iv_rank_percentile > config.max_iv_rank_percentile:
        reasons.append("implied_volatility_rank_too_high_for_buying")
    if quote.lot_size is not None and quote.lot_size <= 0:
        reasons.append("invalid_lot_size")
    return reasons


def _score_direction(
    bars: Sequence[OHLCVBar], data: OptionsSignalInput, config: SignalConfig
) -> tuple[int, int, dict[str, str | int | None], tuple[str, ...]]:
    closes = [bar.close for bar in bars]
    fast = _ema(closes, config.ema_fast)
    slow = _ema(closes, config.ema_slow)
    current_rsi = _rsi(closes)
    atr = _atr(bars)
    vwap = _session_vwap(bars, data.as_of, data.exchange_timezone)
    prior_volume = [bar.volume for bar in bars[-config.volume_lookback - 1 : -1]]
    vol_median = D(str(median(prior_volume))) if prior_volume else ZERO
    volume_multiple = bars[-1].volume / vol_median if vol_median > ZERO else None
    prior = bars[-config.breakout_lookback - 1 : -1]
    prior_high = max(bar.high for bar in prior) if len(prior) == config.breakout_lookback else None
    prior_low = min(bar.low for bar in prior) if len(prior) == config.breakout_lookback else None
    close = closes[-1]
    buffer = (atr or ZERO) * config.breakout_atr_buffer
    bullish_breakout = prior_high is not None and close > prior_high + buffer
    bearish_breakout = prior_low is not None and close < prior_low - buffer
    high_volume = volume_multiple is not None and volume_multiple >= config.min_volume_multiple

    bull = 0
    bear = 0
    reasons: list[str] = []
    if fast > slow:
        bull += 2
        reasons.append("EMA9_above_EMA21")
    elif fast < slow:
        bear += 2
        reasons.append("EMA9_below_EMA21")
    if close > slow:
        bull += 1
        reasons.append("close_above_EMA21")
    elif close < slow:
        bear += 1
        reasons.append("close_below_EMA21")
    if vwap is not None and close > vwap:
        bull += 1
        reasons.append("close_above_session_VWAP")
    elif vwap is not None and close < vwap:
        bear += 1
        reasons.append("close_below_session_VWAP")
    if bullish_breakout:
        bull += 2
        reasons.append("close_breakout_above_10_bar_high")
    if bearish_breakout:
        bear += 2
        reasons.append("close_breakout_below_10_bar_low")
    if current_rsi is not None and D("52") <= current_rsi <= D("70"):
        bull += 1
        reasons.append("RSI_bullish_momentum_band")
    elif current_rsi is not None and D("30") <= current_rsi <= D("48"):
        bear += 1
        reasons.append("RSI_bearish_momentum_band")
    if high_volume:
        if bullish_breakout:
            bull += 1
            reasons.append("breakout_volume_confirmation")
        elif bearish_breakout:
            bear += 1
            reasons.append("breakdown_volume_confirmation")

    evidence: dict[str, str | int | None] = {
        "close": str(close),
        "ema_fast": str(fast),
        "ema_slow": str(slow),
        "rsi_14": str(current_rsi) if current_rsi is not None else None,
        "atr_14": str(atr) if atr is not None else None,
        "session_vwap": str(vwap) if vwap is not None else None,
        "volume_multiple_vs_prior_median": str(volume_multiple) if volume_multiple is not None else None,
        "prior_10_bar_high": str(prior_high) if prior_high is not None else None,
        "prior_10_bar_low": str(prior_low) if prior_low is not None else None,
        "bullish_breakout": int(bullish_breakout),
        "bearish_breakout": int(bearish_breakout),
        "high_volume": int(high_volume),
        "bullish_score": bull,
        "bearish_score": bear,
    }
    return bull, bear, evidence, tuple(reasons)


def _candidate_options(
    data: OptionsSignalInput, option_type: OptionType, config: SignalConfig
) -> list[tuple[Decimal, OptionQuote]]:
    candidates: list[tuple[Decimal, OptionQuote]] = []
    for quote in data.chain:
        if quote.option_type != option_type:
            continue
        if _option_blockers(quote, data, config):
            continue
        assert quote.delta is not None and quote.theta is not None
        mid = (quote.bid + quote.ask) / D(2)
        spread_pct = (quote.ask - quote.bid) * HUNDRED / mid
        theta_pct = abs(quote.theta) * HUNDRED / quote.ask
        delta_distance = abs(abs(quote.delta) - config.target_abs_delta)
        # Lower is better. Delta fit, spread, and theta drag are deliberately
        # explicit; OI and volume are used as minimum quality gates, not direction.
        rank = delta_distance * D(2) + spread_pct / HUNDRED + theta_pct / HUNDRED * D(2)
        candidates.append((rank, quote))
    return sorted(candidates, key=lambda item: item[0])


def evaluate_options_signal(
    data: OptionsSignalInput, config: SignalConfig = SignalConfig()
) -> OptionsSignalResult:
    """Evaluate a live options snapshot and return a conservative signal.

    The engine is deterministic and offline: callers must fetch/normalize live
    prices first. It never guesses missing values or calls broker APIs.
    """
    base = dict(
        underlying_symbol=data.underlying_symbol,
        as_of=data.as_of,
        underlying_source=data.spot_source or None,
        chain_source=data.chain_source or None,
    )
    blockers = _freshness_blockers(data, config)
    if blockers:
        return OptionsSignalResult(
            action="NO_TRADE", direction_score=0, opposing_score=0,
            reasons=("Signal suppressed by data-quality/risk gates.",), blockers=tuple(blockers), **base
        )

    bull, bear, evidence, trend_reasons = _score_direction(data.bars, data, config)
    evidence.update({
        "underlying_spot": str(data.spot),
        "spot_age_seconds": str(_age_seconds(data.as_of, data.spot_timestamp)),
        "chain_age_seconds": str(_age_seconds(data.as_of, data.chain_timestamp)),
        "expiry_minutes_remaining": str(D(str((data.expiry_at - data.as_of).total_seconds())) / D(60)),
        "iv_rank_percentile": str(data.iv_rank_percentile) if data.iv_rank_percentile is not None else None,
    })

    active = data.active_position
    if active is not None:
        held = next((q for q in data.chain if q.option_type == active.option_type and q.strike == active.strike and q.expiry_at == active.expiry_at), None)
        if held is None or held.bid <= ZERO or held.ask < held.bid:
            return OptionsSignalResult(
                action="NO_TRADE", direction_score=max(bull, bear), opposing_score=min(bull, bear),
                reasons=("Cannot safely evaluate exit without a valid quote for the held contract.",),
                blockers=("held_contract_quote_unavailable",), evidence=evidence, **base
            )
        if active.entry_premium <= ZERO:
            return OptionsSignalResult(
                action="NO_TRADE", direction_score=max(bull, bear), opposing_score=min(bull, bear),
                reasons=("Active position has an invalid entry premium.",),
                blockers=("invalid_position_entry_premium",), evidence=evidence, **base
            )
        change_pct = (held.bid - active.entry_premium) * HUNDRED / active.entry_premium
        exit_reason: str | None = None
        if change_pct <= -config.stop_loss_pct:
            exit_reason = "premium_stop_loss_reached_at_bid"
        elif change_pct >= config.take_profit_pct:
            exit_reason = "premium_take_profit_reached_at_bid"
        elif active.option_type == "call" and bear >= config.min_direction_score and bear - bull >= config.min_score_edge:
            exit_reason = "underlying_bearish_signal_reversed"
        elif active.option_type == "put" and bull >= config.min_direction_score and bull - bear >= config.min_score_edge:
            exit_reason = "underlying_bullish_signal_reversed"
        if exit_reason:
            action: SignalAction = "EXIT_CALL" if active.option_type == "call" else "EXIT_PUT"
            return OptionsSignalResult(
                action=action, direction_score=max(bull, bear), opposing_score=min(bull, bear),
                selected_option_type=active.option_type, strike=active.strike,
                reference_entry_premium=held.bid, option_return_pct_at_bid=change_pct,
                reasons=(exit_reason,), evidence=evidence, **base
            )
        return OptionsSignalResult(
            action="NO_TRADE", direction_score=max(bull, bear), opposing_score=min(bull, bear),
            selected_option_type=active.option_type, strike=active.strike,
            reference_entry_premium=held.bid, option_return_pct_at_bid=change_pct,
            reasons=("Active position remains open; no stop, target, or reversal exit condition is met.",),
            blockers=("active_position_no_exit_triggered",), evidence=evidence, **base
        )

    if data.iv_rank_percentile is not None and data.iv_rank_percentile > config.max_iv_rank_percentile:
        return OptionsSignalResult(
            action="NO_TRADE", direction_score=max(bull, bear), opposing_score=min(bull, bear),
            reasons=("IV rank is too high for a long-premium setup.",),
            blockers=("implied_volatility_rank_too_high_for_buying",), evidence=evidence, **base
        )

    direction: OptionType | None = None
    score = max(bull, bear)
    opposing = min(bull, bear)
    if bull >= config.min_direction_score and bull - bear >= config.min_score_edge and evidence["bullish_breakout"] == 1 and evidence["high_volume"] == 1:
        direction = "call"
    elif bear >= config.min_direction_score and bear - bull >= config.min_score_edge and evidence["bearish_breakout"] == 1 and evidence["high_volume"] == 1:
        direction = "put"
    if direction is None:
        return OptionsSignalResult(
            action="NO_TRADE", direction_score=score, opposing_score=opposing,
            reasons=("No confirmed, volume-backed directional breakout; do not chase an unconfirmed move.",) + trend_reasons,
            blockers=("directional_setup_not_confirmed",), evidence=evidence, **base
        )

    candidates = _candidate_options(data, direction, config)
    if not candidates:
        rejected = [q for q in data.chain if q.option_type == direction]
        blocker_counts: dict[str, int] = {}
        for quote in rejected:
            for reason in _option_blockers(quote, data, config):
                blocker_counts[reason] = blocker_counts.get(reason, 0) + 1
        summary = tuple(sorted(blocker_counts, key=lambda key: (-blocker_counts[key], key))) or ("no_options_for_direction",)
        return OptionsSignalResult(
            action="NO_TRADE", direction_score=score, opposing_score=opposing,
            reasons=(f"Underlying breakout confirms {direction}, but no option passes liquidity/Greek/theta filters.",) + trend_reasons,
            blockers=summary, evidence=evidence, **base
        )

    _, chosen = candidates[0]
    if chosen.lot_size is None:
        evidence["lot_size_status"] = "unavailable; premium risk per lot not calculated"
    risk_per_lot = chosen.ask * D(chosen.lot_size) if chosen.lot_size else None
    lots: int | None = None
    if data.capital is not None and risk_per_lot is not None and risk_per_lot > ZERO:
        risk_budget = data.capital * data.risk_fraction
        lots = int((risk_budget / risk_per_lot).to_integral_value(rounding=ROUND_FLOOR))
        if lots < 1:
            evidence["sizing_status"] = "risk budget smaller than one lot; signal downgraded"
            return OptionsSignalResult(
                action="NO_TRADE", direction_score=score, opposing_score=opposing,
                selected_option_type=direction, strike=chosen.strike,
                reference_entry_premium=chosen.ask, estimated_premium_risk_per_lot=risk_per_lot,
                suggested_lots_by_risk_budget=0,
                reasons=("Signal quality passed, but one lot exceeds the configured premium-risk budget.",) + trend_reasons,
                blockers=("risk_budget_below_one_lot",), evidence=evidence, **base
            )
    action = "BUY_CALL" if direction == "call" else "BUY_PUT"
    evidence.update({
        "selected_delta": str(chosen.delta) if chosen.delta is not None else None,
        "selected_gamma": str(chosen.gamma) if chosen.gamma is not None else None,
        "selected_theta": str(chosen.theta) if chosen.theta is not None else None,
        "selected_vega": str(chosen.vega) if chosen.vega is not None else None,
        "selected_relative_spread_pct": str((chosen.ask - chosen.bid) * HUNDRED / ((chosen.ask + chosen.bid) / D(2))),
        "selected_open_interest": str(chosen.open_interest),
        "selected_volume": str(chosen.volume),
        "selected_lot_size": chosen.lot_size,
        "estimated_premium_risk_per_lot": str(risk_per_lot) if risk_per_lot is not None else None,
        "risk_fraction": str(data.risk_fraction),
    })
    return OptionsSignalResult(
        action=action, direction_score=score, opposing_score=opposing,
        selected_option_type=direction, strike=chosen.strike,
        reference_entry_premium=chosen.ask, estimated_premium_risk_per_lot=risk_per_lot,
        suggested_lots_by_risk_budget=lots,
        reasons=("Confirmed breakout with volume; option passed quote, spread, Greek, delta and theta filters.",) + trend_reasons,
        evidence=evidence, **base
    )
