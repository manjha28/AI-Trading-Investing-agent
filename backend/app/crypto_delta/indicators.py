"""Incremental Decimal indicator series (O(n)) for the crypto backtester.

Same algorithms as app/services/indicators.py (SMA-seeded EMA, Wilder RSI/ATR) but returning a value
for every bar at once; a parity test pins the last value of each series to the shared library.
None means "not enough history yet" and is never replaced by an estimate.
"""
from decimal import Decimal, localcontext
from typing import Sequence

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def ema_series(closes: Sequence[Decimal], period: int) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    with localcontext() as context:
        context.prec = 34
        average = sum(closes[:period], ZERO) / Decimal(period)
        out[period - 1] = average
        alpha = Decimal(2) / Decimal(period + 1)
        for index in range(period, len(closes)):
            average = closes[index] * alpha + average * (Decimal(1) - alpha)
            out[index] = average
    return [+value if value is not None else None for value in out]  # same final rounding as the shared library


def rsi_series(closes: Sequence[Decimal], period: int = 14) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    changes = [right - left for left, right in zip(closes, closes[1:])]
    gains = [max(change, ZERO) for change in changes]
    losses = [max(-change, ZERO) for change in changes]
    avg_gain = sum(gains[:period], ZERO) / Decimal(period)
    avg_loss = sum(losses[:period], ZERO) / Decimal(period)

    def value() -> Decimal:
        if avg_gain == 0 and avg_loss == 0:
            return Decimal("50")
        if avg_loss == 0:
            return HUNDRED
        if avg_gain == 0:
            return ZERO
        with localcontext() as context:
            context.prec = 34
            raw = HUNDRED - HUNDRED / (Decimal(1) + avg_gain / avg_loss)
        return +raw

    out[period] = value()
    for step in range(period, len(changes)):
        avg_gain = (avg_gain * Decimal(period - 1) + gains[step]) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + losses[step]) / Decimal(period)
        out[step + 1] = value()
    return out


def atr_series(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal], period: int = 14,
) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    value = sum(true_ranges[:period], ZERO) / Decimal(period)
    out[period] = value
    for step in range(period, len(true_ranges)):
        value = (value * Decimal(period - 1) + true_ranges[step]) / Decimal(period)
        out[step + 1] = value
    return out


def highest_prior(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Highest value of the previous ``period`` bars, excluding the current bar."""
    return [max(values[i - period:i]) if i >= period else None for i in range(len(values))]


def lowest_prior(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    return [min(values[i - period:i]) if i >= period else None for i in range(len(values))]
