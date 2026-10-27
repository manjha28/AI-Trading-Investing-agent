import base64
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
from email.utils import parsedate_to_datetime
import logging
import random
import asyncio
import re
import threading
import time
from enum import Enum
from typing import Any, Literal, Mapping, TypedDict
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, selectinload
from websockets.exceptions import InvalidStatus, WebSocketException

from app.broker_adapters import (
    BrokerAdapterError,
    BrokerRateLimited,
    BrokerReconnectRequired,
    DhanAdapter,
    get_broker_adapter,
)
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
    BrokerCredential,
    UserMarketDataSettings,
    User,
    UserSession,
    Watchlist,
    WatchlistInstrument,
)
from app.services.indicators import IndicatorValue, atr, compute_indicators, ema, rsi, sma
from app.services.options_signal_engine import (
    ActiveOptionPosition,
    OHLCVBar as SignalOHLCVBar,
    OptionQuote as SignalOptionQuote,
    OptionsSignalInput,
    evaluate_options_signal,
)
from app.services import fundamentals as fundamentals_service
from app.services.price_freshness import classify_quote
from app.provider_adapters import (
    BinanceProvider,
    DeltaExchangeProvider,
    FinnhubProvider,
    AngelOneProvider,
    DhanV2Adapter,
    UpstoxProvider,
    YahooFinanceProvider,
    binance_provider_symbol,
    candidate_for_instrument,
    canonical_provider_symbol,
)
from app.twelve_data import TwelveDataProvider
from app.security import hash_token

router = APIRouter(prefix="/market-data", tags=["market data"])
indicators_router = APIRouter(tags=["market data indicators"])
BarInterval = Literal["1d", "1h", "60m", "25m", "15m", "5m", "1m"]
MAX_BARS = 500
MAX_BACKFILL_CHUNKS = 20
MAX_BACKFILL_REQUESTS = 24
BACKFILL_CHUNK_DAYS = 450
RETRY_AFTER_CAP_SECONDS = 8
MARKET_DATA_FALLBACK_PRIORITY = ("dhan", "angel_one", "yfinance")
INSTRUMENT_SEARCH_PRIORITY = ("angel_one", "dhan", "yfinance")
logger = logging.getLogger(__name__)
_provider_rate_lock = threading.Lock()
_provider_next_request: dict[str, float] = {}


def _dhan_feed_close_code(status_code: int | None) -> int:
    if status_code == 401:
        return 4401
    if status_code == 403:
        return 4403
    if status_code is not None and 4000 <= status_code <= 4999:
        return status_code
    return 1011


class DhanLiveFeedHub:
    def __init__(self) -> None:
        self._adapter = DhanAdapter()
        self._lock = asyncio.Lock()
        self._groups: dict[str, dict[str, tuple[dict[str, tuple[str, str]], asyncio.Queue[dict[str, Any]]]]] = {
            "ticker": {},
            "depth": {},
        }
        self._updates: dict[str, asyncio.Queue[dict[str, tuple[str, str]]] | None] = {
            "ticker": None,
            "depth": None,
        }
        self._tasks: dict[str, asyncio.Task[None] | None] = {
            "ticker": None,
            "depth": None,
        }
        self._credentials: dict[str, tuple[str, str] | None] = {
            "ticker": None,
            "depth": None,
        }
        self._connection_counts = {"ticker": 0, "depth": 0}
        self._depth_snapshot_times: dict[str, datetime] = {}
        self._tick_counters = {"upstream_received": 0, "clients_fanned_out": 0}
        self._counter_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        logger.info(
            "Dhan shared feed manager ready at application startup "
            "ticker_upstream_connections=0 depth_upstream_connections=0; "
            "authorized subscriptions open a single shared upstream per feed"
        )
        if self._counter_task is None:
            self._counter_task = asyncio.create_task(self._log_tick_counters())

    async def _log_tick_counters(self) -> None:
        while True:
            await asyncio.sleep(30)
            logger.info(
                "Dhan ticker delivery counters upstream_ticks_received=%d "
                "ticks_fanned_out=%d upstream_connections=%d subscriptions=%d",
                self._tick_counters["upstream_received"],
                self._tick_counters["clients_fanned_out"],
                self._connection_counts["ticker"],
                len(self._all_subscriptions("ticker")),
            )

    def last_depth_snapshot_at(self, instrument_id: UUID) -> datetime | None:
        return self._depth_snapshot_times.get(str(instrument_id))

    async def stop(self) -> None:
        async with self._lock:
            tasks = [task for task in self._tasks.values() if task is not None]
            if self._counter_task is not None:
                self._counter_task.cancel()
                tasks.append(self._counter_task)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._counter_task = None
            self._tasks = {"ticker": None, "depth": None}
            self._updates = {"ticker": None, "depth": None}
            self._credentials = {"ticker": None, "depth": None}
            self._connection_counts = {"ticker": 0, "depth": 0}
            for groups in self._groups.values():
                groups.clear()

    async def join(
        self,
        feed: str,
        group_id: str,
        subscriptions: dict[str, tuple[str, str]],
        access_token: str,
        tokens: Mapping[str, str],
    ) -> asyncio.Queue[dict[str, Any]]:
        if feed not in self._groups:
            raise ValueError("Unsupported Dhan feed")
        credential_identity = (access_token, tokens["client_id"])
        async with self._lock:
            current_identity = self._credentials[feed]
            if current_identity is not None and current_identity != credential_identity:
                raise BrokerAdapterError(
                    "A different Dhan account already owns the shared upstream feed."
                )
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
            self._credentials[feed] = credential_identity
            self._groups[feed][group_id] = (subscriptions, queue)
            if self._tasks[feed] is None:
                update_queue: asyncio.Queue[dict[str, tuple[str, str]]] = asyncio.Queue(maxsize=1)
                self._updates[feed] = update_queue
                initial = self._all_subscriptions(feed)
                self._tasks[feed] = asyncio.create_task(
                    self._run_feed(feed, access_token, tokens, initial, update_queue)
                )
            else:
                self._publish_updates(feed)
            self._log_counts(feed)
            return queue

    async def update(
        self,
        feed: str,
        group_id: str,
        subscriptions: dict[str, tuple[str, str]],
    ) -> None:
        async with self._lock:
            current = self._groups[feed].get(group_id)
            if current is None:
                return
            self._groups[feed][group_id] = (subscriptions, current[1])
            self._publish_updates(feed)
            self._log_counts(feed)

    async def leave(self, feed: str, group_id: str) -> None:
        async with self._lock:
            self._groups[feed].pop(group_id, None)
            if self._groups[feed]:
                self._publish_updates(feed)
                self._log_counts(feed)
                return
            task = self._tasks[feed]
            if task is not None:
                self._publish_updates(feed)
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self._tasks[feed] = None
            self._updates[feed] = None
            self._credentials[feed] = None
            self._log_counts(feed)

    def _all_subscriptions(self, feed: str) -> dict[str, tuple[str, str]]:
        subscriptions: dict[str, tuple[str, str]] = {}
        for group_subscriptions, _queue in self._groups[feed].values():
            subscriptions.update(group_subscriptions)
        return subscriptions

    def _publish_updates(self, feed: str) -> None:
        updates = self._updates[feed]
        if updates is None:
            return
        if updates.full():
            updates.get_nowait()
        updates.put_nowait(self._all_subscriptions(feed))

    def _publish(
        self,
        feed: str,
        message: dict[str, Any],
        instrument_id: str | None = None,
    ) -> int:
        delivered = 0
        for subscriptions, queue in self._groups[feed].values():
            if instrument_id is not None and instrument_id not in subscriptions:
                continue
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)
            delivered += 1
        return delivered

    def _log_counts(self, feed: str, connection_count: int | None = None) -> None:
        counts = [len(subscriptions) for subscriptions, _queue in self._groups[feed].values()]
        unique_count = len(self._all_subscriptions(feed))
        logger.info(
            "Dhan shared feed state feed=%s upstream_connections=%s "
            "subscriptions=%d references=%d clients=%d",
            feed,
            connection_count if connection_count is not None else self._connection_counts[feed],
            unique_count,
            sum(counts),
            len(counts),
        )

    async def _run_feed(
        self,
        feed: str,
        access_token: str,
        tokens: Mapping[str, str],
        initial: dict[str, tuple[str, str]],
        updates: asyncio.Queue[dict[str, tuple[str, str]]],
    ) -> None:
        async def status_callback(status: str) -> None:
            self._connection_counts[feed] = 1 if status == "active" else 0
            self._publish(feed, {"type": "status", "status": status})
            self._log_counts(feed)

        try:
            if feed == "ticker":
                async for instrument_id, bar in self._adapter.stream_watchlist_market_quotes(
                    access_token, tokens, initial, updates, status_callback
                ):
                    self._tick_counters["upstream_received"] += 1
                    self._tick_counters["clients_fanned_out"] += self._publish(
                        feed,
                        {"type": "tick", "instrument_id": instrument_id, "bar": bar},
                        instrument_id,
                    )
            else:
                async for instrument_id, depth in self._adapter.stream_watchlist_market_depth(
                    access_token, tokens, initial, updates, status_callback
                ):
                    self._depth_snapshot_times[instrument_id] = depth["timestamp"]
                    self._publish(
                        feed,
                        {"type": "depth", "instrument_id": instrument_id, "depth": depth},
                        instrument_id,
                    )
        except asyncio.CancelledError:
            raise
        except BrokerReconnectRequired as error:
            self._publish(
                feed,
                {"type": "error", "message": str(error), "status_code": error.status_code},
            )
        except Exception as error:
            logger.warning(
                "Dhan shared feed stopped feed=%s error_class=%s reason=%s",
                feed,
                type(error).__name__,
                str(error),
            )
            self._publish(
                feed,
                {
                    "type": "error",
                    "message": str(error) if isinstance(error, BrokerAdapterError)
                    else f"Dhan {feed} feed stopped ({type(error).__name__}).",
                    "status_code": (
                        error.status_code
                        if isinstance(error, BrokerAdapterError)
                        else None
                    ),
                },
            )
        finally:
            self._connection_counts[feed] = 0
            self._log_counts(feed)
            if self._tasks[feed] is asyncio.current_task():
                self._tasks[feed] = None
                self._updates[feed] = None
                self._credentials[feed] = None


dhan_live_feed_hub = DhanLiveFeedHub()


class ReasonCategory(str, Enum):
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    CIRCUIT_BREAKER_PROBE_IN_PROGRESS = "circuit_breaker_probe_in_progress"
    CLOSED_SESSION_NO_NEW_BARS = "closed_session_no_new_bars"
    INSTRUMENT_NOT_FOUND = "instrument_not_found"
    INSTRUMENT_MAPPING_FAILED = "instrument_mapping_failed"
    INVALID_INSTRUMENT_MAPPING = "invalid_instrument_mapping"
    INVALID_PROVIDER_INSTRUMENT = "invalid_provider_instrument"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    INTERNAL_ERROR = "internal_error"
    INVALID_TIME_RANGE = "invalid_time_range"
    MARKET_TIMESTAMP_UNAVAILABLE = "market_timestamp_unavailable"
    NO_MARKET_DATA_PROVIDER_CONFIGURED = "no_market_data_provider_configured"
    PROVIDER_AUTHENTICATION_FAILED = "provider_authentication_failed"
    PROVIDER_DATA_ALREADY_STORED = "provider_data_already_stored"
    PROVIDER_EXCEEDED_BAR_LIMIT = "provider_exceeded_bar_limit"
    PROVIDER_EXCEEDED_SEARCH_LIMIT = "provider_exceeded_search_limit"
    PROVIDER_IDENTITY_MISMATCH = "provider_identity_mismatch"
    PROVIDER_INSTRUMENT_MASTER_UNAVAILABLE = "provider_instrument_master_unavailable"
    PROVIDER_MAPPING_CONFLICT = "provider_mapping_conflict"
    PROVIDER_NETWORK_ERROR = "provider_network_error"
    PROVIDER_NO_HISTORY_FOR_RANGE = "provider_no_history_for_range"
    PROVIDER_INVALID_RESPONSE = "provider_invalid_response"
    PROVIDER_QUOTE_TIMESTAMP_UNAVAILABLE = "provider_quote_timestamp_unavailable"
    PROVIDER_QUOTE_UNAVAILABLE = "provider_quote_unavailable"
    PROVIDER_RATE_LIMIT_EXCEEDED = "provider_rate_limit_exceeded"
    PROVIDER_RATE_LIMITED = "rate_limited"
    PROVIDER_REQUEST_CAP_REACHED = "provider_request_cap_reached"
    PROVIDER_REQUEST_FAILED = "provider_request_failed"
    PROVIDER_RESPONSE_TOO_LARGE = "provider_response_too_large"
    PROVIDER_RETURNED_DUPLICATE_OR_UNSORTED_BARS = "provider_returned_duplicate_or_unsorted_bars"
    PROVIDER_RETURNED_OUT_OF_RANGE_BAR = "provider_returned_out_of_range_bar"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    PROVIDER_SUBSCRIPTION_REQUIRED = "provider_subscription_required"
    PROVIDER_SYMBOL_UNAVAILABLE = "provider_symbol_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_UNAVAILABLE_OR_INVALID_RESPONSE = "provider_unavailable_or_invalid_response"
    PROVIDER_UNEXPECTED_ERROR = "provider_unexpected_error"
    SEARCH_QUERY_REQUIRED = "search_query_required"
    STALE = "stale"
    SYNTHETIC_MARKET_DATA_DISABLED = "synthetic_market_data_disabled"
    UNVERIFIED_INSTRUMENT_MAPPING = "unverified_instrument_mapping"
    UNSUPPORTED_INTERVAL = "unsupported_interval"
    UNSUPPORTED_INTERVAL_OR_LOOKBACK = "unsupported_interval_or_lookback"
    UNSUPPORTED_PROVIDER_ASSET_CLASS = "unsupported_provider_asset_class"
    UNAVAILABLE = "unavailable"
    YFINANCE_COOLDOWN_ACTIVE = "yfinance_cooldown_active"


def _reason_category(value: str | None) -> ReasonCategory | None:
    if value is None:
        return None
    if value.startswith("internal_error:"):
        return ReasonCategory.INTERNAL_ERROR
    try:
        return ReasonCategory(value)
    except ValueError:
        return ReasonCategory.PROVIDER_REQUEST_FAILED


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


def _safe_provider_error_code(code: str) -> str:
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", code)
        or any(
            marker in code
            for marker in ("secret", "token", "jwt", "password", "pin", "feed")
        )
    ):
        return "provider_request_failed"
    return code


def _user_market_data_settings(db: Session, user_id: UUID) -> UserMarketDataSettings | None:
    try:
        return db.get(UserMarketDataSettings, user_id)
    except ProgrammingError as error:
        if getattr(error.orig, "sqlstate", None) != "42P01":
            raise
        logger.error("Market-data settings table is missing; database migrations are pending.")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "database_migration_required",
                "message": "Database migrations are pending. Run `alembic upgrade head` from the backend directory.",
            },
        ) from None


class MarketDataProviderChain:
    def __init__(self, providers: list[MarketDataProvider]):
        self.providers = providers
        self.name = providers[0].name if providers else "unavailable"
        self.is_mock = False
        self.unavailable_providers: list[dict[str, str]] = []

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


class MarketDataSourceOption(BaseModel):
    name: str
    label: str
    available: bool
    status: str
    enabled: bool = True
    expires_at: datetime | None = None


class DhanMarketDataProvider:
    name = "dhan"
    is_mock = False
    _base_url = "https://api.dhan.co/v2"

    def __init__(self, adapter: DhanAdapter, tokens: dict[str, str]):
        self.adapter = adapter
        self.tokens = tokens
        self.market_data_adapter = DhanV2Adapter(tokens)

    @property
    def last_response_status(self) -> int | None:
        return self.market_data_adapter.last_response_status

    @property
    def last_http_diagnostics(self) -> list[dict[str, Any]]:
        return self.market_data_adapter.last_http_diagnostics

    def _candidate(self, row: dict[str, str]) -> InstrumentCandidate:
        exchange = row["EXCH_ID"].upper()
        if exchange not in {"NSE", "BSE"}:
            raise MarketDataProviderError("provider_symbol_unavailable")
        segment = "NSE_EQ" if exchange == "NSE" else "BSE_EQ"
        symbol = (
            row.get("UNDERLYING_SYMBOL", "").strip()
            or row.get("SYMBOL_NAME", "").strip().removesuffix("-EQ")
        )
        return candidate_for_instrument(
            self.name,
            f"{row['SECURITY_ID']}|{segment}",
            symbol,
            symbol=symbol,
            display_name=row.get("DISPLAY_NAME") or symbol,
            asset_class="equity",
            venue=exchange,
            currency="INR",
            country="IN",
            timezone_name="Asia/Kolkata",
        ).model_copy(update={"is_verified": True})

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        needle = query.strip().casefold()
        if not needle:
            raise MarketDataProviderError("search_query_required")
        rows = list({
            id(row): row
            for (exchange, symbol), row in self.adapter._equity_index().items()
            if needle in symbol.casefold()
            and row.get("EXCH_ID", "").upper() == exchange
        }.values())
        return [self._candidate(row) for row in rows[:limit]]

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        security_id, separator, segment = provider_instrument_id.partition("|")
        if not separator or segment not in {"NSE_EQ", "BSE_EQ"} or not security_id.isdigit():
            raise MarketDataProviderError("invalid_instrument_mapping")
        row = next(
            (
                item for item in self.adapter._equity_index().values()
                if item.get("SECURITY_ID") == security_id
                and item.get("EXCH_ID", "").upper()
                == ("NSE" if segment == "NSE_EQ" else "BSE")
            ),
            None,
        )
        if row is None:
            raise MarketDataProviderError("instrument_not_found")
        return self._candidate(row)

    def _mapping(self, instrument: InstrumentCandidate) -> tuple[str, str]:
        expected_segment = {
            "NSE": "NSE_EQ",
            "XNSE": "NSE_EQ",
            "NSI": "NSE_EQ",
            "NSE_EQ": "NSE_EQ",
            "BSE": "BSE_EQ",
            "XBOM": "BSE_EQ",
            "BOM": "BSE_EQ",
            "BSE_EQ": "BSE_EQ",
        }.get(instrument.venue.upper())
        if expected_segment is None:
            raise MarketDataProviderError("provider_symbol_unavailable")
        security_id, separator, mapped_segment = instrument.provider_instrument_id.partition("|")
        if not security_id.isdigit() or (
            separator and mapped_segment != expected_segment
        ):
            raise MarketDataProviderError("invalid_instrument_mapping")
        if not separator:
            indexed = self.adapter.find_verified_equity_mapping(
                instrument.symbol, instrument.venue
            )
            if indexed is None or indexed != (security_id, expected_segment):
                raise MarketDataProviderError("invalid_instrument_mapping")
        return security_id, expected_segment

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        security_id, segment = self._mapping(instrument)
        try:
            return self.adapter.get_market_quote(
                self.tokens["access_token"], self.tokens, segment, security_id
            )
        except BrokerAdapterError as error:
            raise MarketDataProviderError(
                error.category, status_code=error.status_code
            ) from None

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        if interval not in {"1d", "1h", "25m", "15m", "5m", "1m"}:
            raise MarketDataProviderError("unsupported_interval")
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise MarketDataProviderError("invalid_time_range")
        security_id, segment = self._mapping(instrument)
        bars: dict[datetime, HistoricalBar] = {}
        chunk_end = end
        chunk_days = 365 if interval == "1d" else 90
        for _ in range(24):
            if chunk_end <= start or len(bars) >= limit:
                break
            chunk_start = max(start, chunk_end - timedelta(days=chunk_days))
            try:
                returned = self.market_data_adapter.get_historical_bars(
                    segment,
                    security_id,
                    interval,
                    chunk_start,
                    chunk_end,
                )
            except BrokerAdapterError as error:
                raise MarketDataProviderError(
                    error.category, status_code=error.status_code
                ) from None
            for bar in returned:
                bars[bar.timestamp] = bar
            chunk_end = chunk_start
        if chunk_end > start and len(bars) < limit:
            raise MarketDataProviderError("provider_request_cap_reached")
        return sorted(bars.values(), key=lambda bar: bar.timestamp)[-limit:]

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=True,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote", "stream"),
            status="authenticated_session",
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
    return_on_equity_pct: Decimal | None
    debt_to_equity: Decimal | None
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


class InstrumentMasterInfo(BaseModel):
    isin: str | None
    exchange: str | None
    segment: str | None
    instrument_type: str | None
    lot_size: str | None
    tick_size: str | None
    listing_date: str | None
    expiry: str | None
    source: str


class CompanyProfileResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    status: Literal["available", "unavailable"]
    name: str | None
    sector: str | None
    industry: str | None
    description: str | None
    market_cap: Decimal | None
    currency: str | None
    source: str
    fetched_at: datetime | None
    message: str
    instrument_master: InstrumentMasterInfo
    disclaimer: str = "Company information is provider-reported decision support, not investment advice. No FX conversion is applied."


class OptionExpiryResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    expiries: list[date]
    source: str = "Dhan"
    fetched_at: datetime


class OptionGreeksResponse(BaseModel):
    delta: Decimal | None = None
    theta: Decimal | None = None
    gamma: Decimal | None = None
    vega: Decimal | None = None


class OptionContractResponse(BaseModel):
    security_id: str | None = None
    average_price: Decimal | None = None
    implied_volatility: Decimal | None = None
    last_price: Decimal | None = None
    oi: int | None = None
    previous_close_price: Decimal | None = None
    previous_oi: int | None = None
    previous_volume: int | None = None
    top_ask_price: Decimal | None = None
    top_ask_quantity: int | None = None
    top_bid_price: Decimal | None = None
    top_bid_quantity: int | None = None
    volume: int | None = None
    greeks: OptionGreeksResponse


class OptionStrikeResponse(BaseModel):
    strike: Decimal
    ce: OptionContractResponse | None
    pe: OptionContractResponse | None


class OptionChainResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    expiry: date
    underlying_price: Decimal | None
    strikes: list[OptionStrikeResponse]
    source: str = "Dhan"
    fetched_at: datetime


class OptionsSignalAPIResponse(BaseModel):
    action: Literal["BUY_CALL", "BUY_PUT", "EXIT_CALL", "EXIT_PUT", "NO_TRADE"]
    underlying_symbol: str
    as_of: datetime
    direction_score: int
    opposing_score: int
    selected_option_type: Literal["call", "put"] | None = None
    strike: Decimal | None = None
    reference_entry_premium: Decimal | None = None
    estimated_premium_risk_per_lot: Decimal | None = None
    suggested_lots_by_risk_budget: int | None = None
    option_return_pct_at_bid: Decimal | None = None
    reasons: list[str]
    blockers: list[str]
    evidence: dict[str, str | int | None]
    underlying_source: str | None
    chain_source: str | None
    bar_source: str
    chain_retrieved_at: datetime
    last_bar_timestamp: datetime | None
    disclaimer: str = "Decision support, not investment advice. No order was placed."


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
    angel_one_provider: AngelOneProvider | None = None,
    dhan_provider: DhanMarketDataProvider | None = None,
) -> MarketDataProviderChain:
    providers: list[MarketDataProvider] = []
    selected_names = names or settings.market_data_provider_names
    for name in selected_names:
        if name == "angel_one" and angel_one_provider is not None:
            providers.append(angel_one_provider)
        elif name == "dhan" and dhan_provider is not None:
            providers.append(dhan_provider)
        elif name == "twelve_data":
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


def _market_data_credentials(
    db: Session,
    user_id: UUID,
    provider_name: str,
) -> tuple[AngelOneProvider | None, DhanMarketDataProvider | None, str | None]:
    from app.api.brokers import _fresh_tokens, decrypt_tokens

    angel_provider = None
    dhan_provider = None
    upstox_token = None
    if provider_name == "angel_one":
        row = _user_market_data_settings(db, user_id)
        api_key = (
            settings.angel_one_api_key.get_secret_value()
            if settings.angel_one_api_key
            else ""
        )
        if (
            row is None
            or not row.encrypted_angel_tokens
            or not api_key.strip()
            or not settings.angel_one_client_public_ip.strip()
        ):
            raise HTTPException(
                status_code=409,
                detail="Angel One market-data authentication is required.",
            )
        tokens = decrypt_tokens(row.encrypted_angel_tokens)
        expiry = row.angel_expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry is None or expiry <= datetime.now(timezone.utc) + timedelta(minutes=1):
            try:
                tokens, expiry = AngelOneProvider.refresh_session(api_key, tokens)
            except MarketDataProviderError:
                row.angel_expires_at = datetime.now(timezone.utc)
                db.commit()
                raise HTTPException(
                    status_code=401,
                    detail="Angel One session expired. Reconnect to restore market data.",
                ) from None
            row.encrypted_angel_tokens = _encrypt_market_tokens(tokens)
            row.angel_expires_at = expiry
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
        angel_provider = AngelOneProvider(api_key, tokens)
    elif provider_name == "dhan":
        credential = db.scalar(
            select(BrokerCredential).where(
                BrokerCredential.user_id == user_id,
                BrokerCredential.broker == "dhan",
            )
        )
        if credential is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "not_configured", "message": "Connect Dhan before requesting Dhan market data."},
            )
        tokens, blocker = _validated_dhan_tokens(credential)
        if blocker is not None:
            raise HTTPException(status_code=409, detail={
                "code": "config_blocker",
                "message": blocker,
            })
        dhan_provider = DhanMarketDataProvider(DhanAdapter(), tokens)
    elif provider_name == "upstox":
        credential = db.scalar(
            select(BrokerCredential).where(
                BrokerCredential.user_id == user_id,
                BrokerCredential.broker == "upstox",
            )
        )
        if credential is None:
            raise HTTPException(
                status_code=409,
                detail="upstox broker authentication is required.",
            )
        adapter = get_broker_adapter("upstox")
        tokens = _fresh_tokens(db, adapter, credential)
        upstox_token = tokens["access_token"]
    return angel_provider, dhan_provider, upstox_token


def _validated_dhan_tokens(
    credential: BrokerCredential,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, str], str | None]:
    from app.api.brokers import decrypt_tokens

    try:
        tokens = decrypt_tokens(credential.encrypted_tokens)
    except HTTPException:
        return {}, "Dhan credentials cannot be decrypted; reconnect Dhan."
    access_token = tokens.get("access_token", "")
    client_id = tokens.get("client_id", "")
    if not access_token or not client_id:
        return {}, "Dhan access token or client ID is missing; reconnect Dhan."
    try:
        encoded = access_token.split(".")[1]
        payload = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        )
        claims = json.loads(payload)
        expiry_value = claims.get("exp") if isinstance(claims, dict) else None
        if not isinstance(expiry_value, (int, float)) or isinstance(expiry_value, bool):
            raise ValueError
        jwt_expiry = datetime.fromtimestamp(expiry_value, timezone.utc)
    except (IndexError, ValueError, TypeError, OverflowError, json.JSONDecodeError):
        return {}, "Dhan access token expiry could not be verified; reconnect Dhan."
    current = now or datetime.now(timezone.utc)
    persisted_expiry = credential.expires_at
    if persisted_expiry.tzinfo is None:
        persisted_expiry = persisted_expiry.replace(tzinfo=timezone.utc)
    if min(jwt_expiry, persisted_expiry) <= current:
        return {}, "Dhan access token has expired; reconnect Dhan before retrying."
    return tokens, None


def _encrypt_market_tokens(tokens: dict[str, str]) -> str:
    from app.api.brokers import encrypt_tokens

    return encrypt_tokens(tokens)


def _available_market_data_sources(
    db: Session, user_id: UUID
) -> list[MarketDataSourceOption]:
    labels = {
        "angel_one": "Angel One SmartAPI",
        "dhan": "Dhan",
        "yfinance": "Yahoo Finance",
        "twelve_data": "Twelve Data",
        "finnhub": "Finnhub",
        "delta_exchange": "Delta Exchange",
        "binance": "Binance",
        "upstox": "Upstox",
    }
    configured = set(settings.market_data_provider_names)
    market_settings = _user_market_data_settings(db, user_id)
    angel_api_key = (
        settings.angel_one_api_key.get_secret_value()
        if settings.angel_one_api_key
        else ""
    )
    angel_configured = bool(
        angel_api_key.strip() and settings.angel_one_client_public_ip.strip()
    )
    angel_connected = bool(
        market_settings
        and market_settings.encrypted_angel_tokens
        and angel_configured
    )
    broker_credentials = {
        credential.broker: credential
        for credential in db.scalars(
            select(BrokerCredential).where(
                BrokerCredential.user_id == user_id,
                BrokerCredential.broker.in_(("dhan", "upstox")),
            )
        )
    }
    result: list[MarketDataSourceOption] = []
    disabled_providers = set(
        market_settings.disabled_providers if market_settings else []
    )
    for name in MARKET_DATA_FALLBACK_PRIORITY:
        if name not in labels:
            continue
        if name not in configured:
            result.append(MarketDataSourceOption(
                name=name,
                label=labels[name],
                available=False,
                status="provider_disabled_by_configuration",
            ))
            continue
        if name == "angel_one":
            angel_expiry = (
                market_settings.angel_expires_at
                if market_settings is not None
                else None
            )
            if angel_expiry is not None and angel_expiry.tzinfo is None:
                angel_expiry = angel_expiry.replace(tzinfo=timezone.utc)
            angel_session_valid = bool(
                angel_connected
                and angel_expiry is not None
                and angel_expiry > datetime.now(timezone.utc)
            )
            result.append(MarketDataSourceOption(
                name=name,
                label=labels[name],
                available=angel_connected,
                status=(
                    "connected"
                    if angel_session_valid
                    else "session_expired"
                    if angel_connected
                    else "needs_connection"
                    if angel_configured
                    else "provider_configuration_required"
                ),
                expires_at=angel_expiry,
            ))
        elif name in {"dhan", "upstox"}:
            credential = broker_credentials.get(name)
            enabled = (
                settings.broker_dhan_enabled
                and bool(
                    settings.dhan_api_key
                    and settings.dhan_api_key.get_secret_value().strip()
                    and settings.dhan_api_secret
                    and settings.dhan_api_secret.get_secret_value().strip()
                )
                if name == "dhan"
                else settings.broker_upstox_enabled
                and bool(
                    settings.upstox_client_id
                    and settings.upstox_client_id.get_secret_value().strip()
                    and settings.upstox_client_secret
                    and settings.upstox_client_secret.get_secret_value().strip()
                )
            )
            dhan_blocker = None
            if name == "dhan" and credential is not None and enabled:
                _, dhan_blocker = _validated_dhan_tokens(credential)
            connected = bool(credential and enabled and dhan_blocker is None)
            result.append(MarketDataSourceOption(
                name=name,
                label=labels[name],
                available=connected,
                status=(
                    "connected"
                    if connected
                    else "config_blocker"
                    if dhan_blocker
                    else "needs_connection"
                ),
            ))
        elif name == "twelve_data":
            available = bool(settings.twelve_data_api_key)
            result.append(MarketDataSourceOption(
                name=name,
                label=labels[name],
                available=available,
                status="configured" if available else "provider_configuration_required",
            ))
        elif name == "finnhub":
            available = bool(settings.finnhub_api_key)
            result.append(MarketDataSourceOption(
                name=name,
                label=labels[name],
                available=available,
                status="configured" if available else "provider_configuration_required",
            ))
        elif name == "yfinance":
            result.append(MarketDataSourceOption(
                name=name,
                label=labels[name],
                available=True,
                status="configured_connectivity_unverified",
            ))
    for source in result:
        source.enabled = source.name not in disabled_providers
    return result


def _default_market_data_source(
    db: Session, user_id: UUID
) -> str | None:
    sources = {
        option.name: option
        for option in _available_market_data_sources(db, user_id)
    }
    return next(
        (
            name for name in MARKET_DATA_FALLBACK_PRIORITY
            if (
                sources.get(name) is not None
                and sources[name].available
                and sources[name].enabled
            )
        ),
        None,
    )


def _selected_market_data_source(
    db: Session, user_id: UUID, sources: list[MarketDataSourceOption]
) -> str | None:
    preference = _user_market_data_settings(db, user_id)
    selected = preference.selected_provider if preference is not None else None
    indexed = {source.name: source for source in sources}
    if (
        selected is not None
        and selected in indexed
        and indexed[selected].available
        and indexed[selected].enabled
    ):
        return selected
    return _default_market_data_source(db, user_id)


def get_market_data_provider(
    active_session: UserSession = Depends(get_active_session),
    db: Session = Depends(get_db),
) -> MarketDataProvider:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before accessing market data.",
            },
        )
    return market_data_provider_for_user(db, active_session.user_id)


def market_data_provider_for_user(db: Session, user_id: UUID) -> MarketDataProvider:
    configured = set(settings.market_data_provider_names)
    source_options = {
        option.name: option
        for option in _available_market_data_sources(db, user_id)
    }
    providers: list[MarketDataProvider] = []
    unavailable: list[dict[str, str]] = []
    for name in INSTRUMENT_SEARCH_PRIORITY:
        option = source_options.get(name)
        if option is not None and not option.enabled:
            unavailable.append({
                "provider": name,
                "reason": "provider_disabled_by_user",
            })
            continue
        if name not in configured or option is None or not option.available:
            unavailable.append({
                "provider": name,
                "reason": (
                    "provider_not_enabled_by_configuration"
                    if name not in configured or option is None
                    else "config_blocker"
                    if option.status == "config_blocker"
                    else "not_configured_or_authenticated"
                ),
            })
            continue
        try:
            angel_provider, dhan_provider, upstox_token = (
                _market_data_credentials(db, user_id, name)
                if name in {"angel_one", "dhan", "upstox"}
                else (None, None, None)
            )
        except HTTPException as error:
            logger.info("Skipping unavailable market-data provider %s for user %s.", name, user_id)
            detail = error.detail
            unavailable.append({
                "provider": name,
                "reason": (
                    "config_blocker"
                    if isinstance(detail, dict)
                    and detail.get("code") == "config_blocker"
                    else "authentication_expired"
                    if error.status_code == 401
                    else "authentication_required"
                    if error.status_code == 409
                    else "provider_unavailable"
                ),
            })
            continue
        providers.extend(create_market_data_provider_chain(
            (name,),
            upstox_access_token=upstox_token,
            angel_one_provider=angel_provider,
            dhan_provider=dhan_provider,
        ).providers)
    chain = MarketDataProviderChain(providers)
    chain.unavailable_providers = unavailable
    return chain


def _providers(provider: MarketDataProvider) -> list[MarketDataProvider]:
    providers = list(getattr(provider, "providers", [provider]))
    if not any(item.name in MARKET_DATA_FALLBACK_PRIORITY for item in providers):
        return providers
    indexed = {item.name: item for item in providers}
    ordered = [
        indexed[name]
        for name in MARKET_DATA_FALLBACK_PRIORITY
        if name in indexed
    ]
    ordered.extend(
        item for item in providers
        if item.name not in MARKET_DATA_FALLBACK_PRIORITY
    )
    return ordered


class ProviderCircuitBreaker:
    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_seconds: float = 30,
        clock=time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        self._half_open: set[str] = set()

    def before_attempt(self, provider: str) -> str | None:
        with self._lock:
            now = self.clock()
            blocked_until = self._blocked_until.get(provider)
            if blocked_until is None:
                return None
            if now < blocked_until:
                return "circuit_breaker_open"
            if provider in self._half_open:
                return "circuit_breaker_probe_in_progress"
            self._half_open.add(provider)
            return None

    def record_success(self, provider: str) -> None:
        with self._lock:
            self._failures.pop(provider, None)
            self._blocked_until.pop(provider, None)
            self._half_open.discard(provider)

    def reset(self, provider: str) -> None:
        self.record_success(provider)

    def record_failure(
        self, provider: str, *, cooldown_seconds: float | None = None
    ) -> None:
        with self._lock:
            failures = self._failures.get(provider, 0) + 1
            self._failures[provider] = failures
            self._half_open.discard(provider)
            if failures >= self.threshold or cooldown_seconds is not None:
                cooldown = self.cooldown_seconds
                if cooldown_seconds is not None:
                    cooldown = max(0.0, min(cooldown_seconds, 300.0))
                self._blocked_until[provider] = self.clock() + cooldown


_provider_circuit_breaker = ProviderCircuitBreaker()


def reset_provider_circuit_breaker(provider: str) -> None:
    _provider_circuit_breaker.reset(provider)


def _market_session_state(
    instrument: Instrument | InstrumentCandidate | None,
    now: datetime | None = None,
) -> Literal["open", "closed", "unknown"]:
    if instrument is None:
        return "unknown"
    country = instrument.country.upper()
    if country == "IN":
        zone = ZoneInfo("Asia/Kolkata")
        opening, closing = (9, 15), (15, 30)
    elif country == "US":
        zone = ZoneInfo("America/New_York")
        opening, closing = (9, 30), (16, 0)
    else:
        return "unknown"
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    if local.weekday() >= 5:
        return "closed"
    clock = (local.hour, local.minute)
    return "open" if opening <= clock < closing else "closed"


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
    if mapping is None:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.instrument_id == instrument.id,
                InstrumentProviderIdentifier.provider == provider.name,
            )
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
        candidate = candidate.model_copy(update={"is_verified": mapping.is_verified})
        return _validated_candidate(provider, candidate), mapping

    candidate: InstrumentCandidate | None = None
    if provider.name in {"twelve_data", "angel_one", "dhan"}:
        matches = (
            provider.search_instruments_for_exchange(
                instrument.symbol, instrument.venue, 50
            )
            if isinstance(provider, AngelOneProvider)
            else provider.search_instruments(instrument.symbol, 50)
        )
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
    existing_mapping = db.scalar(
        select(InstrumentProviderIdentifier).where(
            InstrumentProviderIdentifier.provider == provider.name,
            InstrumentProviderIdentifier.provider_instrument_id
            == candidate.provider_instrument_id,
        )
    )
    if existing_mapping is not None:
        if existing_mapping.instrument_id != instrument.id:
            raise MarketDataProviderError("provider_mapping_conflict")
        mapping = existing_mapping
        if mapping not in instrument.provider_identifiers:
            instrument.provider_identifiers.append(mapping)
        return _validated_candidate(provider, candidate), mapping

    mapping = InstrumentProviderIdentifier(
        instrument_id=instrument.id,
        provider=provider.name,
        provider_instrument_id=candidate.provider_instrument_id,
        provider_symbol=candidate.provider_symbol,
        is_verified=candidate.is_verified,
        last_seen_at=datetime.now(timezone.utc),
    )
    try:
        with db.begin_nested():
            db.add(mapping)
            db.flush()
    except IntegrityError:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.provider == provider.name,
                InstrumentProviderIdentifier.provider_instrument_id
                == candidate.provider_instrument_id,
            )
        )
        if mapping is None or mapping.instrument_id != instrument.id:
            raise MarketDataProviderError("provider_mapping_conflict")
    if mapping not in instrument.provider_identifiers:
        instrument.provider_identifiers.append(mapping)
    return _validated_candidate(provider, candidate), mapping


def _provider_history_limitation(
    provider_name: str, interval: str, start: datetime
) -> str | None:
    supported = {
        "angel_one": {"1d", "1h", "15m", "5m", "1m"},
        "dhan": {"1d", "1m", "5m", "15m", "25m", "1h"},
        "twelve_data": {"1d", "1h", "15m"},
        "finnhub": {"1d", "1h", "15m"},
        "yfinance": {"1d", "1h", "15m", "1m"},
        "delta_exchange": {"1d", "1h", "15m", "5m", "1m"},
        "binance": {"1d", "1h", "15m", "5m", "1m"},
        "upstox": {"1d", "1h", "15m", "1m"},
    }
    intervals = supported.get(provider_name)
    if intervals is not None and interval not in intervals:
        return "unsupported_interval"
    if provider_name == "yfinance":
        lookback_days = {"1m": 7, "15m": 60, "1h": 730}.get(interval)
        if lookback_days is not None and start < datetime.now(timezone.utc) - timedelta(days=lookback_days):
            return f"yfinance_{interval}_lookback_exceeded_{lookback_days}_days"
    return None


class ProviderHealthResponse(BaseModel):
    name: str
    healthy: bool
    is_mock: bool
    capabilities: list[str]
    status: str


class ProviderResponse(BaseModel):
    provider: ProviderHealthResponse
    configuration: str


class MarketDataSettingsResponse(BaseModel):
    selected_provider: str | None
    sources: list[MarketDataSourceOption]


class MarketDataSourceRequest(BaseModel):
    provider: Literal[
        "angel_one", "dhan", "yfinance", "twelve_data", "finnhub",
        "delta_exchange", "binance", "upstox",
    ]


class MarketDataProviderEnableRequest(BaseModel):
    enabled: bool


class ProviderBreakerResetResponse(BaseModel):
    provider: str
    state: Literal["closed"]
    consecutive_failures: int


class AngelOneConnectRequest(BaseModel):
    client_code: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=4, max_length=32)
    totp: str = Field(pattern=r"^[0-9]{6}$")


class AngelOneConnectResponse(BaseModel):
    connected: bool
    expires_at: datetime
    message: str


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
    tick_size: str | None
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


class ProviderAttemptResponse(BaseModel):
    provider: str
    outcome: Literal[
        "used", "unavailable", "unsupported", "stale", "skipped",
        "rate_limited", "no_new_bars", "config_blocker",
    ]
    reason: str | None = None
    reason_category: ReasonCategory | None = None
    http_status: int | None = None
    latency_ms: float | None = None
    bars: int = 0


class BackfillChunkDetail(TypedDict):
    start: datetime
    end: datetime
    provider: str | None
    response_status: int | None
    rows_returned: int
    rows_written: int
    outcome: str
    reason: str | None


class HistoryResponse(BaseModel):
    instrument_id: UUID
    interval: BarInterval
    freshness: FreshnessResponse
    bars: list[MarketBarResponse]
    offset: int
    limit: int
    provider_attempts: list[ProviderAttemptResponse] = Field(default_factory=list)
    fallback_reason: str | None = None
    provider: str | None = None
    fetched_at: datetime | None = None
    as_of: datetime | None = None
    currency: str | None = None
    session_state: Literal["open", "closed", "unknown"] = "unknown"
    is_cached: bool = True
    sync_status: Literal["success", "unavailable", "no_new_bars"] = "success"


class IndicatorValueResponse(BaseModel):
    value: Decimal | None
    is_synthetic: bool
    bars_required: int
    bars_used: int
    status: Literal["available", "insufficient_history"]


class IndicatorSeriesPointResponse(BaseModel):
    timestamp: datetime
    value: Decimal


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
    indicator_series: dict[str, list[IndicatorSeriesPointResponse]]
    disclaimer: str = "Decision support, not investment advice."


def _indicator_result(value: IndicatorValue) -> IndicatorValueResponse:
    return IndicatorValueResponse(
        value=value.value,
        is_synthetic=value.is_synthetic,
        bars_required=value.bars_required,
        bars_used=value.bars_used,
        status="available" if value.value is not None else "insufficient_history",
    )


def _ema_values(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    average = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = average
    alpha = Decimal("2") / Decimal(period + 1)
    for index in range(period, len(values)):
        average = values[index] * alpha + average * (Decimal("1") - alpha)
        result[index] = average
    return result


def _indicator_series(
    bars: list[MarketBar], timezone_name: str
) -> dict[str, list[IndicatorSeriesPointResponse]]:
    keys = (
        "sma_20", "sma_50", "sma_200", "ema_20", "ema_50", "ema_200",
        "vwap", "bb_upper", "bb_middle", "bb_lower", "rsi_14",
        "macd", "macd_signal", "macd_histogram", "atr_14",
    )
    values: dict[str, list[IndicatorSeriesPointResponse]] = {
        key: [] for key in keys
    }
    closes = [bar.close for bar in bars]
    with localcontext() as context:
        context.prec = 34
        ema_values = {
            period: _ema_values(closes, period)
            for period in (20, 50, 200, 12, 26)
        }
        macd_values: list[Decimal] = []
        macd_indices: list[int] = []
        for index, (fast, slow) in enumerate(zip(ema_values[12], ema_values[26])):
            if fast is not None and slow is not None:
                macd_values.append(fast - slow)
                macd_indices.append(index)
        macd_index_by_bar = {
            bar_index: macd_index
            for macd_index, bar_index in enumerate(macd_indices)
        }
        signal_values = _ema_values(macd_values, 9)
        cumulative_volume = Decimal("0")
        cumulative_value = Decimal("0")
        current_session = None
        exchange_timezone = ZoneInfo(timezone_name)
        for index, bar in enumerate(bars):
            timestamp = _utc(bar.timestamp)
            if index >= 19:
                window = closes[index - 19:index + 1]
                mean = sum(window, Decimal("0")) / Decimal(20)
                variance = sum(
                    ((close - mean) ** 2 for close in window), Decimal("0")
                ) / Decimal(20)
                deviation = variance.sqrt() * Decimal("2")
                values["bb_upper"].append(IndicatorSeriesPointResponse(
                    timestamp=timestamp, value=mean + deviation
                ))
                values["bb_middle"].append(IndicatorSeriesPointResponse(
                    timestamp=timestamp, value=mean
                ))
                values["bb_lower"].append(IndicatorSeriesPointResponse(
                    timestamp=timestamp, value=mean - deviation
                ))
            for key, period, calculator in (
                ("sma_20", 20, sma),
                ("sma_50", 50, sma),
                ("sma_200", 200, sma),
                ("rsi_14", 14, rsi),
                ("atr_14", 14, atr),
            ):
                calculated = calculator(bars[:index + 1], period).value
                if calculated is not None:
                    values[key].append(IndicatorSeriesPointResponse(
                        timestamp=timestamp, value=calculated
                    ))
            for period in (20, 50, 200):
                calculated = ema_values[period][index]
                if calculated is not None:
                    values[f"ema_{period}"].append(IndicatorSeriesPointResponse(
                        timestamp=timestamp, value=calculated
                    ))

            session_date = timestamp.astimezone(exchange_timezone).date()
            if session_date != current_session:
                cumulative_volume = Decimal("0")
                cumulative_value = Decimal("0")
                current_session = session_date
            typical_price = (bar.high + bar.low + bar.close) / Decimal("3")
            cumulative_volume += bar.volume
            cumulative_value += typical_price * bar.volume
            if cumulative_volume > 0:
                values["vwap"].append(IndicatorSeriesPointResponse(
                    timestamp=timestamp,
                    value=cumulative_value / cumulative_volume,
                ))

            macd_index = macd_index_by_bar.get(index)
            if macd_index is not None:
                macd_value = macd_values[macd_index]
                values["macd"].append(IndicatorSeriesPointResponse(
                    timestamp=timestamp, value=macd_value
                ))
                signal_value = signal_values[macd_index]
                if signal_value is not None:
                    values["macd_signal"].append(IndicatorSeriesPointResponse(
                        timestamp=timestamp, value=signal_value
                    ))
                    values["macd_histogram"].append(IndicatorSeriesPointResponse(
                        timestamp=timestamp, value=macd_value - signal_value
                    ))
    return values


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
    freshness_status: Literal[
        "fresh", "stale", "historical", "eod_close", "unavailable", "synthetic",
        "unverified", "future_dated",
    ]
    freshness_message: str
    is_cached: bool
    is_synthetic: bool
    is_verified: bool
    provider_configured: bool
    provider_request_status: Literal["succeeded", "not_attempted", "failed"]
    provider_attempts: list[ProviderAttemptResponse] = Field(default_factory=list)
    fallback_reason: str | None = None
    provider_connection_status: Literal[
        "not_applicable_rest", "connected", "reconnecting", "disconnected", "unavailable"
    ] = "not_applicable_rest"
    feed_active: bool = False
    market_timestamp: datetime
    market_age_seconds: float
    absolute_change: Decimal | None = None
    percent_change: Decimal | None = None
    data_type: Literal[
        "provider_quote", "provider_snapshot", "live_tick", "daily_close"
    ]
    delivery_mode: Literal[
        "on_demand_rest", "rest_snapshot", "websocket_tick"
    ] = "on_demand_rest"
    stream_status: Literal[
        "not_configured", "active", "reconnecting", "stale", "unavailable"
    ] = "not_configured"
    provider_delay_seconds: None = None
    provider: str | None = None
    fetched_at: datetime | None = None
    as_of: datetime | None = None
    interval: str = "quote"
    session_state: Literal["open", "closed", "unknown"] = "unknown"
    bar: HistoricalBar


class WatchlistQuoteResponse(BaseModel):
    source: str
    retrieved_at: datetime
    freshness: Literal["fresh", "stale"]
    freshness_status: Literal[
        "fresh", "stale", "historical", "eod_close", "unavailable", "synthetic",
        "unverified", "future_dated",
    ]
    is_synthetic: bool
    is_verified: bool
    market_timestamp: datetime
    market_age_seconds: float
    data_type: Literal[
        "provider_quote", "provider_snapshot", "live_tick", "daily_close"
    ]
    close: Decimal
    absolute_change: Decimal | None = None
    percent_change: Decimal | None = None


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


class WatchlistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class WatchlistResponse(BaseModel):
    id: UUID
    name: str
    instrument_count: int
    created_at: datetime


def _instrument_response(
    instrument: Instrument, *, include_tick_size: bool = False
) -> InstrumentResponse:
    tick_size = None
    if include_tick_size:
        dhan_mapping = next(
            (
                mapping
                for mapping in instrument.provider_identifiers
                if mapping.provider == "dhan" and mapping.is_verified
            ),
            None,
        )
        if dhan_mapping is not None:
            details = DhanAdapter.get_instrument_master_details(
                dhan_mapping.provider_instrument_id,
                instrument.venue,
                instrument.symbol,
            )
            tick_size = details.get("tick_size") if details else None
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
        tick_size=tick_size,
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


def _dhan_option_chain_context(
    db: Session,
    user_id: UUID,
    instrument: Instrument,
) -> tuple[DhanAdapter, dict[str, str], int, str]:
    if instrument.is_synthetic or instrument.country.upper() != "IN":
        raise HTTPException(
            status_code=422,
            detail="Dhan option chain requires a verified Indian underlying instrument.",
        )
    if instrument.asset_class not in {"equity", "index"}:
        raise HTTPException(
            status_code=422,
            detail="Dhan option chain is available only for equity and index underlyings.",
        )

    _, provider, _ = _market_data_credentials(db, user_id, "dhan")
    if provider is None:
        raise HTTPException(status_code=409, detail="Connect Dhan to request its option chain.")

    adapter = provider.adapter
    mapping = next(
        (
            item
            for item in instrument.provider_identifiers
            if item.provider == "dhan" and item.is_verified
        ),
        None,
    )
    if mapping is not None:
        parts = mapping.provider_instrument_id.split("|", 1)
        security_id = parts[0]
        segment = parts[1] if len(parts) == 2 else {
            "equity": {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}.get(instrument.venue.upper()),
            "index": "IDX_I",
        }[instrument.asset_class]
    elif instrument.asset_class == "equity":
        resolved = adapter.find_verified_equity_mapping(
            instrument.symbol, instrument.venue
        )
        if resolved is None:
            raise HTTPException(
                status_code=422,
                detail="Dhan has no verified underlying mapping for this instrument.",
            )
        security_id, segment = resolved
    else:
        raise HTTPException(
            status_code=422,
            detail="Dhan has no verified underlying mapping for this instrument.",
        )

    if segment not in {"NSE_EQ", "BSE_EQ", "IDX_I", "MCX_COMM"}:
        raise HTTPException(
            status_code=422,
            detail="Dhan option chain does not support this underlying segment.",
        )
    try:
        underlying_scrip = int(security_id)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="Dhan underlying security ID is invalid.",
        ) from error
    if underlying_scrip <= 0:
        raise HTTPException(
            status_code=422,
            detail="Dhan underlying security ID is invalid.",
        )
    return adapter, provider.tokens, underlying_scrip, segment


def _option_decimal(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise HTTPException(
            status_code=502,
            detail=f"Dhan option-chain response has invalid {field}.",
        ) from error
    if not result.is_finite():
        raise HTTPException(
            status_code=502,
            detail=f"Dhan option-chain response has invalid {field}.",
        )
    return result


def _option_integer(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(
            status_code=502,
            detail=f"Dhan option-chain response has invalid {field}.",
        ) from error


def _option_contract(raw: dict[str, Any]) -> OptionContractResponse:
    greeks = raw.get("greeks")
    if not isinstance(greeks, dict):
        greeks = {}
    return OptionContractResponse(
        security_id=str(raw["security_id"]) if raw.get("security_id") is not None else None,
        average_price=_option_decimal(raw.get("average_price"), "average price"),
        implied_volatility=_option_decimal(
            raw.get("implied_volatility"), "implied volatility"
        ),
        last_price=_option_decimal(raw.get("last_price"), "last price"),
        oi=_option_integer(raw.get("oi"), "open interest"),
        previous_close_price=_option_decimal(
            raw.get("previous_close_price"), "previous close"
        ),
        previous_oi=_option_integer(raw.get("previous_oi"), "previous open interest"),
        previous_volume=_option_integer(raw.get("previous_volume"), "previous volume"),
        top_ask_price=_option_decimal(raw.get("top_ask_price"), "ask price"),
        top_ask_quantity=_option_integer(
            raw.get("top_ask_quantity"), "ask quantity"
        ),
        top_bid_price=_option_decimal(raw.get("top_bid_price"), "bid price"),
        top_bid_quantity=_option_integer(
            raw.get("top_bid_quantity"), "bid quantity"
        ),
        volume=_option_integer(raw.get("volume"), "volume"),
        greeks=OptionGreeksResponse(
            delta=_option_decimal(greeks.get("delta"), "delta"),
            theta=_option_decimal(greeks.get("theta"), "theta"),
            gamma=_option_decimal(greeks.get("gamma"), "gamma"),
            vega=_option_decimal(greeks.get("vega"), "vega"),
        ),
    )


def _freshness(
    db: Session,
    instrument_id: UUID,
    interval: str,
    *,
    source: str | None = None,
    now: datetime | None = None,
) -> FreshnessResponse:
    instrument = db.get(Instrument, instrument_id)
    is_synthetic_instrument = instrument.is_synthetic if instrument else False
    sync_query = select(MarketDataSync).where(
            MarketDataSync.instrument_id == instrument_id,
            MarketDataSync.interval == interval,
        )
    if source is not None:
        sync_query = sync_query.where(MarketDataSync.source == source)
    sync = db.scalar(sync_query.order_by(
            MarketDataSync.attempted_at.desc(),
            MarketDataSync.succeeded_at.is_not(None).desc(),
        ))
    latest_bar_query = (
        select(MarketBar)
        .where(MarketBar.instrument_id == instrument_id, MarketBar.interval == interval)
        .where(MarketBar.is_synthetic.is_(True) if is_synthetic_instrument else MarketBar.is_synthetic.is_(False))
    )
    if source is not None:
        latest_bar_query = latest_bar_query.where(MarketBar.source == source)
    latest_bar = db.scalar(
        latest_bar_query.order_by(MarketBar.timestamp.desc()).limit(1)
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
    if interval == "1d":
        start_utc = start_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day_start = end_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        end_utc = end_day_start + (
            timedelta(days=1) if end_utc > end_day_start else timedelta()
        )
    return start_utc, end_utc


def _canonical_interval(interval: BarInterval) -> BarInterval:
    return "1h" if interval == "60m" else interval


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
    reason_category: ReasonCategory | None,
    attempted_at: datetime,
    http_diagnostics: list[dict[str, Any]] | None = None,
) -> None:
    if reason_category is not None and not isinstance(reason_category, ReasonCategory):
        raise ValueError("reason_category must be a ReasonCategory")
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
        reason_category=reason_category.value if reason_category else None,
        http_diagnostics=http_diagnostics or [],
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
                if active_provider.name == "dhan" and interval != "1d":
                    bars = bars[-limit:]
                else:
                    raise MarketDataProviderError("provider_exceeded_bar_limit")
            timestamps = [bar.timestamp for bar in bars]
            if timestamps != sorted(set(timestamps)):
                raise MarketDataProviderError("provider_returned_duplicate_or_unsorted_bars")
            if any(not start <= bar.timestamp < end for bar in bars):
                raise MarketDataProviderError("provider_returned_out_of_range_bar")
            return bars
        except MarketDataProviderError as error:
            if error.status_code == 429 and attempt == 0:
                time.sleep(_retry_after_seconds(error.retry_after, attempt))
                continue
            raise
        except ValidationError:
            raise MarketDataProviderError(
                "provider_unavailable_or_invalid_response"
            ) from None
    raise MarketDataProviderError("provider_rate_limit_exceeded", status_code=429)


def _sync_dhan_history(
    db: Session,
    instrument: Instrument,
    user: User,
    interval: str,
    start: datetime,
    end: datetime,
    limit: int,
    cached_bars: list[MarketBar],
) -> bool:
    if instrument.is_synthetic or instrument.currency.upper() != "INR":
        return False
    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == user.id,
            BrokerCredential.broker == "dhan",
        )
    )
    adapter = DhanAdapter()
    if credential is None:
        return False
    if interval != "1d" and start < datetime.now(timezone.utc) - timedelta(days=365 * 5 + 2):
        raise HTTPException(
            status_code=422,
            detail="Dhan intraday history is available for up to five years; choose a later start date.",
        )
    try:
        if instrument.asset_class != "equity":
            return False
        mapping_values = adapter.find_verified_equity_mapping(
            instrument.symbol, instrument.venue
        )
        if mapping_values is None:
            logger.warning(
                "Dhan history unavailable instrument_id=%s reason=instrument_mapping_unverified",
                instrument.id,
            )
            return False
        security_id, exchange_segment = mapping_values
        mapping_owner = db.scalar(
            select(InstrumentProviderIdentifier.instrument_id).where(
                InstrumentProviderIdentifier.provider == adapter.slug,
                InstrumentProviderIdentifier.provider_instrument_id == security_id,
                InstrumentProviderIdentifier.instrument_id != instrument.id,
            )
        )
        if mapping_owner is not None:
            logger.warning(
                "Dhan history unavailable instrument_id=%s reason=instrument_mapping_conflict",
                instrument.id,
            )
            return False
        mapping = next(
            (
                item for item in instrument.provider_identifiers
                if item.provider == adapter.slug
            ),
            None,
        )
        if mapping is not None and mapping.provider_instrument_id != security_id:
            logger.warning(
                "Dhan history unavailable instrument_id=%s reason=instrument_mapping_conflict",
                instrument.id,
            )
            return False
        if mapping is None:
            mapping = InstrumentProviderIdentifier(
                instrument_id=instrument.id,
                provider=adapter.slug,
                provider_instrument_id=security_id,
                provider_symbol=instrument.symbol,
                is_verified=True,
                last_seen_at=datetime.now(timezone.utc),
            )
            instrument.provider_identifiers.append(mapping)
            db.add(mapping)
        else:
            mapping.is_verified = True
            mapping.last_seen_at = datetime.now(timezone.utc)

        requested_start = start
        requested_end = end
        if cached_bars:
            oldest = min(_utc(row.timestamp) for row in cached_bars)
            newest = max(_utc(row.timestamp) for row in cached_bars)
            if start < oldest:
                requested_end = min(end, oldest)
            elif end > newest + timedelta(minutes={
                "1m": 1, "5m": 5, "15m": 15, "1h": 60,
                "1d": 1440,
            }[interval]):
                requested_start = max(start, newest + timedelta(minutes={
                    "1m": 1, "5m": 5, "15m": 15, "1h": 60,
                    "1d": 1440,
                }[interval]))
            else:
                return False
        if requested_start >= requested_end:
            return False

        from app.api.brokers import _fresh_tokens

        tokens = _fresh_tokens(db, adapter, credential)
        candidate = candidate_for_instrument(
            adapter.slug,
            security_id,
            instrument.symbol,
            symbol=instrument.symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            venue=instrument.venue,
            currency=instrument.currency,
            country=instrument.country,
            timezone_name="Asia/Kolkata",
        )
        bars: list[HistoricalBar] = []
        chunk_end = requested_end
        while chunk_end > requested_start and len(bars) < limit:
            chunk_start = max(requested_start, chunk_end - timedelta(days=90))
            chunk = adapter.get_historical_bars(
                tokens["access_token"],
                tokens,
                exchange_segment,
                security_id,
                interval,
                chunk_start,
                chunk_end,
            )
            bars.extend(chunk)
            chunk_end = chunk_start
        bars = sorted(
            {bar.timestamp: bar for bar in bars}.values(),
            key=lambda bar: bar.timestamp,
        )[-limit:]
        if not bars:
            logger.info(
                "Dhan history returned no candles instrument_id=%s interval=%s",
                instrument.id,
                interval,
            )
            retrieved_at = datetime.now(timezone.utc)
            _sync_record(
                db,
                instrument.id,
                interval,
                source=adapter.slug,
                attempted_at=retrieved_at,
                status="success",
                rows_received=0,
                safe_error_code=None,
                requested_start=requested_start,
                requested_end=requested_end,
                requested_limit=limit,
                safe_reason="provider_no_history_for_range",
            )
            _record_sync_attempt(
                db,
                instrument_id=instrument.id,
                interval=interval,
                provider=adapter.slug,
                start=requested_start,
                end=requested_end,
                limit=limit,
                response_status=200,
                rows_returned=0,
                rows_written=0,
                outcome="success",
                reason="provider_no_history_for_range",
                reason_category=ReasonCategory.PROVIDER_NO_HISTORY_FOR_RANGE,
                attempted_at=retrieved_at,
            )
            db.commit()
            return True
        retrieved_at = datetime.now(timezone.utc)
        rows_written = _persist_real_bars(
            db, instrument, adapter, candidate, interval, bars, retrieved_at
        )
        _sync_record(
            db,
            instrument.id,
            interval,
            source=adapter.slug,
            attempted_at=retrieved_at,
            status="success",
            rows_received=len(bars),
            safe_error_code=None,
            requested_start=requested_start,
            requested_end=requested_end,
            requested_limit=limit,
            rows_written=rows_written,
            safe_reason=None if rows_written else "provider_data_already_stored",
        )
        _record_sync_attempt(
            db,
            instrument_id=instrument.id,
            interval=interval,
            provider=adapter.slug,
            start=requested_start,
            end=requested_end,
            limit=limit,
            response_status=None,
            rows_returned=len(bars),
            rows_written=rows_written,
            outcome="success" if rows_written else "unchanged",
            reason=None if rows_written else "provider_data_already_stored",
            reason_category=(
                None
                if rows_written
                else ReasonCategory.PROVIDER_DATA_ALREADY_STORED
            ),
            attempted_at=retrieved_at,
        )
        db.commit()
        return True
    except HTTPException:
        db.rollback()
        raise
    except BrokerReconnectRequired as error:
        credential.expires_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(
            status_code=401,
            detail="Dhan authentication expired. Reconnect and retry market-data sync.",
        ) from error
    except (BrokerAdapterError, BrokerRateLimited, KeyError, ValueError) as error:
        db.rollback()
        _log_market_data_failure(
            "Dhan historical request failed (instrument_id=%s interval=%s)",
            error,
            "dhan_historical_data_unavailable",
            instrument.id,
            interval,
        )
        return False


@router.get("/settings", response_model=MarketDataSettingsResponse)
def get_market_data_settings(
    active_session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
) -> MarketDataSettingsResponse:
    sources = _available_market_data_sources(db, active_session.user_id)
    selected = _selected_market_data_source(
        db, active_session.user_id, sources
    )
    return MarketDataSettingsResponse(selected_provider=selected, sources=sources)


@router.put(
    "/settings/providers/{provider_name}",
    response_model=MarketDataSettingsResponse,
)
def set_market_data_provider_enabled(
    provider_name: str,
    request: MarketDataProviderEnableRequest,
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MarketDataSettingsResponse:
    if provider_name not in MARKET_DATA_FALLBACK_PRIORITY:
        raise HTTPException(status_code=404, detail="Market-data provider not found")
    preference = _user_market_data_settings(db, active_session.user_id)
    if preference is None:
        preference = UserMarketDataSettings(
            user_id=active_session.user_id,
            disabled_providers=[],
            updated_at=datetime.now(timezone.utc),
        )
        db.add(preference)
    disabled = set(preference.disabled_providers or [])
    if request.enabled:
        disabled.discard(provider_name)
    else:
        disabled.add(provider_name)
    preference.disabled_providers = sorted(disabled)
    if (
        not request.enabled
        and preference.selected_provider == provider_name
    ):
        preference.selected_provider = None
    preference.updated_at = datetime.now(timezone.utc)
    db.commit()
    sources = _available_market_data_sources(db, active_session.user_id)
    return MarketDataSettingsResponse(
        selected_provider=_selected_market_data_source(
            db, active_session.user_id, sources
        ),
        sources=sources,
    )


@router.put("/settings/provider", response_model=MarketDataSettingsResponse)
def select_market_data_provider(
    request: MarketDataSourceRequest,
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MarketDataSettingsResponse:
    sources = _available_market_data_sources(db, active_session.user_id)
    source = next(
        (
            item for item in sources
            if item.name == request.provider
        ),
        None,
    )
    if source is None or not source.available or not source.enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "provider_not_eligible",
                "message": "Selected provider is not eligible for this account.",
            },
        )
    preference = _user_market_data_settings(db, active_session.user_id)
    now = datetime.now(timezone.utc)
    if preference is None:
        preference = UserMarketDataSettings(
            user_id=active_session.user_id,
            selected_provider=request.provider,
            disabled_providers=[],
            updated_at=now,
        )
        db.add(preference)
    else:
        preference.selected_provider = request.provider
        preference.updated_at = now
    db.commit()
    sources = _available_market_data_sources(db, active_session.user_id)
    return MarketDataSettingsResponse(
        selected_provider=_selected_market_data_source(
            db, active_session.user_id, sources
        ),
        sources=sources,
    )


@router.post(
    "/providers/{provider_name}/breaker/reset",
    response_model=ProviderBreakerResetResponse,
)
def reset_market_data_provider_breaker(
    provider_name: str,
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
) -> ProviderBreakerResetResponse:
    if provider_name not in MARKET_DATA_FALLBACK_PRIORITY:
        raise HTTPException(status_code=404, detail="Market-data provider not found")
    reset_provider_circuit_breaker(provider_name)
    return ProviderBreakerResetResponse(
        provider=provider_name,
        state="closed",
        consecutive_failures=0,
    )


def _require_market_data_two_factor(active_session: UserSession) -> None:
    if not active_session.user.two_factor_enabled or not active_session.two_factor_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "two_factor_required",
                "message": "Verify this session with app-based two-factor authentication before connecting Angel One.",
            },
        )


@router.post("/settings/angel-one/connect", response_model=AngelOneConnectResponse)
def connect_angel_one(
    request: AngelOneConnectRequest,
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AngelOneConnectResponse:
    _require_market_data_two_factor(active_session)
    reset_provider_circuit_breaker("angel_one")
    api_key = settings.angel_one_api_key.get_secret_value() if settings.angel_one_api_key else ""
    if not api_key.strip() or not settings.angel_one_client_public_ip.strip():
        raise HTTPException(
            status_code=503,
            detail="Angel One API key and registered client public IP must be configured on the backend.",
        )
    try:
        tokens, expires_at = AngelOneProvider.login(
            api_key,
            request.client_code.strip(),
            request.pin,
            request.totp,
        )
    except MarketDataProviderError as error:
        _log_market_data_failure(
            "Angel One login failed (provider=%s code=%s upstream_status=%s)",
            error,
            error.code,
            "angel_one",
            error.code,
            error.status_code,
        )
        if error.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="Angel One login was rate limited. Wait before retrying.",
            ) from None
        if error.code == "provider_authentication_failed":
            raise HTTPException(
                status_code=401,
                detail="Angel One rejected the client code, PIN, or current authenticator code.",
            ) from None
        if error.code == "provider_timeout":
            raise HTTPException(
                status_code=504,
                detail="Timed out connecting to Angel One SmartAPI. Check provider availability and retry.",
            ) from None
        if error.code == "provider_network_error":
            raise HTTPException(
                status_code=502,
                detail="Could not reach Angel One SmartAPI. Check outbound network access and retry.",
            ) from None
        if error.code == "provider_subscription_required":
            raise HTTPException(
                status_code=403,
                detail="Angel One reports this SmartAPI app or account lacks access. Check its API permissions.",
            ) from None
        if error.code == "invalid_provider_response":
            raise HTTPException(
                status_code=502,
                detail="Angel One SmartAPI returned an invalid login response. Retry or contact provider support.",
            ) from None
        if error.code == "provider_server_error":
            status = (
                f" (HTTP {error.status_code})"
                if error.status_code is not None
                else ""
            )
            raise HTTPException(
                status_code=502,
                detail=f"Angel One SmartAPI returned an upstream server error{status}. Retry later.",
            ) from None
        if error.status_code is not None and 400 <= error.status_code < 500:
            raise HTTPException(
                status_code=502,
                detail=f"Angel One SmartAPI rejected the login request (HTTP {error.status_code}). Check app configuration and retry.",
            ) from None
        raise HTTPException(
            status_code=502,
            detail="Angel One authentication could not be completed because SmartAPI rejected or could not process the login request. Retry after checking provider status.",
        ) from None
    preference = _user_market_data_settings(db, active_session.user_id)
    now = datetime.now(timezone.utc)
    if preference is None:
        preference = UserMarketDataSettings(
            user_id=active_session.user_id,
            selected_provider=None,
            disabled_providers=[],
            updated_at=now,
        )
        db.add(preference)
    preference.encrypted_angel_tokens = _encrypt_market_tokens(tokens)
    preference.angel_expires_at = expires_at
    preference.angel_connected_at = now
    preference.updated_at = now
    db.commit()
    reset_provider_circuit_breaker("angel_one")
    return AngelOneConnectResponse(
        connected=True,
        expires_at=expires_at,
        message="Angel One market-data session connected. Credentials and session tokens remain backend-only.",
    )


@router.post("/settings/angel-one/disconnect", response_model=MarketDataSettingsResponse)
def disconnect_angel_one(
    active_session: UserSession = Depends(require_verified_market_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MarketDataSettingsResponse:
    preference = _user_market_data_settings(db, active_session.user_id)
    reset_provider_circuit_breaker("angel_one")
    if preference is not None:
        preference.encrypted_angel_tokens = None
        preference.angel_expires_at = None
        preference.angel_connected_at = None
        if preference.selected_provider == "angel_one":
            preference.selected_provider = None
        preference.updated_at = datetime.now(timezone.utc)
        db.commit()
    return MarketDataSettingsResponse(
        selected_provider=(
            preference.selected_provider
            if preference is not None and preference.selected_provider
            else _default_market_data_source(db, active_session.user_id)
        ),
        sources=_available_market_data_sources(db, active_session.user_id),
    )


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
    return _instrument_response(
        _get_instrument(db, instrument_id), include_tick_size=True
    )


def _growth(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous in (None, Decimal("0")):
        return None
    return (current - previous) / abs(previous) * Decimal("100")


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator in (None, Decimal("0")):
        return None
    return (numerator / denominator).quantize(Decimal("0.00000001"))


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
        equity_ratio = _ratio(row.net_income, row.total_equity)
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
            return_on_equity_pct=equity_ratio * Decimal("100") if equity_ratio is not None else None,
            debt_to_equity=_ratio(row.total_debt, row.total_equity),
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


@router.get(
    "/instruments/{instrument_id}/company-profile",
    response_model=CompanyProfileResponse,
)
def get_instrument_company_profile(
    instrument_id: UUID,
    _session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
) -> CompanyProfileResponse:
    instrument = _get_instrument(db, instrument_id)
    dhan_mapping = next(
        (
            item
            for item in instrument.provider_identifiers
            if item.provider == "dhan" and item.is_verified
        ),
        None,
    )
    master_details = None
    if dhan_mapping is not None:
        try:
            master_details = DhanAdapter.get_instrument_master_details(
                dhan_mapping.provider_instrument_id,
                instrument.venue,
                instrument.symbol,
            )
        except BrokerAdapterError as error:
            logger.info(
                "Dhan instrument master unavailable instrument_id=%s error=%s",
                instrument.id,
                str(error),
            )
    instrument_master = InstrumentMasterInfo(
        **(
            master_details
            if master_details is not None
            else {
                "isin": None,
                "exchange": instrument.venue,
                "segment": None,
                "instrument_type": instrument.asset_class.upper(),
                "lot_size": None,
                "tick_size": None,
                "listing_date": None,
                "expiry": None,
            }
        ),
        source=(
            "Dhan instrument master"
            if master_details is not None
            else "Instrument record (Dhan master unavailable)"
        ),
    )
    if instrument.is_synthetic:
        return CompanyProfileResponse(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            status="unavailable",
            name=None,
            sector=None,
            industry=None,
            description=None,
            market_cap=None,
            currency=None,
            source="Yahoo Finance",
            fetched_at=None,
            message="Yahoo Finance company profile is unavailable for synthetic instruments.",
            instrument_master=instrument_master,
        )
    mapping = next(
        (
            item
            for item in instrument.provider_identifiers
            if item.provider == "yfinance"
        ),
        None,
    )
    provider_symbol = (
        mapping.provider_symbol
        if mapping is not None
        else canonical_provider_symbol(
            instrument.symbol, instrument.venue, instrument.country
        )
    )
    try:
        profile = fundamentals_service.fetch_yfinance_company_profile(provider_symbol)
    except MarketDataProviderError as error:
        rate_limited = error.code in {
            "provider_rate_limit_exceeded",
            "rate_limited",
            "yfinance_cooldown_active",
        }
        return CompanyProfileResponse(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            status="unavailable",
            name=None,
            sector=None,
            industry=None,
            description=None,
            market_cap=None,
            currency=None,
            source="Yahoo Finance",
            fetched_at=None,
            message=(
                "Yahoo Finance profile unavailable - rate limited."
                if rate_limited
                else "Yahoo Finance did not provide a company profile for this instrument."
            ),
            instrument_master=instrument_master,
        )
    except (TimeoutError, ValueError):
        return CompanyProfileResponse(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            status="unavailable",
            name=None,
            sector=None,
            industry=None,
            description=None,
            market_cap=None,
            currency=None,
            source="Yahoo Finance",
            fetched_at=None,
            message="Yahoo Finance did not provide a company profile for this instrument.",
            instrument_master=instrument_master,
        )
    has_profile = any(
        profile.get(field) is not None
        for field in ("name", "sector", "industry", "description", "market_cap")
    )
    return CompanyProfileResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        status="available" if has_profile else "unavailable",
        name=profile["name"],
        sector=profile["sector"],
        industry=profile["industry"],
        description=profile["description"],
        market_cap=profile["market_cap"],
        currency=profile["currency"] or instrument.currency,
        source="Yahoo Finance",
        fetched_at=datetime.now(timezone.utc) if has_profile else None,
        message=(
            "Yahoo Finance company profile returned."
            if has_profile
            else "Yahoo Finance returned no company-profile fields for this instrument."
        ),
        instrument_master=instrument_master,
    )


@router.get(
    "/instruments/{instrument_id}/option-chain/expiries",
    response_model=OptionExpiryResponse,
)
def get_instrument_option_expiries(
    instrument_id: UUID,
    active_session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
) -> OptionExpiryResponse:
    instrument = _get_instrument(db, instrument_id)
    adapter, tokens, underlying_scrip, underlying_segment = (
        _dhan_option_chain_context(db, active_session.user_id, instrument)
    )
    try:
        raw_expiries = adapter.get_option_expiries(
            tokens["access_token"],
            tokens,
            underlying_scrip,
            underlying_segment,
        )
    except BrokerReconnectRequired as error:
        raise HTTPException(
            status_code=401,
            detail="Dhan session expired. Reconnect to restore option-chain access.",
        ) from error
    except BrokerRateLimited as error:
        raise HTTPException(
            status_code=429,
            detail="Dhan option-chain rate limit reached; retry after 3 seconds.",
        ) from error
    except BrokerAdapterError as error:
        raise HTTPException(
            status_code=502,
            detail="Dhan could not provide the option expiry list.",
        ) from error
    try:
        expiries = sorted({date.fromisoformat(value) for value in raw_expiries})
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail="Dhan option expiry list contains an invalid date.",
        ) from error
    return OptionExpiryResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        expiries=expiries,
        fetched_at=datetime.now(timezone.utc),
    )


@router.get(
    "/instruments/{instrument_id}/option-chain",
    response_model=OptionChainResponse,
)
def get_instrument_option_chain(
    instrument_id: UUID,
    expiry: date = Query(...),
    active_session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
) -> OptionChainResponse:
    instrument = _get_instrument(db, instrument_id)
    adapter, tokens, underlying_scrip, underlying_segment = (
        _dhan_option_chain_context(db, active_session.user_id, instrument)
    )
    try:
        payload = adapter.get_option_chain(
            tokens["access_token"],
            tokens,
            underlying_scrip,
            underlying_segment,
            expiry.isoformat(),
        )
    except BrokerReconnectRequired as error:
        raise HTTPException(
            status_code=401,
            detail="Dhan session expired. Reconnect to restore option-chain access.",
        ) from error
    except BrokerRateLimited as error:
        raise HTTPException(
            status_code=429,
            detail="Dhan option-chain rate limit reached; retry after 3 seconds.",
        ) from error
    except BrokerAdapterError as error:
        raise HTTPException(
            status_code=502,
            detail="Dhan could not provide the option chain.",
        ) from error

    raw_strikes = payload.get("oc")
    if not isinstance(raw_strikes, dict):
        raise HTTPException(
            status_code=502,
            detail="Dhan option-chain response has no strike data.",
        )
    strikes = []
    for raw_strike, raw_contracts in raw_strikes.items():
        if not isinstance(raw_contracts, dict):
            raise HTTPException(
                status_code=502,
                detail="Dhan option-chain response has an invalid strike.",
            )
        strike = _option_decimal(raw_strike, "strike")
        if strike is None:
            raise HTTPException(
                status_code=502,
                detail="Dhan option-chain response has an invalid strike.",
            )
        call = raw_contracts.get("ce")
        put = raw_contracts.get("pe")
        if call is not None and not isinstance(call, dict):
            raise HTTPException(
                status_code=502,
                detail="Dhan option-chain response has an invalid call contract.",
            )
        if put is not None and not isinstance(put, dict):
            raise HTTPException(
                status_code=502,
                detail="Dhan option-chain response has an invalid put contract.",
            )
        strikes.append(OptionStrikeResponse(
            strike=strike,
            ce=_option_contract(call) if isinstance(call, dict) else None,
            pe=_option_contract(put) if isinstance(put, dict) else None,
        ))
    strikes.sort(key=lambda item: item.strike)
    return OptionChainResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        expiry=expiry,
        underlying_price=_option_decimal(payload.get("last_price"), "underlying price"),
        strikes=strikes,
        fetched_at=datetime.now(timezone.utc),
    )


@router.get(
    "/instruments/{instrument_id}/option-chain/signal",
    response_model=OptionsSignalAPIResponse,
)
def get_instrument_option_signal(
    instrument_id: UUID,
    expiry: date = Query(...),
    position_option_type: Literal["call", "put"] | None = None,
    position_strike: Decimal | None = Query(default=None, gt=0),
    position_entry_premium: Decimal | None = Query(default=None, gt=0),
    capital: Decimal | None = Query(default=None, gt=0),
    risk_fraction: Decimal = Query(default=Decimal("0.005"), gt=0, le=Decimal("0.02")),
    active_session: UserSession = Depends(require_verified_market_session),
    db: Session = Depends(get_db),
) -> OptionsSignalAPIResponse:
    """Evaluate an options setup from Dhan chain data and stored real Dhan 5m bars.

    This endpoint is advisory-only. It never submits or simulates orders. Both
    the option chain and intraday bars are sourced from Dhan to avoid mixing
    candle histories from different providers. Retrieval time is not treated as
    a source market timestamp, so unverifiable quote freshness blocks action.
    """
    provided_position_fields = (
        position_option_type is not None,
        position_strike is not None,
        position_entry_premium is not None,
    )
    if any(provided_position_fields) and not all(provided_position_fields):
        raise HTTPException(
            status_code=422,
            detail="An open position requires option type, strike, and entry premium together.",
        )

    instrument = _get_instrument(db, instrument_id)
    market_zone = ZoneInfo("Asia/Kolkata")
    now = datetime.now(timezone.utc)
    session_date = now.astimezone(market_zone).date()
    expiry_at = datetime.combine(
        expiry,
        datetime_time(hour=15, minute=30),
        tzinfo=market_zone,
    )

    def load_current_session_rows() -> list[MarketBar]:
        rows = list(db.scalars(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == "5m",
                MarketBar.source == "dhan",
                MarketBar.is_synthetic.is_(False),
            )
            .order_by(MarketBar.timestamp.desc())
            .limit(100)
        ))
        rows.reverse()
        return [
            bar for bar in rows
            if _utc(bar.timestamp).astimezone(market_zone).date() == session_date
        ]

    current_session_rows = load_current_session_rows()

    bars = [
        SignalOHLCVBar(
            timestamp=_utc(bar.timestamp),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            is_synthetic=bar.is_synthetic,
        )
        for bar in current_session_rows
    ]

    chain_response = get_instrument_option_chain(
        instrument_id=instrument_id,
        expiry=expiry,
        active_session=active_session,
        db=db,
    )
    now = datetime.now(timezone.utc)
    market_session_open = _market_session_state(instrument, now) == "open"
    chain_quotes: list[SignalOptionQuote] = []
    for strike_row in chain_response.strikes:
        for option_type, contract in (("call", strike_row.ce), ("put", strike_row.pe)):
            if contract is None:
                continue
            chain_quotes.append(SignalOptionQuote(
                strike=strike_row.strike,
                option_type=option_type,
                expiry_at=expiry_at,
                bid=contract.top_bid_price or Decimal("0"),
                ask=contract.top_ask_price or Decimal("0"),
                bid_qty=Decimal(contract.top_bid_quantity or 0),
                ask_qty=Decimal(contract.top_ask_quantity or 0),
                ltp=contract.last_price or Decimal("0"),
                volume=Decimal(contract.volume or 0),
                open_interest=Decimal(contract.oi or 0),
                delta=contract.greeks.delta,
                gamma=contract.greeks.gamma,
                theta=contract.greeks.theta,
                vega=contract.greeks.vega,
                implied_volatility=contract.implied_volatility,
                lot_size=None,
            ))

    spot = chain_response.underlying_price or Decimal("0")
    active_position = None
    if all(provided_position_fields):
        active_position = ActiveOptionPosition(
            option_type=position_option_type,
            strike=position_strike,
            expiry_at=expiry_at,
            entry_premium=position_entry_premium,
        )

    signal = evaluate_options_signal(OptionsSignalInput(
        underlying_symbol=instrument.symbol,
        chain_underlying_symbol=chain_response.symbol,
        as_of=now,
        spot=spot,
        spot_timestamp=None,
        spot_source="dhan_option_chain_underlying",
        chain_timestamp=None,
        chain_source=chain_response.source,
        chain_spot=spot,
        expiry_at=expiry_at,
        bars=bars,
        chain=chain_quotes,
        exchange_timezone="Asia/Kolkata",
        capital=capital,
        risk_fraction=risk_fraction,
        active_position=active_position,
        market_session_open=market_session_open,
    ))
    return OptionsSignalAPIResponse(
        action=signal.action,
        underlying_symbol=signal.underlying_symbol,
        as_of=signal.as_of,
        direction_score=signal.direction_score,
        opposing_score=signal.opposing_score,
        selected_option_type=signal.selected_option_type,
        strike=signal.strike,
        reference_entry_premium=signal.reference_entry_premium,
        estimated_premium_risk_per_lot=signal.estimated_premium_risk_per_lot,
        suggested_lots_by_risk_budget=signal.suggested_lots_by_risk_budget,
        option_return_pct_at_bid=signal.option_return_pct_at_bid,
        reasons=list(signal.reasons),
        blockers=list(signal.blockers),
        evidence=signal.evidence,
        underlying_source=signal.underlying_source,
        chain_source=signal.chain_source,
        bar_source="dhan",
        chain_retrieved_at=_utc(chain_response.fetched_at),
        last_bar_timestamp=_utc(bars[-1].timestamp) if bars else None,
    )


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
    instrument_id = instrument.id
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
    except (MarketDataProviderError, TimeoutError, ValueError) as error:
        db.rollback()
        message = (
            "No Yahoo Finance symbol mapping is available for this instrument."
            if isinstance(error, MarketDataProviderError)
            and error.code == "provider_symbol_unavailable"
            else "The Yahoo Finance symbol mapping conflicts with another instrument."
            if isinstance(error, MarketDataProviderError)
            and error.code == "provider_mapping_conflict"
            else "The provider could not supply fundamentals for this instrument. "
            "No synthetic fallback is used and no values were estimated."
        )
        return FundamentalsSyncResponse(
            instrument_id=instrument_id,
            status="unavailable",
            source=None,
            fetched_at=None,
            fundamentals_count=0,
            message=message,
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
            instrument_id=instrument_id,
            status="unavailable",
            source=snapshot.source,
            fetched_at=_utc(snapshot.fetched_at),
            fundamentals_count=len(rows),
            message="The provider returned no non-null fundamentals. No values were estimated.",
        )
    return FundamentalsSyncResponse(
        instrument_id=instrument_id,
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
        instrument_id=instrument_id,
        symbol=instrument.symbol,
        currency=instrument.currency,
        snapshot=snapshot_response,
        fundamentals=rows,
        unavailable=unavailable,
        message=(
            "No non-null fundamentals are stored for this instrument; request a provider sync."
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
    source: str | None = Query(default=None, min_length=1, max_length=32),
    _session: UserSession = Depends(require_verified_market_session),
    provider: MarketDataProvider = Depends(get_market_data_provider),
    db: Session = Depends(get_db),
) -> IndicatorsResponse:
    instrument = _get_instrument(db, instrument_id)
    interval = _canonical_interval(interval)
    active_providers = _providers(provider)
    selected_source = source or (
        active_providers[0].name if active_providers else None
    )
    latest_query = select(MarketBar).where(
        MarketBar.instrument_id == instrument.id,
        MarketBar.interval == interval,
    )
    if selected_source is not None:
        latest_query = latest_query.where(MarketBar.source == selected_source)
    else:
        latest_query = latest_query.where(MarketBar.source.is_(None))
    latest_bar = db.scalar(
        latest_query.order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc()).limit(1)
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
            )
        )
        bars.reverse()
    calculated = compute_indicators(bars)
    indicators = {
        name: _indicator_result(value)
        for name, value in calculated.__dict__.items()
    }
    indicator_series = _indicator_series(bars, instrument.timezone)
    return IndicatorsResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        currency=instrument.currency,
        interval=interval,
        freshness=_freshness(db, instrument.id, interval, source=selected_source),
        source=latest_bar.source if latest_bar else None,
        latest_bar=_utc(latest_bar.timestamp) if latest_bar else None,
        bars_available=len(bars),
        indicators=indicators,
        indicator_series=indicator_series,
    )


@router.get("/watchlist", response_model=list[WatchlistEntryResponse])
def get_watchlist(
    watchlist_id: UUID | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = (
        select(WatchlistInstrument)
        .where(WatchlistInstrument.user_id == user.id)
        .options(
            selectinload(WatchlistInstrument.instrument).selectinload(
                Instrument.provider_identifiers
            )
        )
        .order_by(WatchlistInstrument.created_at, WatchlistInstrument.id)
    )
    selected_watchlist = _get_owned_watchlist(
        db, user.id, watchlist_id
    ) if watchlist_id is not None else _get_default_watchlist(db, user.id)
    statement = statement.where(
        WatchlistInstrument.watchlist_id == selected_watchlist.id
    )
    entries = db.scalars(statement).all()
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
        daily_bars = db.scalars(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == "1d",
                MarketBar.is_synthetic.is_(instrument.is_synthetic),
            )
            .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
            .limit(10)
        ).all()
        distinct_bars: list[MarketBar] = []
        seen_timestamps: set[datetime] = set()
        for bar in daily_bars:
            if bar.timestamp in seen_timestamps:
                continue
            seen_timestamps.add(bar.timestamp)
            distinct_bars.append(bar)
            if len(distinct_bars) == 2:
                break
        last_price = quote.close if quote is not None else (
            distinct_bars[0].close if distinct_bars else None
        )
        previous_close = distinct_bars[1].close if len(distinct_bars) > 1 else None
        absolute_change = (
            last_price - previous_close
            if last_price is not None and previous_close is not None
            else None
        )
        percent_change = (
            absolute_change / previous_close * Decimal("100")
            if absolute_change is not None and previous_close not in (None, Decimal("0"))
            else None
        )
        quote_response = None
        if quote is not None:
            retrieved_at = _utc(quote.retrieved_at)
            quote_timestamp = _utc(quote.timestamp)
            _usable, quote_reason, quote_evidence = classify_quote(
                quote, instrument, None, now
            )
            freshness_status = quote_evidence["quote_freshness_status"]
            quote_response = WatchlistQuoteResponse(
                source=quote.source,
                retrieved_at=retrieved_at,
                freshness=(
                    "fresh" if quote_reason is None else "stale"
                ),
                freshness_status=freshness_status,
                is_synthetic=quote.is_synthetic,
                is_verified=any(
                    mapping.provider == (
                        "dhan"
                        if quote.source == "dhan_ws"
                        else "angel_one"
                        if quote.source == "angel_one_ws"
                        else quote.source
                    ) and mapping.is_verified
                    for mapping in instrument.provider_identifiers
                ),
                market_timestamp=quote_timestamp,
                market_age_seconds=max(0.0, quote_evidence["quote_timestamp_age_seconds"]),
                data_type=quote_evidence["quote_data_type"],
                close=quote.close,
                absolute_change=absolute_change,
                percent_change=percent_change,
            )
        elif distinct_bars:
            latest_bar = distinct_bars[0]
            freshness = _freshness(
                db,
                instrument.id,
                "1d",
                source=latest_bar.source,
                now=now,
            )
            quote_timestamp = _utc(latest_bar.timestamp)
            quote_response = WatchlistQuoteResponse(
                source=latest_bar.source,
                retrieved_at=_utc(latest_bar.retrieved_at),
                freshness="fresh" if freshness.status == "fresh" else "stale",
                freshness_status=(
                    freshness.status
                    if freshness.status != "never_fetched"
                    else "unavailable"
                ),
                is_synthetic=latest_bar.is_synthetic,
                is_verified=any(
                    mapping.provider == latest_bar.source and mapping.is_verified
                    for mapping in instrument.provider_identifiers
                ),
                market_timestamp=quote_timestamp,
                market_age_seconds=max(
                    0.0, (now - quote_timestamp).total_seconds()
                ),
                data_type="daily_close",
                close=latest_bar.close,
                absolute_change=absolute_change,
                percent_change=percent_change,
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
            "last_sync_provider": sync.source if sync else None,
            "last_sync_reason": sync.safe_reason or sync.safe_error_code if sync else None,
            "freshness_status": freshness_status,
        })
    return {"instruments": result}


def _get_default_watchlist(db: Session, user_id: UUID) -> Watchlist:
    watchlist = db.scalar(
        select(Watchlist)
        .where(Watchlist.user_id == user_id)
        .order_by(Watchlist.created_at, Watchlist.id)
        .limit(1)
    )
    if watchlist is None:
        watchlist = Watchlist(
            user_id=user_id,
            name="Default",
            created_at=datetime.now(timezone.utc),
        )
        db.add(watchlist)
        db.commit()
        db.refresh(watchlist)
    return watchlist


def _get_owned_watchlist(db: Session, user_id: UUID, watchlist_id: UUID) -> Watchlist:
    watchlist = db.scalar(
        select(Watchlist).where(
            Watchlist.id == watchlist_id,
            Watchlist.user_id == user_id,
        )
    )
    if watchlist is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return watchlist


@router.get("/watchlists", response_model=list[WatchlistResponse])
def list_watchlists(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_default_watchlist(db, user.id)
    watchlists = db.scalars(
        select(Watchlist)
        .where(Watchlist.user_id == user.id)
        .options(selectinload(Watchlist.entries))
        .order_by(Watchlist.created_at, Watchlist.id)
    ).all()
    return [
        WatchlistResponse(
            id=watchlist.id,
            name=watchlist.name,
            instrument_count=len(watchlist.entries),
            created_at=watchlist.created_at,
        )
        for watchlist in watchlists
    ]


@router.post("/watchlists", response_model=WatchlistResponse, status_code=201)
def create_watchlist(
    payload: WatchlistRequest,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Watchlist name cannot be blank")
    existing = db.scalar(
        select(Watchlist.id).where(
            Watchlist.user_id == user.id,
            func.lower(Watchlist.name) == name.casefold(),
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A watchlist with that name already exists")
    watchlist = Watchlist(
        user_id=user.id,
        name=name,
        created_at=datetime.now(timezone.utc),
    )
    db.add(watchlist)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="A watchlist with that name already exists"
        ) from error
    db.refresh(watchlist)
    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        instrument_count=0,
        created_at=watchlist.created_at,
    )


@router.patch("/watchlists/{watchlist_id}", response_model=WatchlistResponse)
def rename_watchlist(
    watchlist_id: UUID,
    payload: WatchlistRequest,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    watchlist = _get_owned_watchlist(db, user.id, watchlist_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Watchlist name cannot be blank")
    existing = db.scalar(
        select(Watchlist.id).where(
            Watchlist.user_id == user.id,
            Watchlist.id != watchlist.id,
            func.lower(Watchlist.name) == name.casefold(),
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A watchlist with that name already exists")
    watchlist.name = name
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="A watchlist with that name already exists"
        ) from error
    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        instrument_count=len(watchlist.entries),
        created_at=watchlist.created_at,
    )


@router.delete("/watchlists/{watchlist_id}", status_code=204)
def delete_watchlist(
    watchlist_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    watchlist = _get_owned_watchlist(db, user.id, watchlist_id)
    db.delete(watchlist)
    db.commit()


@router.post(
    "/watchlists/{watchlist_id}/instruments/{instrument_id}", status_code=204
)
def add_instrument_to_watchlist(
    watchlist_id: UUID,
    instrument_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    watchlist = _get_owned_watchlist(db, user.id, watchlist_id)
    _get_instrument(db, instrument_id)
    existing = db.scalar(
        select(WatchlistInstrument.id).where(
            WatchlistInstrument.watchlist_id == watchlist.id,
            WatchlistInstrument.instrument_id == instrument_id,
        )
    )
    if existing is None:
        db.add(
            WatchlistInstrument(
                user_id=user.id,
                watchlist_id=watchlist.id,
                instrument_id=instrument_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    return None


@router.delete(
    "/watchlists/{watchlist_id}/instruments/{instrument_id}", status_code=204
)
def remove_instrument_from_watchlist(
    watchlist_id: UUID,
    instrument_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    watchlist = _get_owned_watchlist(db, user.id, watchlist_id)
    entry = db.scalar(
        select(WatchlistInstrument).where(
            WatchlistInstrument.watchlist_id == watchlist.id,
            WatchlistInstrument.instrument_id == instrument_id,
        )
    )
    if entry is not None:
        db.delete(entry)
        db.commit()
    return None


@router.post("/watchlist/{instrument_id}", status_code=204)
def add_watchlist_instrument(
    instrument_id: UUID,
    user: User = Depends(get_current_user),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    _get_instrument(db, instrument_id)
    watchlist = _get_default_watchlist(db, user.id)
    existing = db.scalar(
        select(WatchlistInstrument).where(
            WatchlistInstrument.watchlist_id == watchlist.id,
            WatchlistInstrument.instrument_id == instrument_id,
        )
    )
    if existing is None:
        db.add(
            WatchlistInstrument(
                user_id=user.id,
                watchlist_id=watchlist.id,
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
    watchlist = _get_default_watchlist(db, user.id)
    item = db.scalar(
        select(WatchlistInstrument).where(
            WatchlistInstrument.watchlist_id == watchlist.id,
            WatchlistInstrument.instrument_id == instrument_id,
        )
    )
    if item is not None:
        db.delete(item)
        db.commit()
    return None


def _dhan_snapshot_quote(
    db: Session,
    instrument: Instrument,
    user: User,
    *,
    refresh: bool,
    now: datetime,
) -> tuple[QuoteResponse | None, tuple[MarketQuote, bool] | None]:
    if instrument.currency.upper() != "INR" or instrument.is_synthetic:
        return None, None
    live_cache = db.scalar(
        select(MarketQuote).where(
            MarketQuote.instrument_id == instrument.id,
            MarketQuote.source == "dhan_ws",
        )
    )
    live_mapping = next(
        (
            item for item in instrument.provider_identifiers
            if item.provider == "dhan" and item.is_verified
        ),
        None,
    )
    stale_live_quote = (
        (live_cache, live_mapping is not None)
        if live_cache is not None and not live_cache.is_synthetic
        else None
    )
    if live_cache is not None and live_mapping is not None and not refresh:
        cache_age = (now - _utc(live_cache.retrieved_at)).total_seconds()
        price_age = (now - _utc(live_cache.timestamp)).total_seconds()
        if (
            0 <= cache_age <= settings.market_data_quote_freshness_seconds
            and 0 <= price_age <= settings.market_data_quote_freshness_seconds
        ):
            response = _quote_response(
                instrument,
                live_cache,
                freshness="fresh",
                freshness_message=(
                    "Most recent Dhan WebSocket tick is cached; persistent feed status "
                    "is only available while the stream is connected."
                ),
                is_cached=True,
                is_verified=True,
                provider_request_status="not_attempted",
            )
            return (
                response.model_copy(update={
                    "delivery_mode": "websocket_tick",
                    "stream_status": "stale",
                    "provider_connection_status": "disconnected",
                    "feed_active": False,
                }),
                stale_live_quote,
            )
    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == user.id,
            BrokerCredential.broker == "dhan",
        )
    )
    adapter = DhanAdapter()
    if credential is None:
        return None, stale_live_quote
    cached_quote = db.scalar(
        select(MarketQuote).where(
            MarketQuote.instrument_id == instrument.id,
            MarketQuote.source == adapter.slug,
        )
    )
    stale_quote = stale_live_quote or (
        (cached_quote, False)
        if cached_quote is not None and not cached_quote.is_synthetic
        else None
    )
    try:
        stored_mapping = next(
            (
                item
                for item in instrument.provider_identifiers
                if item.provider == adapter.slug and item.is_verified
            ),
            None,
        )
        stored_exchange_segment = (
            "NSE_EQ"
            if instrument.venue.upper() in {"NSE", "XNSE", "NSI", "NSE_EQ"}
            else "BSE_EQ"
            if instrument.venue.upper() in {"BSE", "XBOM", "BOM", "BSE_EQ"}
            else None
        )
        if (
            stored_mapping is not None
            and stored_mapping.provider_instrument_id.isdigit()
            and stored_exchange_segment is not None
        ):
            security_id, exchange_segment = (
                stored_mapping.provider_instrument_id,
                stored_exchange_segment,
            )
        else:
            security_id, exchange_segment = adapter.find_verified_equity_mapping(
                instrument.symbol, instrument.venue
            ) or (None, None)
        if security_id is None or exchange_segment is None:
            return None, stale_quote
        mapping_owner = db.scalar(
            select(InstrumentProviderIdentifier.instrument_id).where(
                InstrumentProviderIdentifier.provider == adapter.slug,
                InstrumentProviderIdentifier.provider_instrument_id == security_id,
                InstrumentProviderIdentifier.instrument_id != instrument.id,
            )
        )
        if mapping_owner is not None:
            return None, stale_quote
        mapping = next(
            (
                item
                for item in instrument.provider_identifiers
                if item.provider == adapter.slug
            ),
            None,
        )
        if mapping is not None and mapping.provider_instrument_id != security_id:
            return None, stale_quote
        if mapping is None:
            mapping = InstrumentProviderIdentifier(
                instrument_id=instrument.id,
                provider=adapter.slug,
                provider_instrument_id=security_id,
                provider_symbol=instrument.symbol,
                is_verified=True,
                last_seen_at=now,
            )
            instrument.provider_identifiers.append(mapping)
            db.add(mapping)
        else:
            mapping.is_verified = True
            mapping.last_seen_at = now
        if cached_quote is not None and not cached_quote.is_synthetic:
            cache_age = (now - _utc(cached_quote.retrieved_at)).total_seconds()
            price_age = (now - _utc(cached_quote.timestamp)).total_seconds()
            if (
                not refresh
                and 0 <= cache_age <= settings.market_data_quote_freshness_seconds
                and 0 <= price_age <= settings.market_data_quote_freshness_seconds
            ):
                db.commit()
                return (
                    _quote_response(
                        instrument,
                        cached_quote,
                        freshness="fresh",
                        freshness_message="Cached Dhan REST market snapshot is within the configured freshness window.",
                        is_cached=True,
                        is_verified=True,
                        provider_request_status="not_attempted",
                    ),
                    stale_quote,
                )

        from app.api.brokers import _fresh_tokens

        tokens = _fresh_tokens(db, adapter, credential)
        bar = adapter.get_market_quote(
            tokens["access_token"], tokens, exchange_segment, security_id
        )
        retrieved_at = datetime.now(timezone.utc)
        if cached_quote is None:
            cached_quote = MarketQuote(
                instrument_id=instrument.id,
                source=adapter.slug,
            )
            db.add(cached_quote)
        cached_quote.timestamp = bar.timestamp
        cached_quote.open = bar.open
        cached_quote.high = bar.high
        cached_quote.low = bar.low
        cached_quote.close = bar.close
        cached_quote.volume = bar.volume
        cached_quote.retrieved_at = retrieved_at
        cached_quote.is_synthetic = False
        db.commit()
        db.refresh(cached_quote)
        is_stale = (
            retrieved_at - _utc(bar.timestamp)
        ).total_seconds() > settings.market_data_quote_freshness_seconds
        return (
            _quote_response(
                instrument,
                cached_quote,
                freshness="stale" if is_stale else "fresh",
                freshness_message=(
                    "Dhan returned a market snapshot older than the configured freshness window."
                    if is_stale
                    else "Quote retrieved from Dhan REST market quote; this is a snapshot, not a WebSocket tick."
                ),
                is_cached=False,
                is_verified=True,
                provider_request_status="succeeded",
            ),
            stale_quote,
        )
    except BrokerReconnectRequired:
        credential.expires_at = now
        db.commit()
        return None, stale_quote
    except HTTPException:
        db.rollback()
        return None, stale_quote
    except (BrokerAdapterError, KeyError, ValueError, TimeoutError):
        db.rollback()
        return None, stale_quote


async def _stream_angel_one_quote(
    websocket: WebSocket,
    instrument_id: UUID,
    active_session: UserSession,
    db: Session,
) -> None:
    instrument = db.scalar(
        select(Instrument)
        .where(Instrument.id == instrument_id)
        .options(selectinload(Instrument.provider_identifiers))
    )
    if (
        instrument is None
        or instrument.currency.upper() != "INR"
        or instrument.asset_class != "equity"
        or instrument.is_synthetic
    ):
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4404)
        return
    try:
        provider, _, _ = _market_data_credentials(
            db, active_session.user_id, "angel_one"
        )
        if provider is None:
            raise MarketDataProviderError("provider_unavailable")
        candidate, mapping = _resolve_provider_candidate(db, instrument, provider)
        if not mapping.is_verified:
            raise MarketDataProviderError("unverified_instrument_mapping")
        db.commit()
    except HTTPException as error:
        db.rollback()
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4401 if error.status_code == 401 else 4404)
        return
    except (MarketDataProviderError, TimeoutError, ValueError):
        db.rollback()
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4404)
        return

    async def send_status(
        status: str,
        message: str | None = None,
        last_snapshot_at: datetime | None = None,
    ) -> None:
        await websocket.send_json({
            "type": "status",
            "status": status,
            "message": message,
            "last_snapshot_at": (
                _utc(last_snapshot_at).isoformat()
                if last_snapshot_at is not None
                else None
            ),
        })

    if _market_session_state(instrument) != "open":
        last_snapshot_at = db.scalar(
            select(func.max(MarketQuote.retrieved_at)).where(
                MarketQuote.instrument_id == instrument.id
            )
        )
        await send_status(
            "market_closed",
            "Market closed - live feed resumes at 09:15 IST",
            last_snapshot_at,
        )
        await websocket.close(code=1000, reason="Market closed")
        return

    stream_task = asyncio.current_task()

    async def monitor_client() -> None:
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            if stream_task is not None:
                stream_task.cancel()

    client_monitor = asyncio.create_task(monitor_client())
    retry_delay = 1
    has_connected = False
    try:
        while True:
            await send_status("connecting" if not has_connected else "reconnecting")
            try:
                async for bar in provider.stream_quote(candidate):
                    retrieved_at = datetime.now(timezone.utc)
                    quote = db.scalar(
                        select(MarketQuote).where(
                            MarketQuote.instrument_id == instrument.id,
                            MarketQuote.source == "angel_one_ws",
                        )
                    )
                    if quote is None:
                        quote = MarketQuote(
                            instrument_id=instrument.id,
                            source="angel_one_ws",
                        )
                        db.add(quote)
                    quote.timestamp = bar.timestamp
                    quote.open = bar.open
                    quote.high = bar.high
                    quote.low = bar.low
                    quote.close = bar.close
                    quote.volume = bar.volume
                    quote.retrieved_at = retrieved_at
                    quote.is_synthetic = False
                    db.commit()
                    db.refresh(quote)
                    response = _quote_response(
                        instrument,
                        quote,
                        freshness="fresh",
                        freshness_message="Live Angel One WebSocket tick received.",
                        is_cached=False,
                        is_verified=True,
                        provider_request_status="succeeded",
                    ).model_copy(update={
                        "delivery_mode": "websocket_tick",
                        "stream_status": "active",
                        "provider_connection_status": "connected",
                        "feed_active": True,
                    })
                    await websocket.send_json({
                        "type": "tick",
                        "quote": response.model_dump(mode="json"),
                    })
                    has_connected = True
                    retry_delay = 1
            except InvalidStatus as error:
                if error.response.status_code == 401:
                    preference = _user_market_data_settings(db, active_session.user_id)
                    if preference is not None:
                        preference.angel_expires_at = datetime.now(timezone.utc)
                        preference.updated_at = datetime.now(timezone.utc)
                        db.commit()
                    try:
                        provider, _, _ = _market_data_credentials(
                            db, active_session.user_id, "angel_one"
                        )
                    except HTTPException:
                        await send_status("unavailable")
                        await websocket.close(code=4401)
                        return
                    if provider is None:
                        await send_status("unavailable")
                        await websocket.close(code=4401)
                        return
                    candidate, mapping = _resolve_provider_candidate(db, instrument, provider)
                    if not mapping.is_verified:
                        await send_status("unavailable")
                        await websocket.close(code=4404)
                        return
                elif error.response.status_code == 403:
                    await send_status("unavailable")
                    await websocket.close(code=4403)
                    return
                else:
                    await send_status("reconnecting")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30)
            except MarketDataProviderError as error:
                if error.status_code == 401 or error.code == "provider_authentication_failed":
                    preference = _user_market_data_settings(db, active_session.user_id)
                    if preference is not None:
                        preference.angel_expires_at = datetime.now(timezone.utc)
                        preference.updated_at = datetime.now(timezone.utc)
                        db.commit()
                    try:
                        provider, _, _ = _market_data_credentials(
                            db, active_session.user_id, "angel_one"
                        )
                    except HTTPException:
                        await send_status("unavailable")
                        await websocket.close(code=4401)
                        return
                    if provider is None:
                        await send_status("unavailable")
                        await websocket.close(code=4401)
                        return
                else:
                    await send_status("unavailable")
                    await websocket.close(code=1011)
                    return
            except (OSError, TimeoutError, WebSocketException):
                await send_status("reconnecting")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
            else:
                await send_status("reconnecting")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    finally:
        client_monitor.cancel()
        try:
            await client_monitor
        except asyncio.CancelledError:
            pass


@router.websocket("/instruments/{instrument_id}/depth")
async def dhan_market_depth_stream(
    websocket: WebSocket,
    instrument_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    await websocket.accept()
    origin = websocket.headers.get("origin")
    parsed_origin = urlsplit(origin or "")
    allowed_origin = origin == settings.frontend_origin or (
        parsed_origin.scheme == "https"
        and parsed_origin.hostname is not None
        and parsed_origin.hostname.endswith(".app.github.dev")
    )
    if origin and not allowed_origin:
        await websocket.close(code=4403)
        return
    raw_token = websocket.cookies.get("session_token")
    active_session = (
        db.scalar(
            select(UserSession)
            .where(
                UserSession.token_hash == hash_token(raw_token),
                UserSession.revoked_at.is_(None),
            )
            .options(selectinload(UserSession.user))
        )
        if raw_token
        else None
    )
    expires_at = active_session.expires_at if active_session is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if active_session is None or expires_at <= datetime.now(timezone.utc):
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Sign in again to use market depth.",
        })
        await websocket.close(code=4401)
        return
    if (
        not active_session.user.email_verified
        or not active_session.user.two_factor_enabled
        or not active_session.two_factor_verified
    ):
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Verify this session before using broker market data.",
        })
        await websocket.close(code=4403)
        return
    if _default_market_data_source(db, active_session.user_id) != "dhan":
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Select Dhan as the market-data source to use market depth.",
        })
        await websocket.close(code=4404)
        return
    instrument = db.scalar(
        select(Instrument)
        .where(Instrument.id == instrument_id)
        .options(selectinload(Instrument.provider_identifiers))
    )
    if (
        instrument is None
        or instrument.country.upper() != "IN"
        or instrument.currency.upper() != "INR"
        or instrument.asset_class != "equity"
        or instrument.is_synthetic
    ):
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Dhan market depth is unavailable for this instrument.",
        })
        await websocket.close(code=4404)
        return
    last_snapshot_at = dhan_live_feed_hub.last_depth_snapshot_at(instrument.id)

    async def send_status(
        status: str,
        message: str | None = None,
    ) -> None:
        await websocket.send_json({
            "type": "status",
            "status": status,
            "message": message,
            "last_snapshot_at": (
                _utc(last_snapshot_at).isoformat()
                if last_snapshot_at is not None
                else None
            ),
        })

    if _market_session_state(instrument) != "open":
        await send_status(
            "market_closed",
            "Market closed - live feed resumes at 09:15 IST",
        )
        await websocket.close(code=1000, reason="Market closed")
        return

    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == active_session.user_id,
            BrokerCredential.broker == "dhan",
        )
    )
    if credential is None:
        await send_status("unavailable", "Connect Dhan to enable live market depth.")
        await websocket.close(code=4404)
        return
    adapter = DhanAdapter()
    mapping = next(
        (
            item
            for item in instrument.provider_identifiers
            if item.provider == adapter.slug and item.is_verified
        ),
        None,
    )
    exchange_segment = (
        "NSE_EQ"
        if instrument.venue.upper() in {"NSE", "XNSE", "NSI", "NSE_EQ"}
        else None
    )
    if mapping is None or not mapping.provider_instrument_id.isdigit() or exchange_segment is None:
        await send_status(
            "unavailable",
            "A verified Dhan instrument mapping is required for market depth.",
        )
        await websocket.close(code=4404)
        return
    try:
        from app.api.brokers import _fresh_tokens

        tokens = _fresh_tokens(db, adapter, credential)
    except HTTPException:
        await send_status("unavailable", "Reconnect Dhan to renew its market-data session.")
        await websocket.close(code=4401)
        return

    await send_status("connecting")
    group_id = str(uuid4())
    try:
        feed_queue = await dhan_live_feed_hub.join(
            "depth",
            group_id,
            {str(instrument.id): (exchange_segment, mapping.provider_instrument_id)},
            tokens["access_token"],
            tokens,
        )
    except BrokerAdapterError as error:
        await send_status("unavailable", str(error))
        await websocket.close(code=4403)
        return
    stream_task = asyncio.current_task()

    async def monitor_client() -> None:
        try:
            while True:
                message = await websocket.receive_text()
                if message == "unsubscribe":
                    if stream_task is not None:
                        stream_task.cancel()
                    return
        except WebSocketDisconnect:
            if stream_task is not None:
                stream_task.cancel()

    client_monitor = asyncio.create_task(monitor_client())
    try:
        while True:
            event = await feed_queue.get()
            if event["type"] == "status":
                await send_status(event["status"])
                continue
            if event["type"] == "error":
                await send_status("unavailable", event["message"])
                await websocket.close(
                    code=_dhan_feed_close_code(event.get("status_code")),
                    reason=event["message"][:120],
                )
                return
            if event["type"] != "depth":
                continue
            depth = event["depth"]
            last_snapshot_at = datetime.now(timezone.utc)
            await websocket.send_json({
                "type": "depth",
                "timestamp": depth["timestamp"].isoformat(),
                "last_snapshot_at": last_snapshot_at.isoformat(),
                "bids": [
                    {**level, "price": str(level["price"])}
                    for level in depth["bids"]
                ],
                "asks": [
                    {**level, "price": str(level["price"])}
                    for level in depth["asks"]
                ],
            })
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except BrokerReconnectRequired:
        credential.expires_at = datetime.now(timezone.utc)
        db.commit()
        await send_status("unavailable", "Reconnect Dhan to renew its market-data session.")
        await websocket.close(code=4401)
    except InvalidStatus as error:
        await send_status(
            "unavailable",
            f"Dhan rejected the market-depth handshake (HTTP {error.response.status_code}).",
        )
        await websocket.close(code=1011)
    except (BrokerAdapterError, OSError, TimeoutError, WebSocketException) as error:
        logger.warning(
            "Dhan depth stream unavailable instrument_id=%s error_class=%s",
            instrument.id,
            type(error).__name__,
        )
        await send_status(
            "unavailable",
            f"Dhan market depth disconnected ({type(error).__name__}).",
        )
        await websocket.close(code=1011)
    finally:
        client_monitor.cancel()
        try:
            await client_monitor
        except asyncio.CancelledError:
            pass
        await dhan_live_feed_hub.leave("depth", group_id)


@router.websocket("/instruments/{instrument_id}/stream")
async def dhan_quote_stream(
    websocket: WebSocket,
    instrument_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    await websocket.accept()
    origin = websocket.headers.get("origin")
    parsed_origin = urlsplit(origin or "")
    allowed_origin = origin == settings.frontend_origin or (
        parsed_origin.scheme == "https"
        and parsed_origin.hostname is not None
        and parsed_origin.hostname.endswith(".app.github.dev")
    )
    if origin and not allowed_origin:
        await websocket.close(code=4403)
        return
    raw_token = websocket.cookies.get("session_token")
    active_session = (
        db.scalar(
            select(UserSession)
            .where(
                UserSession.token_hash == hash_token(raw_token),
                UserSession.revoked_at.is_(None),
            )
            .options(selectinload(UserSession.user))
        )
        if raw_token
        else None
    )
    expires_at = active_session.expires_at if active_session is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if active_session is None or expires_at <= datetime.now(timezone.utc):
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4401)
        return
    if (
        not active_session.user.email_verified
        or not active_session.user.two_factor_enabled
        or not active_session.two_factor_verified
    ):
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4403)
        return

    selected_source = _default_market_data_source(db, active_session.user_id)
    if selected_source == "angel_one":
        await _stream_angel_one_quote(
            websocket, instrument_id, active_session, db
        )
        return
    if selected_source != "dhan":
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4404)
        return

    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == active_session.user_id,
            BrokerCredential.broker == "dhan",
        )
    )
    adapter = DhanAdapter()
    instrument = db.scalar(
        select(Instrument)
        .where(Instrument.id == instrument_id)
        .options(selectinload(Instrument.provider_identifiers))
    )
    if (
        credential is None
        or instrument is None
        or instrument.currency.upper() != "INR"
        or instrument.asset_class != "equity"
        or instrument.is_synthetic
    ):
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4404)
        return

    try:
        mapping_values = await asyncio.to_thread(
            adapter.find_verified_equity_mapping,
            instrument.symbol,
            instrument.venue,
        )
        if mapping_values is None:
            await websocket.send_json({"type": "status", "status": "unavailable"})
            await websocket.close(code=4404)
            return
        security_id, exchange_segment = mapping_values
        mapping_owner = db.scalar(
            select(InstrumentProviderIdentifier.instrument_id).where(
                InstrumentProviderIdentifier.provider == adapter.slug,
                InstrumentProviderIdentifier.provider_instrument_id == security_id,
                InstrumentProviderIdentifier.instrument_id != instrument.id,
            )
        )
        if mapping_owner is not None:
            await websocket.send_json({"type": "status", "status": "unavailable"})
            await websocket.close(code=4404)
            return
        mapping = next(
            (
                item
                for item in instrument.provider_identifiers
                if item.provider == adapter.slug
            ),
            None,
        )
        if mapping is not None and mapping.provider_instrument_id != security_id:
            await websocket.send_json({"type": "status", "status": "unavailable"})
            await websocket.close(code=4404)
            return
        if mapping is None:
            mapping = InstrumentProviderIdentifier(
                instrument_id=instrument.id,
                provider=adapter.slug,
                provider_instrument_id=security_id,
                provider_symbol=instrument.symbol,
                is_verified=True,
                last_seen_at=datetime.now(timezone.utc),
            )
            instrument.provider_identifiers.append(mapping)
            db.add(mapping)
        else:
            mapping.is_verified = True
            mapping.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        from app.api.brokers import _fresh_tokens

        tokens = _fresh_tokens(db, adapter, credential)
    except HTTPException:
        db.rollback()
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=4401)
        return
    except (BrokerAdapterError, KeyError, ValueError, TimeoutError):
        db.rollback()
        await websocket.send_json({"type": "status", "status": "unavailable"})
        await websocket.close(code=1011)
        return

    async def send_status(
        status: str,
        message: str | None = None,
        last_snapshot_at: datetime | None = None,
    ) -> None:
        await websocket.send_json({
            "type": "status",
            "status": status,
            "message": message,
            "last_snapshot_at": (
                _utc(last_snapshot_at).isoformat()
                if last_snapshot_at is not None
                else None
            ),
        })

    if _market_session_state(instrument) != "open":
        last_snapshot_at = db.scalar(
            select(func.max(MarketQuote.retrieved_at)).where(
                MarketQuote.instrument_id == instrument.id
            )
        )
        await send_status(
            "market_closed",
            "Market closed - live feed resumes at 09:15 IST",
            last_snapshot_at,
        )
        await websocket.close(code=1000, reason="Market closed")
        return

    group_id = str(uuid4())
    try:
        feed_queue = await dhan_live_feed_hub.join(
            "ticker",
            group_id,
            {str(instrument.id): (exchange_segment, security_id)},
            tokens["access_token"],
            tokens,
        )
    except BrokerAdapterError as error:
        await send_status("unavailable", str(error))
        await websocket.close(code=4403)
        return

    logger.info(
        "Dhan single-instrument subscription registered instrument_id=%s "
        "backend_websocket_route=/api/v1/market-data/instruments/%s/stream "
        "upstream_url=wss://api-feed.dhan.co?version=2&token=[REDACTED]"
        "&clientId=[REDACTED]&authType=2",
        instrument.id,
        instrument.id,
    )
    client_ticks_sent = 0
    stream_task = asyncio.current_task()

    async def monitor_client() -> None:
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            if stream_task is not None:
                stream_task.cancel()

    client_monitor = asyncio.create_task(monitor_client())

    async def log_client_counters() -> None:
        while True:
            await asyncio.sleep(30)
            logger.info(
                "Dhan single-instrument client delivery counter instrument_id=%s "
                "messages_sent=%d upstream_connections=%d subscriptions=%d",
                instrument.id,
                client_ticks_sent,
                dhan_live_feed_hub._connection_counts["ticker"],
                len(dhan_live_feed_hub._all_subscriptions("ticker")),
            )

    client_counter_task = asyncio.create_task(log_client_counters())
    try:
        while True:
            event = await feed_queue.get()
            if event["type"] == "status":
                await send_status(event["status"])
                continue
            if event["type"] == "error":
                await send_status("unavailable", event["message"])
                await websocket.close(
                    code=_dhan_feed_close_code(event.get("status_code")),
                    reason=event["message"][:120],
                )
                return
            if event["type"] != "tick":
                continue
            bar = event["bar"]
            quote = db.scalar(
                select(MarketQuote).where(
                    MarketQuote.instrument_id == instrument.id,
                    MarketQuote.source == "dhan_ws",
                )
            )
            if quote is None:
                quote = MarketQuote(
                    instrument_id=instrument.id,
                    source="dhan_ws",
                )
                db.add(quote)
            quote.timestamp = bar.timestamp
            quote.open = bar.open
            quote.high = bar.high
            quote.low = bar.low
            quote.close = bar.close
            quote.volume = bar.volume
            quote.retrieved_at = datetime.now(timezone.utc)
            quote.is_synthetic = False
            db.commit()
            db.refresh(quote)
            response = _quote_response(
                instrument,
                quote,
                freshness="fresh",
                freshness_message="Live Dhan WebSocket quote packet received.",
                is_cached=False,
                is_verified=True,
                provider_request_status="succeeded",
            ).model_copy(update={
                "delivery_mode": "websocket_tick",
                "stream_status": "active",
                "provider_connection_status": "connected",
                "feed_active": True,
            })
            await websocket.send_json({
                "type": "tick",
                "quote": response.model_dump(mode="json"),
            })
            client_ticks_sent += 1
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except BrokerReconnectRequired:
        credential.expires_at = datetime.now(timezone.utc)
        db.commit()
        await send_status(
            "unavailable",
            "Dhan rejected the session with HTTP 401; reconnect to renew the feed token.",
        )
        await websocket.close(code=4401)
    except BrokerAdapterError as error:
        await send_status("unavailable", str(error))
        await websocket.close(code=1011)
    finally:
        client_monitor.cancel()
        client_counter_task.cancel()
        try:
            await asyncio.gather(client_monitor, client_counter_task)
        except asyncio.CancelledError:
            pass
        await dhan_live_feed_hub.leave("ticker", group_id)


def _live_quote_change(
    db: Session,
    instrument: Instrument,
    quote_timestamp: datetime,
    close: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    zone = ZoneInfo(instrument.timezone)
    quote_day = quote_timestamp.astimezone(zone).date()
    bars = db.scalars(
        select(MarketBar)
        .where(
            MarketBar.instrument_id == instrument.id,
            MarketBar.interval == "1d",
            MarketBar.is_synthetic.is_(instrument.is_synthetic),
        )
        .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
        .limit(30)
    ).all()
    previous = next(
        (
            bar.close
            for bar in bars
            if _utc(bar.timestamp).astimezone(zone).date() < quote_day
        ),
        None,
    )
    if previous is None:
        return None, None
    absolute_change = close - previous
    percent_change = (
        (absolute_change / previous * Decimal("100")).quantize(Decimal("0.0001"))
        if previous != 0
        else None
    )
    return absolute_change, percent_change


@router.websocket("/watchlists/{watchlist_id}/stream")
async def dhan_watchlist_quote_stream(
    websocket: WebSocket,
    watchlist_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    await websocket.accept()
    origin = websocket.headers.get("origin")
    parsed_origin = urlsplit(origin or "")
    allowed_origin = origin == settings.frontend_origin or (
        parsed_origin.scheme == "https"
        and parsed_origin.hostname is not None
        and parsed_origin.hostname.endswith(".app.github.dev")
    )
    if origin and not allowed_origin:
        await websocket.close(code=4403)
        return
    raw_token = websocket.cookies.get("session_token")
    active_session = (
        db.scalar(
            select(UserSession)
            .where(
                UserSession.token_hash == hash_token(raw_token),
                UserSession.revoked_at.is_(None),
            )
            .options(selectinload(UserSession.user))
        )
        if raw_token
        else None
    )
    expires_at = active_session.expires_at if active_session is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if active_session is None or expires_at <= datetime.now(timezone.utc):
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Sign in again to use the live feed.",
        })
        await websocket.close(code=4401)
        return
    if (
        not active_session.user.email_verified
        or not active_session.user.two_factor_enabled
        or not active_session.two_factor_verified
    ):
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Verify this session before using broker market data.",
        })
        await websocket.close(code=4403)
        return
    if _default_market_data_source(db, active_session.user_id) != "dhan":
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Select Dhan as the market-data source to use this feed.",
        })
        await websocket.close(code=4404)
        return

    watchlist = db.scalar(
        select(Watchlist).where(
            Watchlist.id == watchlist_id,
            Watchlist.user_id == active_session.user_id,
        )
    )
    if watchlist is None:
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Selected watchlist is unavailable.",
        })
        await websocket.close(code=4404)
        return
    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == active_session.user_id,
            BrokerCredential.broker == "dhan",
        )
    )
    if credential is None:
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Connect Dhan on the Brokers page to enable live market data.",
        })
        await websocket.close(code=4404)
        return

    adapter = DhanAdapter()
    current_market_instruments: dict[str, Instrument] = {}

    async def resolve_subscriptions(
        detail_id: UUID | None,
    ) -> tuple[dict[str, tuple[str, str]], int, int]:
        entry_ids = set(
            db.scalars(
                select(WatchlistInstrument.instrument_id).where(
                    WatchlistInstrument.watchlist_id == watchlist_id,
                    WatchlistInstrument.user_id == active_session.user_id,
                )
            ).all()
        )
        if detail_id is not None:
            entry_ids.add(detail_id)
        instruments = db.scalars(
            select(Instrument)
            .where(Instrument.id.in_(entry_ids))
            .options(selectinload(Instrument.provider_identifiers))
        ).all() if entry_ids else []
        subscriptions: dict[str, tuple[str, str]] = {}
        current_market_instruments.clear()
        supported_count = 0
        open_count = 0
        for instrument in instruments:
            if (
                instrument.country.upper() != "IN"
                or instrument.currency.upper() != "INR"
                or instrument.asset_class != "equity"
                or instrument.is_synthetic
            ):
                continue
            supported_count += 1
            if _market_session_state(instrument) != "open":
                continue
            open_count += 1
            mapping = next(
                (
                    item
                    for item in instrument.provider_identifiers
                    if item.provider == adapter.slug and item.is_verified
                ),
                None,
            )
            if mapping is None:
                mapping_values = await asyncio.to_thread(
                    adapter.find_verified_equity_mapping,
                    instrument.symbol,
                    instrument.venue,
                )
                if mapping_values is None:
                    continue
                security_id, exchange_segment = mapping_values
                owner = db.scalar(
                    select(InstrumentProviderIdentifier.instrument_id).where(
                        InstrumentProviderIdentifier.provider == adapter.slug,
                        InstrumentProviderIdentifier.provider_instrument_id == security_id,
                        InstrumentProviderIdentifier.instrument_id != instrument.id,
                    )
                )
                if owner is not None:
                    continue
                mapping = InstrumentProviderIdentifier(
                    instrument_id=instrument.id,
                    provider=adapter.slug,
                    provider_instrument_id=security_id,
                    provider_symbol=instrument.symbol,
                    is_verified=True,
                    last_seen_at=datetime.now(timezone.utc),
                )
                instrument.provider_identifiers.append(mapping)
                db.add(mapping)
            else:
                exchange_segment = (
                    "NSE_EQ"
                    if instrument.venue.upper() in {"NSE", "XNSE"}
                    else "BSE_EQ"
                )
            mapping.last_seen_at = datetime.now(timezone.utc)
            subscriptions[str(instrument.id)] = (
                exchange_segment,
                mapping.provider_instrument_id,
            )
            current_market_instruments[str(instrument.id)] = instrument
        db.commit()
        if len(subscriptions) > 5000:
            raise HTTPException(
                status_code=422,
                detail="Dhan supports at most 5000 instruments on one connection.",
            )
        return subscriptions, supported_count, open_count

    def valid_detail_id(value: Any) -> UUID | None:
        if value in (None, ""):
            return None
        try:
            return UUID(str(value))
        except ValueError:
            return None

    async def send_status(
        status: str,
        message: str | None = None,
        last_snapshot_at: datetime | None = None,
    ) -> None:
        await websocket.send_json({
            "type": "status",
            "status": status,
            "message": message,
            "last_snapshot_at": (
                _utc(last_snapshot_at).isoformat()
                if last_snapshot_at is not None
                else None
            ),
        })

    def watchlist_snapshot_time() -> datetime | None:
        instrument_ids = db.scalars(
            select(WatchlistInstrument.instrument_id).where(
                WatchlistInstrument.watchlist_id == watchlist_id,
                WatchlistInstrument.user_id == active_session.user_id,
            )
        ).all()
        if not instrument_ids:
            return None
        return db.scalar(
            select(func.max(MarketQuote.retrieved_at)).where(
                MarketQuote.instrument_id.in_(instrument_ids)
            )
        )

    try:
        initial_subscriptions, supported_count, open_count = await resolve_subscriptions(None)
    except HTTPException:
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "This watchlist exceeds Dhan's 5000-instrument connection limit.",
        })
        await websocket.close(code=4404)
        return
    if not initial_subscriptions:
        status = "market_closed" if supported_count and not open_count else "unavailable"
        message = (
            "Market closed - live feed resumes at 09:15 IST"
            if status == "market_closed"
            else "No eligible Dhan-mapped instruments are in this watchlist."
        )
        await send_status(
            status,
            message,
            watchlist_snapshot_time() if status == "market_closed" else None,
        )
        await websocket.close(code=1000)
        return

    try:
        from app.api.brokers import _fresh_tokens

        tokens = _fresh_tokens(db, adapter, credential)
    except HTTPException:
        await websocket.send_json({
            "type": "status",
            "status": "unavailable",
            "message": "Reconnect Dhan on the Brokers page to renew its session.",
        })
        await websocket.close(code=4401)
        return

    group_id = str(uuid4())
    try:
        feed_queue = await dhan_live_feed_hub.join(
            "ticker",
            group_id,
            initial_subscriptions,
            tokens["access_token"],
            tokens,
        )
    except BrokerAdapterError as error:
        await send_status("unavailable", str(error))
        await websocket.close(code=4403)
        return

    logger.info(
        "Dhan watchlist subscription registered watchlist_id=%s instruments=%d "
        "backend_websocket_route=/api/v1/market-data/watchlists/%s/stream "
        "upstream_url=wss://api-feed.dhan.co?version=2&token=[REDACTED]"
        "&clientId=[REDACTED]&authType=2",
        watchlist_id,
        len(initial_subscriptions),
        watchlist_id,
    )
    client_ticks_sent = 0
    stream_task = asyncio.current_task()

    async def monitor_client() -> None:
        try:
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    if current_market_instruments and not any(
                        _market_session_state(instrument) == "open"
                        for instrument in current_market_instruments.values()
                    ):
                        await send_status(
                            "market_closed",
                            "Market closed - live feed resumes at 09:15 IST",
                            watchlist_snapshot_time(),
                        )
                        await websocket.close(code=1000)
                        if stream_task is not None:
                            stream_task.cancel()
                        return
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Dhan watchlist stream received invalid client JSON")
                    continue
                if not isinstance(payload, dict) or payload.get("type") != "subscriptions":
                    continue
                detail_id = valid_detail_id(payload.get("detail_instrument_id"))
                try:
                    desired, valid_count, open_count = await resolve_subscriptions(detail_id)
                except HTTPException:
                    await websocket.send_json({
                        "type": "status",
                        "status": "unavailable",
                        "message": "This watchlist exceeds Dhan's 5000-instrument connection limit.",
                    })
                    await websocket.close(code=4404)
                    if stream_task is not None:
                        stream_task.cancel()
                    return
                if not desired:
                    status = "market_closed" if valid_count and not open_count else "unavailable"
                    await send_status(
                        status,
                        (
                            "Market closed - live feed resumes at 09:15 IST"
                            if status == "market_closed"
                            else "No eligible Dhan-mapped instruments are selected."
                        ),
                        watchlist_snapshot_time() if status == "market_closed" else None,
                    )
                    await websocket.close(code=1000)
                    if stream_task is not None:
                        stream_task.cancel()
                    return
                await dhan_live_feed_hub.update("ticker", group_id, desired)
        except WebSocketDisconnect:
            if stream_task is not None:
                stream_task.cancel()

    client_monitor = asyncio.create_task(monitor_client())

    async def log_watchlist_counters() -> None:
        while True:
            await asyncio.sleep(30)
            logger.info(
                "Dhan watchlist client delivery counter watchlist_id=%s "
                "messages_sent=%d upstream_connections=%d subscriptions=%d",
                watchlist_id,
                client_ticks_sent,
                dhan_live_feed_hub._connection_counts["ticker"],
                len(dhan_live_feed_hub._all_subscriptions("ticker")),
            )

    client_counter_task = asyncio.create_task(log_watchlist_counters())
    try:
        while True:
            event = await feed_queue.get()
            if event["type"] == "status":
                await send_status(event["status"])
                continue
            if event["type"] == "error":
                await send_status("unavailable", event["message"])
                await websocket.close(
                    code=_dhan_feed_close_code(event.get("status_code")),
                    reason=event["message"][:120],
                )
                return
            if event["type"] != "tick":
                continue
            instrument_id = event["instrument_id"]
            bar = event["bar"]
            instrument = current_market_instruments.get(instrument_id)
            if instrument is None:
                continue
            if _market_session_state(instrument, bar.timestamp) == "closed":
                last_snapshot_at = db.scalar(
                    select(func.max(MarketQuote.retrieved_at)).where(
                        MarketQuote.instrument_id == instrument.id
                    )
                )
                await send_status(
                    "market_closed",
                    "Market closed - live feed resumes at 09:15 IST",
                    last_snapshot_at,
                )
                await websocket.close(code=1000)
                return
            quote = db.scalar(
                select(MarketQuote).where(
                    MarketQuote.instrument_id == instrument.id,
                    MarketQuote.source == "dhan_ws",
                )
            )
            if quote is None:
                quote = MarketQuote(
                    instrument_id=instrument.id,
                    source="dhan_ws",
                )
                db.add(quote)
            quote.timestamp = bar.timestamp
            quote.open = bar.open
            quote.high = bar.high
            quote.low = bar.low
            quote.close = bar.close
            quote.volume = bar.volume
            quote.retrieved_at = datetime.now(timezone.utc)
            quote.is_synthetic = False
            db.commit()
            db.refresh(quote)
            absolute_change, percent_change = _live_quote_change(
                db, instrument, bar.timestamp, bar.close
            )
            response = _quote_response(
                instrument,
                quote,
                freshness="fresh",
                freshness_message="Live Dhan WebSocket quote packet received.",
                is_cached=False,
                is_verified=True,
                provider_request_status="succeeded",
            ).model_copy(update={
                "delivery_mode": "websocket_tick",
                "stream_status": "active",
                "provider_connection_status": "connected",
                "feed_active": True,
                "absolute_change": absolute_change,
                "percent_change": percent_change,
            })
            await websocket.send_json({
                "type": "tick",
                "quote": response.model_dump(mode="json"),
            })
            client_ticks_sent += 1
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except BrokerReconnectRequired:
        credential.expires_at = datetime.now(timezone.utc)
        db.commit()
        await send_status("unavailable")
        await websocket.close(code=4401)
    except (BrokerAdapterError, HTTPException):
        await send_status("unavailable")
        await websocket.close(code=1011)
    finally:
        client_monitor.cancel()
        client_counter_task.cancel()
        try:
            await asyncio.gather(client_monitor, client_counter_task)
        except asyncio.CancelledError:
            pass
        await dhan_live_feed_hub.leave("ticker", group_id)


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
    active_providers = _providers(provider)
    unavailable_attempts = [
        ProviderAttemptResponse(
            provider=attempt["provider"],
            outcome="unavailable",
            reason=attempt["reason"],
        )
        for attempt in getattr(provider, "unavailable_providers", [])
    ]
    provider_attempts = sorted(
        unavailable_attempts,
        key=lambda attempt: (
            MARKET_DATA_FALLBACK_PRIORITY.index(attempt.provider)
            if attempt.provider in MARKET_DATA_FALLBACK_PRIORITY
            else len(MARKET_DATA_FALLBACK_PRIORITY)
        ),
    )
    fallback_reason = (
        provider_attempts[0].reason if provider_attempts else None
    )

    def record_lower_priority_skips(source: str) -> None:
        if source not in MARKET_DATA_FALLBACK_PRIORITY:
            return
        source_position = MARKET_DATA_FALLBACK_PRIORITY.index(source)
        attempted = {attempt.provider for attempt in provider_attempts}
        enabled_names = {active.name for active in active_providers}
        for lower_priority in MARKET_DATA_FALLBACK_PRIORITY[source_position + 1:]:
            if lower_priority in enabled_names and lower_priority not in attempted:
                provider_attempts.append(ProviderAttemptResponse(
                    provider=lower_priority,
                    outcome="skipped",
                    reason="higher_priority_provider_succeeded",
                ))
    if active_providers and active_providers[0].name == "angel_one":
        live_quote = db.scalar(
            select(MarketQuote).where(
                MarketQuote.instrument_id == instrument.id,
                MarketQuote.source == "angel_one_ws",
            )
        )
        live_mapping = next(
            (
                item for item in instrument.provider_identifiers
                if item.provider == "angel_one" and item.is_verified
            ),
            None,
        )
        if live_quote is not None:
            stale_cache = (live_quote, live_mapping is not None)
            _, _, evidence = classify_quote(live_quote, instrument, None, now)
            if not refresh and live_mapping is not None and evidence["quote_freshness_status"] == "fresh":
                return _quote_response(
                    instrument,
                    live_quote,
                    freshness="fresh",
                    freshness_message="Latest Angel One WebSocket tick is within the configured freshness window.",
                    is_cached=True,
                    is_verified=True,
                    provider_request_status="not_attempted",
                ).model_copy(update={
                    "provider_connection_status": "connected",
                    "feed_active": True,
                    "stream_status": "active",
                    "provider_attempts": provider_attempts,
                    "fallback_reason": fallback_reason,
                })
    dhan_snapshot_attempted = False
    if active_providers and active_providers[0].name == "dhan":
        dhan_quote, stale_dhan_quote = _dhan_snapshot_quote(
            db, instrument, _user, refresh=refresh, now=now
        )
        if dhan_quote is not None:
            if dhan_quote.freshness_status == "fresh":
                provider_attempts.append(ProviderAttemptResponse(
                    provider="dhan",
                    outcome="used",
                    reason=None,
                ))
                record_lower_priority_skips("dhan")
                return dhan_quote.model_copy(update={
                    "provider_attempts": provider_attempts,
                    "fallback_reason": fallback_reason,
                })
            provider_attempts.append(ProviderAttemptResponse(
                provider="dhan",
                outcome="stale",
                reason=dhan_quote.freshness_status,
            ))
            fallback_reason = fallback_reason or dhan_quote.freshness_status
            cached_dhan_quote = db.scalar(
                select(MarketQuote).where(
                    MarketQuote.instrument_id == instrument.id,
                    MarketQuote.source == dhan_quote.source,
                )
            )
            stale_cache = (
                (cached_dhan_quote, dhan_quote.is_verified)
                if cached_dhan_quote is not None
                else stale_dhan_quote
            )
            dhan_snapshot_attempted = dhan_quote.provider_request_status == "succeeded"
        else:
            stale_cache = stale_dhan_quote
    for active_provider in active_providers:
        if dhan_snapshot_attempted and active_provider.name == "dhan":
            continue
        if active_provider.is_mock and not instrument.is_synthetic:
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="skipped",
                reason="synthetic_provider_not_used_for_real_instrument",
            ))
            continue
        attempt_started = time.monotonic()
        breaker_reason = _provider_circuit_breaker.before_attempt(
            active_provider.name
        )
        if breaker_reason is not None:
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="skipped",
                reason=breaker_reason,
                reason_category=_reason_category(breaker_reason),
            ))
            fallback_reason = fallback_reason or breaker_reason
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
                response = _quote_response(
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
                    provider_request_status="not_attempted",
                )
                if response.freshness_status in {"fresh", "unverified", "eod_close"}:
                    db.commit()
                    provider_attempts.append(ProviderAttemptResponse(
                        provider=active_provider.name,
                        outcome="used",
                        reason=(
                            "eod_close"
                            if response.freshness_status == "eod_close"
                            else None
                        ),
                    ))
                    record_lower_priority_skips(active_provider.name)
                    return response.model_copy(update={
                        "provider_attempts": provider_attempts,
                        "fallback_reason": fallback_reason,
                    })
                provider_attempts.append(ProviderAttemptResponse(
                    provider=active_provider.name,
                    outcome="stale",
                    reason=response.freshness_status,
                ))
                fallback_reason = fallback_reason or response.freshness_status
            stale_cache = stale_cache or (
                cached_quote,
                provider_mapping.is_verified if provider_mapping else False,
            )
        try:
            candidate, mapping = _resolve_provider_candidate(db, instrument, active_provider)
        except (MarketDataProviderError, TimeoutError, ValueError) as error:
            reason = (
                _safe_provider_error_code(error.code)
                if isinstance(error, MarketDataProviderError)
                else "instrument_mapping_failed"
            )
            unsupported = reason in {
                "instrument_not_found",
                "provider_symbol_unavailable",
                "invalid_instrument_mapping",
            }
            if not unsupported:
                _provider_circuit_breaker.record_failure(active_provider.name)
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="unsupported" if unsupported else "unavailable",
                reason=reason,
                reason_category=_reason_category(reason),
                http_status=(
                    error.status_code
                    if isinstance(error, MarketDataProviderError)
                    else None
                ),
                latency_ms=(time.monotonic() - attempt_started) * 1000,
            ))
            fallback_reason = fallback_reason or reason
            continue
        except Exception as error:
            db.rollback()
            reason = f"internal_error:{type(error).__name__}"
            logger.exception(
                "Unexpected market-data provider mapping failure; trying next provider.",
                extra={
                    "provider": active_provider.name,
                    "instrument_id": str(instrument.id),
                },
            )
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="unavailable",
                reason=reason,
                reason_category=ReasonCategory.INTERNAL_ERROR,
                http_status=getattr(active_provider, "last_response_status", None),
                latency_ms=(time.monotonic() - attempt_started) * 1000,
            ))
            fallback_reason = fallback_reason or reason
            continue
        if cached_quote is not None and stale_cache is not None:
            stale_cache = (stale_cache[0], mapping.is_verified)
        try:
            bar = HistoricalBar.model_validate(active_provider.get_latest_quote(candidate))
        except (MarketDataProviderError, TimeoutError, ValueError) as error:
            reason = (
                _safe_provider_error_code(error.code)
                if isinstance(error, MarketDataProviderError)
                else "provider_invalid_response"
            )
            if reason == "rate_limited":
                _provider_circuit_breaker.record_failure(
                    active_provider.name,
                    cooldown_seconds=30,
                )
            else:
                _provider_circuit_breaker.record_failure(active_provider.name)
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="unavailable",
                reason=reason,
                reason_category=_reason_category(reason),
                http_status=(
                    error.status_code
                    if isinstance(error, MarketDataProviderError)
                    else getattr(active_provider, "last_response_status", None)
                ),
                latency_ms=(time.monotonic() - attempt_started) * 1000,
            ))
            fallback_reason = fallback_reason or reason
            continue
        except Exception as error:
            db.rollback()
            reason = f"internal_error:{type(error).__name__}"
            logger.exception(
                "Unexpected market-data quote failure; trying next provider.",
                extra={
                    "provider": active_provider.name,
                    "instrument_id": str(instrument.id),
                },
            )
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="unavailable",
                reason=reason,
                reason_category=ReasonCategory.INTERNAL_ERROR,
                http_status=getattr(active_provider, "last_response_status", None),
                latency_ms=(time.monotonic() - attempt_started) * 1000,
            ))
            fallback_reason = fallback_reason or reason
            continue
        if active_provider.is_mock or candidate.is_synthetic:
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="unavailable",
                reason="synthetic_data_not_accepted",
            ))
            fallback_reason = fallback_reason or "synthetic_data_not_accepted"
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
        response = _quote_response(
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
            provider_request_status="succeeded",
        )
        if response.freshness_status not in {"fresh", "unverified", "eod_close"}:
            _provider_circuit_breaker.record_failure(active_provider.name)
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="stale",
                reason=response.freshness_status,
                reason_category=_reason_category(response.freshness_status),
                http_status=getattr(active_provider, "last_response_status", None),
                latency_ms=(time.monotonic() - attempt_started) * 1000,
            ))
            fallback_reason = fallback_reason or response.freshness_status
            stale_cache = (cached_quote, mapping.is_verified)
            continue
        _provider_circuit_breaker.record_success(active_provider.name)
        provider_attempts.append(ProviderAttemptResponse(
            provider=active_provider.name,
            outcome="used",
            reason=(
                "eod_close"
                if response.freshness_status == "eod_close"
                else "unverified_instrument_mapping"
                if response.freshness_status == "unverified"
                else None
            ),
            http_status=getattr(active_provider, "last_response_status", None),
            latency_ms=(time.monotonic() - attempt_started) * 1000,
            bars=1,
        ))
        record_lower_priority_skips(active_provider.name)
        return response.model_copy(update={
            "provider_attempts": provider_attempts,
            "fallback_reason": fallback_reason,
        })

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
            provider_request_status="failed",
        ).model_copy(update={
            "provider_attempts": provider_attempts,
            "fallback_reason": fallback_reason or "all_providers_unavailable_or_stale",
        })
    raise HTTPException(
        status_code=503,
        detail={
            "message": "Quote is temporarily unavailable.",
            "provider_attempts": [
                attempt.model_dump() for attempt in provider_attempts
            ],
            "fallback_reason": fallback_reason or "all_providers_unavailable_or_stale",
        },
    )


def _quote_response(
    instrument: Instrument,
    quote: MarketQuote,
    *,
    freshness: Literal["fresh", "stale"],
    freshness_message: str,
    is_cached: bool,
    is_verified: bool,
    provider_request_status: Literal["succeeded", "not_attempted", "failed"],
) -> QuoteResponse:
    now = datetime.now(timezone.utc)
    usable, reason, evidence = classify_quote(
        quote, instrument, None, now
    )
    freshness_status = evidence["quote_freshness_status"]
    if (
        quote.source == "yfinance"
        and instrument.country.upper() == "IN"
        and evidence["quote_data_type"] == "daily_close"
        and freshness_status == "historical"
        and _market_session_state(instrument, now) == "closed"
    ):
        freshness_status = "eod_close"
        freshness_message = "EOD close from the last available NSE session; not a live quote."
    elif reason == "historical_close_not_current":
        freshness_message = (
            "Daily-bar close only; this is historical data, not a current market quote."
        )
    elif reason == "unverified_provider_mapping":
        freshness_message = (
            "The observation is recent, but its provider instrument mapping is unverified."
        )
    elif reason == "future_dated_price":
        freshness_message = "The provider timestamp is in the future; this price is not usable."
    elif reason == "market_timestamp_unavailable":
        freshness_message = "The provider did not supply a market timestamp."
    elif (
        reason == "stale_price"
        and quote.source != "dhan"
        and "All configured providers failed" not in freshness_message
    ):
        freshness_message = "The market observation or retrieval is outside the freshness window."
    elif not usable and reason == "synthetic_price":
        freshness_message = "Synthetic data is not a current market price."

    return QuoteResponse(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        currency=instrument.currency,
        source=quote.source,
        retrieved_at=_utc(quote.retrieved_at),
        freshness="fresh" if usable else "stale",
        freshness_status=freshness_status,
        freshness_message=freshness_message,
        is_cached=is_cached,
        is_synthetic=quote.is_synthetic,
        is_verified=is_verified,
        provider_configured=True,
        provider_request_status=provider_request_status,
        market_timestamp=_utc(quote.timestamp),
        market_age_seconds=max(0.0, evidence["quote_timestamp_age_seconds"]),
        data_type=evidence["quote_data_type"],
        delivery_mode=(
            "websocket_tick"
            if quote.source in {"dhan_ws", "angel_one_ws"}
            else "rest_snapshot"
            if quote.source == "dhan"
            else "on_demand_rest"
        ),
        stream_status="not_configured",
        provider=quote.source.removesuffix("_ws"),
        fetched_at=_utc(quote.retrieved_at),
        as_of=_utc(quote.timestamp),
        interval="quote",
        session_state=_market_session_state(instrument),
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
    interval = _canonical_interval(interval)
    start_utc, end_utc = _validate_range(interval, start, end)
    if instrument.is_synthetic:
        raise HTTPException(status_code=409, detail="Synthetic instruments cannot sync market history.")
    active_providers = _providers(provider)
    selected_source = active_providers[0].name if active_providers else None
    provider_attempts = [
        ProviderAttemptResponse(
            provider=attempt["provider"],
            outcome="unavailable",
            reason=attempt["reason"],
        )
        for attempt in getattr(provider, "unavailable_providers", [])
    ]

    def read_history(source: str | None) -> HistoryResponse:
        source_provider = next(
            (active for active in active_providers if active.name == source),
            None,
        )
        response = get_history(
            instrument_id=instrument_id,
            interval=interval,
            start=start_utc,
            end=end_utc,
            offset=0,
            limit=limit,
            _user=_user,
            provider=MarketDataProviderChain(
                [source_provider] if source_provider is not None else []
            ),
            db=db,
        )
        return response.model_copy(update={
            "provider_attempts": provider_attempts,
            "fallback_reason": next(
                (attempt.reason for attempt in provider_attempts if attempt.reason),
                None,
            ),
        })

    cached_query = (
        select(MarketBar)
        .where(
            MarketBar.instrument_id == instrument.id,
            MarketBar.interval == interval,
            MarketBar.is_synthetic.is_(False),
            MarketBar.timestamp >= start_utc,
            MarketBar.timestamp < end_utc,
        )
    )
    if selected_source is not None:
        cached_query = cached_query.where(MarketBar.source == selected_source)
    else:
        cached_query = cached_query.where(MarketBar.source.is_(None))
    cached_bars = list(
        db.scalars(
            cached_query.order_by(MarketBar.timestamp)
        )
    )
    missing_ranges: list[tuple[datetime, datetime]] = []
    if interval == "1d" and cached_bars:
        cached_timestamps = sorted({
            _utc(bar.timestamp).replace(hour=0, minute=0, second=0, microsecond=0)
            for bar in cached_bars
        })
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
        missing_ranges.append((start_utc, end_utc))
    else:
        missing_ranges.append((start_utc, end_utc))

    if not missing_ranges:
        return read_history(selected_source)

    selected_provider: MarketDataProvider | None = None
    selected_candidate: InstrumentCandidate | None = None
    validated_bars: list[HistoricalBar] = []
    provider_failures: list[dict[str, str]] = []
    supported_provider_attempted = False
    for active_provider in _providers(provider):
        limitation = _provider_history_limitation(
            active_provider.name, interval, start_utc
        )
        if limitation is not None:
            provider_failures.append({
                "provider": active_provider.name,
                "reason": limitation,
            })
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="unsupported",
                reason=limitation,
                reason_category=ReasonCategory.UNSUPPORTED_INTERVAL_OR_LOOKBACK,
            ))
            continue
        breaker_reason = _provider_circuit_breaker.before_attempt(
            active_provider.name
        )
        if breaker_reason is not None:
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="skipped",
                reason=breaker_reason,
                reason_category=_reason_category(breaker_reason),
            ))
            provider_failures.append({
                "provider": active_provider.name,
                "reason": breaker_reason,
            })
            continue
        supported_provider_attempted = True
        attempted_at = datetime.now(timezone.utc)
        attempt_started = time.monotonic()
        provider_http_diagnostics = getattr(
            active_provider, "last_http_diagnostics", None
        )
        if isinstance(provider_http_diagnostics, list):
            provider_http_diagnostics.clear()
        response_status = None
        safe_error = None
        provider_rows_returned = [0]
        no_new_bars = False
        try:
            candidate, _mapping = _resolve_provider_candidate(db, instrument, active_provider)
            provider_bars: list[HistoricalBar] = []
            provider_ranges = (
                missing_ranges
                if active_provider.name == selected_source
                else [(start_utc, end_utc)]
            )
            if active_provider.name == "dhan" and interval != "1d":
                bounded_ranges: list[tuple[datetime, datetime]] = []
                for range_start, range_end in provider_ranges:
                    chunk_end = range_end
                    while chunk_end > range_start:
                        chunk_start = max(
                            range_start,
                            chunk_end - timedelta(days=89),
                        )
                        bounded_ranges.append((chunk_start, chunk_end))
                        chunk_end = chunk_start
                provider_ranges = sorted(
                    bounded_ranges,
                    key=lambda time_range: time_range[1],
                    reverse=True,
                )
            for range_start, range_end in provider_ranges:
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
                if _market_session_state(instrument, attempted_at) == "closed":
                    no_new_bars = True
                else:
                    raise MarketDataProviderError("provider_no_history_for_range")
            selected_provider = active_provider
            selected_candidate = candidate
            validated_bars = sorted(
                {bar.timestamp: bar for bar in provider_bars}.values(),
                key=lambda bar: bar.timestamp,
            )
        except MarketDataProviderError as error:
            db.rollback()
            safe_error = _safe_provider_error_code(error.code)
            if safe_error == "rate_limited":
                _provider_circuit_breaker.record_failure(
                    active_provider.name,
                    cooldown_seconds=30,
                )
            elif safe_error not in {
                "instrument_not_found",
                "provider_symbol_unavailable",
                "invalid_instrument_mapping",
                "unsupported_interval",
            }:
                _provider_circuit_breaker.record_failure(active_provider.name)
            logger.error(
                "market_data_provider_failure",
                extra={
                    "provider": active_provider.name,
                    "operation": "history_sync",
                    "instrument_id": str(instrument.id),
                    "interval": interval,
                    "error_category": safe_error,
                    "upstream_status": error.status_code,
                },
            )
            provider_failures.append({
                "provider": active_provider.name,
                "reason": safe_error,
            })
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome=(
                    "rate_limited"
                    if safe_error == "rate_limited"
                    else "unsupported"
                    if safe_error in {
                        "instrument_not_found",
                        "provider_symbol_unavailable",
                        "unsupported_interval",
                    }
                    else "unavailable"
                ),
                reason=safe_error,
                reason_category=_reason_category(safe_error),
                http_status=getattr(
                    active_provider, "last_response_status", None
                ) or error.status_code,
                latency_ms=(time.monotonic() - attempt_started) * 1000,
                bars=provider_rows_returned[0],
            ))
            response_status = getattr(
                active_provider, "last_response_status", None
            ) or error.status_code
        except Exception as error:
            db.rollback()
            safe_error = (
                f"internal_error:{type(error).__name__}"
                if active_provider.name == "angel_one"
                else "provider_unexpected_error"
            )
            if active_provider.name != "angel_one":
                _provider_circuit_breaker.record_failure(active_provider.name)
            logger.error(
                "market_data_provider_failure",
                extra={
                    "provider": active_provider.name,
                    "operation": "history_sync",
                    "instrument_id": str(instrument.id),
                    "interval": interval,
                    "error_category": safe_error,
                    "exception_type": type(error).__name__,
                    "upstream_status": getattr(
                        active_provider, "last_response_status", None
                    ),
                },
            )
            response_status = getattr(active_provider, "last_response_status", None)
            provider_failures.append({
                "provider": active_provider.name,
                "reason": safe_error,
            })
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="unavailable",
                reason=safe_error,
                reason_category=_reason_category(safe_error),
                http_status=getattr(
                    active_provider, "last_response_status", None
                ),
                latency_ms=(time.monotonic() - attempt_started) * 1000,
                bars=provider_rows_returned[0],
            ))
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
                reason_category=_reason_category(safe_error or "provider_unavailable"),
                attempted_at=attempted_at,
                http_diagnostics=provider_http_diagnostics,
            )
            db.commit()
            continue
        _provider_circuit_breaker.record_success(active_provider.name)
        if no_new_bars:
            provider_attempts.append(ProviderAttemptResponse(
                provider=active_provider.name,
                outcome="no_new_bars",
                reason_category=ReasonCategory.CLOSED_SESSION_NO_NEW_BARS,
                http_status=getattr(active_provider, "last_response_status", None),
                latency_ms=(time.monotonic() - attempt_started) * 1000,
                bars=0,
            ))
        break
    if selected_provider is None or selected_candidate is None:
        db.commit()
        if not supported_provider_attempted:
            raise HTTPException(
                status_code=422,
                detail={
                    "stage": "provider_capability_check",
                    "message": "No configured provider supports this interval and requested range.",
                    "providers": provider_failures,
                },
            )
        if cached_bars:
            return read_history(selected_source)
        raise HTTPException(
            status_code=503,
            detail={
                "stage": "provider_history_fetch",
                "message": "Historical market data is temporarily unavailable.",
                "providers": provider_failures,
                "provider_attempts": [
                    attempt.model_dump() for attempt in provider_attempts
                ],
            },
        )

    retrieved_at = datetime.now(timezone.utc)
    rows_written = (
        0
        if no_new_bars
        else _persist_real_bars(
            db,
            instrument,
            selected_provider,
            selected_candidate,
            interval,
            validated_bars,
            retrieved_at,
        )
    )

    previous_failures = [
        attempt for attempt in provider_attempts
        if attempt.outcome in {"unavailable", "unsupported"}
        and attempt.provider != selected_provider.name
    ]
    fallback_reason = (
        f"fallback_after_{previous_failures[0].provider}_"
        f"{previous_failures[0].reason or previous_failures[0].outcome}"
        if previous_failures
        else None
    )
    if fallback_reason is not None:
        fallback_reason = fallback_reason[:160]
    success_reason = ";".join(
        reason for reason in (
            fallback_reason,
            "provider_data_already_stored" if not rows_written else None,
        ) if reason
    ) or None
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
        safe_reason="no_new_bars" if no_new_bars else success_reason,
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
        outcome=(
            "no_new_bars"
            if no_new_bars
            else "success"
            if rows_written
            else "unavailable"
        ),
        reason=(
            "no_new_bars"
            if no_new_bars
            else None
            if rows_written
            else "provider_data_already_stored"
        ),
        reason_category=(
            ReasonCategory.CLOSED_SESSION_NO_NEW_BARS
            if no_new_bars
            else None
            if rows_written
            else ReasonCategory.PROVIDER_DATA_ALREADY_STORED
        ),
        attempted_at=retrieved_at,
        http_diagnostics=provider_http_diagnostics,
    )
    db.commit()
    if not no_new_bars:
        provider_attempts.append(ProviderAttemptResponse(
            provider=selected_provider.name,
            outcome="used",
            http_status=getattr(selected_provider, "last_response_status", None),
            latency_ms=(time.monotonic() - attempt_started) * 1000,
            bars=len(validated_bars),
        ))
    return read_history(selected_provider.name).model_copy(update={
        "is_cached": no_new_bars or rows_written == 0,
        "fetched_at": retrieved_at if not no_new_bars else None,
        "sync_status": "no_new_bars" if no_new_bars else "success",
    })


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
    dhan_first_bar = db.scalar(
        select(func.min(MarketBar.timestamp)).where(
            MarketBar.instrument_id == instrument.id,
            MarketBar.interval == "1d",
            MarketBar.source == "dhan",
            MarketBar.is_synthetic.is_(False),
        )
    )
    if dhan_first_bar is not None:
        available_start = _utc(dhan_first_bar).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if start < available_start:
            logger.info(
                "Clamping market-data backfill to earliest stored Dhan bar "
                "(instrument_id=%s from=%s to=%s)",
                instrument.id,
                start.date(),
                available_start.date(),
            )
            start = available_start
    bars_before = _count_real_daily_bars(db, instrument.id, start, end)
    chunks: list[BackfillChunkDetail] = []
    chunks_failed = 0
    chunk_start = start
    while chunk_start < end and len(chunks) < MAX_BACKFILL_CHUNKS:
        chunk_end = min(chunk_start + timedelta(days=BACKFILL_CHUNK_DAYS), end)
        detail: BackfillChunkDetail = {
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
                    reason_category=_reason_category(detail["reason"]),
                    attempted_at=attempted_at,
                )
                succeeded = True
                break
            except MarketDataProviderError as error:
                if error.request_url is not None:
                    logger.warning(
                        "Market history upstream failure provider=%s instrument_id=%s "
                        "url=%s request_body=%s status=%s response_snippet=%s",
                        active_provider.name,
                        instrument.id,
                        error.request_url,
                        error.request_body or "unavailable",
                        error.status_code,
                        error.response_body or "unavailable",
                    )
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
                    reason_category=_reason_category(
                        _safe_provider_error_code(error.code)
                    ),
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
                    reason_category=_reason_category(safe_reason),
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
    provider: MarketDataProvider = Depends(get_market_data_provider),
    db: Session = Depends(get_db),
):
    instrument = _get_instrument(db, instrument_id)
    interval = _canonical_interval(interval)
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="start and end must be supplied together")
    query = select(MarketBar).where(
        MarketBar.instrument_id == instrument.id,
        MarketBar.interval == interval,
    )
    selected_providers = _providers(provider)
    selected_source = selected_providers[0].name if selected_providers else None
    if selected_source is not None:
        query = query.where(MarketBar.source == selected_source)
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
        freshness=_freshness(db, instrument.id, interval, source=selected_source),
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
        provider=selected_source,
        fetched_at=(
            max(_utc(bar.retrieved_at) for bar in bars)
            if bars
            else None
        ),
        as_of=_utc(bars[-1].timestamp) if bars else None,
        currency=instrument.currency,
        session_state=_market_session_state(instrument),
        is_cached=True,
    )
