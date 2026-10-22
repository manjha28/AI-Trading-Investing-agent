from datetime import datetime, timezone
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.api.portfolio_transactions import get_owned_portfolio
from app.db import get_db
from app.models import Instrument, Portfolio, UserSession
from app.models.alerts import AlertEvent, AlertRule
from app.services.alert_engine import RULE_TYPE_PARAMS, RULE_TYPES, evaluate_rules

router = APIRouter(prefix="/alerts", tags=["alerts"])
DISCLAIMER = "Decision support, not investment advice."


class RuleFields(BaseModel):
    scope: Literal["instrument", "position", "portfolio"]
    instrument_id: UUID | None = None
    portfolio_id: UUID | None = None
    rule_type: str
    params: dict[str, Any]
    severity: Literal["info", "attention", "urgent"] = "attention"
    cooldown_seconds: int = Field(default=3600, ge=0, le=31_536_000)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_rule(self) -> "RuleFields":
        if self.rule_type not in RULE_TYPES:
            raise ValueError("Unsupported alert rule type")
        if self.scope == "instrument" and self.instrument_id is None:
            raise ValueError("instrument scope requires instrument_id")
        if self.scope == "position" and (self.instrument_id is None or self.portfolio_id is None):
            raise ValueError("position scope requires portfolio_id and instrument_id")
        if self.scope == "portfolio" and self.portfolio_id is None:
            raise ValueError("portfolio scope requires portfolio_id")
        required = RULE_TYPE_PARAMS[self.rule_type]
        if set(self.params) != required:
            raise ValueError(
                "params must contain exactly: " + ", ".join(sorted(required))
                if required else "This rule type does not accept parameters"
            )
        params = self.params
        if self.rule_type in {"price_above", "price_below"}:
            _number(params, "price", positive=True)
        elif self.rule_type in {"pct_move_nd", "trailing_drawdown_pct"}:
            _integer(params, "days", minimum=1, maximum=365)
            _number(params, "threshold_pct", positive=True, maximum=1000)
            if self.rule_type == "pct_move_nd" and (
                not isinstance(params["direction"], str)
                or params["direction"] not in {"above", "below"}
            ):
                raise ValueError("direction must be above or below")
        elif self.rule_type == "unrealized_gain_pct_above":
            _number(params, "threshold_pct", maximum=100_000)
        elif self.rule_type == "position_weight_above":
            _number(params, "threshold_pct", positive=True, maximum=100)
        elif self.rule_type == "thesis_target_hit":
            _integer(params, "target_index", minimum=0, maximum=9)
        elif self.rule_type == "rsi_threshold":
            _integer(params, "period", minimum=2, maximum=100)
            _number(params, "threshold", minimum=0, maximum=100)
            if not isinstance(params["direction"], str) or params["direction"] not in {"above", "below"}:
                raise ValueError("direction must be above or below")
        elif self.rule_type == "ma_cross":
            _integer(params, "fast_period", minimum=2, maximum=100)
            _integer(params, "slow_period", minimum=3, maximum=300)
            if params["fast_period"] >= params["slow_period"]:
                raise ValueError("fast_period must be less than slow_period")
            if not isinstance(params["direction"], str) or params["direction"] not in {"up", "down"}:
                raise ValueError("direction must be up or down")
        elif self.rule_type == "data_stale":
            _integer(params, "max_age_seconds", minimum=1, maximum=31_536_000)
        if self.rule_type in {
            "thesis_target_hit", "thesis_stop_hit", "unrealized_gain_pct_above",
            "position_weight_above", "holding_period_crosses_365d",
        } and self.scope != "position":
            raise ValueError(f"{self.rule_type} requires position scope")
        if self.rule_type == "position_weight_above" and self.scope != "position":
            raise ValueError("position_weight_above requires position scope")
        return self


def _number(
    params: dict[str, Any], key: str, *, positive: bool = False,
    minimum: float | None = None, maximum: float | None = None,
) -> None:
    value = params.get(key)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not number.is_finite():
        raise ValueError(f"{key} must be finite")
    if positive and number <= 0:
        raise ValueError(f"{key} must be greater than zero")
    if minimum is not None and number < Decimal(str(minimum)):
        raise ValueError(f"{key} must be at least {minimum}")
    if maximum is not None and number > Decimal(str(maximum)):
        raise ValueError(f"{key} must be at most {maximum}")


def _integer(params: dict[str, Any], key: str, *, minimum: int, maximum: int) -> None:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer from {minimum} to {maximum}")


class RuleCreate(RuleFields):
    pass


class RuleUpdate(BaseModel):
    scope: Literal["instrument", "position", "portfolio"] | None = None
    instrument_id: UUID | None = None
    portfolio_id: UUID | None = None
    rule_type: str | None = None
    params: dict[str, Any] | None = None
    severity: Literal["info", "attention", "urgent"] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    enabled: bool | None = None


class RuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: str
    instrument_id: UUID | None
    portfolio_id: UUID | None
    rule_type: str
    params: dict
    severity: str
    cooldown_seconds: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class EventStatusUpdate(BaseModel):
    status: Literal["read", "dismissed", "actioned"]


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    rule_id: UUID
    instrument_id: UUID | None
    fired_at: datetime
    dedupe_key: str
    evidence: dict
    status: str
    severity: str
    rule_type: str
    disclaimer: str = DISCLAIMER


class EvaluationResponse(BaseModel):
    fired: list[EventResponse]
    suppressed: list[dict]
    deduplicated: list[str]
    cooling_down: list[str]
    disclaimer: str = DISCLAIMER


def require_verified_session(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before managing alerts.",
            },
        )
    return active_session


def _validate_targets(db: Session, user_id: UUID, rule: RuleFields) -> None:
    if rule.instrument_id is not None and db.get(Instrument, rule.instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    if rule.portfolio_id is not None:
        get_owned_portfolio(db, user_id, rule.portfolio_id)
    if rule.scope == "position":
        assert rule.portfolio_id is not None and rule.instrument_id is not None
        portfolio = db.get(Portfolio, rule.portfolio_id)
        instrument = db.get(Instrument, rule.instrument_id)
        if portfolio is None or instrument is None:
            raise HTTPException(status_code=404, detail="Alert target not found")
        if instrument.currency != portfolio.base_currency and rule.rule_type in {
            "thesis_target_hit", "thesis_stop_hit", "unrealized_gain_pct_above",
            "position_weight_above", "price_above", "price_below", "pct_move_nd",
            "trailing_drawdown_pct", "rsi_threshold", "ma_cross",
        }:
            raise HTTPException(
                status_code=422,
                detail="Price alert currency differs from portfolio currency; no FX conversion is supported.",
            )


def _owned_rule(db: Session, user_id: UUID, rule_id: UUID) -> AlertRule:
    rule = db.scalar(
        select(AlertRule).where(AlertRule.id == rule_id, AlertRule.user_id == user_id)
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return rule


def _event_response(event: AlertEvent, severity: str, rule_type: str) -> EventResponse:
    return EventResponse(
        id=event.id, rule_id=event.rule_id, instrument_id=event.instrument_id,
        fired_at=event.fired_at, dedupe_key=event.dedupe_key, evidence=event.evidence,
        status=event.status, severity=severity, rule_type=rule_type,
    )


@router.get("/rules", response_model=list[RuleResponse])
def list_rules(
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[AlertRule]:
    return list(
        db.scalars(
            select(AlertRule)
            .where(AlertRule.user_id == active_session.user_id)
            .order_by(AlertRule.created_at.desc())
        )
    )


@router.post("/rules", response_model=RuleResponse, status_code=201)
def create_rule(
    payload: RuleCreate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AlertRule:
    _validate_targets(db, active_session.user_id, payload)
    now = datetime.now(timezone.utc)
    rule = AlertRule(
        user_id=active_session.user_id,
        scope=payload.scope,
        instrument_id=payload.instrument_id,
        portfolio_id=payload.portfolio_id,
        rule_type=payload.rule_type,
        params=payload.params,
        severity=payload.severity,
        cooldown_seconds=payload.cooldown_seconds,
        enabled=payload.enabled,
        created_at=now,
        updated_at=now,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/rules/{rule_id}", response_model=RuleResponse)
def get_rule(
    rule_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> AlertRule:
    return _owned_rule(db, active_session.user_id, rule_id)


@router.patch("/rules/{rule_id}", response_model=RuleResponse)
def update_rule(
    rule_id: UUID,
    payload: RuleUpdate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AlertRule:
    rule = _owned_rule(db, active_session.user_id, rule_id)
    merged = {
        "scope": rule.scope,
        "instrument_id": rule.instrument_id,
        "portfolio_id": rule.portfolio_id,
        "rule_type": rule.rule_type,
        "params": rule.params,
        "severity": rule.severity,
        "cooldown_seconds": rule.cooldown_seconds,
        "enabled": rule.enabled,
        **payload.model_dump(exclude_unset=True),
    }
    try:
        validated = RuleFields.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _validate_targets(db, active_session.user_id, validated)
    for field in (
        "scope", "instrument_id", "portfolio_id", "rule_type", "params",
        "severity", "cooldown_seconds", "enabled",
    ):
        setattr(rule, field, getattr(validated, field))
    rule.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(
    rule_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> None:
    db.delete(_owned_rule(db, active_session.user_id, rule_id))
    db.commit()


@router.get("/events", response_model=list[EventResponse])
def list_events(
    status: Literal["new", "read", "dismissed", "actioned"] | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[EventResponse]:
    query = (
        select(AlertEvent, AlertRule.severity, AlertRule.rule_type)
        .join(AlertRule, AlertRule.id == AlertEvent.rule_id)
        .where(AlertEvent.user_id == active_session.user_id)
    )
    if status is not None:
        query = query.where(AlertEvent.status == status)
    return [
        _event_response(event, severity, rule_type)
        for event, severity, rule_type in db.execute(
            query.order_by(AlertEvent.fired_at.desc()).limit(limit)
        )
    ]


@router.patch("/events/{event_id}", response_model=EventResponse)
def update_event_status(
    event_id: UUID,
    payload: EventStatusUpdate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> EventResponse:
    row = db.execute(
        select(AlertEvent, AlertRule.severity, AlertRule.rule_type)
        .join(AlertRule, AlertRule.id == AlertEvent.rule_id)
        .where(AlertEvent.id == event_id, AlertEvent.user_id == active_session.user_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Alert event not found")
    event, severity, rule_type = row
    event.status = payload.status
    db.commit()
    return _event_response(event, severity, rule_type)


@router.post("/evaluate", response_model=EvaluationResponse)
def evaluate(
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> EvaluationResponse:
    result = evaluate_rules(db, active_session.user_id)
    events = []
    for event in result["fired"]:
        rule = db.get(AlertRule, event.rule_id)
        if rule is not None:
            events.append(_event_response(event, rule.severity, rule.rule_type))
    return EvaluationResponse(
        fired=events,
        suppressed=result["suppressed"],
        deduplicated=result["deduplicated"],
        cooling_down=result["cooling_down"],
    )
