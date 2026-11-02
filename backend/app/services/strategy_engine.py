"""Deterministic, Decimal-only strategy evaluation over stored daily bars.

A strategy is a fixed rule set: an entry fires when the fast SMA crosses above the slow SMA
on the latest completed daily bar, optionally filtered by RSI(14) <= rsi_max; an exit fires
on the opposite cross. Output is research signal history, never an order. Missing, stale,
or synthetic inputs are recorded as rejections with a reason; nothing is estimated.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketBar
from app.models.strategies import Strategy, StrategySignal, StrategyVersion
from app.services.indicators import rsi, sma

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

    fast_period: int = Field(ge=2, le=100)
    slow_period: int = Field(ge=3, le=250)
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


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def evaluate_bars(
    bars: list[MarketBar], params: StrategyParams, now: datetime
) -> Evaluation:
    """Evaluate ascending daily bars. Pure and deterministic for fixed inputs."""
    required = max(params.slow_period + 1, RSI_PERIOD + 1)
    if len(bars) < required:
        return Evaluation(state=STATE_INSUFFICIENT_HISTORY)

    fast_now = sma(bars, params.fast_period).value
    slow_now = sma(bars, params.slow_period).value
    previous = bars[:-1]
    fast_prev = sma(previous, params.fast_period).value
    slow_prev = sma(previous, params.slow_period).value
    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return Evaluation(state=STATE_INSUFFICIENT_HISTORY)

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
            return Evaluation(state=STATE_INSUFFICIENT_HISTORY)
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
    evaluation = evaluate_bars(load_daily_bars(db, strategy.instrument_id), params, now)
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
