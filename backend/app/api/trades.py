from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_csrf
from app.db import get_db
from app.models import JournalTrade, User, UserSession

router = APIRouter(prefix="/trades", tags=["trades"])
CENT = Decimal("0.01")


class TradeCreate(BaseModel):
    asset_class: Literal["equity", "future", "option", "crypto"]
    instrument: str = Field(min_length=1, max_length=64)
    currency: Literal["USD", "INR"] = "USD"
    side: Literal["long", "short"]
    quantity: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    multiplier: Decimal = Field(default=Decimal("1"), gt=0, max_digits=18, decimal_places=8)
    entry_price: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    exit_price: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    entry_time: datetime
    exit_time: datetime | None = None
    fees: Decimal = Field(default=Decimal("0"), ge=0, max_digits=24, decimal_places=2)
    planned_risk: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=2)
    strategy: str | None = Field(default=None, max_length=100)
    setup: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    option_type: Literal["call", "put"] | None = None
    strike: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    expiry: datetime | None = None

    @field_validator("entry_time", "exit_time", "expiry")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("trade timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_trade(self):
        self.instrument = self.instrument.strip().upper()
        if not self.instrument:
            raise ValueError("instrument cannot be blank")
        if (self.exit_price is None) != (self.exit_time is None):
            raise ValueError("exit_price and exit_time must both be set for a closed trade")
        if self.exit_time and self.exit_time < self.entry_time:
            raise ValueError("exit_time cannot precede entry_time")
        if self.asset_class == "option" and (
            self.option_type is None or self.strike is None or self.expiry is None
        ):
            raise ValueError("options require option_type, strike, and expiry")
        if self.asset_class != "option" and any(
            value is not None for value in (self.option_type, self.strike, self.expiry)
        ):
            raise ValueError("option contract details are only valid for options")
        self.tags = list(dict.fromkeys(tag.strip() for tag in self.tags if tag.strip()))
        return self


class TradeResponse(BaseModel):
    id: UUID
    asset_class: str
    instrument: str
    currency: str
    side: str
    quantity: Decimal
    multiplier: Decimal
    entry_price: Decimal
    exit_price: Decimal | None
    entry_time: datetime
    exit_time: datetime | None
    fees: Decimal
    planned_risk: Decimal | None
    strategy: str | None
    setup: str | None
    notes: str | None
    tags: list[str]
    option_type: str | None
    strike: Decimal | None
    expiry: datetime | None
    net_pnl: Decimal | None


class TradeAnalytics(BaseModel):
    currency: str
    closed_trades: int
    open_trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    net_pnl: Decimal
    expectancy: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal


class TradeBreakdownRow(BaseModel):
    key: str
    closed_trades: int
    wins: int
    losses: int
    net_pnl: Decimal
    expectancy: Decimal | None
    profit_factor: Decimal | None


def net_pnl(trade: JournalTrade) -> Decimal | None:
    if trade.exit_price is None:
        return None
    direction = Decimal("1") if trade.side == "long" else Decimal("-1")
    gross = direction * (trade.exit_price - trade.entry_price) * trade.quantity * trade.multiplier
    return (gross - trade.fees).quantize(CENT, rounding=ROUND_HALF_EVEN)


def _breakdown_row(key: str, trades: list[JournalTrade]) -> TradeBreakdownRow:
    results = [result for trade in trades if (result := net_pnl(trade)) is not None]
    wins = [result for result in results if result > 0]
    losses = [result for result in results if result < 0]
    gross_losses = -sum(losses, Decimal("0"))
    net_total = sum(results, Decimal("0")).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return TradeBreakdownRow(
        key=key,
        closed_trades=len(results),
        wins=len(wins),
        losses=len(losses),
        net_pnl=net_total,
        expectancy=(net_total / len(results)).quantize(CENT, rounding=ROUND_HALF_EVEN)
        if results
        else None,
        profit_factor=(
            (sum(wins, Decimal("0")) / gross_losses).quantize(CENT, rounding=ROUND_HALF_EVEN)
            if gross_losses
            else None
        ),
    )


def _response(trade: JournalTrade) -> dict:
    return {**{key: getattr(trade, key) for key in TradeResponse.model_fields if key != "net_pnl"}, "net_pnl": net_pnl(trade)}


@router.post("", response_model=TradeResponse, status_code=201)
def create_trade(
    payload: TradeCreate,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    trade = JournalTrade(
        **payload.model_dump(),
        user_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return _response(trade)


@router.get("", response_model=list[TradeResponse])
def list_trades(
    asset_class: Literal["equity", "future", "option", "crypto"] | None = None,
    instrument: str | None = Query(default=None, max_length=64),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(JournalTrade).where(JournalTrade.user_id == user.id)
    if asset_class:
        statement = statement.where(JournalTrade.asset_class == asset_class)
    if instrument:
        statement = statement.where(JournalTrade.instrument == instrument.strip().upper())
    trades = db.scalars(
        statement.order_by(JournalTrade.entry_time.desc(), JournalTrade.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_response(trade) for trade in trades]


@router.put("/{trade_id}", response_model=TradeResponse)
def update_trade(
    trade_id: UUID,
    payload: TradeCreate,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    trade = db.scalar(
        select(JournalTrade).where(JournalTrade.id == trade_id, JournalTrade.user_id == user.id)
    )
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    for key, value in payload.model_dump().items():
        setattr(trade, key, value)
    db.commit()
    db.refresh(trade)
    return _response(trade)


@router.delete("/{trade_id}", status_code=204)
def delete_trade(
    trade_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    trade = db.scalar(select(JournalTrade).where(JournalTrade.id == trade_id, JournalTrade.user_id == user.id))
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    db.delete(trade)
    db.commit()


@router.get("/analytics/summary", response_model=TradeAnalytics)
def trade_analytics(
    currency: Literal["USD", "INR"] = Query(default="USD"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trades = list(
        db.scalars(
            select(JournalTrade)
            .where(JournalTrade.user_id == user.id, JournalTrade.currency == currency)
            .order_by(JournalTrade.exit_time, JournalTrade.created_at)
        ).all()
    )
    closed = [(trade, net_pnl(trade)) for trade in trades if trade.exit_price is not None]
    results = [result for _, result in closed if result is not None]
    wins = [result for result in results if result > 0]
    losses = [result for result in results if result < 0]
    net_total = sum(results, Decimal("0")).quantize(CENT)
    gross_wins = sum(wins, Decimal("0"))
    gross_losses = -sum(losses, Decimal("0"))
    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for result in results:
        equity += result
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return TradeAnalytics(
        currency=currency,
        closed_trades=len(results),
        open_trades=len(trades) - len(results),
        wins=len(wins),
        losses=len(losses),
        win_rate=(Decimal(len(wins)) / Decimal(len(results)) * 100).quantize(CENT) if results else None,
        net_pnl=net_total,
        expectancy=(net_total / Decimal(len(results))).quantize(CENT) if results else None,
        profit_factor=(gross_wins / gross_losses).quantize(CENT) if gross_losses else None,
        max_drawdown=max_drawdown.quantize(CENT),
    )


@router.get("/analytics/breakdown", response_model=list[TradeBreakdownRow])
def trade_analytics_breakdown(
    currency: Literal["USD", "INR"] = Query(default="USD"),
    group_by: Literal["strategy", "instrument", "asset_class"] = Query(default="strategy"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trades = list(
        db.scalars(
            select(JournalTrade)
            .where(JournalTrade.user_id == user.id, JournalTrade.currency == currency)
            .order_by(JournalTrade.exit_time, JournalTrade.created_at)
        ).all()
    )
    groups: dict[str, list[JournalTrade]] = {}
    for trade in trades:
        raw_key = getattr(trade, group_by)
        key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else "Unspecified"
        groups.setdefault(key, []).append(trade)
    return [
        _breakdown_row(key, group)
        for key, group in sorted(groups.items(), key=lambda item: item[0].casefold())
    ]