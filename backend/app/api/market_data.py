from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from app.api.auth import get_active_session, get_current_user, require_csrf
from app.config import settings
from app.db import get_db
from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProvider,
    MarketDataProviderError,
    MockMarketDataProvider,
    ProviderHealth,
)
from app.models import (
    Instrument,
    InstrumentFundamental,
    InstrumentProviderIdentifier,
    InstrumentSnapshot,
    MarketBar,
    MarketQuote,
    MarketDataSync,
    PortfolioTransaction,
    User,
    UserSession,
    WatchlistInstrument,
)
from app.services.indicators import IndicatorValue, compute_indicators
from app.services import fundamentals as fundamentals_service
from app.provider_adapters import (
    BinanceProvider,
    DeltaExchangeProvider,
    FinnhubProvider,
    UpstoxProvider,
    YahooFinanceProvider,
    binance_provider_symbol,
    candidate_for_instrument,
    canonical_provider_symbol,
)
from app.twelve_data import TwelveDataProvider

router = APIRouter(prefix="/market-data", tags=["market data"])
indicators_router = APIRouter(tags=["market data indicators"])
BarInterval = Literal["1d", "1h", "15m", "1m"]
MAX_BARS = 500


class MarketDataProviderChain:
    def __init__(self, providers: list[MarketDataProvider]):
        self.providers = providers
        self.name = providers[0].name
        self.is_mock = providers[0].is_mock

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        last_error: MarketDataProviderError | None = None
        for provider in self.providers:
            try:
                results = provider.search_instruments(query, limit)
            except (MarketDataProviderError, TimeoutError, ValueError) as error:
                last_error = error if isinstance(error, MarketDataProviderError) else None
                continue
            if results:
                return results
        if last_error is not None and len(self.providers) == 1:
            raise last_error
        return []

    def health(self) -> ProviderHealth:
        primary = self.providers[0].health()
        return ProviderHealth(
            name=",".join(provider.name for provider in self.providers),
            healthy=primary.healthy,
            is_mock=all(provider.is_mock for provider in self.providers),
            capabilities=primary.capabilities,
            status=f"provider_chain:{primary.status}",
        )


class FundamentalSnapshotResponse(BaseModel):
    market_cap: Decimal | None
    pe: Decimal | None
    pb: Decimal | None
    dividend_yield: Decimal | None
    sector: str | None
    industry: str | None
    source: str
    fetched_at: datetime
    stale: bool


class FundamentalPeriodResponse(BaseModel):
    id: UUID
    period_type: str
    period_end: date
    currency: str
    revenue: Decimal | None
    net_income: Decimal | None
    eps: Decimal | None
    operating_margin: Decimal | None
    total_assets: Decimal | None
    total_liabilities: Decimal | None
    total_equity: Decimal | None
    operating_cash_flow: Decimal | None
    free_cash_flow: Decimal | None
    total_debt: Decimal | None
    shares_outstanding: Decimal | None
    source: str
    fetched_at: datetime
    stale: bool
    revenue_yoy_growth_pct: Decimal | None = None
    net_income_yoy_growth_pct: Decimal | None = None
    eps_yoy_growth_pct: Decimal | None = None
    free_cash_flow_yoy_growth_pct: Decimal | None = None


class FundamentalsResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    currency: str
    snapshot: FundamentalSnapshotResponse | None
    fundamentals: list[FundamentalPeriodResponse]
    unavailable: bool
    message: str
    disclaimer: str = "Fundamentals are provider-reported decision support, not investment advice. No FX conversion is applied."


class FundamentalsSyncResponse(BaseModel):
    instrument_id: UUID
    status: Literal["available", "unavailable"]
    source: str | None
    fetched_at: datetime | None
    fundamentals_count: int
    message: str


def create_market_data_provider_chain(
    names: tuple[str, ...] | None = None,
    *,
    upstox_access_token: str | None = None,
) -> MarketDataProviderChain:
    providers: list[MarketDataProvider] = []
    selected_names = names or settings.market_data_provider_names
    for name in selected_names:
        if name == "twelve_data":
            key = settings.twelve_data_api_key.get_secret_value() if settings.twelve_data_api_key else ""
            if key.strip():
                providers.append(TwelveDataProvider(key))
        elif name == "finnhub":
            key = settings.finnhub_api_key.get_secret_value() if settings.finnhub_api_key else ""
            if key.strip():
                providers.append(FinnhubProvider(key))
        elif name == "yfinance":
            providers.append(YahooFinanceProvider())
        elif name == "delta_exchange":
            providers.append(DeltaExchangeProvider())
        elif name == "binance":
            providers.append(BinanceProvider())
        elif name == "upstox":
            if upstox_access_token and upstox_access_token.strip():
                providers.append(UpstoxProvider(upstox_access_token))
        elif name == "mock":
            providers.append(MockMarketDataProvider())
    if not any(provider.name == "mock" for provider in providers):
        providers.append(MockMarketDataProvider())
    return MarketDataProviderChain(providers)


def get_market_data_provider() -> MarketDataProvider:
    names = ("mock",) if os.environ.get("PYTEST_CURRENT_TEST") else None
    return create_market_data_provider_chain(names)


def _providers(provider: MarketDataProvider) -> list[MarketDataProvider]:
    return list(getattr(provider, "providers", [provider]))


def _venue_matches(instrument: Instrument, candidate: InstrumentCandidate) -> bool:
    aliases = {
        "NSE": {"NSE", "XNSE", "NSI", "NSE_EQ"},
        "BSE": {"BSE", "XBOM", "BOM", "BSE_EQ"},
    }
    expected = aliases.get(instrument.venue.upper(), {instrument.venue.upper()})
    actual = aliases.get(candidate.venue.upper(), {candidate.venue.upper()})
    return bool(expected & actual)


def _resolve_provider_candidate(
    db: Session, instrument: Instrument, provider: MarketDataProvider
) -> tuple[InstrumentCandidate, InstrumentProviderIdentifier]:
    mapping = next(
        (item for item in instrument.provider_identifiers if item.provider == provider.name),
        None,
    )
    if mapping is not None:
        candidate = candidate_for_instrument(
            provider.name,
            mapping.provider_instrument_id,
            mapping.provider_symbol,
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            venue=instrument.venue,
            currency=instrument.currency,
            country=instrument.country,
            timezone_name=instrument.timezone,
            is_synthetic=provider.is_mock,
            multiplier=instrument.multiplier,
            expiry=instrument.expiry,
            strike=instrument.strike,
            option_type=instrument.option_type,
        )
        return _validated_candidate(provider, candidate), mapping

    candidate: InstrumentCandidate | None = None
    if provider.name == "twelve_data":
        matches = provider.search_instruments(instrument.symbol, 50)
        candidate = next(
            (
                item for item in matches
                if item.symbol.casefold() == instrument.symbol.casefold()
                and item.currency == instrument.currency
                and _venue_matches(instrument, item)
            ),
            None,
        )
    elif provider.name in {"finnhub", "yfinance", "delta_exchange", "binance"}:
        if provider.name == "finnhub" and instrument.country == "IN":
            exchange = (
                "BSE"
                if instrument.venue.upper() in {"BSE", "XBOM", "BOM", "BSE_EQ"}
                else "NSE"
            )
            provider_symbol = f"{exchange}:{instrument.symbol.upper()}"
        elif provider.name == "yfinance":
            provider_symbol = canonical_provider_symbol(
                instrument.symbol, instrument.venue, instrument.country
            )
        elif provider.name == "binance":
            provider_symbol = binance_provider_symbol(instrument.symbol)
        else:
            provider_symbol = instrument.symbol.upper()
        provider_id = f"{provider_symbol}|{instrument.venue.upper()}"
        candidate = candidate_for_instrument(
            provider.name,
            provider_id,
            provider_symbol,
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            venue=instrument.venue,
            currency=instrument.currency,
            country=instrument.country,
            timezone_name=instrument.timezone,
            multiplier=instrument.multiplier,
            expiry=instrument.expiry,
            strike=instrument.strike,
            option_type=instrument.option_type,
        )
    elif provider.name == "mock":
        if not instrument.is_synthetic:
            raise MarketDataProviderError("synthetic_fallback_disabled_for_real_instrument")
        provider_id = f"SYNTHETIC|{instrument.id}"
        candidate = candidate_for_instrument(
            provider.name,
            provider_id,
            instrument.symbol,
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            venue=instrument.venue,
            currency=instrument.currency,
            country=instrument.country,
            timezone_name=instrument.timezone,
            is_synthetic=True,
            multiplier=instrument.multiplier,
            expiry=instrument.expiry,
            strike=instrument.strike,
            option_type=instrument.option_type,
        )

    if candidate is None:
        raise MarketDataProviderError("provider_symbol_unavailable")
    mapping = InstrumentProviderIdentifier(
        instrument_id=instrument.id,
        provider=provider.name,
        provider_instrument_id=candidate.provider_instrument_id,
        provider_symbol=candidate.provider_symbol,
        is_verified=candidate.is_verified,
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(mapping)
    db.flush()
    instrument.provider_identifiers.append(mapping)
    return _validated_candidate(provider, candidate), mapping


class ProviderHealthResponse(BaseModel):
    name: str
    healthy: bool
    is_mock: bool
    capabilities: list[str]
    status: str


class ProviderResponse(BaseModel):
    provider: ProviderHealthResponse
    configuration: str


class ProviderIdentifierResponse(BaseModel):
    provider: str
    provider_instrument_id: str
    provider_symbol: str
    is_verified: bool


class InstrumentResponse(BaseModel):
    id: UUID
    symbol: str
    display_name: str
    asset_class: str
    venue: str
    currency: str
    country: str
    timezone: str
    multiplier: Decimal | None
    expiry: date | None
    strike: Decimal | None
    option_type: str | None
    is_synthetic: bool
    relink_required: bool
    provider_identifiers: list[ProviderIdentifierResponse]


class MarketBarResponse(BaseModel):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    retrieved_at: datetime
    is_synthetic: bool


class FreshnessResponse(BaseModel):
    status: Literal["fresh", "stale", "unavailable", "never_fetched"]
    source: str | None
    last_successful_update: datetime | None
    latest_bar_timestamp: datetime | None
    is_synthetic: bool
    message: str


class HistoryResponse(BaseModel):
    instrument_id: UUID
    interval: BarInterval
    freshness: FreshnessResponse
    bars: list[MarketBarResponse]
    offset: int
    limit: int


class IndicatorValueResponse(BaseModel):
    value: Decimal | None
    is_synthetic: bool
    bars_required: int
    bars_used: int
    status: Literal["available", "insufficient_history"]


class IndicatorsResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    currency: str
    interval: BarInterval
    freshness: FreshnessResponse
    source: str | None
    latest_bar: datetime | None
    bars_available: int
    indicators: dict[str, IndicatorValueResponse]
    disclaimer: str = "Decision support, not investment advice."


def _indicator_result(value: IndicatorValue) -> IndicatorValueResponse:
    return IndicatorValueResponse(
        value=value.value,
        is_synthetic=value.is_synthetic,
        bars_required=value.bars_required,
        bars_used=value.bars_used,
        status="available" if value.value is not None else "insufficient_history",
    )


def require_verified_market_session(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before viewing indicators.",
            },
        )
    return active_session


class QuoteResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    currency: str
    source: str
    retrieved_at: datetime
    freshness: Literal["fresh", "stale"]
    freshness_message: str
    is_cached: bool
    is_synthetic: bool
    is_verified: bool
    bar: HistoricalBar


class WatchlistQuoteResponse(BaseModel):
    source: str
    retrieved_at: datetime
    freshness: Literal["fresh", "stale"]
    is_synthetic: bool
    is_verified: bool
    close: Decimal


class WatchlistEntryResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    display_name: str
    asset_class: str
    venue: str
    currency: str
    is_synthetic: bool
    relink_required: bool
    quote: WatchlistQuoteResponse | None


def _instrument_response(instrument: Instrument) -> InstrumentResponse:
    return InstrumentResponse(
        id=instrument.id,
        symbol=instrument.symbol,
        display_name=instrument.display_name,
        asset_class=instrument.asset_class,
        venue=instrument.venue,
        currency=instrument.currency,
        country=instrument.country,
        timezone=instrument.timezone,
        multiplier=instrument.multiplier,
        expiry=instrument.expiry,
        strike=instrument.strike,
        option_type=instrument.option_type,
        is_synthetic=instrument.is_synthetic,
        relink_required=instrument.relink_required,
        provider_identifiers=[
            ProviderIdentifierResponse(
                provider=mapping.provider,
                provider_instrument_id=mapping.provider_instrument_id,
                provider_symbol=mapping.provider_symbol,
                is_verified=mapping.is_verified,
            )
            for mapping in instrument.provider_identifiers
        ],
    )


def _validated_candidate(
    provider: MarketDataProvider, candidate: InstrumentCandidate
) -> InstrumentCandidate:
    validated = InstrumentCandidate.model_validate(candidate)
    if validated.provider != provider.name:
        raise MarketDataProviderError("provider_identity_mismatch")
    return validated


def _upsert_provider_instrument(
    db: Session, candidate: InstrumentCandidate, observed_at: datetime
) -> Instrument:
    mapping = db.scalar(
        select(InstrumentProviderIdentifier)
        .where(
            InstrumentProviderIdentifier.provider == candidate.provider,
            InstrumentProviderIdentifier.provider_instrument_id == candidate.provider_instrument_id,
        )
        .options(selectinload(InstrumentProviderIdentifier.instrument).selectinload(Instrument.provider_identifiers))
    )
    if mapping is not None:
        mapping.provider_symbol = candidate.provider_symbol
        mapping.is_verified = candidate.is_verified
        mapping.last_seen_at = observed_at
        return mapping.instrument

    instrument = Instrument(
        symbol=candidate.symbol,
        display_name=candidate.display_name,
        asset_class=candidate.asset_class,
        venue=candidate.venue,
        currency=candidate.currency,
        country=candidate.country,
        timezone=candidate.timezone,
        multiplier=candidate.multiplier,
        expiry=candidate.expiry,
        strike=candidate.strike,
        option_type=candidate.option_type,
        is_synthetic=candidate.is_synthetic,
        created_at=observed_at,
    )
    instrument.provider_identifiers.append(
        InstrumentProviderIdentifier(
            provider=candidate.provider,
            provider_instrument_id=candidate.provider_instrument_id,
            provider_symbol=candidate.provider_symbol,
            is_verified=candidate.is_verified,
            last_seen_at=observed_at,
        )
    )
    db.add(instrument)
    db.flush()
    return instrument


def _get_instrument(db: Session, instrument_id: UUID) -> Instrument:
    instrument = db.scalar(
        select(Instrument)
        .where(Instrument.id == instrument_id)
        .options(selectinload(Instrument.provider_identifiers))
    )
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    return instrument


def _freshness(
    db: Session, instrument_id: UUID, interval: str, *, now: datetime | None = None
) -> FreshnessResponse:
    instrument = db.get(Instrument, instrument_id)
    is_synthetic_instrument = instrument.is_synthetic if instrument else False
    sync = db.scalar(
        select(MarketDataSync).where(
            MarketDataSync.instrument_id == instrument_id,
            MarketDataSync.interval == interval,
        ).order_by(
            MarketDataSync.attempted_at.desc(),
            MarketDataSync.succeeded_at.is_not(None).desc(),
        )
    )
    latest_bar = db.scalar(
        select(MarketBar)
        .where(MarketBar.instrument_id == instrument_id, MarketBar.interval == interval)
        .where(MarketBar.is_synthetic.is_(True) if is_synthetic_instrument else MarketBar.is_synthetic.is_(False))
        .order_by(MarketBar.timestamp.desc())
        .limit(1)
    )
    if sync is None:
        return FreshnessResponse(
            status="never_fetched",
            source=None,
            last_successful_update=None,
            latest_bar_timestamp=_utc(latest_bar.timestamp) if latest_bar else None,
            is_synthetic=latest_bar.is_synthetic if latest_bar else False,
            message="No successful market-data request has been recorded.",
        )
    if sync.status != "success" or latest_bar is None or sync.succeeded_at is None:
        return FreshnessResponse(
            status="unavailable",
            source=sync.source,
            last_successful_update=_utc(sync.succeeded_at) if sync.succeeded_at else None,
            latest_bar_timestamp=_utc(latest_bar.timestamp) if latest_bar else None,
            is_synthetic=latest_bar.is_synthetic if latest_bar else False,
            message="Market data is unavailable for this instrument and interval.",
        )
    freshness_seconds = (
        settings.market_data_daily_freshness_seconds
        if interval == "1d"
        else settings.market_data_intraday_freshness_seconds
    )
    reference_time = now or datetime.now(timezone.utc)
    succeeded_at = _utc(sync.succeeded_at)
    status: Literal["fresh", "stale", "unavailable", "never_fetched"] = (
        "fresh"
        if (reference_time - succeeded_at).total_seconds() <= freshness_seconds
        else "stale"
    )
    return FreshnessResponse(
        status=status,
        source=sync.source,
        last_successful_update=_utc(sync.succeeded_at),
        latest_bar_timestamp=_utc(latest_bar.timestamp),
        is_synthetic=latest_bar.is_synthetic,
        message=(
            "Synthetic fixture data; freshness describes the last successful fetch, "
            "not a live exchange feed."
            if latest_bar.is_synthetic
            else "Freshness is based on the last successful provider fetch."
        ),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_range(interval: str, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise HTTPException(status_code=422, detail="start and end must include a timezone")
    start_utc, end_utc = _utc(start), _utc(end)
    if start_utc >= end_utc:
        raise HTTPException(status_code=422, detail="start must be before end")
    max_days = {"1m": 7, "15m": 60, "1h": 730}.get(interval)
    if max_days is not None and end_utc - start_utc > timedelta(days=max_days):
        raise HTTPException(
            status_code=422,
            detail=f"Date range for {interval} bars cannot exceed {max_days} days",
        )
    return start_utc, end_utc


def _sync_record(
    db: Session,
    instrument_id: UUID,
    interval: str,
    *,
    source: str,
    attempted_at: datetime,
    status: str,
    rows_received: int,
    safe_error_code: str | None,
) -> MarketDataSync:
    sync = db.scalar(
        select(MarketDataSync).where(
            MarketDataSync.instrument_id == instrument_id,
            MarketDataSync.interval == interval,
            MarketDataSync.source == source,
        )
    )
    if sync is None:
        sync = MarketDataSync(
            instrument_id=instrument_id,
            interval=interval,
            source=source,
            attempted_at=attempted_at,
            succeeded_at=attempted_at if status == "success" else None,
            status=status,
            rows_received=rows_received,
            safe_error_code=safe_error_code,
        )
        db.add(sync)
    else:
        sync.attempted_at = attempted_at
        sync.succeeded_at = attempted_at if status == "success" else sync.succeeded_at
        sync.status = status
        sync.rows_received = rows_received
        sync.safe_error_code = safe_error_code
    return sync


@router.get("/provider", response_model=ProviderResponse)
def provider_status(
    provider: MarketDataProvider = Depends(get_market_data_provider),
    _user: User = Depends(get_current_user),
):
    try:
        health: ProviderHealth = provider.health()
    except (MarketDataProviderError, TimeoutError):
        raise HTTPException(status_code=503, detail="Market-data provider is unavailable")
    return ProviderResponse(
        provider=ProviderHealthResponse(
            name=health.name,
            healthy=health.healthy,
            is_mock=health.is_mock,
            capabilities=list(health.capabilities),
            status=health.status,
        ),
        configuration=(
            "synthetic fixture provider"
            if health.is_mock
            else "public providers; no API credentials required"
            if health.status.startswith("provider_chain:public_provider")
            else "credentials configured; live connectivity is not verified"
        ),
    )


@router.get("/instruments", response_model=list[InstrumentResponse])
def search_instruments(
    query: str = Query(min_length=1, max_length=80),
    limit: int = Query(default=20, ge=1, le=50),
    provider: MarketDataProvider = Depends(get_market_data_provider),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        providers = {item.name: item for item in _providers(provider)}
        candidates = [
            _validated_candidate(providers[candidate.provider], candidate)
            for candidate in provider.search_instruments(query.strip(), limit)
            if candidate.provider in providers
        ]
        if len(candidates) > limit:
            raise MarketDataProviderError("provider_exceeded_search_limit")
        instruments = [
            _upsert_provider_instrument(db, candidate, datetime.now(timezone.utc))
            for candidate in candidates
        ]
        db.commit()
        for instrument in instruments:
            db.refresh(instrument)
        return [_instrument_response(instrument) for instrument in instruments]
    except (MarketDataProviderError, TimeoutError, ValueError):
        db.rollback()
        raise HTTPException(status_code=503, detail="Instrument search is temporarily unavailable")


@router.get("/instruments/{instrument_id}", response_model=InstrumentResponse)
def get_instrument(
    instrument_id: UUID,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _instrument_response(_get_instrument(db, instrument_id))


def _growth(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous in (None, Decimal("0")):
        return None
    return (current - previous) / abs(previous) * Decimal("100")


def _fundamental_period_responses(
    rows: list[InstrumentFundamental],
) -> list[FundamentalPeriodResponse]:
    ordered = sorted(rows, key=lambda row: row.period_end)
    values: list[FundamentalPeriodResponse] = []
    for row in ordered:
        previous = [
            candidate for candidate in ordered
            if 360 <= (row.period_end - candidate.period_end).days <= 370
        ]
        prior = min(previous, key=lambda candidate: abs((row.period_end - candidate.period_end).days - 365), default=None)
        values.append(FundamentalPeriodResponse(
            id=row.id,
            period_type=row.period_type,
            period_end=row.period_end,
            currency=row.currency,
            revenue=row.revenue,
            net_income=row.net_income,
            eps=row.eps,
            operating_margin=row.operating_margin,
            total_assets=row.total_assets,
            total_liabilities=row.total_liabilities,
            total_equity=row.total_equity,
            operating_cash_flow=row.operating_cash_flow,
            free_cash_flow=row.free_cash_flow,
            total_debt=row.total_debt,
            shares_outstanding=row.shares_outstanding,
            source=row.source,
            fetched_at=_utc(row.fetched_at),
            stale=fundamentals_service.is_stale(row.fetched_at),
            revenue_yoy_growth_pct=_growth(row.revenue, prior.revenue if prior else None),
            net_income_yoy_growth_pct=_growth(row.net_income, prior.net_income if prior else None),
            eps_yoy_growth_pct=_growth(row.eps, prior.eps if prior else None),
            free_cash_flow_yoy_growth_pct=_growth(row.free_cash_flow, prior.free_cash_flow if prior else None),
        ))
    return values


@router.post(
    "/instruments/{instrument_id}/fundamentals/sync",
    response_model=FundamentalsSyncResponse,
)
def sync_instrument_fundamentals(
    instrument_id: UUID,
    _session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FundamentalsSyncResponse:
    instrument = _get_instrument(db, instrument_id)
    if instrument.is_synthetic:
        return FundamentalsSyncResponse(
            instrument_id=instrument.id,
            status="unavailable",
            source=None,
            fetched_at=None,
            fundamentals_count=0,
            message="Fundamentals are unavailable for synthetic demo instruments; no fallback data was used.",
        )
    chain = create_market_data_provider_chain(("yfinance",))
    yfinance_provider = next(
        (active for active in _providers(chain) if active.name == "yfinance"),
        None,
    )
    if yfinance_provider is None:
        return FundamentalsSyncResponse(
            instrument_id=instrument.id,
            status="unavailable",
            source=None,
            fetched_at=None,
            fundamentals_count=0,
            message="Fundamentals are unavailable because the yfinance provider could not be initialized.",
        )
    try:
        candidate, _mapping = _resolve_provider_candidate(db, instrument, yfinance_provider)
        rows, snapshot = fundamentals_service.sync_instrument_fundamentals(
            db, instrument, candidate.provider_symbol
        )
    except (MarketDataProviderError, TimeoutError, ValueError):
        db.rollback()
        return FundamentalsSyncResponse(
            instrument_id=instrument.id,
            status="unavailable",
            source=None,
            fetched_at=None,
            fundamentals_count=0,
            message="Fundamentals are unavailable from providers. No synthetic fallback was used.",
        )
    snapshot_fields = ("market_cap", "pe", "pb", "dividend_yield", "sector", "industry")
    has_values = any(getattr(snapshot, field) is not None for field in snapshot_fields) or any(
        getattr(row, field) is not None
        for row in rows
        for field in (
            "revenue", "net_income", "eps", "operating_margin", "total_assets",
            "total_liabilities", "total_equity", "operating_cash_flow",
            "free_cash_flow", "total_debt", "shares_outstanding",
        )
    )
    if not has_values:
        return FundamentalsSyncResponse(
            instrument_id=instrument.id,
            status="unavailable",
            source=snapshot.source,
            fetched_at=_utc(snapshot.fetched_at),
            fundamentals_count=len(rows),
            message="The provider returned no non-null fundamentals. No values were estimated.",
        )
    return FundamentalsSyncResponse(
        instrument_id=instrument.id,
        status="available",
        source=snapshot.source,
        fetched_at=_utc(snapshot.fetched_at),
        fundamentals_count=len(rows),
        message="Provider fundamentals synchronized.",
    )


@router.get(
    "/instruments/{instrument_id}/fundamentals",
    response_model=FundamentalsResponse,
)
def get_instrument_fundamentals(
    instrument_id: UUID,
    period_type: Literal["annual", "quarterly"] | None = Query(default=None),
    _session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
) -> FundamentalsResponse:
    instrument = _get_instrument(db, instrument_id)
    query = select(InstrumentFundamental).where(
        InstrumentFundamental.instrument_id == instrument.id
    )
    if period_type is not None:
        query = query.where(InstrumentFundamental.period_type == period_type)
    fundamentals = list(db.scalars(query))
    snapshot = db.scalar(
        select(InstrumentSnapshot)
        .where(InstrumentSnapshot.instrument_id == instrument.id)
        .limit(1)
    )
    snapshot_response = None if snapshot is None else FundamentalSnapshotResponse(
        market_cap=snapshot.market_cap,
        pe=snapshot.pe,
        pb=snapshot.pb,
        dividend_yield=snapshot.dividend_yield,
        sector=snapshot.sector,
        industry=snapshot.industry,
        source=snapshot.source,
        fetched_at=_utc(snapshot.fetched_at),
        stale=fundamentals_service.is_stale(snapshot.fetched_at),
    )
    rows = _fundamental_period_responses(fundamentals)
    snapshot_has_values = snapshot is not None and any(
        getattr(snapshot, field) is not None
        for field in ("market_cap", "pe", "pb", "dividend_yield", "sector", "industry")
    )
    period_has_values = any(
        getattr(row, field) is not None
        for row in fundamentals
        for field in (
            "revenue", "net_income", "eps", "operating_margin", "total_assets",
            "total_liabilities", "total_equity", "operating_cash_flow",
            "free_cash_flow", "total_debt", "shares_outstanding",
        )
    )
    unavailable = not snapshot_has_values and not period_has_values
    return FundamentalsResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        currency=instrument.currency,
        snapshot=snapshot_response,
        fundamentals=rows,
        unavailable=unavailable,
        message=(
            "Fundamentals are unavailable from providers."
            if unavailable
            else "Provider-reported fundamentals are shown with source and freshness."
        ),
    )


@indicators_router.get(
    "/instruments/{instrument_id}/indicators", response_model=IndicatorsResponse
)
def get_instrument_indicators(
    instrument_id: UUID,
    interval: BarInterval = Query(default="1d"),
    _session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
) -> IndicatorsResponse:
    instrument = _get_instrument(db, instrument_id)
    latest_bar = db.scalar(
        select(MarketBar)
        .where(MarketBar.instrument_id == instrument.id, MarketBar.interval == interval)
        .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
        .limit(1)
    )
    bars: list[MarketBar] = []
    if latest_bar is not None:
        bars = list(
            db.scalars(
                select(MarketBar)
                .where(
                    MarketBar.instrument_id == instrument.id,
                    MarketBar.interval == interval,
                    MarketBar.source == latest_bar.source,
                )
                .order_by(MarketBar.timestamp.desc())
                .limit(MAX_BARS)
            )
        )
        bars.reverse()
    calculated = compute_indicators(bars)
    indicators = {
        name: _indicator_result(value)
        for name, value in calculated.__dict__.items()
    }
    return IndicatorsResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        currency=instrument.currency,
        interval=interval,
        freshness=_freshness(db, instrument.id, interval),
        source=latest_bar.source if latest_bar else None,
        latest_bar=_utc(latest_bar.timestamp) if latest_bar else None,
        bars_available=len(bars),
        indicators=indicators,
    )


@router.get("/watchlist", response_model=list[WatchlistEntryResponse])
def get_watchlist(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entries = db.scalars(
        select(WatchlistInstrument)
        .where(WatchlistInstrument.user_id == user.id)
        .options(
            selectinload(WatchlistInstrument.instrument).selectinload(
                Instrument.provider_identifiers
            )
        )
        .order_by(WatchlistInstrument.created_at, WatchlistInstrument.id)
    ).all()
    quotes_by_instrument: dict[UUID, MarketQuote] = {}
    if entries:
        cached_quotes = db.scalars(
            select(MarketQuote)
            .where(
                MarketQuote.instrument_id.in_(
                    [entry.instrument_id for entry in entries]
                )
            )
            .order_by(MarketQuote.retrieved_at.desc(), MarketQuote.source)
        ).all()
        for quote in cached_quotes:
            quotes_by_instrument.setdefault(quote.instrument_id, quote)

    now = datetime.now(timezone.utc)
    result = []
    for entry in entries:
        instrument = entry.instrument
        quote = quotes_by_instrument.get(instrument.id)
        quote_response = None
        if quote is not None:
            retrieved_at = _utc(quote.retrieved_at)
            quote_timestamp = _utc(quote.timestamp)
            age = max(
                (now - retrieved_at).total_seconds(),
                (now - quote_timestamp).total_seconds(),
            )
            quote_response = WatchlistQuoteResponse(
                source=quote.source,
                retrieved_at=retrieved_at,
                freshness=(
                    "fresh"
                    if 0 <= age <= settings.market_data_quote_freshness_seconds
                    else "stale"
                ),
                is_synthetic=quote.is_synthetic,
                is_verified=any(
                    mapping.provider == quote.source and mapping.is_verified
                    for mapping in instrument.provider_identifiers
                ),
                close=quote.close,
            )
        result.append(
            WatchlistEntryResponse(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                display_name=instrument.display_name,
                asset_class=instrument.asset_class,
                venue=instrument.venue,
                currency=instrument.currency,
                is_synthetic=instrument.is_synthetic,
                relink_required=instrument.relink_required,
                quote=quote_response,
            )
        )
    return result


@router.post("/instruments/cleanup-synthetic")
def cleanup_synthetic_instruments(
    provider: MarketDataProvider = Depends(get_market_data_provider),
    _: UserSession = Depends(require_csrf),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    synthetic_instruments = db.scalars(
        select(Instrument).where(
            Instrument.is_synthetic.is_(True),
            Instrument.venue.like("DEMO%"),
        )
    ).all()
    deleted = 0
    marked_for_relink = 0
    deleted_ids: list[str] = []
    relink_ids: list[str] = []
    resolved_replacements: dict[str, list[str]] = {}
    resolution_failures: dict[str, str] = {}
    for instrument in synthetic_instruments:
        linked = db.scalar(
            select(PortfolioTransaction.id)
            .where(PortfolioTransaction.instrument_id == instrument.id)
            .limit(1)
        )
        if linked is not None:
            instrument.relink_required = True
            marked_for_relink += 1
            relink_ids.append(str(instrument.id))
            replacements = []
            try:
                candidates = provider.search_instruments(instrument.symbol, 50)
            except MarketDataProviderError as error:
                candidates = []
                resolution_failures[str(instrument.id)] = error.code
            except (TimeoutError, ValueError):
                candidates = []
                resolution_failures[str(instrument.id)] = "provider_unavailable"
            for candidate in candidates:
                if candidate.is_synthetic or candidate.symbol.casefold() != instrument.symbol.casefold():
                    continue
                replacement = _upsert_provider_instrument(db, candidate, datetime.now(timezone.utc))
                replacements.append(str(replacement.id))
            if replacements:
                resolved_replacements[str(instrument.id)] = replacements
        else:
            db.delete(instrument)
            deleted += 1
            deleted_ids.append(str(instrument.id))
    db.commit()
    return {
        "deleted_unlinked": deleted,
        "marked_for_relink": marked_for_relink,
        "deleted_ids": deleted_ids,
        "relink_ids": relink_ids,
        "resolved_replacements": resolved_replacements,
        "resolution_failures": resolution_failures,
        "message": "Synthetic holdings were retained and marked for relinking.",
    }


@router.post("/watchlist/{instrument_id}", status_code=204)
def add_watchlist_instrument(
    instrument_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    _get_instrument(db, instrument_id)
    existing = db.scalar(
        select(WatchlistInstrument).where(
            WatchlistInstrument.user_id == user.id,
            WatchlistInstrument.instrument_id == instrument_id,
        )
    )
    if existing is None:
        db.add(
            WatchlistInstrument(
                user_id=user.id,
                instrument_id=instrument_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    return None


@router.delete("/watchlist/{instrument_id}", status_code=204)
def remove_watchlist_instrument(
    instrument_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    item = db.scalar(
        select(WatchlistInstrument).where(
            WatchlistInstrument.user_id == user.id,
            WatchlistInstrument.instrument_id == instrument_id,
        )
    )
    if item is not None:
        db.delete(item)
        db.commit()
    return None


@router.get("/instruments/{instrument_id}/quote", response_model=QuoteResponse)
def latest_quote(
    instrument_id: UUID,
    refresh: bool = Query(default=False),
    provider: MarketDataProvider = Depends(get_market_data_provider),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    instrument = _get_instrument(db, instrument_id)
    now = datetime.now(timezone.utc)
    stale_cache: tuple[MarketQuote, bool] | None = None
    for active_provider in _providers(provider):
        if active_provider.is_mock and not instrument.is_synthetic:
            continue
        provider_mapping = next(
            (
                item for item in instrument.provider_identifiers
                if item.provider == active_provider.name
            ),
            None,
        )
        cached_quote = db.scalar(
            select(MarketQuote).where(
                MarketQuote.instrument_id == instrument.id,
                MarketQuote.source == active_provider.name,
            )
        )
        if cached_quote is not None and (
            instrument.is_synthetic or not cached_quote.is_synthetic
        ):
            cache_age = (now - _utc(cached_quote.retrieved_at)).total_seconds()
            if not refresh and 0 <= cache_age <= settings.market_data_quote_freshness_seconds:
                price_age = (now - _utc(cached_quote.timestamp)).total_seconds()
                price_is_stale = price_age > settings.market_data_quote_freshness_seconds
                db.commit()
                return _quote_response(
                    instrument,
                    cached_quote,
                    freshness="stale" if price_is_stale else "fresh",
                    freshness_message=(
                        "Quote data is older than the configured freshness window."
                        if price_is_stale
                        else "Cached quote is within the configured freshness window."
                    ),
                    is_cached=True,
                    is_verified=provider_mapping.is_verified if provider_mapping else False,
                )
            stale_cache = stale_cache or (
                cached_quote,
                provider_mapping.is_verified if provider_mapping else False,
            )
        try:
            candidate, mapping = _resolve_provider_candidate(db, instrument, active_provider)
        except (MarketDataProviderError, TimeoutError, ValueError):
            continue
        if cached_quote is not None and stale_cache is not None:
            stale_cache = (stale_cache[0], mapping.is_verified)
        try:
            bar = HistoricalBar.model_validate(active_provider.get_latest_quote(candidate))
        except (MarketDataProviderError, TimeoutError, ValueError):
            continue

        retrieved_at = datetime.now(timezone.utc)
        if cached_quote is None:
            cached_quote = MarketQuote(instrument_id=instrument.id, source=active_provider.name)
            db.add(cached_quote)
        cached_quote.timestamp = bar.timestamp
        cached_quote.open = bar.open
        cached_quote.high = bar.high
        cached_quote.low = bar.low
        cached_quote.close = bar.close
        cached_quote.volume = bar.volume
        cached_quote.retrieved_at = retrieved_at
        cached_quote.is_synthetic = active_provider.is_mock or candidate.is_synthetic
        price_is_stale = (
            retrieved_at - _utc(bar.timestamp)
        ).total_seconds() > settings.market_data_quote_freshness_seconds
        db.commit()
        db.refresh(cached_quote)
        return _quote_response(
            instrument,
            cached_quote,
            freshness="stale" if price_is_stale else "fresh",
            freshness_message=(
                "Synthetic fallback quote; this is not a live market price."
                if cached_quote.is_synthetic
                else "Provider returned a quote older than the configured freshness window."
                if price_is_stale
                else f"Quote retrieved from {active_provider.name}."
            ),
            is_cached=False,
            is_verified=mapping.is_verified,
        )

    db.rollback()
    if stale_cache is not None:
        cached_quote, is_verified = stale_cache
        return _quote_response(
            instrument,
            cached_quote,
            freshness="stale",
            freshness_message=(
                "All configured providers failed; showing the last cached quote, which may be stale."
            ),
            is_cached=True,
            is_verified=is_verified,
        )
    raise HTTPException(status_code=503, detail="Quote is temporarily unavailable")


def _quote_response(
    instrument: Instrument,
    quote: MarketQuote,
    *,
    freshness: Literal["fresh", "stale"],
    freshness_message: str,
    is_cached: bool,
    is_verified: bool,
) -> QuoteResponse:
    return QuoteResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        currency=instrument.currency,
        source=quote.source,
        retrieved_at=_utc(quote.retrieved_at),
        freshness=freshness,
        freshness_message=freshness_message,
        is_cached=is_cached,
        is_synthetic=quote.is_synthetic,
        is_verified=is_verified,
        bar=HistoricalBar(
            timestamp=_utc(quote.timestamp),
            open=quote.open,
            high=quote.high,
            low=quote.low,
            close=quote.close,
            volume=quote.volume,
        ),
    )


@router.post(
    "/instruments/{instrument_id}/history/sync",
    response_model=HistoryResponse,
)
def sync_history(
    instrument_id: UUID,
    start: datetime,
    end: datetime,
    interval: BarInterval = Query(default="1d"),
    limit: int = Query(default=MAX_BARS, ge=1, le=MAX_BARS),
    provider: MarketDataProvider = Depends(get_market_data_provider),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    instrument = _get_instrument(db, instrument_id)
    start_utc, end_utc = _validate_range(interval, start, end)
    cached_bars = list(
        db.scalars(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == interval,
                MarketBar.is_synthetic.is_(instrument.is_synthetic),
                MarketBar.timestamp >= start_utc,
                MarketBar.timestamp < end_utc,
            )
            .order_by(MarketBar.timestamp)
        )
    )
    missing_ranges: list[tuple[datetime, datetime]] = []
    if interval == "1d" and cached_bars:
        cached_timestamps = sorted({_utc(bar.timestamp) for bar in cached_bars})
        if start_utc < cached_timestamps[0]:
            missing_ranges.append((start_utc, cached_timestamps[0]))
        for previous, current in zip(cached_timestamps, cached_timestamps[1:]):
            if current - previous > timedelta(days=3):
                missing_ranges.append(
                    (previous + timedelta(days=1), current)
                )
        if cached_timestamps[-1] + timedelta(days=1) < end_utc:
            missing_ranges.append(
                (cached_timestamps[-1] + timedelta(days=1), end_utc)
            )
    elif cached_bars:
        db.commit()
        return get_history(
            instrument_id=instrument_id,
            interval=interval,
            start=start_utc,
            end=end_utc,
            offset=0,
            limit=limit,
            _user=_user,
            db=db,
        )
    else:
        missing_ranges.append((start_utc, end_utc))

    if not missing_ranges:
        db.commit()
        return get_history(
            instrument_id=instrument_id,
            interval=interval,
            start=start_utc,
            end=end_utc,
            offset=0,
            limit=limit,
            _user=_user,
            db=db,
        )

    selected_provider: MarketDataProvider | None = None
    selected_candidate: InstrumentCandidate | None = None
    validated_bars: list[HistoricalBar] = []
    yfinance_lookback_error: str | None = None
    for active_provider in _providers(provider):
        if active_provider.is_mock and not instrument.is_synthetic:
            continue
        try:
            candidate, _mapping = _resolve_provider_candidate(db, instrument, active_provider)
            provider_bars: list[HistoricalBar] = []
            for range_start, range_end in missing_ranges:
                remaining = limit - len(provider_bars)
                if remaining <= 0:
                    break
                try:
                    bars = active_provider.get_historical_bars(
                        candidate, interval, range_start, range_end, remaining
                    )
                    candidate_bars = [HistoricalBar.model_validate(bar) for bar in bars]
                    if len(candidate_bars) > remaining:
                        raise MarketDataProviderError("provider_exceeded_bar_limit")
                    if any(
                        not range_start <= bar.timestamp < range_end
                        for bar in candidate_bars
                    ):
                        raise MarketDataProviderError("provider_returned_out_of_range_bar")
                    provider_bars.extend(candidate_bars)
                except MarketDataProviderError:
                    raise
            if not provider_bars:
                raise MarketDataProviderError("provider_no_history_for_range")
            selected_provider = active_provider
            selected_candidate = candidate
            validated_bars = sorted(
                {bar.timestamp: bar for bar in provider_bars}.values(),
                key=lambda bar: bar.timestamp,
            )
            break
        except MarketDataProviderError as error:
            if error.code.startswith("yfinance_") and "lookback_exceeded" in error.code:
                yfinance_lookback_error = error.code
            _sync_record(
                db,
                instrument.id,
                interval,
                source=active_provider.name,
                attempted_at=datetime.now(timezone.utc),
                status="unavailable",
                rows_received=0,
                safe_error_code=error.code[:48],
            )
        except (TimeoutError, ValueError):
            _sync_record(
                db,
                instrument.id,
                interval,
                source=active_provider.name,
                attempted_at=datetime.now(timezone.utc),
                status="unavailable",
                rows_received=0,
                safe_error_code="provider_unavailable_or_invalid_response",
            )
    if selected_provider is None or selected_candidate is None:
        db.commit()
        if yfinance_lookback_error:
            max_days = {"1m": 7, "15m": 60, "1h": 730}.get(interval, 0)
            raise HTTPException(
                status_code=422,
                detail=f"Yahoo Finance {interval} history is limited to the most recent {max_days} days."
                if max_days
                else "Yahoo Finance history is unavailable for the requested range.",
            )
        if cached_bars:
            return get_history(
                instrument_id=instrument_id,
                interval=interval,
                start=start_utc,
                end=end_utc,
                offset=0,
                limit=limit,
                _user=_user,
                db=db,
            )
        raise HTTPException(status_code=503, detail="Historical market data is temporarily unavailable")

    retrieved_at = datetime.now(timezone.utc)
    values = [
        {
            "id": uuid4(),
            "instrument_id": instrument.id,
            "interval": interval,
            "timestamp": bar.timestamp,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "source": selected_provider.name,
            "retrieved_at": retrieved_at,
            "is_synthetic": selected_provider.is_mock or selected_candidate.is_synthetic,
        }
        for bar in validated_bars
    ]
    if values:
        dialect_name = db.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = pg_insert(MarketBar).values(values).on_conflict_do_nothing(
                index_elements=["instrument_id", "interval", "timestamp", "source"]
            )
            db.execute(statement)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(MarketBar).values(values).on_conflict_do_nothing(
                index_elements=["instrument_id", "interval", "timestamp", "source"]
            )
            db.execute(statement)
        else:
            for value in values:
                existing = db.scalar(
                    select(MarketBar.id).where(
                        MarketBar.instrument_id == instrument.id,
                        MarketBar.interval == interval,
                        MarketBar.timestamp == value["timestamp"],
                        MarketBar.source == selected_provider.name,
                    )
                )
                if existing is None:
                    db.add(MarketBar(**value))

    _sync_record(
        db,
        instrument.id,
        interval,
        source=selected_provider.name,
        attempted_at=retrieved_at,
        status="success",
        rows_received=len(validated_bars),
        safe_error_code=None,
    )
    db.commit()
    return get_history(
        instrument_id=instrument_id,
        interval=interval,
        start=start_utc,
        end=end_utc,
        offset=0,
        limit=limit,
        _user=_user,
        db=db,
    )


@router.get("/instruments/{instrument_id}/history", response_model=HistoryResponse)
def get_history(
    instrument_id: UUID,
    interval: BarInterval = Query(default="1d"),
    start: datetime | None = None,
    end: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=MAX_BARS),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    instrument = _get_instrument(db, instrument_id)
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="start and end must be supplied together")
    query = select(MarketBar).where(
        MarketBar.instrument_id == instrument.id,
        MarketBar.interval == interval,
    )
    if not instrument.is_synthetic:
        query = query.where(MarketBar.is_synthetic.is_(False))
    if start is not None and end is not None:
        start_utc, end_utc = _validate_range(interval, start, end)
        query = query.where(MarketBar.timestamp >= start_utc, MarketBar.timestamp < end_utc)
    bars = db.scalars(
        query.order_by(MarketBar.timestamp).offset(offset).limit(limit)
    ).all()
    return HistoryResponse(
        instrument_id=instrument.id,
        interval=interval,
        freshness=_freshness(db, instrument.id, interval),
        bars=[
            MarketBarResponse(
                timestamp=_utc(bar.timestamp),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source=bar.source,
                retrieved_at=_utc(bar.retrieved_at),
                is_synthetic=bar.is_synthetic,
            )
            for bar in bars
        ],
        offset=offset,
        limit=limit,
    )
