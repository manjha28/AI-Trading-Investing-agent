"""Decimal indicators over stored OHLCV bars.

Minimum history is counted in bars, not calendar days: SMA/EMA(n) need n;
RSI/ATR(14) need 15; realized volatility needs 21 closes; trailing drawdown
needs 20; 52-week high/low needs 253 daily bars; volume z-score needs 20;
and gap percent needs 2 bars. Insufficient values are None, never estimates.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Iterable, Protocol


ZERO = Decimal("0")
HUNDRED = Decimal("100")
TRADING_DAYS = Decimal("252")


class OHLCVBar(Protocol):
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_synthetic: bool


@dataclass(frozen=True)
class IndicatorValue:
    value: Decimal | None
    is_synthetic: bool
    bars_required: int
    bars_used: int


@dataclass(frozen=True)
class IndicatorSnapshot:
    sma_20: IndicatorValue
    sma_50: IndicatorValue
    sma_200: IndicatorValue
    ema_20: IndicatorValue
    ema_50: IndicatorValue
    ema_200: IndicatorValue
    rsi_14: IndicatorValue
    atr_14: IndicatorValue
    realized_volatility_20d: IndicatorValue
    drawdown_from_20d_peak: IndicatorValue
    distance_to_52w_high: IndicatorValue
    distance_to_52w_low: IndicatorValue
    volume_z_score_20d: IndicatorValue
    gap_percent: IndicatorValue


def _result(value: Decimal | None, bars: list[OHLCVBar], required: int) -> IndicatorValue:
    return IndicatorValue(
        value=value,
        is_synthetic=any(bar.is_synthetic for bar in bars),
        bars_required=required,
        bars_used=len(bars),
    )


def sma(bars: Iterable[OHLCVBar], period: int) -> IndicatorValue:
    values = list(bars)
    if period <= 0:
        raise ValueError("period must be positive")
    window = values[-period:]
    if len(window) < period:
        return _result(None, window, period)
    return _result(sum((bar.close for bar in window), ZERO) / Decimal(period), window, period)


def ema(bars: Iterable[OHLCVBar], period: int) -> IndicatorValue:
    values = list(bars)
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return _result(None, values, period)
    with localcontext() as context:
        context.prec = 34
        average = sum((bar.close for bar in values[:period]), ZERO) / Decimal(period)
        alpha = Decimal(2) / Decimal(period + 1)
        for bar in values[period:]:
            average = bar.close * alpha + average * (Decimal(1) - alpha)
    return _result(+average, values, period)


def rsi(bars: Iterable[OHLCVBar], period: int = 14) -> IndicatorValue:
    values = list(bars)
    required = period + 1
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < required:
        return _result(None, values, required)
    changes = [right.close - left.close for left, right in zip(values, values[1:])]
    gains = [max(change, ZERO) for change in changes]
    losses = [max(-change, ZERO) for change in changes]
    avg_gain = sum(gains[:period], ZERO) / Decimal(period)
    avg_loss = sum(losses[:period], ZERO) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
    if avg_gain == 0 and avg_loss == 0:
        result = Decimal("50")
    elif avg_loss == 0:
        result = HUNDRED
    elif avg_gain == 0:
        result = ZERO
    else:
        with localcontext() as context:
            context.prec = 34
            result = HUNDRED - HUNDRED / (Decimal(1) + avg_gain / avg_loss)
    return _result(+result, values, required)


def atr(bars: Iterable[OHLCVBar], period: int = 14) -> IndicatorValue:
    values = list(bars)
    required = period + 1
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < required:
        return _result(None, values, required)
    true_ranges = []
    for previous, current in zip(values, values[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    atr_value = sum(true_ranges[:period], ZERO) / Decimal(period)
    for true_range in true_ranges[period:]:
        atr_value = (atr_value * Decimal(period - 1) + true_range) / Decimal(period)
    return _result(atr_value, values, required)


def realized_volatility_20d(bars: Iterable[OHLCVBar]) -> IndicatorValue:
    values = list(bars)
    required = 21
    window = values[-required:]
    if len(window) < required:
        return _result(None, window, required)
    returns = [
        (right.close - left.close) / left.close
        for left, right in zip(window, window[1:])
        if left.close > 0
    ]
    if len(returns) != 20:
        return _result(None, window, required)
    mean = sum(returns, ZERO) / Decimal(20)
    variance = sum(((item - mean) ** 2 for item in returns), ZERO) / Decimal(20)
    with localcontext() as context:
        context.prec = 34
        value = (variance.sqrt() * TRADING_DAYS.sqrt()) * HUNDRED
    return _result(+value, window, required)


def drawdown_from_trailing_peak(bars: Iterable[OHLCVBar], period: int = 20) -> IndicatorValue:
    """Return the positive percent decline from the trailing peak close."""
    values = list(bars)
    window = values[-period:]
    if period <= 0:
        raise ValueError("period must be positive")
    if len(window) < period:
        return _result(None, window, period)
    peak = max(bar.close for bar in window)
    if peak <= 0:
        return _result(None, window, period)
    value = (peak - window[-1].close) * HUNDRED / peak
    return _result(value, window, period)


def pct_change(bars: Iterable[OHLCVBar], period: int) -> IndicatorValue:
    values = list(bars)
    if period <= 0:
        raise ValueError("period must be positive")
    required = period + 1
    window = values[-required:]
    if len(window) < required or window[0].close <= 0:
        return _result(None, window, required)
    value = (window[-1].close - window[0].close) * HUNDRED / window[0].close
    return _result(value, window, required)


def distance_to_52w_high(bars: Iterable[OHLCVBar]) -> IndicatorValue:
    values = list(bars)
    required = 253
    window = values[-required:]
    if len(window) < required:
        return _result(None, window, required)
    high = max(bar.high for bar in window)
    return _result((window[-1].close - high) * HUNDRED / high, window, required)


def distance_to_52w_low(bars: Iterable[OHLCVBar]) -> IndicatorValue:
    values = list(bars)
    required = 253
    window = values[-required:]
    if len(window) < required:
        return _result(None, window, required)
    low = min(bar.low for bar in window)
    return _result((window[-1].close - low) * HUNDRED / low, window, required)


def volume_z_score_20d(bars: Iterable[OHLCVBar]) -> IndicatorValue:
    values = list(bars)
    required = 20
    window = values[-required:]
    if len(window) < required:
        return _result(None, window, required)
    mean = sum((bar.volume for bar in window), ZERO) / Decimal(required)
    variance = sum(((bar.volume - mean) ** 2 for bar in window), ZERO) / Decimal(required)
    if variance == 0:
        return _result(None, window, required)
    with localcontext() as context:
        context.prec = 34
        value = (window[-1].volume - mean) / variance.sqrt()
    return _result(+value, window, required)


def gap_percent(bars: Iterable[OHLCVBar]) -> IndicatorValue:
    values = list(bars)
    required = 2
    window = values[-required:]
    if len(window) < required or window[-2].close <= 0:
        return _result(None, window, required)
    return _result((window[-1].open - window[-2].close) * HUNDRED / window[-2].close, window, required)


def compute_indicators(bars: Iterable[OHLCVBar]) -> IndicatorSnapshot:
    values = list(bars)
    return IndicatorSnapshot(
        sma_20=sma(values, 20),
        sma_50=sma(values, 50),
        sma_200=sma(values, 200),
        ema_20=ema(values, 20),
        ema_50=ema(values, 50),
        ema_200=ema(values, 200),
        rsi_14=rsi(values, 14),
        atr_14=atr(values, 14),
        realized_volatility_20d=realized_volatility_20d(values),
        drawdown_from_20d_peak=drawdown_from_trailing_peak(values, 20),
        distance_to_52w_high=distance_to_52w_high(values),
        distance_to_52w_low=distance_to_52w_low(values),
        volume_z_score_20d=volume_z_score_20d(values),
        gap_percent=gap_percent(values),
    )
