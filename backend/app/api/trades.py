from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_csrf
from app.db import get_db
from app.models import DhanTradeMirror, JournalTrade, User, UserSession

router = APIRouter(prefix="/trades", tags=["trades"])
CENT = Decimal("0.01")


class TradeCreate(BaseModel):
    asset_class: Literal["equity", "future", "option", "crypto"]
    instrument: str = Field(min_length=1, max_length=64)
    currency: Literal["USD", "INR"] = "INR"
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


def _dhan_trade_rows(db: Session, user_id: UUID) -> list[DhanTradeMirror]:
    rows = list(
        db.scalars(
            select(DhanTradeMirror)
            .where(
                DhanTradeMirror.user_id == user_id,
                DhanTradeMirror.source.in_(("dhan", "dhan_csv")),
            )
            .order_by(DhanTradeMirror.trade_at, DhanTradeMirror.trade_id)
        ).all()
    )
    selected: dict[str, DhanTradeMirror] = {}
    for row in rows:
        current = selected.get(row.trade_id)
        if current is None or (row.source == "dhan" and current.source == "dhan_csv"):
            selected[row.trade_id] = row
    return sorted(selected.values(), key=lambda row: (row.trade_at, row.trade_id))


def _realized_dhan_pnl(
    rows: list[DhanTradeMirror],
) -> tuple[dict[str, Decimal | None], int]:
    lots_by_instrument: dict[str, list[dict[str, Decimal | int | None]]] = {}
    realized: dict[str, Decimal | None] = {}
    for row in rows:
        quantity = row.quantity
        remaining = quantity
        direction = 1 if row.side == "BUY" else -1
        instrument_key = row.security_id or f"{row.exchange_segment}:{row.symbol or ''}"
        lots = lots_by_instrument.setdefault(instrument_key, [])
        close_fee_known = row.charges is not None
        close_fee_remaining = row.charges
        realized_amount = Decimal("0")
        matched_any = False
        pnl_known = close_fee_known
        index = 0
        while remaining > 0 and index < len(lots):
            lot = lots[index]
            lot_direction = int(lot["direction"] or 0)
            if lot_direction == direction:
                index += 1
                continue
            lot_quantity = lot["quantity"]
            if not isinstance(lot_quantity, Decimal) or lot_quantity <= 0:
                index += 1
                continue
            matched_quantity = min(remaining, lot_quantity)
            lot_fee = lot["fee_remaining"]
            lot_fee_known = bool(lot["fee_known"])
            opening_fee = (
                (lot_fee * matched_quantity / lot_quantity)
                if isinstance(lot_fee, Decimal)
                else None
            )
            close_fee = (
                (row.charges * matched_quantity / quantity)
                if row.charges is not None
                else None
            )
            gross = (
                (row.price - Decimal(str(lot["price"])))
                * matched_quantity
                * lot_direction
            )
            if not lot_fee_known or opening_fee is None or close_fee is None:
                pnl_known = False
            else:
                realized_amount += gross - opening_fee - close_fee
            if isinstance(lot_fee, Decimal) and opening_fee is not None:
                lot["fee_remaining"] = lot_fee - opening_fee
            if close_fee_remaining is not None and close_fee is not None:
                close_fee_remaining -= close_fee
            lot["quantity"] = lot_quantity - matched_quantity
            remaining -= matched_quantity
            matched_any = True
            if lot["quantity"] == 0:
                lots.pop(index)
            else:
                index += 1
        if matched_any:
            realized[str(row.id)] = (
                realized_amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
                if pnl_known
                else None
            )
        if remaining > 0:
            remaining_fee = (
                row.charges * remaining / quantity
                if row.charges is not None
                else None
            )
            lots.append(
                {
                    "direction": direction,
                    "quantity": remaining,
                    "price": row.price,
                    "fee_remaining": remaining_fee,
                    "fee_known": row.charges is not None,
                }
            )
    return realized, sum(len(lots) for lots in lots_by_instrument.values())


def _journal_trade_response(
    trade: JournalTrade, *, source: str = "manual"
) -> dict:
    return {
        **{
            key: getattr(trade, key)
            for key in TradeResponse.model_fields
            if key != "net_pnl"
        },
        "net_pnl": net_pnl(trade),
        "source": source,
        "source_side": None,
        "calendar_date": (
            (trade.exit_time or trade.entry_time)
            .astimezone(ZoneInfo("Asia/Kolkata"))
            .date()
            .isoformat()
        ),
    }


def _journal_trade_rows(
    db: Session, user_id: UUID, currency: str
) -> tuple[list[dict], dict[str, Decimal | None], int]:
    manual = list(
        db.scalars(
            select(JournalTrade)
            .where(
                JournalTrade.user_id == user_id,
                JournalTrade.currency == currency,
            )
            .order_by(JournalTrade.entry_time.desc(), JournalTrade.created_at.desc())
        ).all()
    )
    dhan_rows = _dhan_trade_rows(db, user_id) if currency == "INR" else []
    realized, open_lots = _realized_dhan_pnl(dhan_rows)
    broker = []
    for row in dhan_rows:
        side = "long" if row.side == "BUY" else "short"
        broker.append(
            {
                "id": str(row.id),
                "provider_trade_id": row.provider_trade_id or row.trade_id,
                "asset_class": "equity",
                "instrument": row.symbol or row.security_id or "Unknown instrument",
                "currency": "INR",
                "side": side,
                "source_side": row.side,
                "quantity": row.quantity,
                "multiplier": Decimal("1"),
                "entry_price": row.price,
                "exit_price": None,
                "entry_time": row.trade_at,
                "exit_time": None,
                "fees": row.charges,
                "planned_risk": None,
                "strategy": None,
                "setup": None,
                "notes": None,
                "tags": [],
                "option_type": None,
                "strike": None,
                "expiry": None,
                "net_pnl": realized.get(str(row.id)),
                "source": row.source,
                "calendar_date": row.trade_at.astimezone(
                    ZoneInfo("Asia/Kolkata")
                ).date().isoformat(),
            }
        )
    manual_rows = [_journal_trade_response(trade) for trade in manual]
    combined = manual_rows + broker
    combined.sort(
        key=lambda row: row["entry_time"],
        reverse=True,
    )
    return combined, realized, open_lots


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


@router.get("/journal")
def journal_trade_history(
    currency: Literal["USD", "INR"] = Query(default="INR"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows, _, _ = _journal_trade_rows(db, user.id, currency)
    return rows


@router.get("/analytics/journal")
def journal_trade_analytics(
    currency: Literal["USD", "INR"] = Query(default="INR"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    rows, realized_broker, open_broker_lots = _journal_trade_rows(
        db, user.id, currency
    )
    closed_events: list[tuple[datetime, str, Decimal | None]] = []
    open_manual_count = 0
    daily: dict[str, dict[str, Any]] = {}
    for row in rows:
        activity_date = row["calendar_date"]
        daily.setdefault(
            activity_date,
            {
                "date": activity_date,
                "trade_count": 0,
                "realized_pnl": Decimal("0"),
                "realized_count": 0,
                "complete": True,
            },
        )["trade_count"] += 1
        source = row["source"]
        if source == "manual":
            if row["exit_time"] is None:
                open_manual_count += 1
                continue
            closed_events.append(
                (
                    row["exit_time"],
                    row["calendar_date"],
                    row["net_pnl"],
                )
            )
        elif str(row["id"]) in realized_broker:
            closed_events.append(
                (
                    row["entry_time"],
                    row["calendar_date"],
                    realized_broker[str(row["id"])],
                )
            )
    closed_events.sort(key=lambda item: item[0])
    known_results = [result for _, _, result in closed_events if result is not None]
    unknown_count = len(closed_events) - len(known_results)
    wins = [result for result in known_results if result > 0]
    losses = [result for result in known_results if result < 0]
    gross_wins = sum(wins, Decimal("0"))
    gross_losses = -sum(losses, Decimal("0"))
    for _, activity_date, result in closed_events:
        item = daily[activity_date]
        item["realized_count"] += 1
        if result is None:
            item["complete"] = False
        else:
            item["realized_pnl"] += result
    for item in daily.values():
        if not item["complete"] or not item["realized_count"]:
            item["realized_pnl"] = None
        elif item["realized_pnl"] is not None:
            item["realized_pnl"] = item["realized_pnl"].quantize(
                CENT, rounding=ROUND_HALF_EVEN
            )
        del item["realized_count"]
        del item["complete"]

    complete = unknown_count == 0
    net_total = sum(known_results, Decimal("0")).quantize(
        CENT, rounding=ROUND_HALF_EVEN
    )
    max_drawdown = Decimal("0")
    equity = Decimal("0")
    peak = Decimal("0")
    for result in known_results:
        equity += result
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    all_closed_known = complete and bool(closed_events)
    return {
        "currency": currency,
        "closed_trades": len(closed_events),
        "open_trades": open_manual_count + open_broker_lots,
        "trades_logged": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "unpriced_closed_trades": unknown_count,
        "win_rate": (
            (Decimal(len(wins)) / Decimal(len(closed_events)) * 100).quantize(
                CENT, rounding=ROUND_HALF_EVEN
            )
            if all_closed_known
            else None
        ),
        "net_pnl": net_total if all_closed_known else None,
        "expectancy": (
            (net_total / Decimal(len(closed_events))).quantize(
                CENT, rounding=ROUND_HALF_EVEN
            )
            if all_closed_known
            else None
        ),
        "profit_factor": (
            (gross_wins / gross_losses).quantize(
                CENT, rounding=ROUND_HALF_EVEN
            )
            if all_closed_known and gross_losses
            else None
        ),
        "max_drawdown": (
            max_drawdown.quantize(CENT, rounding=ROUND_HALF_EVEN)
            if all_closed_known
            else None
        ),
        "daily": sorted(daily.values(), key=lambda item: item["date"]),
    }


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