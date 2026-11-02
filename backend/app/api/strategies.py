from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.db import get_db
from app.models import Instrument, UserSession
from app.models.strategies import Strategy, StrategySignal, StrategyVersion
from app.services.strategy_engine import (
    DISCLAIMER,
    STATE_SIGNAL,
    StrategyParams,
    expire_signals,
    record_evaluation,
)

router = APIRouter(prefix="/strategies", tags=["strategies"])


def require_verified_session(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before managing strategies.",
            },
        )
    return active_session


class StrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    instrument_id: UUID
    params: StrategyParams


class StrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: StrategyParams


class VersionResponse(BaseModel):
    version: int
    params: dict[str, Any]
    created_at: datetime


class StrategyResponse(BaseModel):
    id: UUID
    instrument_id: UUID
    name: str
    description: str
    status: Literal["suspended", "active", "retired"]
    current_version: int
    params: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    disclaimer: str = DISCLAIMER


class SignalResponse(BaseModel):
    id: UUID
    strategy_id: UUID
    strategy_version: int
    instrument_id: UUID
    direction: Literal["entry", "exit"]
    status: Literal["active", "rejected", "expired"]
    rejection_reason: str | None
    bar_timestamp: datetime
    trigger_price: Decimal
    rationale: str
    evidence: dict[str, Any]
    expires_at: datetime
    created_at: datetime
    disclaimer: str = DISCLAIMER


class EvaluationResponse(BaseModel):
    result: Literal["signal", "duplicate", "no_crossover", "insufficient_history"]
    signal: SignalResponse | None
    disclaimer: str = DISCLAIMER


def _owned_strategy(db: Session, user_id: UUID, strategy_id: UUID) -> Strategy:
    strategy = db.scalar(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id)
    )
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


def _version_row(db: Session, strategy: Strategy, version: int) -> StrategyVersion:
    row = db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == version
        )
    )
    if row is None:
        raise HTTPException(status_code=500, detail="Strategy version missing")
    return row


def _strategy_response(db: Session, strategy: Strategy) -> StrategyResponse:
    version = _version_row(db, strategy, strategy.current_version)
    return StrategyResponse(
        id=strategy.id, instrument_id=strategy.instrument_id, name=strategy.name,
        description=strategy.description, status=strategy.status,
        current_version=strategy.current_version, params=version.params,
        created_at=strategy.created_at, updated_at=strategy.updated_at,
    )


def _signal_response(signal: StrategySignal) -> SignalResponse:
    return SignalResponse(
        id=signal.id, strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version, instrument_id=signal.instrument_id,
        direction=signal.direction, status=signal.status,
        rejection_reason=signal.rejection_reason, bar_timestamp=signal.bar_timestamp,
        trigger_price=signal.trigger_price, rationale=signal.rationale,
        evidence=signal.evidence, expires_at=signal.expires_at, created_at=signal.created_at,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("", response_model=list[StrategyResponse])
def list_strategies(
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[StrategyResponse]:
    strategies = db.scalars(
        select(Strategy)
        .where(Strategy.user_id == active_session.user_id)
        .order_by(Strategy.created_at.desc())
    )
    return [_strategy_response(db, strategy) for strategy in strategies]


@router.post("", response_model=StrategyResponse, status_code=201)
def create_strategy(
    payload: StrategyCreate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StrategyResponse:
    if db.get(Instrument, payload.instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    now = _now()
    strategy = Strategy(
        user_id=active_session.user_id, instrument_id=payload.instrument_id,
        name=payload.name, description=payload.description, status="suspended",
        current_version=1, created_at=now, updated_at=now,
    )
    db.add(strategy)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A strategy with this name already exists")
    db.add(StrategyVersion(
        strategy_id=strategy.id, version=1, params=payload.params.to_storage(), created_at=now,
    ))
    db.commit()
    db.refresh(strategy)
    return _strategy_response(db, strategy)


@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> StrategyResponse:
    return _strategy_response(db, _owned_strategy(db, active_session.user_id, strategy_id))


@router.patch("/{strategy_id}", response_model=StrategyResponse)
def update_strategy(
    strategy_id: UUID,
    payload: StrategyUpdate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StrategyResponse:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="name cannot be null")
    for field, value in changes.items():
        if field == "description" and value is None:
            value = ""
        setattr(strategy, field, value)
    strategy.updated_at = _now()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A strategy with this name already exists")
    db.refresh(strategy)
    return _strategy_response(db, strategy)


@router.get("/{strategy_id}/versions", response_model=list[VersionResponse])
def list_versions(
    strategy_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[StrategyVersion]:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    return list(
        db.scalars(
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == strategy.id)
            .order_by(StrategyVersion.version.desc())
        )
    )


@router.post("/{strategy_id}/versions", response_model=StrategyResponse, status_code=201)
def create_version(
    strategy_id: UUID,
    payload: VersionCreate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StrategyResponse:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    if strategy.status == "retired":
        raise HTTPException(status_code=409, detail="Retired strategies cannot be versioned")
    now = _now()
    next_version = strategy.current_version + 1
    db.add(StrategyVersion(
        strategy_id=strategy.id, version=next_version,
        params=payload.params.to_storage(), created_at=now,
    ))
    strategy.current_version = next_version
    strategy.updated_at = now
    db.commit()
    db.refresh(strategy)
    return _strategy_response(db, strategy)


def _transition(db: Session, strategy: Strategy, target: str) -> None:
    if strategy.status == "retired":
        raise HTTPException(status_code=409, detail="Retired strategies cannot change state")
    strategy.status = target
    strategy.updated_at = _now()
    db.commit()


@router.post("/{strategy_id}/activate", response_model=StrategyResponse)
def activate_strategy(
    strategy_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StrategyResponse:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    _transition(db, strategy, "active")
    db.refresh(strategy)
    return _strategy_response(db, strategy)


@router.post("/{strategy_id}/suspend", response_model=StrategyResponse)
def suspend_strategy(
    strategy_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StrategyResponse:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    _transition(db, strategy, "suspended")
    db.refresh(strategy)
    return _strategy_response(db, strategy)


@router.post("/{strategy_id}/retire", response_model=StrategyResponse)
def retire_strategy(
    strategy_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StrategyResponse:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    _transition(db, strategy, "retired")
    db.refresh(strategy)
    return _strategy_response(db, strategy)


@router.post("/{strategy_id}/evaluate", response_model=EvaluationResponse)
def evaluate_strategy(
    strategy_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> EvaluationResponse:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    if strategy.status != "active":
        raise HTTPException(
            status_code=409,
            detail={"code": "strategy_not_active", "message": "Activate the strategy to evaluate it."},
        )
    now = _now()
    expire_signals(db, strategy, now)
    version = _version_row(db, strategy, strategy.current_version)
    evaluation, signal, duplicate = record_evaluation(db, strategy, version, now)
    if duplicate:
        result = "duplicate"
    elif evaluation.state == STATE_SIGNAL:
        result = "signal"
    else:
        result = evaluation.state
    return EvaluationResponse(
        result=result, signal=_signal_response(signal) if signal is not None else None,
    )


@router.get("/{strategy_id}/signals", response_model=list[SignalResponse])
def list_signals(
    strategy_id: UUID,
    status: Literal["active", "rejected", "expired"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[SignalResponse]:
    strategy = _owned_strategy(db, active_session.user_id, strategy_id)
    expire_signals(db, strategy, _now())
    query = select(StrategySignal).where(
        StrategySignal.strategy_id == strategy.id,
        StrategySignal.user_id == active_session.user_id,
    )
    if status is not None:
        query = query.where(StrategySignal.status == status)
    rows = db.scalars(
        query.order_by(StrategySignal.bar_timestamp.desc(), StrategySignal.created_at.desc())
        .limit(limit)
    )
    return [_signal_response(row) for row in rows]
