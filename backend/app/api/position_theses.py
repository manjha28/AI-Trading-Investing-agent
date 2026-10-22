from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.api.portfolio_transactions import calculate_portfolio_ledger, get_owned_portfolio
from app.config import settings
from app.db import get_db
from app.models import (
    Instrument,
    MarketQuote,
    PortfolioTransaction,
    PositionThesis,
    PositionThesisRevision,
    UserSession,
)

router = APIRouter(prefix="/portfolios/{portfolio_id}/theses", tags=["position theses"])

DISCLAIMER = "Decision support, not investment advice."
MAX_LADDER_LEVELS = 10
MAX_CONDITIONS = 10
PERCENT_QUANTUM = Decimal("0.01")
THESIS_FIELDS = (
    "status",
    "entry_reason",
    "horizon_days",
    "conviction",
    "target_levels",
    "stop_level",
    "invalidation_conditions",
    "invalidation_notes",
    "max_loss_pct",
    "review_at",
)
PRICE_CONDITIONS = {"price_below", "price_above"}


def require_verified_session(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before managing position theses.",
            },
        )
    return active_session


class TargetLevel(BaseModel):
    price: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    trim_pct: Decimal = Field(gt=0, le=100, max_digits=7, decimal_places=4)
    note: str | None = Field(default=None, max_length=500)


class InvalidationCondition(BaseModel):
    type: Literal["price_below", "price_above", "loss_from_cost_pct"]
    value: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def percent_in_range(self) -> "InvalidationCondition":
        if self.type == "loss_from_cost_pct" and self.value > 100:
            raise ValueError("loss_from_cost_pct cannot exceed 100")
        return self


class ThesisFields(BaseModel):
    status: Literal["active", "invalidated", "closed"] = "active"
    entry_reason: str = Field(min_length=1, max_length=4000)
    horizon_days: int = Field(ge=1, le=3650)
    conviction: int = Field(ge=1, le=5)
    target_levels: list[TargetLevel] = Field(default_factory=list, max_length=MAX_LADDER_LEVELS)
    stop_level: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    invalidation_conditions: list[InvalidationCondition] = Field(
        default_factory=list, max_length=MAX_CONDITIONS
    )
    invalidation_notes: str | None = Field(default=None, max_length=4000)
    max_loss_pct: Decimal | None = Field(default=None, gt=0, le=100, max_digits=7, decimal_places=4)
    review_at: datetime | None = None

    @field_validator("review_at")
    @classmethod
    def utc_review(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def validate_ladder(self) -> "ThesisFields":
        prices = [level.price for level in self.target_levels]
        if any(later <= earlier for earlier, later in zip(prices, prices[1:])):
            raise ValueError("target_levels must be ordered by strictly ascending price")
        if sum((level.trim_pct for level in self.target_levels), Decimal("0")) > 100:
            raise ValueError("Total trim_pct across target_levels cannot exceed 100")
        if self.stop_level is not None and prices and self.stop_level >= prices[0]:
            raise ValueError("stop_level must be below the first target price")
        return self

    def has_prices(self) -> bool:
        return bool(
            self.target_levels
            or self.stop_level is not None
            or any(item.type in PRICE_CONDITIONS for item in self.invalidation_conditions)
        )


class ThesisCreate(ThesisFields):
    symbol: str = Field(min_length=1, max_length=32)
    instrument_id: UUID
    currency: str | None = Field(default=None, min_length=3, max_length=4)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class ThesisUpdate(BaseModel):
    status: Literal["active", "invalidated", "closed"] | None = None
    entry_reason: str | None = Field(default=None, min_length=1, max_length=4000)
    horizon_days: int | None = Field(default=None, ge=1, le=3650)
    conviction: int | None = Field(default=None, ge=1, le=5)
    target_levels: list[TargetLevel] | None = Field(default=None, max_length=MAX_LADDER_LEVELS)
    stop_level: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    invalidation_conditions: list[InvalidationCondition] | None = Field(
        default=None, max_length=MAX_CONDITIONS
    )
    invalidation_notes: str | None = Field(default=None, max_length=4000)
    max_loss_pct: Decimal | None = Field(default=None, gt=0, le=100, max_digits=7, decimal_places=4)
    review_at: datetime | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=4)


class TargetLevelResponse(BaseModel):
    price: Decimal
    trim_pct: Decimal
    note: str | None
    distance_pct: Decimal | None


class ThesisMarket(BaseModel):
    last_price: Decimal | None
    freshness: Literal["fresh", "stale", "unavailable"]
    is_synthetic: bool
    source: str | None
    retrieved_at: datetime | None
    actionable: bool
    note: str


class ThesisResponse(BaseModel):
    id: UUID
    portfolio_id: UUID
    instrument_id: UUID
    symbol: str
    status: str
    entry_reason: str
    horizon_days: int
    conviction: int
    currency: str
    target_levels: list[TargetLevelResponse]
    stop_level: Decimal | None
    stop_distance_pct: Decimal | None
    invalidation_conditions: list[InvalidationCondition]
    invalidation_notes: str | None
    max_loss_pct: Decimal | None
    review_at: datetime | None
    review_overdue: bool
    market: ThesisMarket
    created_at: datetime
    updated_at: datetime
    disclaimer: str = DISCLAIMER


class RevisionResponse(BaseModel):
    revision_number: int
    changed_fields: list[str]
    snapshot: dict
    created_at: datetime


class CoverageEntry(BaseModel):
    symbol: str
    quantity: Decimal
    instrument_id: UUID | None
    state: Literal["managed", "unmanaged position"]
    reason: Literal["active_thesis", "no_instrument_linked", "no_thesis", "thesis_not_active"]
    thesis_id: UUID | None
    thesis_status: str | None
    conviction: int | None
    review_overdue: bool


class CoverageResponse(BaseModel):
    portfolio_id: UUID
    managed: list[CoverageEntry]
    unmanaged: list[CoverageEntry]
    managed_count: int
    unmanaged_count: int
    disclaimer: str = DISCLAIMER


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _snapshot(thesis: PositionThesis) -> dict:
    return {
        "status": thesis.status,
        "entry_reason": thesis.entry_reason,
        "horizon_days": thesis.horizon_days,
        "conviction": thesis.conviction,
        "target_levels": thesis.target_levels,
        "stop_level": None if thesis.stop_level is None else str(thesis.stop_level),
        "invalidation_conditions": thesis.invalidation_conditions,
        "invalidation_notes": thesis.invalidation_notes,
        "max_loss_pct": None if thesis.max_loss_pct is None else str(thesis.max_loss_pct),
        "review_at": thesis.review_at.isoformat() if thesis.review_at else None,
    }


def _json_fields(fields: ThesisFields) -> dict:
    dumped = fields.model_dump(mode="json", include=set(THESIS_FIELDS))
    return dumped


def _apply(thesis: PositionThesis, values: dict) -> None:
    for name in THESIS_FIELDS:
        value = values[name]
        if name in {"stop_level", "max_loss_pct"} and value is not None:
            value = Decimal(str(value))
        if name == "review_at" and isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        setattr(thesis, name, value)


def _write_revision(db: Session, thesis: PositionThesis, changed: list[str], now: datetime) -> None:
    latest = db.scalar(
        select(func.max(PositionThesisRevision.revision_number)).where(
            PositionThesisRevision.thesis_id == thesis.id
        )
    )
    db.add(
        PositionThesisRevision(
            thesis_id=thesis.id,
            user_id=thesis.user_id,
            revision_number=(latest or 0) + 1,
            changed_fields=changed,
            snapshot=_snapshot(thesis),
            created_at=now,
        )
    )


def _check_currency(portfolio_currency: str, requested: str | None, needs_currency: bool) -> None:
    if requested is None:
        if needs_currency:
            raise HTTPException(
                status_code=422,
                detail=f"currency is required with price levels and must be {portfolio_currency}",
            )
        return
    if requested.upper() != portfolio_currency:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Price levels are in {requested.upper()} but the portfolio currency is "
                f"{portfolio_currency}; no FX conversion is supported."
            ),
        )


def _validation_error(exc: ValidationError) -> HTTPException:
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return HTTPException(status_code=422, detail=jsonable_errors(errors))


def jsonable_errors(errors: list[dict]) -> list[dict]:
    return [{"loc": [str(part) for part in item["loc"]], "msg": item["msg"]} for item in errors]


def _linked_instruments(db: Session, portfolio_id: UUID) -> dict[str, UUID]:
    rows = db.execute(
        select(PortfolioTransaction.symbol, PortfolioTransaction.instrument_id)
        .where(
            PortfolioTransaction.portfolio_id == portfolio_id,
            PortfolioTransaction.instrument_id.is_not(None),
            PortfolioTransaction.transaction_type.in_(("buy", "sell")),
        )
        .order_by(PortfolioTransaction.occurred_at.desc(), PortfolioTransaction.created_at.desc())
    ).all()
    mapping: dict[str, UUID] = {}
    for symbol, instrument_id in rows:
        if symbol and symbol not in mapping:
            mapping[symbol] = instrument_id
    return mapping


def _cached_quotes(db: Session, instrument_ids: list[UUID]) -> dict[UUID, MarketQuote]:
    quotes: dict[UUID, MarketQuote] = {}
    if not instrument_ids:
        return quotes
    for quote in db.scalars(
        select(MarketQuote)
        .where(MarketQuote.instrument_id.in_(instrument_ids))
        .order_by(MarketQuote.retrieved_at.desc(), MarketQuote.source)
    ):
        quotes.setdefault(quote.instrument_id, quote)
    return quotes


def _market(
    quote: MarketQuote | None, instrument: Instrument | None, portfolio_currency: str
) -> ThesisMarket:
    if instrument is not None and instrument.currency != portfolio_currency:
        return ThesisMarket(
            last_price=None,
            freshness="unavailable",
            is_synthetic=False,
            source=None,
            retrieved_at=None,
            actionable=False,
            note=(
                f"Instrument currency {instrument.currency} differs from portfolio currency "
                f"{portfolio_currency}; distances are not shown."
            ),
        )
    if quote is None:
        return ThesisMarket(
            last_price=None,
            freshness="unavailable",
            is_synthetic=False,
            source=None,
            retrieved_at=None,
            actionable=False,
            note="No cached quote is available; distances are not shown.",
        )
    now = datetime.now(timezone.utc)
    age = max(
        (now - _utc(quote.retrieved_at)).total_seconds(),
        (now - _utc(quote.timestamp)).total_seconds(),
    )
    fresh = 0 <= age <= settings.market_data_quote_freshness_seconds
    if quote.is_synthetic:
        note = "Synthetic demo price; do not act on these distances."
    elif not fresh:
        note = "Price is stale; do not act on these distances."
    else:
        note = "Cached price within the freshness window."
    return ThesisMarket(
        last_price=quote.close,
        freshness="fresh" if fresh else "stale",
        is_synthetic=quote.is_synthetic,
        source=quote.source,
        retrieved_at=_utc(quote.retrieved_at),
        actionable=fresh and not quote.is_synthetic,
        note=note,
    )


def _distance(level: Decimal, price: Decimal | None) -> Decimal | None:
    if price is None or price <= 0:
        return None
    return ((level - price) * Decimal("100") / price).quantize(PERCENT_QUANTUM)


def _response(
    thesis: PositionThesis, currency: str, instrument: Instrument | None, quote: MarketQuote | None
) -> ThesisResponse:
    market = _market(quote, instrument, currency)
    price = market.last_price
    now = datetime.now(timezone.utc)
    return ThesisResponse(
        id=thesis.id,
        portfolio_id=thesis.portfolio_id,
        instrument_id=thesis.instrument_id,
        symbol=thesis.symbol,
        status=thesis.status,
        entry_reason=thesis.entry_reason,
        horizon_days=thesis.horizon_days,
        conviction=thesis.conviction,
        currency=currency,
        target_levels=[
            TargetLevelResponse(
                price=Decimal(str(level["price"])),
                trim_pct=Decimal(str(level["trim_pct"])),
                note=level.get("note"),
                distance_pct=_distance(Decimal(str(level["price"])), price),
            )
            for level in thesis.target_levels
        ],
        stop_level=thesis.stop_level,
        stop_distance_pct=None if thesis.stop_level is None else _distance(thesis.stop_level, price),
        invalidation_conditions=[
            InvalidationCondition.model_validate(item) for item in thesis.invalidation_conditions
        ],
        invalidation_notes=thesis.invalidation_notes,
        max_loss_pct=thesis.max_loss_pct,
        review_at=thesis.review_at,
        review_overdue=bool(
            thesis.status == "active" and thesis.review_at and _utc(thesis.review_at) < now
        ),
        market=market,
        created_at=thesis.created_at,
        updated_at=thesis.updated_at,
    )


def _render(db: Session, portfolio, theses: list[PositionThesis]) -> list[ThesisResponse]:
    instrument_ids = list({thesis.instrument_id for thesis in theses})
    instruments = {
        item.id: item
        for item in db.scalars(select(Instrument).where(Instrument.id.in_(instrument_ids)))
    } if instrument_ids else {}
    quotes = _cached_quotes(db, instrument_ids)
    return [
        _response(
            thesis,
            portfolio.base_currency,
            instruments.get(thesis.instrument_id),
            quotes.get(thesis.instrument_id),
        )
        for thesis in theses
    ]


def _owned_thesis(db: Session, user_id: UUID, portfolio_id: UUID, thesis_id: UUID) -> PositionThesis:
    thesis = db.scalar(
        select(PositionThesis).where(
            PositionThesis.id == thesis_id,
            PositionThesis.user_id == user_id,
            PositionThesis.portfolio_id == portfolio_id,
        )
    )
    if thesis is None:
        raise HTTPException(status_code=404, detail="Thesis not found")
    return thesis


def _active_exists(
    db: Session, user_id: UUID, portfolio_id: UUID, instrument_id: UUID, exclude: UUID | None = None
) -> bool:
    query = select(PositionThesis.id).where(
        PositionThesis.user_id == user_id,
        PositionThesis.portfolio_id == portfolio_id,
        PositionThesis.instrument_id == instrument_id,
        PositionThesis.status == "active",
    )
    if exclude is not None:
        query = query.where(PositionThesis.id != exclude)
    return db.scalar(query) is not None


DUPLICATE_ACTIVE = "An active thesis already exists for this position."


@router.get("/coverage", response_model=CoverageResponse)
def thesis_coverage(
    portfolio_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> CoverageResponse:
    user_id = active_session.user_id
    portfolio = get_owned_portfolio(db, user_id, portfolio_id)
    ledger = calculate_portfolio_ledger(db, user_id, portfolio.id)
    linked = _linked_instruments(db, portfolio.id)
    theses = list(
        db.scalars(
            select(PositionThesis)
            .where(PositionThesis.user_id == user_id, PositionThesis.portfolio_id == portfolio.id)
            .order_by(PositionThesis.updated_at.desc())
        )
    )
    now = datetime.now(timezone.utc)
    managed: list[CoverageEntry] = []
    unmanaged: list[CoverageEntry] = []
    for symbol, position in sorted(ledger.positions.items()):
        if position.quantity <= 0:
            continue
        instrument_id = linked.get(symbol)
        for_instrument = [t for t in theses if t.instrument_id == instrument_id] if instrument_id else []
        active = next((t for t in for_instrument if t.status == "active"), None)
        if active is not None:
            managed.append(
                CoverageEntry(
                    symbol=symbol,
                    quantity=position.quantity,
                    instrument_id=instrument_id,
                    state="managed",
                    reason="active_thesis",
                    thesis_id=active.id,
                    thesis_status=active.status,
                    conviction=active.conviction,
                    review_overdue=bool(active.review_at and _utc(active.review_at) < now),
                )
            )
            continue
        latest = for_instrument[0] if for_instrument else None
        reason = (
            "no_instrument_linked"
            if instrument_id is None
            else "thesis_not_active" if latest is not None else "no_thesis"
        )
        unmanaged.append(
            CoverageEntry(
                symbol=symbol,
                quantity=position.quantity,
                instrument_id=instrument_id,
                state="unmanaged position",
                reason=reason,
                thesis_id=latest.id if latest else None,
                thesis_status=latest.status if latest else None,
                conviction=None,
                review_overdue=False,
            )
        )
    return CoverageResponse(
        portfolio_id=portfolio.id,
        managed=managed,
        unmanaged=unmanaged,
        managed_count=len(managed),
        unmanaged_count=len(unmanaged),
    )


@router.get("", response_model=list[ThesisResponse])
def list_theses(
    portfolio_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[ThesisResponse]:
    portfolio = get_owned_portfolio(db, active_session.user_id, portfolio_id)
    theses = list(
        db.scalars(
            select(PositionThesis)
            .where(
                PositionThesis.user_id == active_session.user_id,
                PositionThesis.portfolio_id == portfolio.id,
            )
            .order_by(PositionThesis.symbol, PositionThesis.created_at)
        )
    )
    return _render(db, portfolio, theses)


@router.post("", response_model=ThesisResponse, status_code=201)
def create_thesis(
    portfolio_id: UUID,
    payload: ThesisCreate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ThesisResponse:
    user_id = active_session.user_id
    portfolio = get_owned_portfolio(db, user_id, portfolio_id)
    _check_currency(portfolio.base_currency, payload.currency, payload.has_prices())
    instrument = db.get(Instrument, payload.instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    if instrument.currency != portfolio.base_currency:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Instrument currency {instrument.currency} does not match portfolio currency "
                f"{portfolio.base_currency}; no FX conversion is supported."
            ),
        )
    if _linked_instruments(db, portfolio.id).get(payload.symbol) != payload.instrument_id:
        raise HTTPException(
            status_code=422,
            detail="The symbol must be a portfolio holding linked to this instrument.",
        )
    if payload.status == "active" and _active_exists(
        db, user_id, portfolio.id, payload.instrument_id
    ):
        raise HTTPException(status_code=409, detail=DUPLICATE_ACTIVE)
    now = datetime.now(timezone.utc)
    thesis = PositionThesis(
        user_id=user_id,
        portfolio_id=portfolio.id,
        instrument_id=payload.instrument_id,
        symbol=payload.symbol,
        created_at=now,
        updated_at=now,
    )
    _apply(thesis, _json_fields(payload))
    db.add(thesis)
    try:
        db.flush()
        _write_revision(db, thesis, list(THESIS_FIELDS), now)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICATE_ACTIVE)
    db.refresh(thesis)
    return _render(db, portfolio, [thesis])[0]


@router.get("/{thesis_id}", response_model=ThesisResponse)
def get_thesis(
    portfolio_id: UUID,
    thesis_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> ThesisResponse:
    portfolio = get_owned_portfolio(db, active_session.user_id, portfolio_id)
    thesis = _owned_thesis(db, active_session.user_id, portfolio.id, thesis_id)
    return _render(db, portfolio, [thesis])[0]


@router.get("/{thesis_id}/revisions", response_model=list[RevisionResponse])
def list_revisions(
    portfolio_id: UUID,
    thesis_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[PositionThesisRevision]:
    portfolio = get_owned_portfolio(db, active_session.user_id, portfolio_id)
    thesis = _owned_thesis(db, active_session.user_id, portfolio.id, thesis_id)
    return list(
        db.scalars(
            select(PositionThesisRevision)
            .where(
                PositionThesisRevision.thesis_id == thesis.id,
                PositionThesisRevision.user_id == active_session.user_id,
            )
            .order_by(PositionThesisRevision.revision_number)
        )
    )


@router.patch("/{thesis_id}", response_model=ThesisResponse)
def update_thesis(
    portfolio_id: UUID,
    thesis_id: UUID,
    payload: ThesisUpdate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ThesisResponse:
    user_id = active_session.user_id
    portfolio = get_owned_portfolio(db, user_id, portfolio_id)
    thesis = _owned_thesis(db, user_id, portfolio.id, thesis_id)
    changes = payload.model_dump(exclude_unset=True)
    currency = changes.pop("currency", None)
    for required in ("status", "entry_reason", "horizon_days", "conviction", "target_levels", "invalidation_conditions"):
        if required in changes and changes[required] is None:
            raise HTTPException(status_code=422, detail=f"{required} cannot be null")

    current = _snapshot(thesis)
    merged = {**current, **{key: value for key, value in payload.model_dump(exclude_unset=True, mode="json").items() if key != "currency"}}
    try:
        validated = ThesisFields.model_validate(merged)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    price_fields = {"target_levels", "stop_level", "invalidation_conditions"} & changes.keys()
    _check_currency(portfolio.base_currency, currency, bool(price_fields) and validated.has_prices())

    new_values = _json_fields(validated)
    changed = [
        name for name in THESIS_FIELDS if name in changes and _comparable(new_values[name], name) != _comparable(current[name], name)
    ]
    if not changed:
        return _render(db, portfolio, [thesis])[0]
    if (
        validated.status == "active"
        and thesis.status != "active"
        and _active_exists(db, user_id, portfolio.id, thesis.instrument_id, exclude=thesis.id)
    ):
        raise HTTPException(status_code=409, detail=DUPLICATE_ACTIVE)
    now = datetime.now(timezone.utc)
    _apply(thesis, new_values)
    thesis.updated_at = now
    try:
        db.flush()
        _write_revision(db, thesis, changed, now)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICATE_ACTIVE)
    db.refresh(thesis)
    return _render(db, portfolio, [thesis])[0]


NUMERIC_KEYS = {"price", "trim_pct", "value", "stop_level", "max_loss_pct"}


def _comparable(value, key: str | None = None):
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    if isinstance(value, dict):
        return {name: _comparable(item, name) for name, item in value.items()}
    if isinstance(value, str) and key in NUMERIC_KEYS:
        return str(Decimal(value).normalize())
    return value


@router.delete("/{thesis_id}", status_code=204)
def delete_thesis(
    portfolio_id: UUID,
    thesis_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    portfolio = get_owned_portfolio(db, active_session.user_id, portfolio_id)
    db.delete(_owned_thesis(db, active_session.user_id, portfolio.id, thesis_id))
    db.commit()
    return Response(status_code=204)
