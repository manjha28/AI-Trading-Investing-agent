from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounting import (
    CorporateActionAdjustment,
    LedgerError,
    QUANTITY_QUANTUM,
    calculate_ledger,
    money,
)
from app.api.auth import get_active_session, require_csrf
from app.api.portfolio_transactions import get_owned_portfolio, get_transactions
from app.corporate_action_service import get_portfolio_corporate_actions
from app.db import get_db
from app.models import (
    CorporateAction,
    CorporateActionApplication,
    Instrument,
    PortfolioTransaction,
    UserSession,
)

router = APIRouter(prefix="/corporate-actions", tags=["corporate actions"])


class CorporateActionCreate(BaseModel):
    instrument_id: UUID
    action_type: Literal["split", "bonus", "dividend"]
    ex_date: date
    ratio: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)

    @model_validator(mode="after")
    def validate_action_value(self):
        if self.action_type in {"split", "bonus"}:
            if self.ratio is None or self.amount is not None:
                raise ValueError("Split and bonus actions require ratio and no amount")
        elif self.amount is None or self.ratio is not None:
            raise ValueError("Dividend actions require a per-share amount and no ratio")
        return self


class CorporateActionResponse(BaseModel):
    id: UUID
    instrument_id: UUID
    symbol: str
    action_type: str
    ex_date: date
    ratio: Decimal | None
    amount: Decimal | None
    source: str
    created_at: datetime


class CorporateActionApplicationResponse(BaseModel):
    id: UUID
    corporate_action_id: UUID
    instrument_id: UUID
    symbol: str
    action_type: str
    ex_date: date
    ratio: Decimal | None
    amount: Decimal | None
    source: str
    applied_at: datetime
    reversed_at: datetime | None
    affected_quantity: Decimal
    quantity_adjustment: Decimal
    cash_adjustment: Decimal
    active: bool


def require_verified_session(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before managing corporate actions.",
            },
        )
    return active_session


def _action_response(action: CorporateAction, symbol: str) -> CorporateActionResponse:
    return CorporateActionResponse(
        id=action.id,
        instrument_id=action.instrument_id,
        symbol=symbol,
        action_type=action.action_type,
        ex_date=action.ex_date,
        ratio=action.ratio,
        amount=action.amount,
        source=action.source,
        created_at=action.created_at,
    )


def _application_response(
    application: CorporateActionApplication,
    action: CorporateAction,
    symbol: str,
) -> CorporateActionApplicationResponse:
    return CorporateActionApplicationResponse(
        id=application.id,
        corporate_action_id=action.id,
        instrument_id=action.instrument_id,
        symbol=symbol,
        action_type=action.action_type,
        ex_date=action.ex_date,
        ratio=action.ratio,
        amount=action.amount,
        source=action.source,
        applied_at=application.applied_at,
        reversed_at=application.reversed_at,
        affected_quantity=application.affected_quantity,
        quantity_adjustment=application.quantity_adjustment,
        cash_adjustment=application.cash_adjustment,
        active=application.reversed_at is None,
    )


def _get_owned_action(db: Session, user_id: UUID, action_id: UUID) -> CorporateAction:
    action = db.scalar(
        select(CorporateAction).where(
            CorporateAction.id == action_id,
            CorporateAction.user_id == user_id,
        )
    )
    if action is None:
        raise HTTPException(status_code=404, detail="Corporate action not found")
    return action


def _position_before_ex_date(
    db: Session,
    portfolio_id: UUID,
    user_id: UUID,
    action: CorporateAction,
    symbol: str,
):
    transactions = [
        transaction
        for transaction in get_transactions(db, portfolio_id)
        if (
            transaction.occurred_at.replace(tzinfo=timezone.utc)
            if transaction.occurred_at.tzinfo is None
            else transaction.occurred_at.astimezone(timezone.utc)
        ).date() < action.ex_date
    ]
    previous_actions = [
        applied
        for applied in get_portfolio_corporate_actions(
            db, portfolio_id, user_id, through_date=action.ex_date
        )
        if applied.ex_date < action.ex_date
    ]
    return calculate_ledger(transactions, previous_actions).positions.get(symbol)


def _ensure_action_instrument_matches(
    db: Session,
    portfolio_id: UUID,
    action: CorporateAction,
    symbol: str,
) -> None:
    linked_instruments = set(
        db.scalars(
            select(PortfolioTransaction.instrument_id)
            .where(
                PortfolioTransaction.portfolio_id == portfolio_id,
                PortfolioTransaction.symbol == symbol,
                PortfolioTransaction.transaction_type.in_(("buy", "sell")),
            )
        ).all()
    )
    if linked_instruments != {action.instrument_id}:
        raise HTTPException(
            status_code=409,
            detail="Link every transaction for this holding to the selected instrument before applying its corporate action.",
        )


@router.get("", response_model=list[CorporateActionResponse])
def list_corporate_actions(
    instrument_id: UUID | None = Query(default=None),
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
):
    statement = (
        select(CorporateAction, Instrument.symbol)
        .join(Instrument, Instrument.id == CorporateAction.instrument_id)
        .where(CorporateAction.user_id == active_session.user_id)
        .order_by(CorporateAction.ex_date.desc(), CorporateAction.created_at.desc())
    )
    if instrument_id is not None:
        statement = statement.where(CorporateAction.instrument_id == instrument_id)
    return [
        _action_response(action, symbol)
        for action, symbol in db.execute(statement).all()
    ]


@router.post("", response_model=CorporateActionResponse, status_code=201)
def create_corporate_action(
    payload: CorporateActionCreate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    instrument = db.get(Instrument, payload.instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    action = CorporateAction(
        user_id=active_session.user_id,
        instrument_id=instrument.id,
        action_type=payload.action_type,
        ex_date=payload.ex_date,
        ratio=payload.ratio,
        amount=payload.amount,
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return _action_response(action, instrument.symbol)


@router.get(
    "/portfolios/{portfolio_id}/applications",
    response_model=list[CorporateActionApplicationResponse],
)
def list_portfolio_action_applications(
    portfolio_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
):
    get_owned_portfolio(db, active_session.user_id, portfolio_id)
    rows = db.execute(
        select(CorporateActionApplication, CorporateAction, Instrument.symbol)
        .join(CorporateAction, CorporateAction.id == CorporateActionApplication.corporate_action_id)
        .join(Instrument, Instrument.id == CorporateAction.instrument_id)
        .where(
            CorporateActionApplication.user_id == active_session.user_id,
            CorporateActionApplication.portfolio_id == portfolio_id,
            CorporateAction.user_id == active_session.user_id,
        )
        .order_by(CorporateActionApplication.applied_at.desc())
    ).all()
    return [
        _application_response(application, action, symbol)
        for application, action, symbol in rows
    ]


@router.post(
    "/portfolios/{portfolio_id}/{action_id}/apply",
    response_model=CorporateActionApplicationResponse,
)
def apply_corporate_action(
    portfolio_id: UUID,
    action_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, active_session.user_id, portfolio_id, lock=True)
    action = _get_owned_action(db, active_session.user_id, action_id)
    now = datetime.now(timezone.utc)
    if action.ex_date > now.date():
        raise HTTPException(status_code=422, detail="A corporate action cannot be applied before its ex-date")
    instrument = db.get(Instrument, action.instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    if action.action_type == "dividend" and instrument.currency != portfolio.base_currency:
        raise HTTPException(
            status_code=422,
            detail="Dividend currency must match the portfolio currency; FX conversion is not supported.",
        )
    existing = db.scalar(
        select(CorporateActionApplication).where(
            CorporateActionApplication.user_id == active_session.user_id,
            CorporateActionApplication.portfolio_id == portfolio.id,
            CorporateActionApplication.corporate_action_id == action.id,
            CorporateActionApplication.reversed_at.is_(None),
        )
    )
    if existing is not None:
        return _application_response(existing, action, instrument.symbol)

    same_date_application = db.scalar(
        select(CorporateActionApplication.id)
        .join(CorporateAction, CorporateAction.id == CorporateActionApplication.corporate_action_id)
        .where(
            CorporateActionApplication.user_id == active_session.user_id,
            CorporateActionApplication.portfolio_id == portfolio.id,
            CorporateActionApplication.reversed_at.is_(None),
            CorporateAction.instrument_id == action.instrument_id,
            CorporateAction.ex_date == action.ex_date,
            CorporateAction.id != action.id,
        )
    )
    if same_date_application is not None:
        raise HTTPException(
            status_code=409,
            detail="Another active corporate action is already applied to this instrument on the ex-date.",
        )

    _ensure_action_instrument_matches(db, portfolio.id, action, instrument.symbol)
    position = _position_before_ex_date(
        db, portfolio.id, active_session.user_id, action, instrument.symbol
    )
    if position is None or position.quantity <= 0:
        raise HTTPException(
            status_code=409,
            detail="No shares of this instrument were held before the ex-date.",
        )

    affected_quantity = position.quantity
    if action.action_type == "split":
        if action.ratio is None:
            raise HTTPException(status_code=422, detail="Split action ratio is missing")
        quantity_adjustment = (affected_quantity * (action.ratio - Decimal("1"))).quantize(
            QUANTITY_QUANTUM
        )
        cash_adjustment = Decimal("0.00")
    elif action.action_type == "bonus":
        if action.ratio is None:
            raise HTTPException(status_code=422, detail="Bonus action ratio is missing")
        quantity_adjustment = (affected_quantity * action.ratio).quantize(QUANTITY_QUANTUM)
        cash_adjustment = Decimal("0.00")
    else:
        if action.amount is None:
            raise HTTPException(status_code=422, detail="Dividend action amount is missing")
        quantity_adjustment = Decimal("0").quantize(QUANTITY_QUANTUM)
        cash_adjustment = money(affected_quantity * action.amount)

    application = CorporateActionApplication(
        user_id=active_session.user_id,
        portfolio_id=portfolio.id,
        corporate_action_id=action.id,
        applied_at=now,
        affected_quantity=affected_quantity,
        quantity_adjustment=quantity_adjustment,
        cash_adjustment=cash_adjustment,
    )
    db.add(application)
    try:
        db.flush()
        adjustments = [
            *(
                applied
                for applied in get_portfolio_corporate_actions(
                    db, portfolio.id, active_session.user_id
                )
                if applied.id != action.id
            ),
            CorporateActionAdjustment(
                id=action.id,
                symbol=instrument.symbol,
                action_type=action.action_type,
                ex_date=action.ex_date,
                ratio=action.ratio,
                amount=action.amount,
            ),
        ]
        calculate_ledger(get_transactions(db, portfolio.id), adjustments)
        db.commit()
    except LedgerError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Action would invalidate the portfolio ledger: {error}") from error
    except IntegrityError as error:
        db.rollback()
        concurrent = db.scalar(
            select(CorporateActionApplication).where(
                CorporateActionApplication.user_id == active_session.user_id,
                CorporateActionApplication.portfolio_id == portfolio.id,
                CorporateActionApplication.corporate_action_id == action.id,
                CorporateActionApplication.reversed_at.is_(None),
            )
        )
        if concurrent is not None:
            return _application_response(concurrent, action, instrument.symbol)
        raise HTTPException(status_code=409, detail="Corporate action application conflicts with an existing adjustment") from error
    db.refresh(application)
    return _application_response(application, action, instrument.symbol)


@router.post(
    "/portfolios/{portfolio_id}/applications/{application_id}/reverse",
    response_model=CorporateActionApplicationResponse,
)
def reverse_corporate_action(
    portfolio_id: UUID,
    application_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, active_session.user_id, portfolio_id, lock=True)
    application = db.scalar(
        select(CorporateActionApplication).where(
            CorporateActionApplication.id == application_id,
            CorporateActionApplication.user_id == active_session.user_id,
            CorporateActionApplication.portfolio_id == portfolio.id,
        )
    )
    if application is None:
        raise HTTPException(status_code=404, detail="Corporate action application not found")
    action = _get_owned_action(db, active_session.user_id, application.corporate_action_id)
    instrument = db.get(Instrument, action.instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    if application.reversed_at is None:
        if action.action_type in {"split", "bonus"}:
            ex_date_transactions = get_transactions(db, portfolio.id)
            if any(
                transaction.symbol == instrument.symbol
                and transaction.transaction_type in {"buy", "sell"}
                and (
                    transaction.occurred_at.replace(tzinfo=timezone.utc)
                    if transaction.occurred_at.tzinfo is None
                    else transaction.occurred_at.astimezone(timezone.utc)
                ).date()
                >= action.ex_date
                for transaction in ex_date_transactions
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A split or bonus cannot be reversed after later trades; correct those transactions first.",
                )
        application.reversed_at = datetime.now(timezone.utc)
        try:
            calculate_ledger(
                get_transactions(db, portfolio.id),
                get_portfolio_corporate_actions(
                    db, portfolio.id, active_session.user_id
                ),
            )
            db.commit()
        except LedgerError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Reversal would invalidate the portfolio ledger: {error}",
            ) from error
    db.refresh(application)
    return _application_response(application, action, instrument.symbol)
