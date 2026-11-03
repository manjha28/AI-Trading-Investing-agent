"""Deterministic, Decimal-only strategy evaluation over stored daily bars.

A strategy is a fixed rule set: an entry fires when the fast SMA crosses above the slow SMA
on the latest completed daily bar, optionally filtered by RSI(14) <= rsi_max; an exit fires
on the opposite cross. Output is research signal history, never an order. Missing, stale,
or synthetic inputs are recorded as rejections with a reason; nothing is estimated.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
import pandas as pd

from app.models import Instrument, MarketBar
from app.models.strategies import Strategy, StrategySignal, StrategyVersion
from app.services.indicators import rsi, sma
from app.services.indicator_library import minimum_bars_for
from app.services.strategy_contract import FastSlowMAAdapter, StrategyContext

DISCLAIMER = "Decision support, not investment advice."
DAILY_INTERVAL = "1d"
HISTORY_LIMIT = 300
RSI_PERIOD = 14
RULE_DESCRIPTION = "SMA crossover on the latest completed daily bar"

STATE_SIGNAL = "signal"
STATE_NO_CROSSOVER = "no_crossover"
STATE_INSUFFICIENT_HISTORY = "insufficient_history"


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: Literal["sma_rsi", "fast_slow_ma"] = "sma_rsi"
    fast_period: int = Field(ge=2, le=100)
    slow_period: int = Field(ge=3, le=250)
    ma_type: Literal["SMA", "EMA", "RMA", "WMA", "VWMA", "TEMA", "DEMA", "TMA", "ALMA", "HMA", "LSMA", "ZLEMA"] = "EMA"
    rsi_max: Decimal | None = Field(default=None, ge=0, le=100)
    max_bar_age_days: int = Field(default=5, ge=1, le=10)
    expiry_hours: int = Field(default=48, ge=1, le=720)

    @model_validator(mode="after")
    def check_periods(self) -> "StrategyParams":
        if self.slow_period <= self.fast_period:
            raise ValueError("slow_period must be greater than fast_period")
        return self

    def to_storage(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class Evaluation:
    state: str
    direction: str | None = None
    status: str | None = None
    rejection_reason: str | None = None
    bar_timestamp: datetime | None = None
    trigger_price: Decimal | None = None
    rationale: str = ""
    evidence: dict[str, Any] | None = None
    data_sufficiency: dict[str, Any] | None = None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _insufficient_history(
    required: int, available: int, bars: list[MarketBar], instrument: Instrument | None,
) -> Evaluation:
    return Evaluation(
        state=STATE_INSUFFICIENT_HISTORY,
        data_sufficiency={
            "instrument": instrument.symbol if instrument is not None else (
                str(bars[0].instrument_id) if bars else "unavailable"
            ),
            "interval": DAILY_INTERVAL,
            "source": bars[-1].source if bars else "unavailable",
            "bars_required": required,
            "bars_available": available,
        },
    )


def evaluate_bars(
    bars: list[MarketBar], params: StrategyParams, now: datetime,
    instrument: Instrument | None = None,
) -> Evaluation:
    """Evaluate ascending daily bars. Pure and deterministic for fixed inputs."""
    if params.strategy_id == "fast_slow_ma":
        return _evaluate_fast_slow_bars(bars, params, now, instrument)
    required = max(params.slow_period + 1, RSI_PERIOD + 1)
    if len(bars) < required:
        return _insufficient_history(required, len(bars), bars, instrument)

    fast_now = sma(bars, params.fast_period).value
    slow_now = sma(bars, params.slow_period).value
    previous = bars[:-1]
    fast_prev = sma(previous, params.fast_period).value
    slow_prev = sma(previous, params.slow_period).value
    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return _insufficient_history(required, len(bars), bars, instrument)

    if fast_prev <= slow_prev and fast_now > slow_now:
        direction = "entry"
    elif fast_prev >= slow_prev and fast_now < slow_now:
        direction = "exit"
    else:
        return Evaluation(state=STATE_NO_CROSSOVER)

    latest = bars[-1]
    bar_time = _utc(latest.timestamp)
    rsi_value = rsi(bars, RSI_PERIOD).value
    reason: str | None = None
    if any(bar.is_synthetic for bar in bars):
        reason = "synthetic_data"
    elif now - bar_time > timedelta(days=params.max_bar_age_days):
        reason = "stale_bars"
    elif direction == "entry" and params.rsi_max is not None:
        if rsi_value is None:
            return _insufficient_history(required, len(bars), bars, instrument)
        if rsi_value > params.rsi_max:
            reason = "rsi_above_max"

    rationale = (
        f"SMA({params.fast_period}) crossed {'above' if direction == 'entry' else 'below'} "
        f"SMA({params.slow_period}) on the latest daily bar; "
        f"{RULE_DESCRIPTION}."
    )
    evidence: dict[str, Any] = {
        "rule": RULE_DESCRIPTION,
        "sma_fast": _text(fast_now),
        "sma_fast_previous": _text(fast_prev),
        "sma_slow": _text(slow_now),
        "sma_slow_previous": _text(slow_prev),
        "rsi_14": _text(rsi_value),
        "rsi_max": _text(params.rsi_max),
        "close": _text(latest.close),
        "bars_used": len(bars),
        "data_timestamp_utc": bar_time.isoformat(),
        "is_synthetic": any(bar.is_synthetic for bar in bars),
        "disclaimer": DISCLAIMER,
    }
    return Evaluation(
        state=STATE_SIGNAL,
        direction=direction,
        status="rejected" if reason else "active",
        rejection_reason=reason,
        bar_timestamp=bar_time,
        trigger_price=latest.close,
        rationale=rationale,
        evidence=evidence,
    )


def _evaluate_fast_slow_bars(
    bars: list[MarketBar], params: StrategyParams, now: datetime,
    instrument: Instrument | None = None,
) -> Evaluation:
    if not bars:
        return _insufficient_history(1, 0, bars, instrument)
    required = max(
        minimum_bars_for(params.ma_type.lower(), {"length": params.fast_period}),
        minimum_bars_for(params.ma_type.lower(), {"length": params.slow_period}),
    ) + 1
    rows = [{
        "time": _utc(bar.timestamp), "open": bar.open, "high": bar.high,
        "low": bar.low, "close": bar.close, "volume": bar.volume,
        "is_synthetic": bar.is_synthetic,
    } for bar in bars]
    latest = bars[-1]
    context = StrategyContext(
        instrument=instrument or type("StrategyInstrument", (), {
            "id": latest.instrument_id, "product_id": str(latest.instrument_id),
            "symbol": "unknown",
        })(),
        as_of=now,
        frames={"1d": pd.DataFrame(rows)},
        provenance={"1d": latest.source},
    )
    completed = context.completed_frame("1d")
    if len(completed) < required:
        return _insufficient_history(required, len(completed), bars, instrument)
    adapter = FastSlowMAAdapter(
        fast_period=params.fast_period, slow_period=params.slow_period,
        ma_type=params.ma_type, allow_short=False,
    )
    signals = adapter.evaluate(context)
    if not signals:
        return Evaluation(state=STATE_NO_CROSSOVER)
    signal = signals[0]
    latest_completed = completed.iloc[-1]
    bar_time = latest_completed["time"].to_pydatetime()
    completed_synthetic = completed.get("is_synthetic", pd.Series(False, index=completed.index)).astype(bool)
    reason = None
    if completed_synthetic.any():
        reason = "synthetic_data"
    elif now - bar_time > timedelta(days=params.max_bar_age_days):
        reason = "stale_bars"
    diagnostics = dict(signal.diagnostics)
    diagnostics.update({
        "rule": "Fast/slow moving-average crossover on latest completed daily bar",
        "disclaimer": DISCLAIMER,
        "is_synthetic": bool(completed_synthetic.any()),
        "status": "rejected" if reason else "active",
        "rejection_reason": reason,
    })
    evidence = {
        "strategy_signal": {
            "strategy_id": signal.strategy_id,
            "strategy_version": signal.strategy_version,
            "product_id": signal.product_id,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "decision_time": signal.decision_time.isoformat(),
            "entry_price": str(signal.entry_price) if signal.entry_price is not None else None,
            "entry_reference": signal.entry_reference,
            "stop_price": None,
            "stop_logic": signal.stop_logic,
            "target_prices": [],
            "target_logic": signal.target_logic,
            "invalidation_conditions": list(signal.invalidation_conditions),
            "position_sizing_inputs": {},
            "provenance": dict(signal.provenance),
            "diagnostics": diagnostics,
        },
        **diagnostics,
    }
    return Evaluation(
        state=STATE_SIGNAL,
        direction="entry",
        status="rejected" if reason else "active",
        rejection_reason=reason,
        bar_timestamp=bar_time,
        trigger_price=signal.entry_price,
        rationale=(f"{params.ma_type}({params.fast_period}) crossed above "
                   f"{params.ma_type}({params.slow_period}) on the latest completed daily bar."),
        evidence=evidence,
    )


def load_daily_bars(db: Session, instrument_id: UUID) -> list[MarketBar]:
    """Real daily bars from the most recent source only, ascending by time."""
    latest_source = db.scalar(
        select(MarketBar.source)
        .where(MarketBar.instrument_id == instrument_id, MarketBar.interval == DAILY_INTERVAL,
               MarketBar.is_synthetic.is_(False))
        .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
        .limit(1)
    )
    if latest_source is None:
        return []
    rows = list(
        db.scalars(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument_id,
                MarketBar.interval == DAILY_INTERVAL,
                MarketBar.is_synthetic.is_(False),
                MarketBar.source == latest_source,
            )
            .order_by(MarketBar.timestamp.desc())
            .limit(HISTORY_LIMIT)
        )
    )
    return list(reversed(rows))


def load_backtest_bars(
    db: Session, instrument_id: UUID, as_of: datetime,
) -> tuple[str | None, list[MarketBar]]:
    """Load all completed real bars from one newest stored source; never substitute sources."""
    source = db.scalar(
        select(MarketBar.source)
        .where(
            MarketBar.instrument_id == instrument_id,
            MarketBar.interval == DAILY_INTERVAL,
            MarketBar.is_synthetic.is_(False),
        )
        .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
        .limit(1)
    )
    if source is None:
        return None, []
    cutoff = _utc(as_of) - timedelta(days=1)
    bars = list(db.scalars(
        select(MarketBar)
        .where(
            MarketBar.instrument_id == instrument_id,
            MarketBar.interval == DAILY_INTERVAL,
            MarketBar.source == source,
            MarketBar.is_synthetic.is_(False),
            MarketBar.timestamp <= cutoff,
        )
        .order_by(MarketBar.timestamp.asc())
    ))
    return source, bars


def expire_signals(db: Session, strategy: Strategy, now: datetime) -> None:
    changed = False
    active = db.scalars(
        select(StrategySignal).where(
            StrategySignal.strategy_id == strategy.id, StrategySignal.status == "active"
        )
    )
    for signal in active:
        if _utc(signal.expires_at) <= now:
            signal.status = "expired"
            changed = True
    if changed:
        db.commit()


def record_evaluation(
    db: Session, strategy: Strategy, version: StrategyVersion, now: datetime
) -> tuple[Evaluation, StrategySignal | None, bool]:
    """Evaluate the strategy and persist any crossover as a signal.

    Returns (evaluation, signal, duplicate). Re-evaluating the same bar and direction
    returns the existing signal and never inserts a second row.
    """
    params = StrategyParams.model_validate(version.params)
    evaluation = evaluate_bars(
        load_daily_bars(db, strategy.instrument_id), params, now,
        db.get(Instrument, strategy.instrument_id),
    )
    if evaluation.state != STATE_SIGNAL:
        return evaluation, None, False

    dedupe_key = f"{evaluation.direction}:{evaluation.bar_timestamp.isoformat()}"
    existing = db.scalar(
        select(StrategySignal).where(
            StrategySignal.strategy_id == strategy.id,
            StrategySignal.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return evaluation, existing, True

    signal = StrategySignal(
        user_id=strategy.user_id,
        strategy_id=strategy.id,
        strategy_version=version.version,
        instrument_id=strategy.instrument_id,
        direction=evaluation.direction,
        status=evaluation.status,
        rejection_reason=evaluation.rejection_reason,
        bar_timestamp=evaluation.bar_timestamp,
        trigger_price=evaluation.trigger_price,
        rationale=evaluation.rationale,
        evidence=evaluation.evidence,
        dedupe_key=dedupe_key,
        expires_at=evaluation.bar_timestamp + timedelta(hours=params.expiry_hours),
        created_at=now,
    )
    db.add(signal)
    db.commit()
    db.refresh(signal)
    return evaluation, signal, False
