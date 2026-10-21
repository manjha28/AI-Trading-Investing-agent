import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.accounting import LedgerError, calculate_ledger, money
from app.api.auth import get_current_user, require_csrf
from app.db import get_db
from app.models import Portfolio, PortfolioTransaction, User, UserSession
from app.models import Instrument
from app.api.market_data import get_market_data_provider, latest_quote
from app.market_data import MarketDataProvider

router = APIRouter(prefix="/portfolios/{portfolio_id}", tags=["portfolio accounting"])
CSV_COLUMNS = (
    "transaction_type",
    "symbol",
    "quantity",
    "unit_price",
    "amount",
    "fee",
    "occurred_at",
)
MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 1_000


class TransactionCreate(BaseModel):
    transaction_type: Literal["deposit", "withdrawal", "buy", "sell", "dividend"]
    amount: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=2)
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    unit_price: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    fee: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=24, decimal_places=2)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        symbol = value.strip().upper() if value else None
        if symbol and not symbol[0].isalnum() and symbol[0] != "^":
            raise ValueError("symbol must start with a letter, number, or index caret")
        return symbol

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_fields(self):
        is_trade = self.transaction_type in {"buy", "sell"}
        if is_trade and (not self.symbol or self.quantity is None or self.unit_price is None):
            raise ValueError("Equity trades require symbol, quantity, and unit_price")
        if is_trade and self.amount is not None:
            raise ValueError("Trade amount is calculated from quantity and unit price")
        if not is_trade and (self.amount is None or self.quantity is not None or self.unit_price is not None):
            raise ValueError("Cash transactions require amount and do not accept trade fields")
        if not is_trade and self.symbol is not None and self.transaction_type != "dividend":
            raise ValueError("Only dividend transactions accept a symbol")
        if self.transaction_type == "dividend" and not self.symbol:
            raise ValueError("Dividend transactions require a symbol")
        return self


class TransactionCsvImport(BaseModel):
    csv_content: str = Field(min_length=1, max_length=MAX_IMPORT_BYTES)

    @field_validator("csv_content")
    @classmethod
    def enforce_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError(f"CSV content must be at most {MAX_IMPORT_BYTES} bytes")
        return value


class CsvImportRow(BaseModel):
    row_number: int
    status: Literal["valid", "duplicate", "invalid"]
    transaction_type: str | None = None
    symbol: str | None = None
    errors: list[str] = Field(default_factory=list)


class CsvImportPreview(BaseModel):
    rows: list[CsvImportRow]
    valid_count: int
    duplicate_count: int
    invalid_count: int


class CsvImportResult(BaseModel):
    imported_count: int
    skipped_duplicate_count: int


class TransactionResponse(BaseModel):
    id: UUID
    transaction_type: str
    symbol: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal
    fee: Decimal
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)


class PositionResponse(BaseModel):
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    average_cost: Decimal


class PortfolioSummary(BaseModel):
    currency: str
    cash_balance: Decimal
    invested_cost: Decimal
    realized_pnl: Decimal
    unrealized_pnl: None = None
    market_value: None = None
    positions: list[PositionResponse]


class PositionInstrumentAttach(BaseModel):
    instrument_id: UUID


class PositionInstrumentAttachResponse(BaseModel):
    symbol: str
    instrument_id: UUID
    linked_transactions: int


class ValuationPositionResponse(BaseModel):
    symbol: str
    instrument_id: UUID | None
    quantity: Decimal
    cost_basis: Decimal
    last_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    weight_percent: Decimal | None
    day_change: Decimal | None
    day_change_percent: Decimal | None
    freshness: Literal["fresh", "stale", "unavailable"]
    is_synthetic: bool
    source: str | None
    retrieved_at: datetime | None
    message: str


class PortfolioValuationResponse(BaseModel):
    currency: str
    cash_balance: Decimal
    market_value: Decimal
    total_value: Decimal
    unrealized_pnl: Decimal
    day_change: Decimal
    confidence: Literal["reliable", "unreliable"]
    reasons: list[str]
    last_updated: datetime | None
    positions: list[ValuationPositionResponse]


def get_owned_portfolio(
    db: Session, user_id: UUID, portfolio_id: UUID, *, lock: bool = False
) -> Portfolio:
    statement = select(Portfolio).where(
        Portfolio.id == portfolio_id,
        Portfolio.user_id == user_id,
    )
    if lock:
        statement = statement.with_for_update()
    portfolio = db.scalar(statement)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


def get_transactions(db: Session, portfolio_id: UUID) -> list[PortfolioTransaction]:
    return list(
        db.scalars(
            select(PortfolioTransaction)
            .where(PortfolioTransaction.portfolio_id == portfolio_id)
            .order_by(
                PortfolioTransaction.occurred_at,
                PortfolioTransaction.created_at,
                PortfolioTransaction.id,
            )
        ).all()
    )


def _transaction_key(transaction: PortfolioTransaction | TransactionCreate) -> tuple:
    transaction_type = transaction.transaction_type
    amount = (
        money(transaction.quantity * transaction.unit_price)
        if transaction_type in {"buy", "sell"}
        else money(transaction.amount)
    )
    return (
        transaction_type,
        transaction.symbol,
        transaction.quantity,
        transaction.unit_price,
        amount,
        money(transaction.fee),
        _as_utc(transaction.occurred_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_csv_text(value: str | None) -> str:
    text = value or ""
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _parse_csv_import(
    content: str, existing: list[PortfolioTransaction]
) -> tuple[CsvImportPreview, list[TransactionCreate]]:
    rows: list[CsvImportRow] = []
    candidates: list[TransactionCreate] = []
    existing_keys = {_transaction_key(transaction) for transaction in existing}
    seen_keys: set[tuple] = set()
    if "\x00" in content:
        return CsvImportPreview(
            rows=[CsvImportRow(row_number=1, status="invalid", errors=["CSV contains a null byte"])],
            valid_count=0,
            duplicate_count=0,
            invalid_count=1,
        ), []

    try:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline=""), strict=True)
        fieldnames = reader.fieldnames
        if fieldnames is None or len(fieldnames) != len(CSV_COLUMNS) or set(fieldnames) != set(CSV_COLUMNS):
            raise ValueError(f"CSV header must contain exactly: {', '.join(CSV_COLUMNS)}")

        for row_number, record in enumerate(reader, start=2):
            if row_number > MAX_IMPORT_ROWS + 1:
                rows.append(
                    CsvImportRow(
                        row_number=row_number,
                        status="invalid",
                        errors=[f"CSV exceeds the {MAX_IMPORT_ROWS}-row limit"],
                    )
                )
                break
            if None in record or any(value is None for value in record.values()):
                rows.append(
                    CsvImportRow(
                        row_number=row_number,
                        status="invalid",
                        errors=["Row has missing or extra columns"],
                    )
                )
                continue

            values = {key: value.strip() for key, value in record.items()}
            try:
                transaction_type = values["transaction_type"]
                if not values["occurred_at"]:
                    raise ValueError("occurred_at is required")
                payload = TransactionCreate.model_validate(
                    {
                        "transaction_type": transaction_type,
                        "symbol": values["symbol"] or None,
                        "quantity": values["quantity"] or None,
                        "unit_price": values["unit_price"] or None,
                        "amount": values["amount"] or None,
                        "fee": values["fee"] or "0",
                        "occurred_at": values["occurred_at"],
                    }
                )
            except (ValidationError, ValueError) as error:
                if isinstance(error, ValidationError):
                    errors = [issue["msg"] for issue in error.errors()]
                else:
                    errors = [str(error)]
                rows.append(
                    CsvImportRow(
                        row_number=row_number,
                        status="invalid",
                        transaction_type=values.get("transaction_type") or None,
                        symbol=values.get("symbol") or None,
                        errors=errors,
                    )
                )
                continue

            key = _transaction_key(payload)
            if key in existing_keys or key in seen_keys:
                rows.append(
                    CsvImportRow(
                        row_number=row_number,
                        status="duplicate",
                        transaction_type=payload.transaction_type,
                        symbol=payload.symbol,
                    )
                )
                continue
            seen_keys.add(key)
            candidates.append(payload)
            rows.append(
                CsvImportRow(
                    row_number=row_number,
                    status="valid",
                    transaction_type=payload.transaction_type,
                    symbol=payload.symbol,
                )
            )
    except (csv.Error, ValueError) as error:
        rows = [CsvImportRow(row_number=1, status="invalid", errors=[str(error)])]
        candidates = []

    if candidates and not any(row.status == "invalid" for row in rows):
        created_at = datetime.now(timezone.utc)
        provisional = [
            PortfolioTransaction(
                transaction_type=payload.transaction_type,
                symbol=payload.symbol,
                quantity=payload.quantity,
                unit_price=payload.unit_price,
                amount=(
                    money(payload.quantity * payload.unit_price)
                    if payload.transaction_type in {"buy", "sell"}
                    else payload.amount
                ),
                fee=payload.fee,
                occurred_at=_as_utc(payload.occurred_at),
                created_at=created_at + timedelta(microseconds=index),
            )
            for index, payload in enumerate(candidates)
        ]
        try:
            calculate_ledger([*existing, *provisional])
        except LedgerError as error:
            rows = [
                row.model_copy(
                    update={"status": "invalid", "errors": [f"Import would invalidate the ledger: {error}"]}
                )
                if row.status == "valid"
                else row
                for row in rows
            ]
            candidates = []

    preview = CsvImportPreview(
        rows=rows,
        valid_count=sum(row.status == "valid" for row in rows),
        duplicate_count=sum(row.status == "duplicate" for row in rows),
        invalid_count=sum(row.status == "invalid" for row in rows),
    )
    return preview, candidates


@router.post("/transactions", response_model=TransactionResponse, status_code=201)
def add_transaction(
    portfolio_id: UUID,
    payload: TransactionCreate,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id, lock=True)
    gross_amount = (
        money(payload.quantity * payload.unit_price)
        if payload.transaction_type in {"buy", "sell"}
        else payload.amount
    )
    transaction = PortfolioTransaction(
        portfolio_id=portfolio.id,
        transaction_type=payload.transaction_type,
        symbol=payload.symbol,
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        amount=gross_amount,
        fee=payload.fee,
        occurred_at=_as_utc(payload.occurred_at),
        created_at=datetime.now(timezone.utc),
    )
    db.add(transaction)
    db.flush()
    try:
        calculate_ledger(get_transactions(db, portfolio.id))
    except LedgerError as error:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    db.commit()
    db.refresh(transaction)
    return transaction


@router.put("/transactions/{transaction_id}", response_model=TransactionResponse)
def update_transaction(
    portfolio_id: UUID,
    transaction_id: UUID,
    payload: TransactionCreate,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id, lock=True)
    transaction = db.scalar(
        select(PortfolioTransaction).where(
            PortfolioTransaction.id == transaction_id,
            PortfolioTransaction.portfolio_id == portfolio.id,
        )
    )
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    transaction.transaction_type = payload.transaction_type
    transaction.symbol = payload.symbol
    transaction.quantity = payload.quantity
    transaction.unit_price = payload.unit_price
    transaction.amount = (
        money(payload.quantity * payload.unit_price)
        if payload.transaction_type in {"buy", "sell"}
        else payload.amount
    )
    transaction.fee = payload.fee
    transaction.occurred_at = _as_utc(payload.occurred_at)
    try:
        calculate_ledger(get_transactions(db, portfolio.id))
    except LedgerError as error:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    db.commit()
    db.refresh(transaction)
    return transaction


@router.get("/transactions", response_model=list[TransactionResponse])
def list_transactions(
    portfolio_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id)
    return get_transactions(db, portfolio.id)


@router.get("/transactions/export")
def export_transactions(
    portfolio_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for transaction in get_transactions(db, portfolio.id):
        writer.writerow(
            (
                transaction.transaction_type,
                _safe_csv_text(transaction.symbol),
                transaction.quantity or "",
                transaction.unit_price or "",
                "" if transaction.transaction_type in {"buy", "sell"} else transaction.amount,
                transaction.fee,
                _as_utc(transaction.occurred_at).isoformat(),
            )
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="portfolio-{portfolio.id}-transactions.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/transactions/import/preview", response_model=CsvImportPreview)
def preview_transaction_import(
    portfolio_id: UUID,
    payload: TransactionCsvImport,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id)
    preview, _ = _parse_csv_import(payload.csv_content, get_transactions(db, portfolio.id))
    return preview


@router.post("/transactions/import", response_model=CsvImportResult)
def import_transactions(
    portfolio_id: UUID,
    payload: TransactionCsvImport,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id, lock=True)
    existing = get_transactions(db, portfolio.id)
    preview, candidates = _parse_csv_import(payload.csv_content, existing)
    if preview.invalid_count:
        raise HTTPException(status_code=422, detail=preview.model_dump(mode="json"))

    duplicates = preview.duplicate_count
    if not candidates:
        return CsvImportResult(imported_count=0, skipped_duplicate_count=duplicates)

    created_at = datetime.now(timezone.utc)
    imported = [
        PortfolioTransaction(
            portfolio_id=portfolio.id,
            transaction_type=transaction.transaction_type,
            symbol=transaction.symbol,
            quantity=transaction.quantity,
            unit_price=transaction.unit_price,
            amount=(
                money(transaction.quantity * transaction.unit_price)
                if transaction.transaction_type in {"buy", "sell"}
                else transaction.amount
            ),
            fee=transaction.fee,
            occurred_at=_as_utc(transaction.occurred_at),
            created_at=created_at + timedelta(microseconds=index),
        )
        for index, transaction in enumerate(candidates)
    ]
    db.add_all(imported)
    try:
        db.flush()
        calculate_ledger([*existing, *imported])
        db.commit()
    except LedgerError as error:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Import conflicts with existing transactions") from error
    return CsvImportResult(imported_count=len(imported), skipped_duplicate_count=duplicates)


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(
    portfolio_id: UUID,
    transaction_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id, lock=True)
    transaction = db.scalar(
        select(PortfolioTransaction).where(
            PortfolioTransaction.id == transaction_id,
            PortfolioTransaction.portfolio_id == portfolio.id,
        )
    )
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    db.delete(transaction)
    db.flush()
    try:
        calculate_ledger(get_transactions(db, portfolio.id))
    except LedgerError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot remove this transaction because it would invalidate the ledger: {error}",
        ) from error
    db.commit()


@router.get("/summary", response_model=PortfolioSummary)
def portfolio_summary(
    portfolio_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id)
    ledger = calculate_ledger(get_transactions(db, portfolio.id))
    positions = [
        PositionResponse(
            symbol=symbol,
            quantity=position.quantity,
            cost_basis=money(position.cost_basis),
            average_cost=money(position.cost_basis / position.quantity),
        )
        for symbol, position in sorted(ledger.positions.items())
    ]
    return PortfolioSummary(
        currency=portfolio.base_currency,
        cash_balance=ledger.cash_balance,
        invested_cost=sum((position.cost_basis for position in ledger.positions.values()), Decimal("0")),
        realized_pnl=ledger.realized_pnl,
        positions=positions,
    )


@router.post(
    "/positions/{symbol}/instrument",
    response_model=PositionInstrumentAttachResponse,
)
def attach_instrument_to_position(
    portfolio_id: UUID,
    symbol: str,
    payload: PositionInstrumentAttach,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id, lock=True)
    normalized_symbol = symbol.strip().upper()
    ledger = calculate_ledger(get_transactions(db, portfolio.id))
    if normalized_symbol not in ledger.positions:
        raise HTTPException(status_code=404, detail="Open portfolio position not found")
    instrument = db.get(Instrument, payload.instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    if instrument.symbol.casefold() != normalized_symbol.casefold():
        raise HTTPException(
            status_code=422,
            detail="Selected instrument symbol does not match the portfolio holding",
        )
    if instrument.currency != portfolio.base_currency:
        raise HTTPException(
            status_code=422,
            detail="Instrument currency must match the portfolio currency; FX conversion is not supported",
        )

    transactions = db.scalars(
        select(PortfolioTransaction).where(
            PortfolioTransaction.portfolio_id == portfolio.id,
            PortfolioTransaction.symbol == normalized_symbol,
            PortfolioTransaction.transaction_type.in_(("buy", "sell")),
        )
    ).all()
    for transaction in transactions:
        transaction.instrument_id = instrument.id
    db.commit()
    return PositionInstrumentAttachResponse(
        symbol=normalized_symbol,
        instrument_id=instrument.id,
        linked_transactions=len(transactions),
    )


@router.get("/valuation", response_model=PortfolioValuationResponse)
def portfolio_valuation(
    portfolio_id: UUID,
    refresh: bool = False,
    user: User = Depends(get_current_user),
    provider: MarketDataProvider = Depends(get_market_data_provider),
    db: Session = Depends(get_db),
):
    portfolio = get_owned_portfolio(db, user.id, portfolio_id)
    ledger = calculate_ledger(get_transactions(db, portfolio.id))
    linked_transactions = db.scalars(
        select(PortfolioTransaction)
        .where(
            PortfolioTransaction.portfolio_id == portfolio.id,
            PortfolioTransaction.instrument_id.is_not(None),
        )
        .order_by(
            PortfolioTransaction.occurred_at.desc(),
            PortfolioTransaction.created_at.desc(),
        )
    ).all()
    instrument_by_symbol: dict[str, Instrument] = {}
    for transaction in linked_transactions:
        if (
            transaction.symbol
            and transaction.transaction_type in {"buy", "sell"}
            and transaction.symbol not in instrument_by_symbol
        ):
            instrument = db.get(Instrument, transaction.instrument_id)
            if instrument is not None:
                instrument_by_symbol[transaction.symbol] = instrument

    reasons: list[str] = []
    positions: list[ValuationPositionResponse] = []
    market_value_total = Decimal("0")
    unrealized_total = Decimal("0")
    day_change_total = Decimal("0")
    last_updated: datetime | None = None
    for symbol, position in sorted(ledger.positions.items()):
        instrument = instrument_by_symbol.get(symbol)
        if instrument is None:
            message = "No instrument is linked to this holding."
            reasons.append(f"{symbol}: {message}")
            positions.append(
                ValuationPositionResponse(
                    symbol=symbol,
                    instrument_id=None,
                    quantity=position.quantity,
                    cost_basis=money(position.cost_basis),
                    last_price=None,
                    market_value=None,
                    unrealized_pnl=None,
                    weight_percent=None,
                    day_change=None,
                    day_change_percent=None,
                    freshness="unavailable",
                    is_synthetic=False,
                    source=None,
                    retrieved_at=None,
                    message=message,
                )
            )
            continue
        if instrument.currency != portfolio.base_currency:
            message = (
                f"Instrument currency {instrument.currency} does not match portfolio "
                f"currency {portfolio.base_currency}; no FX conversion was applied."
            )
            reasons.append(f"{symbol}: {message}")
            positions.append(
                ValuationPositionResponse(
                    symbol=symbol,
                    instrument_id=instrument.id,
                    quantity=position.quantity,
                    cost_basis=money(position.cost_basis),
                    last_price=None,
                    market_value=None,
                    unrealized_pnl=None,
                    weight_percent=None,
                    day_change=None,
                    day_change_percent=None,
                    freshness="unavailable",
                    is_synthetic=False,
                    source=None,
                    retrieved_at=None,
                    message=message,
                )
            )
            continue
        try:
            quote = latest_quote(
                instrument_id=instrument.id,
                refresh=refresh,
                provider=provider,
                _user=user,
                db=db,
            )
        except HTTPException as error:
            if error.status_code != 503:
                raise
            message = "No provider returned a quote for this instrument."
            reasons.append(f"{symbol}: {message}")
            positions.append(
                ValuationPositionResponse(
                    symbol=symbol,
                    instrument_id=instrument.id,
                    quantity=position.quantity,
                    cost_basis=money(position.cost_basis),
                    last_price=None,
                    market_value=None,
                    unrealized_pnl=None,
                    weight_percent=None,
                    day_change=None,
                    day_change_percent=None,
                    freshness="unavailable",
                    is_synthetic=False,
                    source=None,
                    retrieved_at=None,
                    message=message,
                )
            )
            continue

        last_price = quote.bar.close
        position_value = money(last_price * position.quantity)
        position_pnl = money(position_value - position.cost_basis)
        day_change = money((quote.bar.close - quote.bar.open) * position.quantity)
        day_change_percent = (
            (quote.bar.close - quote.bar.open) * Decimal("100") / quote.bar.open
            if quote.bar.open > 0
            else None
        )
        market_value_total += position_value
        unrealized_total += position_pnl
        day_change_total += day_change
        quote_time = _as_utc(quote.retrieved_at)
        last_updated = max(last_updated, quote_time) if last_updated else quote_time
        if quote.freshness == "stale":
            reasons.append(f"{symbol}: quote is stale.")
        if quote.is_synthetic:
            reasons.append(f"{symbol}: quote is synthetic fallback data.")
        positions.append(
            ValuationPositionResponse(
                symbol=symbol,
                instrument_id=instrument.id,
                quantity=position.quantity,
                cost_basis=money(position.cost_basis),
                last_price=last_price,
                market_value=position_value,
                unrealized_pnl=position_pnl,
                weight_percent=None,
                day_change=day_change,
                day_change_percent=day_change_percent,
                freshness=quote.freshness,
                is_synthetic=quote.is_synthetic,
                source=quote.source,
                retrieved_at=quote_time,
                message=quote.freshness_message,
            )
        )

    positions = [
        position.model_copy(
            update={
                "weight_percent": (
                    position.market_value * Decimal("100")
                    / (ledger.cash_balance + market_value_total)
                    if position.market_value is not None
                    and ledger.cash_balance + market_value_total > 0
                    else None
                )
            }
        )
        for position in positions
    ]
    return PortfolioValuationResponse(
        currency=portfolio.base_currency,
        cash_balance=ledger.cash_balance,
        market_value=money(market_value_total),
        total_value=money(ledger.cash_balance + market_value_total),
        unrealized_pnl=money(unrealized_total),
        day_change=money(day_change_total),
        confidence="unreliable" if reasons else "reliable",
        reasons=reasons,
        last_updated=last_updated,
        positions=positions,
    )