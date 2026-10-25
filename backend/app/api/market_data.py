from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import logging
import random
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, select
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
    MarketDataSyncAttempt,
    PortfolioTransaction,
    Portfolio,
    ResearchNote,
    ResearchSource,
    InstrumentResearchBrief,
    InstrumentResearchRun,
    InstrumentSourceLink,
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
MAX_BACKFILL_CHUNKS = 20
MAX_BACKFILL_REQUESTS = 24
BACKFILL_CHUNK_DAYS = 450
RETRY_AFTER_CAP_SECONDS = 8
logger = logging.getLogger(__name__)
_provider_rate_lock = threading.Lock()
_provider_next_request: dict[str, float] = {}


def _log_market_data_failure(
    message: str, error: Exception, safe_reason: str, *context: object
) -> None:
    sanitized_error = RuntimeError(f"{type(error).__name__}: {safe_reason}")
    logger.error(
        message,
        *context,
        exc_info=(
            type(sanitized_error),
            sanitized_error,
            error.__traceback__,
        ),
    )


class MarketDataProviderChain:
    def __init__(self, providers: list[MarketDataProvider]):
        self.providers = providers
        self.name = providers[0].name if providers else "unavailable"
        self.is_mock = False

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not self.providers:
            raise MarketDataProviderError("no_market_data_provider_configured")
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
        if not self.providers:
            return ProviderHealth(
                name="unavailable",
                healthy=False,
                is_mock=False,
                capabilities=(),
                status="no_real_provider_configured",
            )
        primary = self.providers[0].health()
        return ProviderHealth(
            name=",".join(provider.name for provider in self.providers),
            healthy=primary.healthy,
            is_mock=False,
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
    providers = [provider for provider in providers if not provider.is_mock]
    return MarketDataProviderChain(providers)


def get_market_data_provider() -> MarketDataProvider:
    return create_market_data_provider_chain()


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
    if validated.is_synthetic or provider.is_mock:
        raise MarketDataProviderError("synthetic_market_data_disabled")
    return validated


def _upsert_provider_instrument(
    db: Session, candidate: InstrumentCandidate, observed_at: datetime
) -> Instrument:
    if candidate.is_synthetic:
        raise MarketDataProviderError("synthetic_market_data_disabled")
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
    requested_start: datetime | None = None,
    requested_end: datetime | None = None,
    requested_limit: int | None = None,
    rows_written: int = 0,
    response_status: int | None = None,
    safe_reason: str | None = None,
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
            requested_start=requested_start,
            requested_end=requested_end,
            requested_limit=requested_limit,
            rows_written=rows_written,
            response_status=response_status,
            safe_reason=safe_reason,
        )
        db.add(sync)
    else:
        sync.attempted_at = attempted_at
        sync.succeeded_at = attempted_at if status == "success" else sync.succeeded_at
        sync.status = status
        sync.rows_received = rows_received
        sync.safe_error_code = safe_error_code
        sync.requested_start = requested_start
        sync.requested_end = requested_end
        sync.requested_limit = requested_limit
        sync.rows_written = rows_written
        sync.response_status = response_status
        sync.safe_reason = safe_reason
    return sync


def _record_sync_attempt(
    db: Session,
    *,
    instrument_id: UUID,
    interval: str,
    provider: str,
    start: datetime,
    end: datetime,
    limit: int,
    response_status: int | None,
    rows_returned: int,
    rows_written: int,
    outcome: str,
    reason: str | None,
    attempted_at: datetime,
) -> None:
    db.add(MarketDataSyncAttempt(
        instrument_id=instrument_id,
        interval=interval,
        provider=provider,
        requested_start=start,
        requested_end=end,
        requested_limit=limit,
        response_status=response_status,
        rows_returned=rows_returned,
        rows_written=rows_written,
        outcome=outcome,
        reason=reason[:200] if reason else None,
        attempted_at=attempted_at,
    ))


def _normalize_bar_timestamp(
    bar: HistoricalBar, candidate: InstrumentCandidate, interval: str
) -> HistoricalBar:
    if interval != "1d":
        return bar
    # Daily candles are keyed by their exchange-local session date, stored at UTC midnight.
    session_date = bar.timestamp.astimezone(ZoneInfo(candidate.timezone)).date()
    return bar.model_copy(update={
        "timestamp": datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
    })


def _persist_real_bars(
    db: Session,
    instrument: Instrument,
    provider: MarketDataProvider,
    candidate: InstrumentCandidate,
    interval: str,
    bars: list[HistoricalBar],
    retrieved_at: datetime,
) -> int:
    if provider.is_mock or candidate.is_synthetic:
        raise MarketDataProviderError("synthetic_market_data_disabled")
    values: list[dict] = []
    for bar in bars:
        values.append({
            "id": uuid4(),
            "instrument_id": instrument.id,
            "interval": interval,
            "timestamp": bar.timestamp,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "source": provider.name,
            "retrieved_at": retrieved_at,
            "is_synthetic": False,
        })
    if not values:
        return 0
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = pg_insert(MarketBar).values(values).on_conflict_do_nothing(
            index_elements=["instrument_id", "interval", "timestamp", "source"]
        )
        result = db.execute(statement)
        return max(0, result.rowcount or 0)
    if dialect_name == "sqlite":
        statement = sqlite_insert(MarketBar).values(values).on_conflict_do_nothing(
            index_elements=["instrument_id", "interval", "timestamp", "source"]
        )
        result = db.execute(statement)
        return max(0, result.rowcount or 0)
    written = 0
    for value in values:
        existing = db.scalar(
            select(MarketBar.id).where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == interval,
                MarketBar.timestamp == value["timestamp"],
                MarketBar.source == provider.name,
            )
        )
        if existing is None:
            db.add(MarketBar(**value))
            written += 1
    return written


def _provider_host(provider: MarketDataProvider) -> str:
    base_url = str(getattr(provider, "_base_url", "https://query1.finance.yahoo.com"))
    return urlsplit(base_url).hostname or provider.name


def _wait_for_provider(host: str) -> None:
    with _provider_rate_lock:
        now = time.monotonic()
        next_allowed = _provider_next_request.get(host, now)
        scheduled = max(now, next_allowed)
        _provider_next_request[host] = scheduled + 1.0
    if scheduled > now:
        time.sleep(scheduled - now)


def _retry_after_seconds(value: str | None, attempt: int) -> float:
    try:
        requested = float(value) if value is not None else 0.0
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            requested = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            requested = 0.0
    backoff = min(RETRY_AFTER_CAP_SECONDS, max(0.5, requested or (2 ** attempt)))
    return min(RETRY_AFTER_CAP_SECONDS, backoff + random.uniform(0, 0.2))


def _fetch_provider_history(
    active_provider: MarketDataProvider,
    candidate: InstrumentCandidate,
    interval: str,
    start: datetime,
    end: datetime,
    limit: int,
    request_budget: list[int] | None = None,
    rows_returned: list[int] | None = None,
) -> list[HistoricalBar]:
    if active_provider.is_mock or candidate.is_synthetic:
        raise MarketDataProviderError("synthetic_market_data_disabled")
    for attempt in range(2):
        if request_budget is not None:
            if request_budget[0] <= 0:
                raise MarketDataProviderError("provider_request_cap_reached")
            request_budget[0] -= 1
        _wait_for_provider(_provider_host(active_provider))
        try:
            returned = active_provider.get_historical_bars(
                candidate, interval, start, end, limit
            )
            if rows_returned is not None:
                rows_returned[0] += len(returned)
            bars = [
                _normalize_bar_timestamp(HistoricalBar.model_validate(bar), candidate, interval)
                for bar in returned
            ]
            if len(bars) > limit:
                raise MarketDataProviderError("provider_exceeded_bar_limit")
            if any(not start <= bar.timestamp < end for bar in bars):
                raise MarketDataProviderError("provider_returned_out_of_range_bar")
            return bars
        except MarketDataProviderError as error:
            if error.status_code == 429 and attempt == 0:
                time.sleep(_retry_after_seconds(error.retry_after, attempt))
                continue
            raise
    raise MarketDataProviderError("provider_rate_limit_exceeded", status_code=429)


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
            else "no real market-data provider configured"
            if health.status == "no_real_provider_configured"
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
    active_session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
):
    owned_instrument_ids = set(db.scalars(
        select(PortfolioTransaction.instrument_id)
        .join(Portfolio, Portfolio.id == PortfolioTransaction.portfolio_id)
        .where(
            Portfolio.user_id == active_session.user_id,
            PortfolioTransaction.instrument_id.is_not(None),
        )
    ))
    owned_instrument_ids.update(db.scalars(
        select(WatchlistInstrument.instrument_id).where(
            WatchlistInstrument.user_id == active_session.user_id
        )
    ))
    if not owned_instrument_ids:
        return {
            "deleted_unlinked": 0,
            "marked_for_relink": 0,
            "deleted_ids": [],
            "relink_ids": [],
            "cleared_transaction_ids": {},
            "resolved_replacements": {},
            "resolution_failures": {},
            "message": "No owned synthetic demo instruments were found.",
        }
    synthetic_instruments = db.scalars(
        select(Instrument).where(
            Instrument.is_synthetic.is_(True),
            Instrument.symbol == "ALPHA",
            Instrument.venue.like("DEMO%"),
            Instrument.id.in_(owned_instrument_ids),
        )
    ).all()
    deleted = 0
    marked_for_relink = 0
    deleted_ids: list[str] = []
    relink_ids: list[str] = []
    cleared_transaction_ids: dict[str, list[str]] = {}
    resolved_replacements: dict[str, list[str]] = {}
    resolution_failures: dict[str, str] = {}
    for instrument in synthetic_instruments:
        linked_ids = db.scalars(
            select(PortfolioTransaction.id)
            .join(Portfolio, Portfolio.id == PortfolioTransaction.portfolio_id)
            .where(
                PortfolioTransaction.instrument_id == instrument.id,
                Portfolio.user_id == active_session.user_id,
            )
        ).all()
        other_owner_transaction = db.scalar(
            select(PortfolioTransaction.id)
            .join(Portfolio, Portfolio.id == PortfolioTransaction.portfolio_id)
            .where(
                PortfolioTransaction.instrument_id == instrument.id,
                Portfolio.user_id != active_session.user_id,
            )
            .limit(1)
        )
        other_owner_watchlist = db.scalar(
            select(WatchlistInstrument.id)
            .where(
                WatchlistInstrument.instrument_id == instrument.id,
                WatchlistInstrument.user_id != active_session.user_id,
            )
            .limit(1)
        )
        if other_owner_transaction is not None or other_owner_watchlist is not None:
            resolution_failures[str(instrument.id)] = "linked_to_another_user"
            continue
        if linked_ids:
            db.execute(
                PortfolioTransaction.__table__.update()
                .where(PortfolioTransaction.id.in_(linked_ids))
                .values(instrument_id=None)
            )
            instrument.relink_required = True
            marked_for_relink += 1
            relink_ids.append(str(instrument.id))
            cleared_transaction_ids[str(instrument.id)] = [
                str(transaction_id) for transaction_id in linked_ids
            ]
            for table_model in (
                MarketBar,
                MarketQuote,
                MarketDataSync,
                MarketDataSyncAttempt,
                InstrumentFundamental,
                InstrumentSnapshot,
                InstrumentProviderIdentifier,
            ):
                db.execute(delete(table_model).where(table_model.instrument_id == instrument.id))
            db.execute(
                delete(InstrumentSourceLink).where(
                    InstrumentSourceLink.instrument_id == instrument.id,
                    InstrumentSourceLink.user_id == active_session.user_id,
                )
            )
            owned_run_ids = select(InstrumentResearchRun.id).where(
                InstrumentResearchRun.instrument_id == instrument.id,
                InstrumentResearchRun.user_id == active_session.user_id,
            )
            db.execute(
                delete(InstrumentResearchBrief).where(
                    InstrumentResearchBrief.instrument_id == instrument.id,
                    InstrumentResearchBrief.run_id.in_(owned_run_ids),
                )
            )
            db.execute(
                delete(InstrumentResearchRun).where(
                    InstrumentResearchRun.instrument_id == instrument.id,
                    InstrumentResearchRun.user_id == active_session.user_id,
                )
            )
            db.execute(
                delete(ResearchNote).where(
                    ResearchNote.instrument_id == instrument.id,
                    ResearchNote.user_id == active_session.user_id,
                )
            )
            db.execute(
                delete(ResearchSource).where(
                    ResearchSource.instrument_id == instrument.id,
                    ResearchSource.user_id == active_session.user_id,
                )
            )
            db.execute(
                delete(WatchlistInstrument).where(
                    WatchlistInstrument.instrument_id == instrument.id,
                    WatchlistInstrument.user_id == active_session.user_id,
                )
            )
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
            any_transaction = db.scalar(
                select(PortfolioTransaction.id)
                .where(PortfolioTransaction.instrument_id == instrument.id)
                .limit(1)
            )
            if any_transaction is not None:
                resolution_failures[str(instrument.id)] = "linked_to_another_user"
                continue
            other_owner_research = any(
                db.scalar(
                    select(model.id)
                    .where(
                        model.instrument_id == instrument.id,
                        model.user_id != active_session.user_id,
                    )
                    .limit(1)
                )
                is not None
                for model in (
                    ResearchSource,
                    ResearchNote,
                    InstrumentResearchRun,
                    InstrumentSourceLink,
                )
            )
            other_owner_brief = db.scalar(
                select(InstrumentResearchBrief.id)
                .join(
                    InstrumentResearchRun,
                    InstrumentResearchRun.id == InstrumentResearchBrief.run_id,
                )
                .where(
                    InstrumentResearchBrief.instrument_id == instrument.id,
                    InstrumentResearchRun.user_id != active_session.user_id,
                )
                .limit(1)
            )
            if other_owner_research or other_owner_brief is not None:
                resolution_failures[str(instrument.id)] = "linked_to_another_user"
                continue
            for table_model in (
                MarketBar,
                MarketQuote,
                MarketDataSync,
                MarketDataSyncAttempt,
                InstrumentFundamental,
                InstrumentSnapshot,
                InstrumentProviderIdentifier,
                InstrumentSourceLink,
                InstrumentResearchBrief,
                InstrumentResearchRun,
                ResearchNote,
                ResearchSource,
                WatchlistInstrument,
            ):
                db.execute(delete(table_model).where(table_model.instrument_id == instrument.id))
            db.delete(instrument)
            deleted += 1
            deleted_ids.append(str(instrument.id))
    db.commit()
    return {
        "deleted_unlinked": deleted,
        "marked_for_relink": marked_for_relink,
        "deleted_ids": deleted_ids,
        "relink_ids": relink_ids,
        "cleared_transaction_ids": cleared_transaction_ids,
        "resolved_replacements": resolved_replacements,
        "resolution_failures": resolution_failures,
        "message": "Linked ALPHA instruments were retained, unlinked from transactions, and marked for relinking.",
    }


def _user_market_data_instrument_ids(db: Session, user_id: UUID) -> set[UUID]:
    instrument_ids = set(db.scalars(
        select(WatchlistInstrument.instrument_id).where(
            WatchlistInstrument.user_id == user_id
        )
    ))
    portfolios = list(db.scalars(select(Portfolio).where(Portfolio.user_id == user_id)))
    from app.api.portfolio_transactions import calculate_portfolio_ledger

    for portfolio in portfolios:
        ledger = calculate_portfolio_ledger(db, user_id, portfolio.id)
        open_symbols = {
            symbol
            for symbol, position in ledger.positions.items()
            if position.quantity > 0
        }
        if not open_symbols:
            continue
        rows = db.execute(
            select(PortfolioTransaction.symbol, PortfolioTransaction.instrument_id)
            .where(
                PortfolioTransaction.portfolio_id == portfolio.id,
                PortfolioTransaction.instrument_id.is_not(None),
                PortfolioTransaction.transaction_type.in_(("buy", "sell")),
            )
            .order_by(
                PortfolioTransaction.occurred_at.desc(),
                PortfolioTransaction.created_at.desc(),
                PortfolioTransaction.id.desc(),
            )
        ).all()
        linked: dict[str, UUID] = {}
        for symbol, instrument_id in rows:
            if symbol in open_symbols and symbol not in linked:
                linked[symbol] = instrument_id
        instrument_ids.update(linked.values())
    return instrument_ids


@router.post("/purge-synthetic")
def purge_synthetic_market_data(
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(MarketBar.instrument_id, func.count(MarketBar.id))
        .where(MarketBar.is_synthetic.is_(True))
        .group_by(MarketBar.instrument_id)
    ).all()
    quote_rows = db.execute(
        select(MarketQuote.instrument_id, func.count(MarketQuote.id))
        .where(MarketQuote.is_synthetic.is_(True))
        .group_by(MarketQuote.instrument_id)
    ).all()
    counts = {
        instrument_id: {"bars_deleted": count, "quotes_deleted": 0}
        for instrument_id, count in rows
    }
    for instrument_id, count in quote_rows:
        counts.setdefault(instrument_id, {"bars_deleted": 0, "quotes_deleted": 0})
        counts[instrument_id]["quotes_deleted"] = count
    db.execute(delete(MarketBar).where(MarketBar.is_synthetic.is_(True)))
    db.execute(delete(MarketQuote).where(MarketQuote.is_synthetic.is_(True)))
    db.commit()
    return {
        "deleted_by_instrument": [
            {
                "instrument_id": str(instrument_id),
                **values,
            }
            for instrument_id, values in counts.items()
        ],
        "bars_deleted": sum(item["bars_deleted"] for item in counts.values()),
        "quotes_deleted": sum(item["quotes_deleted"] for item in counts.values()),
    }


@router.get("/syncs")
def get_market_data_syncs(
    active_session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
):
    instrument_ids = _user_market_data_instrument_ids(db, active_session.user_id)
    if not instrument_ids:
        return {"attempts": []}
    attempts = db.execute(
        select(MarketDataSyncAttempt, Instrument.symbol)
        .join(Instrument, Instrument.id == MarketDataSyncAttempt.instrument_id)
        .where(MarketDataSyncAttempt.instrument_id.in_(instrument_ids))
        .order_by(MarketDataSyncAttempt.attempted_at.desc())
        .limit(20)
    ).all()
    return {
        "attempts": [
            {
                "instrument_id": attempt.instrument_id,
                "symbol": symbol,
                "provider": attempt.provider,
                "interval": attempt.interval,
                "requested_start": _utc(attempt.requested_start),
                "requested_end": _utc(attempt.requested_end),
                "requested_limit": attempt.requested_limit,
                "response_status": attempt.response_status,
                "rows_returned": attempt.rows_returned,
                "rows_written": attempt.rows_written,
                "outcome": attempt.outcome,
                "reason": attempt.reason,
                "attempted_at": _utc(attempt.attempted_at),
            }
            for attempt, symbol in attempts
        ]
    }


@router.get("/coverage")
def get_market_data_coverage(
    active_session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
):
    instrument_ids = _user_market_data_instrument_ids(db, active_session.user_id)
    if not instrument_ids:
        return {"instruments": []}
    instruments = db.scalars(
        select(Instrument)
        .where(Instrument.id.in_(instrument_ids))
        .order_by(Instrument.symbol, Instrument.venue)
    ).all()
    result = []
    for instrument in instruments:
        bars = db.execute(
            select(func.count(MarketBar.id), func.min(MarketBar.timestamp), func.max(MarketBar.timestamp))
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == "1d",
                MarketBar.is_synthetic.is_(False),
            )
        ).one()
        sync = db.scalar(
            select(MarketDataSync)
            .where(
                MarketDataSync.instrument_id == instrument.id,
                MarketDataSync.interval == "1d",
            )
            .order_by(MarketDataSync.attempted_at.desc())
            .limit(1)
        )
        if sync is None:
            freshness_status = "never-synced"
        elif sync.status != "success":
            freshness_status = "failed"
        elif bars[2] is None or (
            datetime.now(timezone.utc) - _utc(bars[2])
        ).total_seconds() > settings.market_data_daily_freshness_seconds:
            freshness_status = "stale"
        else:
            freshness_status = "fresh"
        result.append({
            "instrument_id": instrument.id,
            "symbol": instrument.symbol,
            "display_name": instrument.display_name,
            "venue": instrument.venue,
            "bars_stored": bars[0],
            "oldest_bar": _utc(bars[1]) if bars[1] else None,
            "newest_bar": _utc(bars[2]) if bars[2] else None,
            "last_sync_status": sync.status if sync else None,
            "last_sync_reason": sync.safe_reason or sync.safe_error_code if sync else None,
            "freshness_status": freshness_status,
        })
    return {"instruments": result}


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
        if active_provider.is_mock or candidate.is_synthetic:
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
    if instrument.is_synthetic:
        raise HTTPException(status_code=409, detail="Synthetic instruments cannot sync market history.")
    cached_bars = list(
        db.scalars(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == interval,
                MarketBar.is_synthetic.is_(False),
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
                missing_ranges.append((previous + timedelta(days=1), current))
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
        attempted_at = datetime.now(timezone.utc)
        response_status = None
        safe_error = None
        provider_rows_returned = [0]
        try:
            candidate, _mapping = _resolve_provider_candidate(db, instrument, active_provider)
            provider_bars: list[HistoricalBar] = []
            for range_start, range_end in missing_ranges:
                remaining = limit - len(provider_bars)
                if remaining <= 0:
                    break
                provider_bars.extend(_fetch_provider_history(
                    active_provider,
                    candidate,
                    interval,
                    range_start,
                    range_end,
                    remaining,
                    rows_returned=provider_rows_returned,
                ))
            if not provider_bars:
                raise MarketDataProviderError("provider_no_history_for_range")
            selected_provider = active_provider
            selected_candidate = candidate
            validated_bars = sorted(
                {bar.timestamp: bar for bar in provider_bars}.values(),
                key=lambda bar: bar.timestamp,
            )
        except MarketDataProviderError as error:
            _log_market_data_failure(
                "Market history provider failed (provider=%s instrument_id=%s interval=%s)",
                error,
                error.code,
                active_provider.name,
                instrument.id,
                interval,
            )
            if error.code.startswith("yfinance_") and "lookback_exceeded" in error.code:
                yfinance_lookback_error = error.code
            response_status = error.status_code or getattr(
                active_provider, "last_response_status", None
            )
            safe_error = error.code
        except Exception as error:
            _log_market_data_failure(
                "Unexpected market history failure (provider=%s instrument_id=%s interval=%s)",
                error,
                "provider_unavailable_or_invalid_response",
                active_provider.name,
                instrument.id,
                interval,
            )
            safe_error = "provider_unavailable_or_invalid_response"
            response_status = getattr(active_provider, "last_response_status", None)
        if selected_provider is not active_provider:
            _sync_record(
                db,
                instrument.id,
                interval,
                source=active_provider.name,
                attempted_at=attempted_at,
                status="unavailable",
                rows_received=provider_rows_returned[0],
                safe_error_code=(safe_error or "provider_unavailable")[:48],
                requested_start=start_utc,
                requested_end=end_utc,
                requested_limit=limit,
                response_status=response_status,
                safe_reason=safe_error or "provider_unavailable",
            )
            _record_sync_attempt(
                db,
                instrument_id=instrument.id,
                interval=interval,
                provider=active_provider.name,
                start=start_utc,
                end=end_utc,
                limit=limit,
                response_status=response_status,
                rows_returned=provider_rows_returned[0],
                rows_written=0,
                outcome="unavailable",
                reason=safe_error or "provider_unavailable",
                attempted_at=attempted_at,
            )
            continue
        break
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
    rows_written = _persist_real_bars(
        db,
        instrument,
        selected_provider,
        selected_candidate,
        interval,
        validated_bars,
        retrieved_at,
    )

    _sync_record(
        db,
        instrument.id,
        interval,
        source=selected_provider.name,
        attempted_at=retrieved_at,
        status="success",
        rows_received=provider_rows_returned[0],
        safe_error_code=None,
        requested_start=start_utc,
        requested_end=end_utc,
        requested_limit=limit,
        rows_written=rows_written,
        response_status=getattr(selected_provider, "last_response_status", None),
        safe_reason=None if rows_written else "provider_data_already_stored",
    )
    _record_sync_attempt(
        db,
        instrument_id=instrument.id,
        interval=interval,
        provider=selected_provider.name,
        start=start_utc,
        end=end_utc,
        limit=limit,
        response_status=getattr(selected_provider, "last_response_status", None),
        rows_returned=provider_rows_returned[0],
        rows_written=rows_written,
        outcome="success" if rows_written else "unavailable",
        reason=None if rows_written else "provider_data_already_stored",
        attempted_at=retrieved_at,
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


def _count_real_daily_bars(
    db: Session, instrument_id: UUID, start: datetime, end: datetime
) -> int:
    return int(db.scalar(
        select(func.count(MarketBar.id)).where(
            MarketBar.instrument_id == instrument_id,
            MarketBar.interval == "1d",
            MarketBar.is_synthetic.is_(False),
            MarketBar.timestamp >= start,
            MarketBar.timestamp < end,
        )
    ) or 0)


def _backfill_instrument(
    db: Session,
    instrument: Instrument,
    provider: MarketDataProvider,
    *,
    years: int,
    request_budget: list[int],
) -> dict:
    if instrument.is_synthetic:
        return {
            "instrument_id": str(instrument.id),
            "symbol": instrument.symbol,
            "status": "unavailable",
            "bars_before": 0,
            "bars_after": 0,
            "chunks_attempted": 0,
            "chunks_failed": 0,
            "chunks": [],
            "reason": "synthetic_instrument_requires_relink",
        }
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=365 * years)).replace(hour=0)
    bars_before = _count_real_daily_bars(db, instrument.id, start, end)
    chunks: list[dict] = []
    chunks_failed = 0
    chunk_start = start
    while chunk_start < end and len(chunks) < MAX_BACKFILL_CHUNKS:
        chunk_end = min(chunk_start + timedelta(days=BACKFILL_CHUNK_DAYS), end)
        detail = {
            "start": chunk_start,
            "end": chunk_end,
            "provider": None,
            "response_status": None,
            "rows_returned": 0,
            "rows_written": 0,
            "outcome": "unavailable",
            "reason": None,
        }
        succeeded = False
        rate_limited = False
        for active_provider in _providers(provider):
            attempted_at = datetime.now(timezone.utc)
            detail["provider"] = active_provider.name
            provider_rows_returned = [0]
            try:
                candidate, _mapping = _resolve_provider_candidate(db, instrument, active_provider)
                bars = _fetch_provider_history(
                    active_provider,
                    candidate,
                    "1d",
                    chunk_start,
                    chunk_end,
                    MAX_BARS,
                    request_budget,
                    rows_returned=provider_rows_returned,
                )
                if not bars:
                    raise MarketDataProviderError("provider_no_history_for_range")
                detail["rows_returned"] = len(bars)
                detail["response_status"] = getattr(active_provider, "last_response_status", None)
                detail["rows_written"] = _persist_real_bars(
                    db,
                    instrument,
                    active_provider,
                    candidate,
                    "1d",
                    bars,
                    attempted_at,
                )
                detail["outcome"] = (
                    "success" if detail["rows_written"] else "unchanged"
                )
                detail["reason"] = (
                    None if detail["rows_written"] else "no_new_bars_written"
                )
                _sync_record(
                    db,
                    instrument.id,
                    "1d",
                    source=active_provider.name,
                    attempted_at=attempted_at,
                    status="success",
                    rows_received=len(bars),
                    safe_error_code=None,
                    requested_start=chunk_start,
                    requested_end=chunk_end,
                    requested_limit=MAX_BARS,
                    rows_written=detail["rows_written"],
                    response_status=detail["response_status"],
                    safe_reason=detail["reason"],
                )
                _record_sync_attempt(
                    db,
                    instrument_id=instrument.id,
                    interval="1d",
                    provider=active_provider.name,
                    start=chunk_start,
                    end=chunk_end,
                    limit=MAX_BARS,
                    response_status=detail["response_status"],
                    rows_returned=len(bars),
                    rows_written=detail["rows_written"],
                    outcome=detail["outcome"],
                    reason=detail["reason"],
                    attempted_at=attempted_at,
                )
                succeeded = True
                break
            except MarketDataProviderError as error:
                _log_market_data_failure(
                    "Market history backfill chunk failed (provider=%s instrument_id=%s)",
                    error,
                    error.code,
                    active_provider.name,
                    instrument.id,
                )
                detail["response_status"] = error.status_code or getattr(
                    active_provider, "last_response_status", None
                )
                detail["rows_returned"] = provider_rows_returned[0]
                detail["reason"] = error.code
                _sync_record(
                    db,
                    instrument.id,
                    "1d",
                    source=active_provider.name,
                    attempted_at=attempted_at,
                    status="unavailable",
                    rows_received=provider_rows_returned[0],
                    safe_error_code=error.code[:48],
                    requested_start=chunk_start,
                    requested_end=chunk_end,
                    requested_limit=MAX_BARS,
                    response_status=detail["response_status"],
                    safe_reason=error.code,
                )
                _record_sync_attempt(
                    db,
                    instrument_id=instrument.id,
                    interval="1d",
                    provider=active_provider.name,
                    start=chunk_start,
                    end=chunk_end,
                    limit=MAX_BARS,
                    response_status=detail["response_status"],
                    rows_returned=provider_rows_returned[0],
                    rows_written=0,
                    outcome="unavailable",
                    reason=error.code,
                    attempted_at=attempted_at,
                )
                if error.status_code == 429:
                    rate_limited = True
                    break
                if error.code == "provider_request_cap_reached":
                    break
            except Exception as error:
                _log_market_data_failure(
                    "Unexpected market history backfill failure (provider=%s instrument_id=%s)",
                    error,
                    "provider_unavailable_or_invalid_response",
                    active_provider.name,
                    instrument.id,
                )
                safe_reason = "provider_unavailable_or_invalid_response"
                detail["reason"] = safe_reason
                detail["rows_returned"] = provider_rows_returned[0]
                detail["response_status"] = getattr(
                    active_provider, "last_response_status", None
                )
                _sync_record(
                    db,
                    instrument.id,
                    "1d",
                    source=active_provider.name,
                    attempted_at=attempted_at,
                    status="unavailable",
                    rows_received=provider_rows_returned[0],
                    safe_error_code=safe_reason,
                    requested_start=chunk_start,
                    requested_end=chunk_end,
                    requested_limit=MAX_BARS,
                    response_status=detail["response_status"],
                    safe_reason=safe_reason,
                )
                _record_sync_attempt(
                    db,
                    instrument_id=instrument.id,
                    interval="1d",
                    provider=active_provider.name,
                    start=chunk_start,
                    end=chunk_end,
                    limit=MAX_BARS,
                    response_status=detail["response_status"],
                    rows_returned=provider_rows_returned[0],
                    rows_written=0,
                    outcome="unavailable",
                    reason=safe_reason,
                    attempted_at=attempted_at,
                )
        if not succeeded:
            chunks_failed += 1
            if detail["reason"] is None:
                detail["reason"] = (
                    "provider_request_cap_reached"
                    if request_budget[0] <= 0
                    else "no_real_provider_available"
                )
            if rate_limited:
                detail["outcome"] = "partial"
            if request_budget[0] <= 0 or rate_limited:
                chunks.append(detail)
                db.commit()
                break
        chunks.append(detail)
        db.commit()
        chunk_start = chunk_end
    bars_after = _count_real_daily_bars(db, instrument.id, start, end)
    written = bars_after - bars_before
    status = (
        "partial" if chunks_failed
        else "success" if written > 0
        else "unchanged"
    )
    return {
        "instrument_id": str(instrument.id),
        "symbol": instrument.symbol,
        "status": status,
        "bars_before": bars_before,
        "bars_after": bars_after,
        "chunks_attempted": len(chunks),
        "chunks_failed": chunks_failed,
        "chunks": chunks,
    }


@router.post("/instruments/{instrument_id}/history/backfill")
def backfill_instrument_history(
    instrument_id: UUID,
    years: int = Query(default=2, ge=1, le=10),
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    provider: MarketDataProvider = Depends(get_market_data_provider),
    db: Session = Depends(get_db),
):
    instrument = _get_instrument(db, instrument_id)
    if instrument_id not in _user_market_data_instrument_ids(
        db, active_session.user_id
    ):
        raise HTTPException(status_code=404, detail="Instrument is not linked to this user's holdings or watchlist.")
    budget = [MAX_BACKFILL_REQUESTS]
    return _backfill_instrument(
        db, instrument, provider, years=years, request_budget=budget
    )


@router.post("/backfill/holdings")
def backfill_open_holdings(
    years: int = Query(default=2, ge=1, le=10),
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    provider: MarketDataProvider = Depends(get_market_data_provider),
    db: Session = Depends(get_db),
):
    instrument_ids = sorted(
        _user_market_data_instrument_ids(db, active_session.user_id),
        key=str,
    )
    instruments = [
        instrument for instrument_id in instrument_ids
        if (instrument := db.get(Instrument, instrument_id)) is not None
    ]
    request_budget = [MAX_BACKFILL_REQUESTS]
    results = []
    for instrument in instruments:
        if request_budget[0] <= 0:
            results.append({
                "instrument_id": str(instrument.id),
                "symbol": instrument.symbol,
                "status": "partial",
                "bars_before": None,
                "bars_after": None,
                "chunks_attempted": 0,
                "chunks_failed": 0,
                "chunks": [],
                "reason": "provider_request_cap_reached",
            })
            continue
        results.append(_backfill_instrument(
            db,
            instrument,
            provider,
            years=years,
            request_budget=request_budget,
        ))
    return {
        "instruments": results,
        "provider_requests_remaining": request_budget[0],
        "status": (
            "partial"
            if any(result["status"] in {"partial", "unavailable"} for result in results)
            else "success"
            if any(result["status"] == "success" for result in results)
            else "unchanged"
        ),
    }


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
