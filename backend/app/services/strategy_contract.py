"""Read-only strategy signal contracts and the registered daily-bar adapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

import pandas as pd

from app.services.indicator_library import INDICATORS, calculate_indicator, minimum_bars_for


Direction = Literal["LONG", "SHORT"]
TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400,
}
OHLCV_FIELDS = ("time", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class StrategySpecification:
    strategy_id: str
    version: str
    timeframes: tuple[str, ...]
    required_fields: tuple[str, ...]
    regimes: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.version:
            raise ValueError("strategy_id and version are required")
        unsupported = set(self.timeframes).difference(TIMEFRAME_SECONDS)
        if unsupported:
            raise ValueError(f"unknown strategy timeframes: {sorted(unsupported)}")


@dataclass(frozen=True)
class StrategyContext:
    instrument: Any
    as_of: datetime
    frames: Mapping[str, pd.DataFrame]
    regime: Any = None
    profile: Any = None
    provenance: Mapping[str, str] = field(default_factory=dict)
    state: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("strategy context as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(timezone.utc))

    def completed_frame(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        frame = self.frames.get(timeframe)
        if frame is None:
            return pd.DataFrame(columns=list(OHLCV_FIELDS))
        if frame.empty:
            return frame.copy().reset_index(drop=True)
        missing = set(OHLCV_FIELDS).difference(frame.columns)
        if missing:
            raise ValueError(f"{timeframe} frame missing fields: {sorted(missing)}")
        result = frame.copy()
        parsed_times = pd.to_datetime(result["time"], errors="raise")
        if parsed_times.isna().any() or parsed_times.dt.tz is None:
            raise ValueError(f"{timeframe} timestamps must be valid and timezone-aware")
        result["time"] = parsed_times.dt.tz_convert("UTC")
        if result["time"].duplicated().any():
            raise ValueError(f"{timeframe} frame contains duplicate candle timestamps")
        result = result.sort_values("time")
        close_times = result["time"] + pd.to_timedelta(TIMEFRAME_SECONDS[timeframe], unit="s")
        # Anti-lookahead contract: accept a bar only when its CLOSE time is <= as_of.
        result = result.loc[close_times <= pd.Timestamp(self.as_of)].reset_index(drop=True)
        return result


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    strategy_version: str
    product_id: str
    symbol: str
    direction: Direction
    decision_time: datetime
    entry_price: Decimal | None
    entry_reference: str
    stop_price: Decimal | None
    stop_logic: str
    target_prices: tuple[Decimal, ...]
    target_logic: str
    invalidation_conditions: tuple[str, ...]
    position_sizing_inputs: Mapping[str, Any]
    provenance: Mapping[str, str]
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError("direction must be LONG or SHORT")
        if self.decision_time.tzinfo is None or self.decision_time.utcoffset() is None:
            raise ValueError("signal decision_time must be timezone-aware")
        object.__setattr__(self, "decision_time", self.decision_time.astimezone(timezone.utc))
        if not self.strategy_id or not self.product_id or not self.symbol:
            raise ValueError("signal strategy and instrument identity are required")
        for name in ("entry_price", "stop_price"):
            raw = getattr(self, name)
            if raw is not None:
                value = Decimal(str(raw))
                if not value.is_finite() or value <= 0:
                    raise ValueError(f"{name} must be a positive finite price")
                object.__setattr__(self, name, value)
        targets = tuple(Decimal(str(value)) for value in self.target_prices)
        if any(not value.is_finite() or value <= 0 for value in targets):
            raise ValueError("target prices must be positive and finite")
        object.__setattr__(self, "target_prices", targets)


@runtime_checkable
class StrategyAdapter(Protocol):
    specification: StrategySpecification

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]: ...


@dataclass(frozen=True)
class StrategyAvailability:
    status: str
    required_data: tuple[str, ...]
    limitations: tuple[str, ...]
    minimum_history: Mapping[str, int] | None = None


@dataclass(frozen=True)
class ParameterSchema:
    name: str
    label: str
    type: str
    default: int | float | str | bool | None
    minimum: int | float | None = None
    maximum: int | float | None = None
    options: tuple[str, ...] = ()
    optional: bool = False


@dataclass(frozen=True)
class StrategyRegistration:
    specification: StrategySpecification
    display_name: str
    asset_class: str
    availability: StrategyAvailability
    parameters: tuple[ParameterSchema, ...]


MA_TYPES = tuple(item.id.upper() for item in INDICATORS if item.category == "trend")
FAST_SLOW_SPECIFICATION = StrategySpecification(
    "fast_slow_ma", "1.0.0", ("1d",), OHLCV_FIELDS,
    description="Uses completed daily bars only; produces evidence without sizing or execution.",
)
SMA_RSI_SPECIFICATION = StrategySpecification(
    "sma_rsi", "1.0.0", ("1d",), OHLCV_FIELDS,
    description="Existing fast/slow SMA crossover with optional RSI(14) entry filter.",
)
_COMMON_PARAMETERS = (
    ParameterSchema("fast_period", "Fast length", "integer", 20, 2, 100),
    ParameterSchema("slow_period", "Slow length", "integer", 50, 3, 250),
    ParameterSchema("max_bar_age_days", "Maximum bar age (days)", "integer", 5, 1, 10),
    ParameterSchema("expiry_hours", "Signal history expiry (hours)", "integer", 48, 1, 720),
)
STRATEGY_REGISTRY: tuple[StrategyRegistration, ...] = (
    StrategyRegistration(
        FAST_SLOW_SPECIFICATION, "Fast/Slow Moving Average", "equity",
        StrategyAvailability(
            "available_ohlcv", ("completed 1d OHLCV bars",),
            ("No sizing, stops, targets, fills, or execution are produced.",
             "Minimum history scales with the selected MA type and lengths."),
            {"1d": 51},
        ),
        (
            ParameterSchema("ma_type", "Moving average", "select", "EMA", options=MA_TYPES),
            *_COMMON_PARAMETERS,
        ),
    ),
    StrategyRegistration(
        SMA_RSI_SPECIFICATION, "SMA + RSI Crossover", "equity",
        StrategyAvailability(
            "available_ohlcv", ("completed 1d OHLCV bars",),
            ("Preserves the existing SMA crossover and optional RSI(14) entry filter.",),
            {"1d": 51},
        ),
        (
            *_COMMON_PARAMETERS,
            ParameterSchema("rsi_max", "Maximum RSI(14) for entry", "number", None, 0, 100, optional=True),
        ),
    ),
)


def registry_payload() -> list[dict[str, Any]]:
    return [
        {
            "strategy_id": item.specification.strategy_id,
            "version": item.specification.version,
            "display_name": item.display_name,
            "description": item.specification.description,
            "asset_class": item.asset_class,
            "timeframes": list(item.specification.timeframes),
            "required_fields": list(item.specification.required_fields),
            "availability": {
                "status": item.availability.status,
                "required_data": list(item.availability.required_data),
                "limitations": list(item.availability.limitations),
                "minimum_history": dict(item.availability.minimum_history or {}),
            },
            "parameters": [
                {**asdict(parameter), "options": list(parameter.options)}
                for parameter in item.parameters
            ],
        }
        for item in STRATEGY_REGISTRY
    ]


def _product_id(instrument: Any) -> str:
    return str(getattr(instrument, "product_id", getattr(instrument, "id", "")))


class FastSlowMAAdapter:
    specification = FAST_SLOW_SPECIFICATION

    def __init__(self, fast_period: int = 20, slow_period: int = 50,
                 ma_type: str = "EMA", allow_short: bool = False):
        if fast_period < 1 or slow_period <= fast_period:
            raise ValueError("slow_period must be greater than a positive fast_period")
        if ma_type.upper() not in MA_TYPES:
            raise ValueError(f"unsupported moving average type: {ma_type}")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.ma_type = ma_type.upper()
        self.allow_short = allow_short

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]:
        frame = context.completed_frame("1d")
        indicator_id = self.ma_type.lower()
        fast_args = {"length": self.fast_period}
        slow_args = {"length": self.slow_period}
        min_fast = minimum_bars_for(indicator_id, fast_args)
        min_slow = minimum_bars_for(indicator_id, slow_args)
        required = max(min_fast, min_slow) + 1
        if len(frame) < required:
            return []
        fast = calculate_indicator(indicator_id, frame, parameters=fast_args)
        slow = calculate_indicator(indicator_id, frame, parameters=slow_args)
        prior_frame = frame.iloc[:-1].reset_index(drop=True)
        fast_prior = calculate_indicator(indicator_id, prior_frame, parameters=fast_args)
        slow_prior = calculate_indicator(indicator_id, prior_frame, parameters=slow_args)
        if not all(result.available for result in (fast, slow, fast_prior, slow_prior)):
            return []
        fast_now, fast_previous = fast.values[-1], fast_prior.values[-1]
        slow_now, slow_previous = slow.values[-1], slow_prior.values[-1]
        if None in (fast_now, fast_previous, slow_now, slow_previous):
            return []
        direction: Direction | None = None
        if fast_previous <= slow_previous and fast_now > slow_now:
            direction = "LONG"
        elif self.allow_short and fast_previous >= slow_previous and fast_now < slow_now:
            direction = "SHORT"
        if direction is None:
            return []

        last = frame.iloc[-1]
        close_time = (pd.Timestamp(last["time"]) + pd.Timedelta(seconds=TIMEFRAME_SECONDS["1d"])).to_pydatetime()
        source = context.provenance.get("1d", "unspecified")
        return [StrategySignal(
            strategy_id=self.specification.strategy_id,
            strategy_version=self.specification.version,
            product_id=_product_id(context.instrument),
            symbol=str(getattr(context.instrument, "symbol", "")),
            direction=direction,
            decision_time=close_time,
            entry_price=Decimal(str(last["close"])),
            entry_reference="latest completed daily close",
            stop_price=None,
            stop_logic="No stop logic is produced in this decision-support adapter.",
            target_prices=(),
            target_logic="No target logic is produced in this decision-support adapter.",
            invalidation_conditions=("Opposite fast/slow moving-average crossover",),
            position_sizing_inputs={},
            provenance={"1d": source},
            diagnostics={
                "data_window_start": frame.iloc[0]["time"].isoformat(),
                "data_window_end": last["time"].isoformat(),
                "bars_used": len(frame),
                "source": source,
                "ma_type": self.ma_type,
                "fast_period": self.fast_period,
                "slow_period": self.slow_period,
                "fast_previous": str(fast_previous),
                "slow_previous": str(slow_previous),
                "fast_latest": str(fast_now),
                "slow_latest": str(slow_now),
                "close": str(Decimal(str(last["close"]))),
                "as_of": context.as_of.isoformat(),
            },
        )]


class SMARSIAdapter:
    """Contract wrapper around the existing SMA/RSI evaluator, preserving its decisions."""
    specification = SMA_RSI_SPECIFICATION

    def __init__(self, fast_period: int = 20, slow_period: int = 50,
                 rsi_max: Decimal | str | None = None, max_bar_age_days: int = 5):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_max = rsi_max
        self.max_bar_age_days = max_bar_age_days

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]:
        from types import SimpleNamespace
        from app.services.strategy_engine import StrategyParams, evaluate_bars

        frame = context.completed_frame("1d")
        rows = [SimpleNamespace(
            close=Decimal(str(row.close)), timestamp=row.time.to_pydatetime(),
            is_synthetic=bool(getattr(row, "is_synthetic", False)),
        ) for row in frame.itertuples()]
        params = StrategyParams(
            strategy_id="sma_rsi", fast_period=self.fast_period, slow_period=self.slow_period,
            rsi_max=self.rsi_max, max_bar_age_days=self.max_bar_age_days,
        )
        result = evaluate_bars(rows, params, context.as_of)
        if result.state != "signal":
            return []
        latest = frame.iloc[-1]
        direction: Direction = "LONG" if result.direction == "entry" else "SHORT"
        return [StrategySignal(
            strategy_id=self.specification.strategy_id,
            strategy_version=self.specification.version,
            product_id=_product_id(context.instrument),
            symbol=str(getattr(context.instrument, "symbol", "")),
            direction=direction,
            decision_time=result.bar_timestamp or context.as_of,
            entry_price=result.trigger_price,
            entry_reference="latest completed daily close",
            stop_price=None,
            stop_logic="No stop logic is produced by the existing SMA/RSI strategy.",
            target_prices=(),
            target_logic="No target logic is produced by the existing SMA/RSI strategy.",
            invalidation_conditions=("Opposite fast/slow SMA crossover",),
            position_sizing_inputs={},
            provenance={"1d": context.provenance.get("1d", "unspecified")},
            diagnostics={**(result.evidence or {}), "status": result.status,
                         "rejection_reason": result.rejection_reason,
                         "entry_reference": "latest completed daily close"},
        )]


# Explicit adapter registration; no filesystem scanning or dynamic imports.
STRATEGY_ADAPTERS: Mapping[str, type[StrategyAdapter]] = {
    "fast_slow_ma": FastSlowMAAdapter,
    "sma_rsi": SMARSIAdapter,
}
