"""Built-in crypto strategies. Each maps completed candles to a TARGET position per bar close.

target[i] in {-1, 0, +1} is decided using bars 0..i only; the engine executes any change at the open
of bar i+1 (no look-ahead). Levels and signals are computed deterministically in Python.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from app.crypto_delta.candles import Candle
from app.crypto_delta.indicators import ema_series, highest_prior, lowest_prior, rsi_series


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    kind: str  # int | decimal | bool
    default: Any
    minimum: Any = None
    maximum: Any = None


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    description: str
    params: list[ParamSpec]
    targets: Callable[[list[Candle], dict[str, Any]], list[int]]
    warmup: Callable[[dict[str, Any]], int] = field(default=lambda p: 0)


RISK_PARAMS = [
    ParamSpec("allow_short", "Allow short positions", "bool", True),
    ParamSpec("stop_atr_mult", "Stop distance (× ATR, 0 = none)", "decimal", Decimal("2"), Decimal("0"), Decimal("20")),
    ParamSpec("take_profit_r", "Take profit (× stop distance, 0 = none)", "decimal", Decimal("0"), Decimal("0"), Decimal("20")),
    ParamSpec("atr_period", "ATR period", "int", 14, 2, 100),
]


def _ema_cross(candles: list[Candle], p: dict[str, Any]) -> list[int]:
    closes = [c.close for c in candles]
    fast, slow = ema_series(closes, p["fast_period"]), ema_series(closes, p["slow_period"])
    out = []
    for f, s in zip(fast, slow):
        if f is None or s is None:
            out.append(0)
        elif f > s:
            out.append(1)
        elif f < s:
            out.append(-1 if p["allow_short"] else 0)
        else:
            out.append(0)
    return out


def _rsi_reversion(candles: list[Candle], p: dict[str, Any]) -> list[int]:
    rsi = rsi_series([c.close for c in candles], p["rsi_period"])
    out, state = [], 0
    for value in rsi:
        if value is None:
            out.append(0)
            continue
        if state == 0:
            if value <= p["oversold"]:
                state = 1
            elif value >= p["overbought"] and p["allow_short"]:
                state = -1
        elif state == 1 and value >= p["exit_level"]:
            state = 0
        elif state == -1 and value <= p["exit_level"]:
            state = 0
        out.append(state)
    return out


def _donchian(candles: list[Candle], p: dict[str, Any]) -> list[int]:
    closes = [c.close for c in candles]
    entry_high = highest_prior([c.high for c in candles], p["entry_period"])
    entry_low = lowest_prior([c.low for c in candles], p["entry_period"])
    exit_low = lowest_prior([c.low for c in candles], p["exit_period"])
    exit_high = highest_prior([c.high for c in candles], p["exit_period"])
    out, state = [], 0
    for i, close in enumerate(closes):
        if entry_high[i] is None or entry_low[i] is None:
            out.append(0)
            continue
        if state == 0:
            if close > entry_high[i]:
                state = 1
            elif close < entry_low[i] and p["allow_short"]:
                state = -1
        elif state == 1 and exit_low[i] is not None and close < exit_low[i]:
            state = 0
        elif state == -1 and exit_high[i] is not None and close > exit_high[i]:
            state = 0
        out.append(state)
    return out


STRATEGIES: dict[str, Strategy] = {
    "ema_cross": Strategy(
        "ema_cross", "EMA crossover (trend)",
        "Long while the fast EMA is above the slow EMA, short while below. Decided at bar close.",
        [ParamSpec("fast_period", "Fast EMA", "int", 12, 2, 200), ParamSpec("slow_period", "Slow EMA", "int", 48, 3, 400), *RISK_PARAMS],
        _ema_cross, lambda p: p["slow_period"],
    ),
    "rsi_reversion": Strategy(
        "rsi_reversion", "RSI mean reversion",
        "Buy when RSI is oversold and hold until it recovers to the exit level; mirror for shorts.",
        [ParamSpec("rsi_period", "RSI period", "int", 14, 2, 100), ParamSpec("oversold", "Oversold at or below", "decimal", Decimal("30"), Decimal("1"), Decimal("49")),
         ParamSpec("overbought", "Overbought at or above", "decimal", Decimal("70"), Decimal("51"), Decimal("99")),
         ParamSpec("exit_level", "Exit at RSI", "decimal", Decimal("50"), Decimal("10"), Decimal("90")), *RISK_PARAMS],
        _rsi_reversion, lambda p: p["rsi_period"] + 1,
    ),
    "donchian_breakout": Strategy(
        "donchian_breakout", "Donchian channel breakout",
        "Long when the close breaks the prior N-bar high, short on a break of the prior N-bar low; exit on the shorter channel.",
        [ParamSpec("entry_period", "Entry channel (bars)", "int", 40, 5, 400), ParamSpec("exit_period", "Exit channel (bars)", "int", 20, 3, 200), *RISK_PARAMS],
        _donchian, lambda p: p["entry_period"],
    ),
}


class ParamError(ValueError):
    pass


def resolve_params(strategy: Strategy, supplied: dict[str, Any]) -> dict[str, Any]:
    """Defaults + validated overrides. Unknown keys and out-of-range values are rejected."""
    known = {spec.key: spec for spec in strategy.params}
    unknown = set(supplied) - set(known)
    if unknown:
        raise ParamError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    resolved: dict[str, Any] = {}
    for key, spec in known.items():
        value = supplied.get(key, spec.default)
        try:
            if spec.kind == "bool":
                if not isinstance(value, bool):
                    raise ValueError
            elif spec.kind == "int":
                if isinstance(value, (bool, float)) or int(value) != value:
                    raise ValueError
                value = int(value)
            else:
                if isinstance(value, float):
                    raise ValueError  # numbers arrive as strings or ints, never floats
                value = Decimal(str(value))
                if not value.is_finite():
                    raise ValueError
        except (ValueError, TypeError, ArithmeticError):
            raise ParamError(f"{spec.label} is not a valid {spec.kind}") from None
        if spec.minimum is not None and value < spec.minimum or spec.maximum is not None and value > spec.maximum:
            raise ParamError(f"{spec.label} must be between {spec.minimum} and {spec.maximum}")
        resolved[key] = value
    if strategy.id == "ema_cross" and resolved["fast_period"] >= resolved["slow_period"]:
        raise ParamError("Fast EMA must be shorter than the slow EMA")
    if strategy.id == "rsi_reversion" and not resolved["oversold"] < resolved["exit_level"] < resolved["overbought"]:
        raise ParamError("Require oversold < exit level < overbought")
    return resolved
