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

from app.api.auth import get_current_user, require_csrf
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
    InstrumentProviderIdentifier,
    MarketBar,
    MarketQuote,
    MarketDataSync,
    PortfolioTransaction,
    User,
    UserSession,
    WatchlistInstrument,
)
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
    cached_range = db.scalar(
        select(MarketBar.id)
        .where(
            MarketBar.instrument_id == instrument.id,
            MarketBar.interval == interval,
            MarketBar.is_synthetic.is_(True if instrument.is_synthetic else False),
            MarketBar.timestamp >= start_utc,
            MarketBar.timestamp < end_utc,
        )
        .limit(1)
    )
    if cached_range is not None:
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
            bars = active_provider.get_historical_bars(
                candidate, interval, start_utc, end_utc, limit
            )
            candidate_bars = [HistoricalBar.model_validate(bar) for bar in bars]
            if len(candidate_bars) > limit:
                raise MarketDataProviderError("provider_exceeded_bar_limit")
            if any(not start_utc <= bar.timestamp < end_utc for bar in candidate_bars):
                raise MarketDataProviderError("provider_returned_out_of_range_bar")
            selected_provider = active_provider
            selected_candidate = candidate
            validated_bars = candidate_bars
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
            max_days = {"1m": 7, "15m": 60, "1h": 730}[interval]
            raise HTTPException(
                status_code=422,
                detail=f"Yahoo Finance {interval} history is limited to the most recent {max_days} days.",
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
