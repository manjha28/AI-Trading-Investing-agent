from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting import CorporateActionAdjustment
from app.models import (
    CorporateAction,
    CorporateActionApplication,
    Instrument,
)


def get_portfolio_corporate_actions(
    db: Session,
    portfolio_id: UUID,
    user_id: UUID,
    *,
    through_date: date | None = None,
) -> list[CorporateActionAdjustment]:
    statement = (
        select(CorporateAction, Instrument.symbol)
        .join(
            CorporateActionApplication,
            CorporateActionApplication.corporate_action_id == CorporateAction.id,
        )
        .join(Instrument, Instrument.id == CorporateAction.instrument_id)
        .where(
            CorporateActionApplication.portfolio_id == portfolio_id,
            CorporateActionApplication.user_id == user_id,
            CorporateAction.user_id == user_id,
            CorporateActionApplication.reversed_at.is_(None),
        )
        .order_by(CorporateAction.ex_date, CorporateAction.id)
    )
    if through_date is not None:
        statement = statement.where(CorporateAction.ex_date <= through_date)
    return [
        CorporateActionAdjustment(
            id=action.id,
            symbol=symbol,
            action_type=action.action_type,
            ex_date=action.ex_date,
            ratio=action.ratio,
            amount=action.amount,
        )
        for action, symbol in db.execute(statement).all()
    ]
