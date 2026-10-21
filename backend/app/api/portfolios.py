from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_csrf
from app.db import get_db
from app.models import Portfolio, User, UserSession

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_currency: str = Field(pattern=r"^[A-Za-z]{3}$")

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Portfolio name cannot be blank")
        return value

    @field_validator("base_currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        currency = value.upper()
        if currency not in {"USD", "INR"}:
            raise ValueError("Only USD and INR portfolio currencies are currently supported")
        return currency


class PortfolioResponse(BaseModel):
    id: UUID
    name: str
    base_currency: str
    created_at: datetime


@router.post("", response_model=PortfolioResponse, status_code=201)
def create_portfolio(
    payload: PortfolioCreate,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = Portfolio(
        user_id=user.id,
        name=payload.name,
        base_currency=payload.base_currency,
        created_at=datetime.now(timezone.utc),
    )
    db.add(portfolio)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="A portfolio with this name already exists") from error
    db.refresh(portfolio)
    return portfolio


@router.get("", response_model=list[PortfolioResponse])
def list_portfolios(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(Portfolio).where(Portfolio.user_id == user.id).order_by(Portfolio.created_at)
    ).all()


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
def get_portfolio(
    portfolio_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    portfolio = db.scalar(
        select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user.id)
    )
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio