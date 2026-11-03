from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import re
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
from app.services.strategy_contract import registry_payload
from app.services.trade_metrics import net_pnl, summarize_net_pnls

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


class TradeNoteUpdate(BaseModel):
    notes: str | None = Field(default=None, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip()[:64] for value in values if value.strip()))


class TradeAnnotationUpdate(BaseModel):
    notes: str | None = Field(default=None, max_length=4000)
    tags: list[str] | None = Field(default=None, max_length=20)
    strategy: str | None = Field(default=None, max_length=100)
    setup: str | None = Field(default=None, max_length=100)

    @field_validator("tags")
    @classmethod
    def normalize_annotation_tags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return list(dict.fromkeys(value.strip()[:64] for value in values if value.strip()))


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


def _breakdown_row(key: str, trades: list[JournalTrade]) -> TradeBreakdownRow:
    results = [result for trade in trades if (result := net_pnl(trade)) is not None]
    return _breakdown_results(key, results)


def _breakdown_results(key: str, results: list[Decimal]) -> TradeBreakdownRow:
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


_DERIVATIVE_SEGMENTS = {"NSE_FNO", "BSE_FNO", "MCX_COMM"}
_OPTION_PATTERN = re.compile(
    r"\b(?P<day>\d{1,2})\s+(?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"\s+(?P<strike>\d+(?:\.\d+)?)\s+(?P<kind>CALL|PUT|CE|PE)\b",
    re.IGNORECASE,
)


def _contract_details(row: DhanTradeMirror) -> tuple[date | None, Decimal | None, str | None]:
    match = _OPTION_PATTERN.search(row.symbol or "")
    if not match:
        return None, None, None
    month = datetime.strptime(match.group("month").title(), "%b").month
    year = row.trade_at.year
    expiry = date(year, month, int(match.group("day")))
    if expiry < row.trade_at.date():
        expiry = date(year + 1, month, int(match.group("day")))
    kind = match.group("kind").upper()
    return expiry, Decimal(match.group("strike")), "call" if kind in {"CALL", "CE"} else "put"


def _instrument_identity(row: DhanTradeMirror) -> tuple[Any, ...]:
    segment = (row.exchange_segment or "").upper()
    symbol = (row.symbol or "").strip().upper()
    if symbol in {"", "0", "NONE", "UNKNOWN"}:
        symbol = ""
    if segment in _DERIVATIVE_SEGMENTS:
        expiry, strike, option_type = _contract_details(row)
        return segment, symbol or row.security_id, expiry, strike, option_type
    return segment, symbol or row.security_id


def _classify_segment(segment: str | None, symbol: str | None) -> str:
    normalized = (segment or "").upper()
    if normalized in {"NSE_EQ", "BSE_EQ"}:
        return "equity"
    if normalized in _DERIVATIVE_SEGMENTS:
        if normalized != "MCX_COMM" and _OPTION_PATTERN.search(symbol or ""):
            return "option"
        return "future"
    return "future"


def _match_dhan_fills(
    rows: list[DhanTradeMirror],
) -> dict[str, Any]:
    lots_by_instrument: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    realized_by_fill: dict[str, Decimal | None] = {}
    matches: list[dict[str, Any]] = []
    matched_fill_ids: set[str] = set()
    for row in rows:
        quantity = row.quantity
        remaining = quantity
        direction = 1 if row.side == "BUY" else -1
        identity = _instrument_identity(row)
        lots = lots_by_instrument.setdefault(identity, [])
        realized_amount = Decimal("0")
        pnl_known = row.charges is not None
        matched_any = False
        index = 0
        while remaining > 0 and index < len(lots):
            lot = lots[index]
            if lot["direction"] == direction:
                index += 1
                continue
            lot_quantity = lot["quantity"]
            matched_quantity = min(remaining, lot_quantity)
            opening_fee = (
                lot["fee_remaining"] * matched_quantity / lot_quantity
                if lot["fee_remaining"] is not None
                else None
            )
            closing_fee = (
                row.charges * matched_quantity / quantity
                if row.charges is not None
                else None
            )
            gross = (row.price - lot["price"]) * matched_quantity * lot["direction"]
            known = lot["fee_known"] and opening_fee is not None and closing_fee is not None
            net = (gross - opening_fee - closing_fee) if known else None
            matches.append({
                "entry_fill_id": str(lot["row"].id),
                "exit_fill_id": str(row.id),
                "identity": identity,
                "quantity": matched_quantity,
                "entry_time": lot["row"].trade_at,
                "exit_time": row.trade_at,
                "side": "long" if lot["direction"] == 1 else "short",
                "fees": (
                    opening_fee + closing_fee
                    if opening_fee is not None and closing_fee is not None
                    else None
                ),
                "net_pnl": net.quantize(CENT, rounding=ROUND_HALF_EVEN) if net is not None else None,
            })
            matched_fill_ids.update((str(lot["row"].id), str(row.id)))
            if net is None:
                pnl_known = False
            else:
                realized_amount += net
            if lot["fee_remaining"] is not None and opening_fee is not None:
                lot["fee_remaining"] -= opening_fee
            lot["quantity"] -= matched_quantity
            remaining -= matched_quantity
            matched_any = True
            if lot["quantity"] == 0:
                lots.pop(index)
            else:
                index += 1
        if matched_any:
            realized_by_fill[str(row.id)] = (
                realized_amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
                if pnl_known else None
            )
        if remaining > 0:
            remaining_fee = row.charges * remaining / quantity if row.charges is not None else None
            lots.append({
                "direction": direction,
                "quantity": remaining,
                "price": row.price,
                "fee_remaining": remaining_fee,
                "fee_known": row.charges is not None,
                "row": row,
            })

    as_of = datetime.now(timezone.utc).date()
    open_fill_ids: set[str] = set()
    expired_fill_ids: set[str] = set()
    open_positions: set[tuple[Any, ...]] = set()
    expired_positions: set[tuple[Any, ...]] = set()
    unattributed_fill_ids: set[str] = set()
    for identity, lots in lots_by_instrument.items():
        for lot in lots:
            row = lot["row"]
            fill_id = str(row.id)
            open_fill_ids.add(fill_id)
            symbol = (row.symbol or "").strip().upper()
            security_id = (row.security_id or "").strip()
            if symbol in {"", "0", "NONE", "UNKNOWN"} and security_id in {"", "0"}:
                unattributed_fill_ids.add(fill_id)
                continue
            position_key = (*identity, lot["direction"])
            expiry, _, _ = _contract_details(row)
            if row.exchange_segment in _DERIVATIVE_SEGMENTS and expiry is not None and expiry < as_of:
                expired_fill_ids.add(fill_id)
                expired_positions.add(position_key)
            else:
                open_positions.add(position_key)
    all_fill_ids = {str(row.id) for row in rows}
    unresolved_fill_ids = open_fill_ids | expired_fill_ids | unattributed_fill_ids
    closed_fill_ids = matched_fill_ids - unresolved_fill_ids
    return {
        "realized_by_fill": realized_by_fill,
        "matches": matches,
        "matched_fill_ids": closed_fill_ids,
        "open_fill_ids": open_fill_ids - expired_fill_ids - unattributed_fill_ids,
        "expired_fill_ids": expired_fill_ids,
        "unattributed_fill_ids": unattributed_fill_ids,
        "unmatched_fill_ids": all_fill_ids - closed_fill_ids - unresolved_fill_ids,
        "open_positions": len(open_positions),
        "expired_positions": len(expired_positions),
    }


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
        "annotation_id": str(trade.id),
        "source_side": None,
        "match_state": "manual",
        "position_status": "closed" if trade.exit_time is not None else "open",
        "calendar_date": (
            (trade.exit_time or trade.entry_time)
            .astimezone(ZoneInfo("Asia/Kolkata"))
            .date()
            .isoformat()
        ),
    }


def _journal_trade_rows(
    db: Session, user_id: UUID, currency: str
) -> tuple[list[dict], dict[str, Any]]:
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
    matching = _match_dhan_fills(dhan_rows)
    realized = matching["realized_by_fill"]
    broker = []
    for row in dhan_rows:
        side = "long" if row.side == "BUY" else "short"
        broker.append(
            {
                "id": str(row.id),
                "provider_trade_id": row.provider_trade_id or row.trade_id,
                "asset_class": _classify_segment(row.exchange_segment, row.symbol),
                "instrument": (
                    row.symbol if row.symbol and row.symbol.strip().upper() not in {"0", "NONE", "UNKNOWN"}
                    else row.security_id if row.security_id and row.security_id.strip() not in {"", "0"}
                    else "Unattributable fill"
                ),
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
                "strategy": row.strategy,
                "setup": row.setup,
                "notes": row.notes,
                "tags": row.tags or [],
                "option_type": _contract_details(row)[2],
                "strike": _contract_details(row)[1],
                "expiry": (
                    datetime.combine(_contract_details(row)[0], datetime.min.time(), tzinfo=timezone.utc)
                    if _contract_details(row)[0] is not None else None
                ),
                "net_pnl": realized.get(str(row.id)),
                "source": row.source,
                "annotation_id": str(row.id),
                "match_state": (
                    "expired" if str(row.id) in matching["expired_fill_ids"]
                    else "open lot" if str(row.id) in matching["open_fill_ids"]
                    else "unattributed" if str(row.id) in matching["unattributed_fill_ids"]
                    else "matched" if str(row.id) in matching["matched_fill_ids"]
                    else "unmatched"
                ),
                "position_status": (
                    "expired_outcome_unknown" if str(row.id) in matching["expired_fill_ids"]
                    else "open" if str(row.id) in matching["open_fill_ids"]
                    else "matched" if str(row.id) in matching["matched_fill_ids"]
                    else "unattributed" if str(row.id) in matching["unattributed_fill_ids"]
                    else "unmatched"
                ),
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
    return combined, matching


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
    view: Literal["trades", "fills"] = Query(default="trades"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows, matching = _journal_trade_rows(db, user.id, currency)
    if view == "fills":
        return [row for row in rows if row["source"] in {"dhan", "dhan_csv"}]

    manual_rows = [row for row in rows if row["source"] == "manual"]
    dhan_rows = _dhan_trade_rows(db, user.id) if currency == "INR" else []
    fill_by_id = {str(row.id): row for row in dhan_rows}
    trade_rows: list[dict[str, Any]] = list(manual_rows)
    for index, match in enumerate(matching["matches"]):
        entry = fill_by_id.get(match["entry_fill_id"])
        exit_fill = fill_by_id.get(match["exit_fill_id"])
        if entry is None or exit_fill is None:
            continue
        expiry, strike, option_type = _contract_details(entry)
        trade_rows.append({
            "id": f"match-{index}-{match['entry_fill_id']}-{match['exit_fill_id']}",
            "annotation_id": str(entry.id),
            "asset_class": _classify_segment(entry.exchange_segment, entry.symbol),
            "instrument": (
                entry.symbol if entry.symbol and entry.symbol.strip().upper() not in {"0", "NONE", "UNKNOWN"}
                else entry.security_id if entry.security_id and entry.security_id.strip() not in {"", "0"}
                else "Unattributable fill"
            ),
            "currency": "INR",
            "side": match["side"],
            "source_side": None,
            "quantity": match["quantity"],
            "multiplier": Decimal("1"),
            "entry_price": entry.price,
            "exit_price": exit_fill.price,
            "entry_time": match["entry_time"],
            "exit_time": match["exit_time"],
            "entry_fill_id": str(entry.id),
            "exit_fill_id": str(exit_fill.id),
            "fees": match["fees"],
            "planned_risk": None,
            "strategy": entry.strategy,
            "setup": entry.setup,
            "notes": entry.notes,
            "tags": entry.tags or [],
            "option_type": option_type,
            "strike": strike,
            "expiry": datetime.combine(expiry, datetime.min.time(), tzinfo=timezone.utc) if expiry else None,
            "net_pnl": match["net_pnl"],
            "source": entry.source,
            "match_state": "matched",
            "position_status": "closed",
            "calendar_date": match["exit_time"].astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat(),
        })
    trade_rows.sort(key=lambda row: row["entry_time"], reverse=True)
    return trade_rows


@router.get("/analytics/journal")
def journal_trade_analytics(
    currency: Literal["USD", "INR"] = Query(default="INR"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    rows, matching = _journal_trade_rows(db, user.id, currency)
    realized_broker = matching["realized_by_fill"]
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
    manual_closed = [
        (row["exit_time"], row["calendar_date"], row["net_pnl"])
        for row in rows if row["source"] == "manual" and row["exit_time"] is not None
    ]
    closed_events.extend(manual_closed)
    for match in matching["matches"]:
        closed_events.append((
            match["exit_time"],
            match["exit_time"].astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat(),
            match["net_pnl"],
        ))
    closed_events.sort(key=lambda item: item[0])
    known_results = [result for _, _, result in closed_events if result is not None]
    unknown_count = len(closed_events) - len(known_results)
    metrics = summarize_net_pnls(known_results)
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
    all_closed_known = complete and bool(closed_events)
    return {
        "currency": currency,
        "closed_trades": len(closed_events),
        "open_trades": open_manual_count + matching["open_positions"],
        "trades_logged": len(rows),
        "fill_count": sum(1 for row in rows if row["source"] in {"dhan", "dhan_csv"}),
        "manual_trade_count": sum(1 for row in rows if row["source"] == "manual"),
        "matched_closed_trades": len(matching["matches"]) + len(manual_closed),
        "fifo_match_slices": len(matching["matches"]),
        "expired_trades": matching["expired_positions"],
        "matched_fill_rows": len(matching["matched_fill_ids"]),
        "open_fill_rows": len(matching["open_fill_ids"]),
        "unmatched_fill_rows": len(matching["unmatched_fill_ids"]),
        "unattributed_fill_rows": len(matching["unattributed_fill_ids"]),
        "real_open_positions": matching["open_positions"],
        "fill_reconciliation": (
            f"{len(matching['matched_fill_ids'])} matched fill rows + "
            f"{len(matching['open_fill_ids'])} real open-position fill rows + "
            f"{len(matching['expired_fill_ids'])} expired fill rows + "
            f"{len(matching['unmatched_fill_ids'])} unmatched fill rows + "
            f"{len(matching['unattributed_fill_ids'])} unattributed fill rows = "
            f"{sum(1 for row in rows if row['source'] in {'dhan', 'dhan_csv'})} fills"
        ),
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "unpriced_closed_trades": unknown_count,
        "win_rate": (
            metrics["win_rate"]
            if all_closed_known
            else None
        ),
        "net_pnl": metrics["net_pnl"] if all_closed_known else None,
        "expectancy": metrics["expectancy"] if all_closed_known else None,
        "profit_factor": metrics["profit_factor"] if all_closed_known else None,
        "max_drawdown": metrics["max_drawdown"] if all_closed_known else None,
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


def _annotation_lists(db: Session, user_id: UUID) -> dict[str, list[str]]:
    manual = list(db.scalars(select(JournalTrade).where(JournalTrade.user_id == user_id)).all())
    broker = list(db.scalars(select(DhanTradeMirror).where(
        DhanTradeMirror.user_id == user_id,
        DhanTradeMirror.source.in_(("dhan", "dhan_csv")),
    )).all())

    def unique(values: list[str]) -> list[str]:
        by_casefold: dict[str, str] = {}
        for value in values:
            normalized = value.strip()
            if normalized:
                by_casefold.setdefault(normalized.casefold(), normalized)
        return sorted(by_casefold.values(), key=str.casefold)

    registered = [item["strategy_id"] for item in registry_payload()]
    strategies = unique(registered + [row.strategy or "" for row in manual + broker])
    setups = unique([row.setup or "" for row in manual + broker])
    tags = unique([tag for row in manual + broker for tag in (row.tags or [])])
    return {"strategies": strategies, "setups": setups, "tags": tags}


def _canonical_annotation(value: str | None, choices: list[str]) -> str | None:
    normalized = value.strip() if value is not None else ""
    if not normalized:
        return None
    return next((choice for choice in choices if choice.casefold() == normalized.casefold()), normalized)


@router.get("/annotation-options")
def trade_annotation_options(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, list[str]]:
    return _annotation_lists(db, user.id)


@router.patch("/{trade_id}/annotations")
def update_trade_annotations(
    trade_id: UUID,
    payload: TradeAnnotationUpdate,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    manual = db.scalar(select(JournalTrade).where(
        JournalTrade.id == trade_id, JournalTrade.user_id == user.id
    ))
    broker = None if manual is not None else db.scalar(select(DhanTradeMirror).where(
        DhanTradeMirror.id == trade_id,
        DhanTradeMirror.user_id == user.id,
        DhanTradeMirror.source.in_(("dhan", "dhan_csv")),
    ))
    row = manual or broker
    if row is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    data = payload.model_dump(exclude_unset=True)
    options = _annotation_lists(db, user.id)
    if "notes" in data:
        row.notes = data["notes"].strip() or None if data["notes"] is not None else None
    if "tags" in data and data["tags"] is not None:
        unique_tags: dict[str, str] = {}
        for tag in data["tags"]:
            normalized_tag = _canonical_annotation(tag, options["tags"]) or tag.strip()
            if normalized_tag:
                unique_tags.setdefault(normalized_tag.casefold(), normalized_tag)
        row.tags = list(unique_tags.values())
    if "strategy" in data:
        row.strategy = _canonical_annotation(data["strategy"], options["strategies"])
    if "setup" in data:
        row.setup = _canonical_annotation(data["setup"], options["setups"])
    db.commit()
    return {
        "notes": row.notes,
        "tags": row.tags or [],
        "strategy": row.strategy,
        "setup": row.setup,
    }


@router.patch("/{trade_id}/notes")
def update_trade_notes(
    trade_id: UUID,
    payload: TradeNoteUpdate,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    manual = db.scalar(
        select(JournalTrade).where(JournalTrade.id == trade_id, JournalTrade.user_id == user.id)
    )
    if manual is not None:
        manual.notes = payload.notes
        manual.tags = payload.tags
        result = {"notes": manual.notes, "tags": manual.tags}
    else:
        broker = db.scalar(
            select(DhanTradeMirror).where(
                DhanTradeMirror.id == trade_id,
                DhanTradeMirror.user_id == user.id,
                DhanTradeMirror.source.in_(("dhan", "dhan_csv")),
            )
        )
        if broker is None:
            raise HTTPException(status_code=404, detail="Trade not found")
        broker.notes = payload.notes
        broker.tags = payload.tags
        result = {"notes": broker.notes, "tags": broker.tags}
    db.commit()
    return result


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
    metrics = summarize_net_pnls(results)
    return TradeAnalytics(
        currency=currency,
        closed_trades=int(metrics["closed_trades"]),
        open_trades=len(trades) - len(results),
        wins=int(metrics["wins"]),
        losses=int(metrics["losses"]),
        win_rate=metrics["win_rate"],
        net_pnl=metrics["net_pnl"],
        expectancy=metrics["expectancy"],
        profit_factor=metrics["profit_factor"],
        max_drawdown=metrics["max_drawdown"],
    )


@router.get("/analytics/breakdown", response_model=list[TradeBreakdownRow])
def trade_analytics_breakdown(
    currency: Literal["USD", "INR"] = Query(default="USD"),
    group_by: Literal["strategy", "instrument", "asset_class"] = Query(default="strategy"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if group_by == "strategy":
        journal_rows, matching = _journal_trade_rows(db, user.id, currency)
        groups_by_strategy: dict[str, list[Decimal]] = {}
        for row in journal_rows:
            if row["source"] != "manual":
                continue
            key = (row["strategy"] or "").strip() or "untagged"
            groups_by_strategy.setdefault(key, [])
            if row["exit_time"] is not None and row["net_pnl"] is not None:
                groups_by_strategy[key].append(row["net_pnl"])
        if currency == "INR":
            fills = {str(row.id): row for row in _dhan_trade_rows(db, user.id)}
            for match in matching["matches"]:
                entry = fills.get(match["entry_fill_id"])
                if entry is None:
                    continue
                key = (entry.strategy or "").strip() or "untagged"
                groups_by_strategy.setdefault(key, [])
                if match["net_pnl"] is not None:
                    groups_by_strategy[key].append(match["net_pnl"])
        return [
            _breakdown_results(key, values)
            for key, values in sorted(groups_by_strategy.items(), key=lambda item: item[0].casefold())
        ]
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
