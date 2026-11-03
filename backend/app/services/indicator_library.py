"""Pure indicator calculations ported from the frozen del_trading_bot reference."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

import numpy as np
import pandas as pd


Category = Literal["trend", "momentum", "volatility", "volume"]


@dataclass(frozen=True)
class Parameter:
    name: str
    type: str
    default: int | float
    bounds: tuple[int | float, int | float]


@dataclass(frozen=True)
class IndicatorDefinition:
    id: str
    display_name: str
    category: Category
    parameters: tuple[Parameter, ...]
    minimum_bars: int
    output_series_name: str
    monetary: bool


@dataclass(frozen=True)
class IndicatorResult:
    indicator_id: str
    available: bool
    reason: str | None
    bars_required: int
    bars_available: int
    output_series_name: str
    values: tuple[Decimal | None, ...]


_LENGTH = (Parameter("length", "integer", 20, (1, 500)),)


# Explicit catalog: no import-time discovery or filesystem scanning.
INDICATORS: tuple[IndicatorDefinition, ...] = (
    IndicatorDefinition("sma", "Simple Moving Average", "trend", _LENGTH, 20, "sma", True),
    IndicatorDefinition("ema", "Exponential Moving Average", "trend", _LENGTH, 20, "ema", True),
    IndicatorDefinition("rma", "Running Moving Average", "trend", _LENGTH, 20, "rma", True),
    IndicatorDefinition("wma", "Weighted Moving Average", "trend", _LENGTH, 20, "wma", True),
    IndicatorDefinition("vwma", "Volume Weighted Moving Average", "trend", _LENGTH, 20, "vwma", True),
    IndicatorDefinition("tema", "Triple Exponential Moving Average", "trend", _LENGTH, 58, "tema", True),
    IndicatorDefinition("dema", "Double Exponential Moving Average", "trend", _LENGTH, 39, "dema", True),
    IndicatorDefinition("tma", "Triangular Moving Average", "trend", _LENGTH, 39, "tma", True),
    IndicatorDefinition(
        "alma", "Arnaud Legoux Moving Average", "trend",
        (_LENGTH[0], Parameter("offset", "number", 0.85, (0.0, 1.0)),
         Parameter("sigma", "number", 6.0, (0.1, 20.0))), 20, "alma", True,
    ),
    IndicatorDefinition("hma", "Hull Moving Average", "trend", _LENGTH, 24, "hma", True),
    IndicatorDefinition("lsma", "Least Squares Moving Average", "trend", _LENGTH, 20, "lsma", True),
    IndicatorDefinition("zlema", "Zero Lag Exponential Moving Average", "trend", _LENGTH, 20, "zlema", True),
    IndicatorDefinition("rsi", "Relative Strength Index (Wilder)", "momentum",
                        (Parameter("period", "integer", 14, (1, 500)),), 15, "rsi", False),
    IndicatorDefinition("atr", "Average True Range", "volatility",
                        (Parameter("period", "integer", 14, (1, 500)),), 14, "atr", True),
    IndicatorDefinition("session_vwap", "Session VWAP", "volume", (), 1, "session_vwap", True),
    IndicatorDefinition("volume_expansion", "Volume Expansion", "volume",
                        (Parameter("lookback", "integer", 5, (1, 500)),), 6, "volume_expansion", False),
)
_DEFINITIONS = {item.id: item for item in INDICATORS}


# Origin: del_trading_bot/long_strategy.py, ma_sma.
def ma_sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=1).mean()


# Origin: del_trading_bot/long_strategy.py, ma_ema. Kept over duplicate short ema; matching VPVR's warm-up guard is added.
def ma_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


# Origin: del_trading_bot/long_strategy.py, ma_rma.
def ma_rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


# Origin: del_trading_bot/long_strategy.py, ma_wma.
def ma_wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1, dtype=float)

    def weighted(window: np.ndarray) -> float:
        active_weights = weights[-len(window):]
        return float(np.dot(window, active_weights) / active_weights.sum())

    return series.rolling(window=length, min_periods=length).apply(weighted, raw=True)


# Origin: del_trading_bot/long_strategy.py, ma_vwma.
def ma_vwma(frame: pd.DataFrame, length: int) -> pd.Series:
    close = frame["close"].astype(float)
    if "volume" not in frame.columns:
        return ma_sma(close, length)
    volume = frame["volume"].astype(float)
    denominator = volume.rolling(window=length, min_periods=1).sum().replace(0, np.nan)
    return (close * volume).rolling(window=length, min_periods=1).sum() / denominator


# Origin: del_trading_bot/long_strategy.py, ma_tema.
def ma_tema(series: pd.Series, length: int) -> pd.Series:
    first = ma_ema(series, length)
    second = ma_ema(first, length)
    third = ma_ema(second, length)
    return 3.0 * first - 3.0 * second + third


# Origin: del_trading_bot/long_strategy.py, ma_dema.
def ma_dema(series: pd.Series, length: int) -> pd.Series:
    first = ma_ema(series, length)
    return 2.0 * first - ma_ema(first, length)


# Origin: del_trading_bot/long_strategy.py, ma_tma.
def ma_tma(series: pd.Series, length: int) -> pd.Series:
    return ma_sma(ma_sma(series, length), length)


# Origin: del_trading_bot/long_strategy.py, ma_alma.
def ma_alma(series: pd.Series, length: int, offset: float = 0.85, sigma: float = 6.0) -> pd.Series:
    center = math.floor(offset * (length - 1))
    scale = length / sigma
    weights = np.array([math.exp(-((index - center) ** 2) / (2 * scale * scale))
                        for index in range(length)])
    weights /= weights.sum()

    def alma(window: np.ndarray) -> float:
        return float(np.dot(window, weights)) if len(window) == length else float("nan")

    return series.rolling(window=length, min_periods=length).apply(alma, raw=True)


# Origin: del_trading_bot/long_strategy.py, ma_hma. Deprecated fillna(method="bfill") is modernized to bfill().
def ma_hma(series: pd.Series, length: int) -> pd.Series:
    half_length = max(1, length // 2)
    root_length = max(1, int(math.sqrt(length)))
    difference = 2.0 * ma_wma(series, half_length) - ma_wma(series, length)
    return ma_wma(difference.bfill(), root_length)


# Origin: del_trading_bot/long_strategy.py, ma_lsma.
def ma_lsma(series: pd.Series, length: int) -> pd.Series:
    def linear_regression(window: np.ndarray) -> float:
        x = np.arange(length, dtype=float)
        y = np.asarray(window, dtype=float)
        n = len(x)
        denominator = n * (x * x).sum() - x.sum() ** 2
        if denominator == 0:
            return float(y[-1])
        slope = (n * (x * y).sum() - x.sum() * y.sum()) / denominator
        intercept = (y.sum() - slope * x.sum()) / n
        return float(slope * (n - 1) + intercept)

    return series.rolling(window=length, min_periods=length).apply(linear_regression, raw=True)


# Origin: del_trading_bot/long_strategy.py, ma_zlema.
def ma_zlema(series: pd.Series, length: int) -> pd.Series:
    lag = (length - 1) // 2
    adjusted = series + (series - series.shift(lag))
    return ma_ema(adjusted.fillna(series), length)


# Origin: del_trading_bot/long_strategy.py, calculate_atr. Kept over the duplicate short atr because both use the same simple rolling true-range mean.
def calculate_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    previous_close = frame["close"].astype(float).shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    return true_range.rolling(window=period, min_periods=1).mean()


# Origin: del_trading_bot/strategy-short/mhr_pump_short/indicators.py, rsi (Wilder).
def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    result = pd.Series(50.0, index=series.index, dtype=float)
    both = (gain > 0) & (loss > 0)
    result[both] = 100.0 - 100.0 / (1.0 + gain[both] / loss[both])
    result[(gain > 0) & (loss == 0)] = 100.0
    result[(gain == 0) & (loss > 0)] = 0.0
    result.iloc[:period] = 50.0
    return result


# Origin: del_trading_bot/vpvr/indicators.py, session_vwap.
def session_vwap(frame: pd.DataFrame) -> pd.Series:
    times = pd.to_datetime(frame["time"], utc=True)
    sessions = times.dt.strftime("%Y-%m-%d")
    typical_price = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    price_volume = typical_price * frame["volume"]
    result = pd.Series(index=frame.index, dtype=float)
    for session in sessions.drop_duplicates():
        mask = sessions == session
        cumulative_volume = frame.loc[mask, "volume"].cumsum().replace(0, np.nan)
        result.loc[mask] = price_volume.loc[mask].cumsum() / cumulative_volume
    return result


# Origin: del_trading_bot/vpvr/indicators.py, volume_expansion.
def volume_expansion(frame: pd.DataFrame, lookback: int = 5) -> pd.Series:
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    prior_average = volume.shift(1).rolling(lookback, min_periods=lookback).mean()
    return volume / prior_average.where(prior_average > 0)


_MA_FUNCTIONS = {
    "sma": ma_sma, "ema": ma_ema, "rma": ma_rma, "wma": ma_wma,
    "vwma": ma_vwma, "tema": ma_tema, "dema": ma_dema, "tma": ma_tma,
    "alma": ma_alma, "hma": ma_hma, "lsma": ma_lsma, "zlema": ma_zlema,
}


def decimal_output(value: Any) -> Decimal:
    """Preserve computed precision at the strategy boundary; never construct Decimal from float."""
    return Decimal(str(value))


def quantize_for_display(value: Decimal, tick_size: Decimal | str) -> Decimal:
    """Quantise a price-like Decimal only when formatting it for display."""
    tick = tick_size if isinstance(tick_size, Decimal) else Decimal(str(tick_size))
    if not tick.is_finite() or tick <= 0:
        raise ValueError("tick_size must be a positive finite Decimal or string")
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def minimum_bars_for(indicator_id: str, parameters: dict[str, int | float] | None = None) -> int:
    definition = _DEFINITIONS.get(indicator_id)
    if definition is None:
        raise ValueError(f"unknown indicator id: {indicator_id}")
    params = {item.name: item.default for item in definition.parameters}
    params.update(parameters or {})
    if "length" in params:
        length = int(params["length"])
        if indicator_id == "hma":
            return length + math.ceil(math.sqrt(length)) - 1
        if indicator_id == "dema":
            return 2 * length - 1
        if indicator_id == "tema":
            return 3 * length - 2
        if indicator_id == "tma":
            return 2 * length - 1
        return length
    if "period" in params:
        return int(params["period"]) + (1 if indicator_id == "rsi" else 0)
    if "lookback" in params:
        return int(params["lookback"]) + 1
    return definition.minimum_bars


def calculate_indicator(
    indicator_id: str,
    data: pd.DataFrame | pd.Series,
    *,
    parameters: dict[str, int | float] | None = None,
) -> IndicatorResult:
    """Calculate one registered indicator; only Decimal or None values cross this boundary."""
    definition = _DEFINITIONS.get(indicator_id)
    if definition is None:
        raise ValueError(f"unknown indicator id: {indicator_id}")
    params = {item.name: item.default for item in definition.parameters}
    params.update(parameters or {})
    allowed = {item.name: item for item in definition.parameters}
    if set(params) - set(allowed):
        raise ValueError(f"unsupported parameters for {indicator_id}")
    for name, value in params.items():
        schema = allowed[name]
        low, high = schema.bounds
        if not low <= value <= high or (schema.type == "integer" and not isinstance(value, int)):
            raise ValueError(f"invalid {name} for {indicator_id}")

    frame = data.to_frame(name=data.name or "close") if isinstance(data, pd.Series) else data.copy()
    bars_available = len(frame)
    required = minimum_bars_for(indicator_id, params)
    if bars_available < required:
        return IndicatorResult(indicator_id, False,
                               f"requires {required} bars; {bars_available} available", required,
                               bars_available, definition.output_series_name, ())
    needed = {"close"} if indicator_id in _MA_FUNCTIONS or indicator_id == "rsi" else set()
    if indicator_id in {"atr", "session_vwap"}:
        needed = {"high", "low", "close"}
    if indicator_id == "session_vwap":
        needed.add("time")
    if indicator_id in {"session_vwap", "volume_expansion"}:
        needed.add("volume")
    missing = needed - set(frame.columns)
    if missing:
        return IndicatorResult(indicator_id, False, f"missing required columns: {', '.join(sorted(missing))}",
                               required, bars_available, definition.output_series_name, ())

    if indicator_id in _MA_FUNCTIONS:
        close = frame["close"].astype(float)
        output = (_MA_FUNCTIONS[indicator_id](frame, int(params["length"])) if indicator_id == "vwma"
                  else ma_alma(close, int(params["length"]), float(params["offset"]), float(params["sigma"]))
                  if indicator_id == "alma" else _MA_FUNCTIONS[indicator_id](close, int(params["length"])))
    elif indicator_id == "rsi":
        output = rsi_wilder(frame["close"].astype(float), int(params["period"]))
    elif indicator_id == "atr":
        output = calculate_atr(frame, int(params["period"]))
    elif indicator_id == "session_vwap":
        output = session_vwap(frame)
    else:
        output = volume_expansion(frame, int(params["lookback"]))

    values: list[Decimal | None] = []
    for value in output.tolist():
        if pd.isna(value) or not math.isfinite(float(value)):
            values.append(None)
        else:
            values.append(decimal_output(value))
    return IndicatorResult(indicator_id, True, None, required, bars_available,
                           definition.output_series_name, tuple(values))
