import asyncio
import base64
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.market_data import (
    DhanMarketDataProvider,
    MarketDataSourceOption,
    MarketDataProviderChain,
    ProviderCircuitBreaker,
    ReasonCategory,
    get_market_data_provider,
    market_data_provider_for_user,
    _default_market_data_source,
    _resolve_provider_candidate,
)
from app.api import auth, brokers, market_data
from app import broker_adapters
from app.broker_adapters import BrokerReconnectRequired, DhanAdapter
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
)
from app.models import (
    BrokerCredential,
    InstrumentProviderIdentifier,
    Instrument,
    MarketBar,
    MarketDataSync,
    MarketDataSyncAttempt,
    MarketQuote,
    UserMarketDataSettings,
    UserSession,
)
from app.security import hash_token
from app.provider_adapters import AngelOneProvider, BinanceProvider, DeltaExchangeProvider, UpstoxProvider
from market_data_fixtures import RecordedMarketDataProvider


def _test_dhan_access_token() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    }).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def prepare_dhan_quote(client: TestClient, test_engine, monkeypatch) -> None:
    monkeypatch.setattr(settings, "broker_dhan_enabled", True)
    monkeypatch.setattr(settings, "dhan_api_key", SecretStr("test-api-key"))
    monkeypatch.setattr(settings, "dhan_api_secret", SecretStr("test-api-secret"))
    monkeypatch.setattr(
        settings,
        "dhan_redirect_uri",
        "https://app.example.com/api/v1/brokers/dhan/callback",
    )
    monkeypatch.setattr(
        settings, "broker_encryption_key", SecretStr(Fernet.generate_key().decode())
    )
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as session:
        user_session = session.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        now = datetime.now(timezone.utc)
        session.add(
            BrokerCredential(
                user_id=user_session.user_id,
                broker="dhan",
                encrypted_tokens=brokers.encrypt_tokens({
                    "access_token": _test_dhan_access_token(),
                    "client_id": "test-client-id",
                }),
                expires_at=now + timedelta(hours=1),
                connected_at=now,
            )
        )
        session.commit()


def use_dhan_market_data_provider() -> None:
    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [
            DhanMarketDataProvider(
                DhanAdapter(),
                {"access_token": _test_dhan_access_token(), "client_id": "test-client-id"},
            )
        ]
    )


@pytest.fixture
def test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(test_engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_market_data_provider] = RecordedMarketDataProvider
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_provider_circuit_breaker(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data,
        "_provider_circuit_breaker",
        ProviderCircuitBreaker(threshold=3, cooldown_seconds=30),
    )


def test_missing_market_settings_table_reports_migration_command() -> None:
    class UndefinedTableError(Exception):
        sqlstate = "42P01"

    class MissingSettingsSession:
        def get(self, model, key):
            raise ProgrammingError(
                "SELECT user_market_data_settings",
                {},
                UndefinedTableError(),
            )

    with pytest.raises(HTTPException) as error:
        market_data_provider_for_user(MissingSettingsSession(), uuid4())

    assert error.value.status_code == 503
    assert error.value.detail == {
        "code": "database_migration_required",
        "message": "Database migrations are pending. Run `alembic upgrade head` from the backend directory.",
    }


def test_saved_provider_preference_does_not_reorder_fixed_fallback_priority(
    test_engine, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "market_data_providers", "angel_one,dhan,yfinance")
    sources = [
        MarketDataSourceOption(
            name=name,
            label=name,
            available=True,
            status="connected",
        )
        for name in ("angel_one", "dhan", "yfinance")
    ]
    monkeypatch.setattr(market_data, "_available_market_data_sources", lambda db, user_id: sources)
    monkeypatch.setattr(
        market_data,
        "_market_data_credentials",
        lambda db, user_id, provider_name: (None, None, None),
    )
    monkeypatch.setattr(
        market_data,
        "create_market_data_provider_chain",
        lambda names, **kwargs: MarketDataProviderChain([
            SimpleNamespace(name=name, is_mock=False) for name in names
        ]),
    )
    user_id = uuid4()
    with Session(test_engine) as session:
        session.add(UserMarketDataSettings(
            user_id=user_id,
            selected_provider="dhan",
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()

        chain = market_data_provider_for_user(session, user_id)
        assert [provider.name for provider in chain.providers] == [
            "angel_one",
            "dhan",
            "yfinance",
        ]
        session.delete(session.get(UserMarketDataSettings, user_id))
        session.commit()
        assert _default_market_data_source(session, user_id) == "dhan"
        assert [provider.name for provider in market_data._providers(chain)] == [
            "dhan",
            "angel_one",
            "yfinance",
        ]
    app.dependency_overrides.clear()


def register(client: TestClient, email: str = "market@example.com") -> None:
    delivered: list[str] = []
    with patch.object(auth, "send_verification_email", lambda _email, token: delivered.append(token)):
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "correct-horse-battery"},
        )
        assert response.status_code == 202, response.text
        verified = client.post("/api/v1/auth/verify-email", json={"token": delivered[0]})
        assert verified.status_code == 200, verified.text
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert login_response.status_code == 200, login_response.text


def test_market_data_endpoints_require_authentication(client: TestClient) -> None:
    response = client.get("/api/v1/market-data/provider")
    assert response.status_code == 401


def test_watchlist_is_user_scoped_and_reports_cached_quote_provenance(
    client: TestClient,
) -> None:
    register(client)
    instrument = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]
    instrument_id = instrument["id"]

    added = client.post(
        f"/api/v1/market-data/watchlist/{instrument_id}",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token", "")},
    )
    assert added.status_code == 204, added.text
    assert client.get("/api/v1/market-data/watchlist").json() == [
        {
            "instrument_id": instrument_id,
            "symbol": "ALPHA",
            "display_name": instrument["display_name"],
            "asset_class": instrument["asset_class"],
            "venue": instrument["venue"],
            "currency": instrument["currency"],
            "is_synthetic": False,
            "relink_required": False,
            "quote": None,
        }
    ]

    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")
    assert quote.status_code == 200, quote.text
    entry = client.get("/api/v1/market-data/watchlist").json()[0]
    assert entry["quote"]["source"] == "recorded"
    assert entry["quote"]["is_synthetic"] is False
    assert entry["quote"]["is_verified"] is False
    assert entry["quote"]["close"] == quote.json()["bar"]["close"]

    register(client, "other-market@example.com")
    assert client.get("/api/v1/market-data/watchlist").json() == []
    login_response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "market@example.com",
            "password": "correct-horse-battery",
        },
    )
    assert login_response.status_code == 200, login_response.text
    assert len(client.get("/api/v1/market-data/watchlist").json()) == 1


def test_ist_epoch_dhan_tick_is_open_during_nse_session() -> None:
    raw_ltt = int(datetime(2025, 4, 15, 10, 30, tzinfo=timezone.utc).timestamp())
    tick = broker_adapters._dhan_ist_epoch_to_utc(raw_ltt)

    assert tick == datetime(2025, 4, 15, 5, 0, tzinfo=timezone.utc)
    assert market_data._market_session_state(
        SimpleNamespace(country="IN"),
        tick,
    ) == "open"


def test_watchlist_stream_skips_closed_session_tick_without_closing(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "dhan-closed-tick@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)
    instrument = next(
        item
        for item in client.get("/api/v1/market-data/instruments?query=ALPHA").json()
        if item["venue"] == "DEMO_IN"
    )
    use_dhan_market_data_provider()
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as session:
        user_session = session.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        user_session.user.two_factor_enabled = True
        user_session.two_factor_verified = True
        session.commit()

    csrf_headers = {"X-CSRF-Token": client.cookies.get("csrf_token", "")}
    watchlist = client.post(
        "/api/v1/market-data/watchlists",
        json={"name": "Closed tick test"},
        headers=csrf_headers,
    )
    assert watchlist.status_code == 201, watchlist.text
    added = client.post(
        f"/api/v1/market-data/watchlists/{watchlist.json()['id']}"
        f"/instruments/{instrument['id']}",
        headers=csrf_headers,
    )
    assert added.status_code == 204, added.text

    monkeypatch.setattr(
        DhanAdapter,
        "find_verified_equity_mapping",
        lambda _self, _symbol, _venue: ("11536", "NSE_EQ"),
    )
    monkeypatch.setattr(
        market_data,
        "_market_session_state",
        lambda _instrument, _now=None, *, raw_ltt=None, parsed_ltt_utc=None: (
            "closed" if raw_ltt is not None else "open"
        ),
    )
    queued_messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    queued_messages.put_nowait({"type": "status", "status": "active"})
    queued_messages.put_nowait({
        "type": "tick",
        "instrument_id": instrument["id"],
        "bar": HistoricalBar(
            timestamp=datetime(2025, 4, 15, 5, 0, tzinfo=timezone.utc),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        ),
    })
    queued_messages.put_nowait({"type": "status", "status": "active"})

    async def join_feed(_feed, _group_id, _subscriptions, _token, _tokens):
        return queued_messages

    monkeypatch.setattr(market_data.dhan_live_feed_hub, "join", join_feed)
    with client.websocket_connect(
        f"/api/v1/market-data/watchlists/{watchlist.json()['id']}/stream",
        headers={"Origin": settings.frontend_origin},
    ) as stream:
        assert stream.receive_json()["status"] == "active"
        closed_status = stream.receive_json()
        assert closed_status["status"] == "market_closed"
        assert stream.receive_json()["status"] == "active"

    with Session(test_engine) as session:
        assert session.scalar(
            select(MarketQuote).where(MarketQuote.source == "dhan_ws")
        ) is None


def test_search_keeps_same_symbol_in_different_venues_distinct(client: TestClient) -> None:
    register(client)

    response = client.get("/api/v1/market-data/instruments?query=ALPHA&limit=10")

    assert response.status_code == 200, response.text
    instruments = response.json()
    assert len(instruments) == 2
    assert {item["symbol"] for item in instruments} == {"ALPHA"}
    assert {item["venue"] for item in instruments} == {"DEMO_US", "DEMO_IN"}
    assert len({item["id"] for item in instruments}) == 2
    assert all(not item["is_synthetic"] for item in instruments)
    assert all(item["provider_identifiers"][0]["is_verified"] is False for item in instruments)


def test_recorded_fixture_reports_real_data_capabilities(client: TestClient) -> None:
    register(client)

    response = client.get("/api/v1/market-data/provider")

    assert response.status_code == 200
    assert response.json()["provider"]["healthy"] is True
    assert response.json()["provider"]["is_mock"] is False
    assert "historical_ohlcv" in response.json()["provider"]["capabilities"]
    assert response.json()["provider"]["status"] == "recorded_test_fixture"


def test_market_data_source_selection_does_not_reorder_provider_routing(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "source-selection@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)

    initial = client.get("/api/v1/market-data/settings")
    selected = client.put(
        "/api/v1/market-data/settings/provider",
        json={"provider": "yfinance"},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token", "")},
    )
    refreshed = client.get("/api/v1/market-data/settings")

    assert initial.status_code == 200, initial.text
    assert initial.json()["selected_provider"] == "dhan"
    assert selected.status_code == 200, selected.text
    assert selected.json()["selected_provider"] == "yfinance"
    assert refreshed.json()["selected_provider"] == "yfinance"
    with Session(test_engine) as session:
        preference = session.scalar(select(UserMarketDataSettings))
        assert preference is not None
        assert preference.selected_provider == "yfinance"


def test_market_data_settings_default_follows_provider_priority(
    client: TestClient, monkeypatch
) -> None:
    register(client, "source-priority@example.com")
    sources = [
        MarketDataSourceOption(
            name=name,
            label=name,
            available=True,
            status="connected",
        )
        for name in ("yfinance", "dhan", "angel_one")
    ]
    monkeypatch.setattr(
        market_data, "_available_market_data_sources", lambda db, user_id: sources
    )

    response = client.get("/api/v1/market-data/settings")

    assert response.status_code == 200, response.text
    assert response.json()["selected_provider"] == "dhan"


def test_history_sync_is_idempotent_and_returns_provenance_and_freshness(
    client: TestClient, test_engine
) -> None:
    register(client)
    instruments = client.get("/api/v1/market-data/instruments?query=ALPHA").json()
    instrument_id = next(item["id"] for item in instruments if item["venue"] == "DEMO_US")
    url = (
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync"
        "?interval=1d&start=2026-01-01T00:00:00Z&end=2026-01-04T00:00:00Z&limit=10"
    )

    first = client.post(url)
    second = client.post(url)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(first.json()["bars"]) == 3
    assert second.json()["bars"] == first.json()["bars"]
    assert first.json()["freshness"]["status"] == "fresh"
    assert first.json()["freshness"]["source"] == "recorded"
    assert first.json()["freshness"]["is_synthetic"] is False
    assert first.json()["freshness"]["latest_bar_timestamp"] == "2026-01-03T00:00:00Z"
    assert isinstance(first.json()["provider_attempts"], list)
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketBar)) == 3
        assert session.scalar(select(func.count()).select_from(MarketDataSync)) == 1


def test_record_sync_attempt_rejects_unknown_reason_category(test_engine) -> None:
    with Session(test_engine) as session:
        with pytest.raises(ValueError, match="reason_category must be a ReasonCategory"):
            market_data._record_sync_attempt(
                session,
                instrument_id=uuid4(),
                interval="1d",
                provider="angel_one",
                start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end=datetime(2026, 1, 2, tzinfo=timezone.utc),
                limit=10,
                response_status=500,
                rows_returned=0,
                rows_written=0,
                outcome="unavailable",
                reason="provider_request_failed",
                reason_category="unknown_category",  # type: ignore[arg-type]
                attempted_at=datetime.now(timezone.utc),
            )


def test_history_request_bounds_and_timezone_are_validated(client: TestClient) -> None:
    register(client)
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    base_url = f"/api/v1/market-data/instruments/{instrument_id}/history/sync"

    naive = client.post(
        base_url,
        params={"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00", "interval": "1d"},
    )
    too_long = client.post(
        base_url,
        params={
            "start": "2024-01-01T00:00:00Z",
            "end": "2026-01-01T00:00:00Z",
            "interval": "1h",
        },
    )
    too_many = client.post(
        base_url,
        params={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
            "interval": "1d",
            "limit": 501,
        },
    )

    assert naive.status_code == 422
    assert too_long.status_code == 200
    assert too_many.status_code == 422


def test_removed_25_minute_interval_is_rejected(
    client: TestClient, test_engine
) -> None:
    register(client, "removed-interval@example.com")
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]

    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "25m",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
        },
    )

    assert response.status_code == 422
    with Session(test_engine) as session:
        assert session.scalar(
            select(func.count()).select_from(MarketDataSync)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(MarketDataSyncAttempt)
        ) == 0


def test_unsupported_provider_interval_and_lookback_are_reported_without_requests(
    client: TestClient,
) -> None:
    register(client, "provider-capabilities@example.com")
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]

    class YahooMustNotFetch(RecordedMarketDataProvider):
        name = "yfinance"
        is_mock = False

        def get_historical_bars(self, *_args, **_kwargs):
            raise AssertionError("unsupported Yahoo history must be skipped")

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [YahooMustNotFetch()]
    )
    recent_end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    old_start = recent_end - timedelta(days=15)
    lookback = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1m",
            "start": old_start.isoformat(),
            "end": recent_end.isoformat(),
        },
    )
    unsupported = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "5m",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
        },
    )

    assert lookback.status_code == 422
    assert lookback.json()["detail"]["stage"] == "provider_capability_check"
    assert lookback.json()["detail"]["providers"][0]["reason"] == (
        "yfinance_1m_lookback_exceeded_7_days"
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["providers"][0]["reason"] == "unsupported_interval"


def test_stored_history_is_unavailable_before_fetch_and_supports_pagination(
    client: TestClient,
) -> None:
    register(client)
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    url = f"/api/v1/market-data/instruments/{instrument_id}/history"

    missing = client.get(url, params={"interval": "1d"})
    synced = client.post(
        f"{url}/sync",
        params={
            "interval": "1d",
            "start": "2026-02-01T00:00:00Z",
            "end": "2026-02-04T00:00:00Z",
        },
    )
    page = client.get(url, params={"interval": "1d", "offset": 1, "limit": 1})

    assert missing.status_code == 200
    assert missing.json()["freshness"]["status"] == "never_fetched"
    assert synced.status_code == 200
    assert len(page.json()["bars"]) == 1
    assert page.json()["offset"] == 1


def test_provider_failure_is_safely_reported_and_freshness_unavailable(client: TestClient) -> None:
    register(client)

    class FailingProvider(RecordedMarketDataProvider):
        def get_historical_bars(self, instrument, interval, start, end, limit):
            raise MarketDataProviderError("secret_token_must_not_be_returned")

    app.dependency_overrides[get_market_data_provider] = FailingProvider
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    sync = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-03-03T00:00:00Z",
        },
    )
    history = client.get(
        f"/api/v1/market-data/instruments/{instrument_id}/history",
        params={"interval": "1d"},
    )

    assert sync.status_code == 503
    assert "secret_token" not in sync.text
    assert history.json()["freshness"]["status"] == "unavailable"


def test_quote_and_history_fall_back_to_yahoo_without_mixing_sources(
    client: TestClient,
) -> None:
    register(client, "provider-fallback@example.com")
    instrument_id = client.get(
        "/api/v1/market-data/instruments?query=ALPHA"
    ).json()[0]["id"]
    stale_timestamp = datetime.now(timezone.utc) - timedelta(minutes=10)

    class StaleAngelProvider:
        name = "angel_one"
        is_mock = False

        def search_instruments(self, query, limit):
            return [InstrumentCandidate(
                provider=self.name,
                provider_instrument_id="ANGEL-ALPHA",
                provider_symbol="ALPHA",
                symbol="ALPHA",
                display_name="Alpha Fixture Corporation",
                asset_class="equity",
                venue="DEMO_US",
                currency="USD",
                country="US",
                timezone="UTC",
                is_synthetic=False,
                is_verified=True,
            )]

        def get_latest_quote(self, instrument):
            return HistoricalBar(
                timestamp=stale_timestamp,
                open="9",
                high="11",
                low="8",
                close="10",
                volume="100",
            )

        def get_historical_bars(self, instrument, interval, start, end, limit):
            raise MarketDataProviderError("provider_network_error")

    class UnavailableDhanProvider:
        name = "dhan"
        is_mock = False

        def search_instruments(self, query, limit):
            raise MarketDataProviderError("provider_network_error")

    class FreshYahooProvider:
        name = "yfinance"
        is_mock = False

        def get_latest_quote(self, instrument):
            return HistoricalBar(
                timestamp=datetime.now(timezone.utc),
                open="10",
                high="12",
                low="9",
                close="11",
                volume="120",
            )

        def get_historical_bars(self, instrument, interval, start, end, limit):
            return [HistoricalBar(
                timestamp=start,
                open="10",
                high="12",
                low="9",
                close="11",
                volume="120",
            )]

    chain = MarketDataProviderChain([
        StaleAngelProvider(),
        UnavailableDhanProvider(),
        FreshYahooProvider(),
    ])
    app.dependency_overrides[get_market_data_provider] = lambda: chain

    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")
    start = datetime.now(timezone.utc) - timedelta(days=2)
    end = datetime.now(timezone.utc)
    history = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )

    assert quote.status_code == 200, quote.text
    assert quote.json()["source"] == "yfinance"
    assert quote.json()["freshness_status"] == "historical"
    assert [attempt["outcome"] for attempt in quote.json()["provider_attempts"]] == [
        "unavailable",
        "stale",
        "stale",
    ]
    assert quote.json()["fallback_reason"] == "provider_network_error"
    assert history.status_code == 200, history.text
    assert history.json()["freshness"]["source"] == "yfinance"
    assert {bar["source"] for bar in history.json()["bars"]} == {"yfinance"}
    assert [attempt["outcome"] for attempt in history.json()["provider_attempts"]] == [
        "unavailable",
        "unavailable",
        "used",
    ]


@pytest.mark.parametrize(
    ("success_provider", "expected_source", "expected_calls"),
    [
        (
            "angel_one",
            "angel_one",
            ["dhan:quote", "angel_one:quote"],
        ),
        (
            "dhan",
            "dhan",
            ["dhan:quote"],
        ),
        (
            "yfinance",
            "yfinance",
            ["dhan:quote", "angel_one:quote", "yfinance:quote"],
        ),
    ],
)
def test_quote_router_uses_fixed_priority_and_stops_after_success(
    client: TestClient,
    monkeypatch,
    success_provider: str,
    expected_source: str,
    expected_calls: list[str],
) -> None:
    register(client, f"provider-order-{success_provider}@example.com")
    instrument_data = client.get(
        "/api/v1/market-data/instruments?query=ALPHA"
    ).json()[0]
    instrument_id = instrument_data["id"]
    call_order: list[str] = []
    monkeypatch.setattr(
        market_data,
        "_provider_circuit_breaker",
        ProviderCircuitBreaker(threshold=3, cooldown_seconds=30),
    )

    class TestProvider:
        is_mock = False

        def __init__(self, name: str):
            self.name = name

        def search_instruments(self, _query, _limit):
            return [InstrumentCandidate(
                provider=self.name,
                provider_instrument_id=f"{self.name}-ALPHA",
                provider_symbol="ALPHA",
                symbol="ALPHA",
                display_name=instrument_data["display_name"],
                asset_class=instrument_data["asset_class"],
                venue=instrument_data["venue"],
                currency=instrument_data["currency"],
                country=instrument_data["country"],
                timezone=instrument_data["timezone"],
                is_synthetic=False,
                is_verified=True,
            )]

        def get_latest_quote(self, _candidate):
            call_order.append(f"{self.name}:quote")
            if self.name != success_provider:
                raise MarketDataProviderError("provider_request_failed")
            return HistoricalBar(
                timestamp=datetime.now(timezone.utc),
                open="10",
                high="12",
                low="9",
                close="11",
                volume="100",
            )

    providers = [
        TestProvider("yfinance"),
        TestProvider("dhan"),
        TestProvider("angel_one"),
    ]
    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(providers)

    response = client.get(
        f"/api/v1/market-data/instruments/{instrument_id}/quote",
        params={"refresh": "true"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["source"] == expected_source
    assert call_order == expected_calls


def test_quote_router_reports_all_provider_failures_without_fabricated_quote(
    client: TestClient, monkeypatch
) -> None:
    register(client, "all-providers-failed@example.com")
    instrument_data = client.get(
        "/api/v1/market-data/instruments?query=ALPHA"
    ).json()[0]
    call_order: list[str] = []
    monkeypatch.setattr(
        market_data,
        "_provider_circuit_breaker",
        ProviderCircuitBreaker(threshold=3, cooldown_seconds=30),
    )

    class FailedProvider:
        is_mock = False

        def __init__(self, name: str):
            self.name = name

        def search_instruments(self, _query, _limit):
            return [InstrumentCandidate(
                provider=self.name,
                provider_instrument_id=f"{self.name}-ALPHA",
                provider_symbol="ALPHA",
                symbol="ALPHA",
                display_name=instrument_data["display_name"],
                asset_class=instrument_data["asset_class"],
                venue=instrument_data["venue"],
                currency=instrument_data["currency"],
                country=instrument_data["country"],
                timezone=instrument_data["timezone"],
                is_synthetic=False,
                is_verified=True,
            )]

        def get_latest_quote(self, _candidate):
            call_order.append(f"{self.name}:quote")
            raise MarketDataProviderError("provider_request_failed")

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain([
        FailedProvider("yfinance"),
        FailedProvider("dhan"),
        FailedProvider("angel_one"),
    ])

    response = client.get(
        f"/api/v1/market-data/instruments/{instrument_data['id']}/quote",
        params={"refresh": "true"},
    )

    assert response.status_code == 503
    assert call_order == [
        "dhan:quote", "angel_one:quote", "yfinance:quote",
    ]
    attempts = response.json()["detail"]["provider_attempts"]
    assert [item["provider"] for item in attempts] == [
        "dhan", "angel_one", "yfinance"
    ]
    assert all(item["reason_category"] == "provider_request_failed" for item in attempts)


def test_provider_circuit_breaker_records_bounded_rate_limit_cooldown() -> None:
    current_time = [10.0]
    breaker = ProviderCircuitBreaker(
        threshold=3,
        cooldown_seconds=45,
        clock=lambda: current_time[0],
    )

    breaker.record_failure("angel_one", cooldown_seconds=30)
    assert breaker.before_attempt("angel_one") == "circuit_breaker_open"
    current_time[0] += 30
    assert breaker.before_attempt("angel_one") is None
    assert breaker.before_attempt("angel_one") == "circuit_breaker_probe_in_progress"
    breaker.record_success("angel_one")
    assert breaker.before_attempt("angel_one") is None


def test_history_provider_rejects_duplicate_or_unsorted_bars() -> None:
    timestamp = datetime(2026, 1, 5, tzinfo=timezone.utc)
    candidate = InstrumentCandidate(
        provider="angel_one",
        provider_instrument_id="ANGEL-ALPHA",
        provider_symbol="ALPHA",
        symbol="ALPHA",
        display_name="Alpha",
        asset_class="equity",
        venue="NSE",
        currency="INR",
        country="IN",
        timezone="Asia/Kolkata",
        is_synthetic=False,
    )

    class MalformedProvider:
        name = "angel_one"
        is_mock = False

        def get_historical_bars(self, *_args):
            return [
                HistoricalBar(
                    timestamp=timestamp,
                    open="10",
                    high="11",
                    low="9",
                    close="10",
                    volume="100",
                ),
                HistoricalBar(
                    timestamp=timestamp,
                    open="10",
                    high="11",
                    low="9",
                    close="10",
                    volume="100",
                ),
            ]

    with pytest.raises(MarketDataProviderError) as error:
        market_data._fetch_provider_history(
            MalformedProvider(),
            candidate,
            "1d",
            timestamp - timedelta(days=1),
            timestamp + timedelta(days=1),
            10,
        )
    assert error.value.code == "provider_returned_duplicate_or_unsorted_bars"


def test_dhan_history_failure_falls_back_to_angel_one_and_reports_provenance(
    client: TestClient, test_engine, caplog
) -> None:
    register(client, "dhan-history-fallback@example.com")
    instrument_data = client.get(
        "/api/v1/market-data/instruments?query=ALPHA"
    ).json()[0]
    instrument_id = instrument_data["id"]
    added = client.post(
        f"/api/v1/market-data/watchlist/{instrument_id}",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token", "")},
    )
    assert added.status_code == 204, added.text

    class DhanHistoryFailure:
        name = "dhan"
        is_mock = False

        def search_instruments(self, _query, _limit):
            return [InstrumentCandidate(
                provider=self.name,
                provider_instrument_id="verified-dhan-alpha",
                provider_symbol="ALPHA",
                symbol="ALPHA",
                display_name=instrument_data["display_name"],
                asset_class="equity",
                venue=instrument_data["venue"],
                currency=instrument_data["currency"],
                country="US",
                timezone="UTC",
                is_synthetic=False,
                is_verified=True,
            )]

        def get_historical_bars(
            self, _instrument, _interval, _start, _end, _limit
        ):
            raise MarketDataProviderError("provider_request_failed")

    class AngelHistoryProvider:
        name = "angel_one"
        is_mock = False

        def search_instruments(self, _query, _limit):
            return [InstrumentCandidate(
                provider=self.name,
                provider_instrument_id="verified-angel-alpha",
                provider_symbol="ALPHA",
                symbol="ALPHA",
                display_name=instrument_data["display_name"],
                asset_class="equity",
                venue=instrument_data["venue"],
                currency=instrument_data["currency"],
                country="US",
                timezone="UTC",
                is_synthetic=False,
                is_verified=True,
            )]

        def get_historical_bars(
            self, _instrument, _interval, start, _end, _limit
        ):
            return [
                HistoricalBar(
                    timestamp=start + timedelta(days=index),
                    open=Decimal("10"),
                    high=Decimal("12"),
                    low=Decimal("9"),
                    close=Decimal("11"),
                    volume=Decimal("100"),
                )
                for index in range(2)
            ]

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [AngelHistoryProvider(), DhanHistoryFailure()]
    )
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    end = start + timedelta(days=2)
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["freshness"]["source"] == "angel_one"
    assert {bar["source"] for bar in payload["bars"]} == {"angel_one"}
    assert [item["outcome"] for item in payload["provider_attempts"]] == [
        "unavailable",
        "used",
    ]
    assert [
        item["reason_category"] for item in payload["provider_attempts"]
    ] == ["provider_request_failed", None]
    assert any(
        record.getMessage() == "market_data_provider_failure"
        and getattr(record, "provider", None) == "dhan"
        and getattr(record, "error_category", None) == "provider_request_failed"
        for record in caplog.records
    )

    with Session(test_engine) as session:
        sync = session.scalar(
            select(MarketDataSync).where(
                MarketDataSync.instrument_id == UUID(instrument_id),
                MarketDataSync.source == "angel_one",
            )
        )
        attempts = list(session.scalars(
            select(MarketDataSyncAttempt).where(
                MarketDataSyncAttempt.instrument_id == UUID(instrument_id)
            )
        ))
        bars = list(session.scalars(
            select(MarketBar).where(
                MarketBar.instrument_id == UUID(instrument_id)
            )
        ))
        assert sync is not None
        assert sync.status == "success"
        assert sync.safe_reason == "fallback_after_dhan_provider_request_failed"
        assert any(
            item.provider == "dhan"
            and item.reason == "provider_request_failed"
            and item.reason_category == "provider_request_failed"
            for item in attempts
        )
        assert next(
            item for item in attempts if item.provider == "angel_one"
        ).reason_category is None
        assert {bar.source for bar in bars} == {"angel_one"}

    coverage = client.get("/api/v1/market-data/coverage")
    assert coverage.status_code == 200, coverage.text
    row = next(
        item for item in coverage.json()["instruments"]
        if item["instrument_id"] == instrument_id
    )
    assert row["last_sync_provider"] == "angel_one"
    assert row["last_sync_reason"] == "fallback_after_dhan_provider_request_failed"


def test_history_sync_records_all_provider_failures_without_exposing_details(
    client: TestClient, test_engine
) -> None:
    register(client, "sync-all-providers-fail@example.com")
    instrument_id = client.get(
        "/api/v1/market-data/instruments?query=ALPHA"
    ).json()[0]["id"]
    call_order: list[str] = []

    class FailingProvider:
        is_mock = False

        def __init__(self, name: str, error_code: str) -> None:
            self.name = name
            self.error_code = error_code

        def search_instruments(self, _query: str, _limit: int):
            call_order.append(self.name)
            raise MarketDataProviderError(self.error_code)

        def get_historical_bars(
            self, _instrument, _interval, _start, _end, _limit
        ):
            call_order.append(self.name)
            raise MarketDataProviderError(self.error_code)

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [
            FailingProvider("yfinance", "provider_request_failed"),
            FailingProvider("dhan", "provider_request_failed"),
            FailingProvider("angel_one", "provider_response_too_large"),
        ]
    )
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": start.isoformat(),
            "end": (start + timedelta(days=2)).isoformat(),
        },
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["stage"] == "provider_history_fetch"
    assert [
        item["reason"] for item in response.json()["detail"]["providers"]
    ] == [
        "provider_request_failed",
        "provider_response_too_large",
        "provider_request_failed",
    ]
    assert call_order == ["dhan", "angel_one", "yfinance"]
    with Session(test_engine) as session:
        attempts = list(session.scalars(
            select(MarketDataSyncAttempt).where(
                MarketDataSyncAttempt.instrument_id == UUID(instrument_id)
            ).order_by(MarketDataSyncAttempt.attempted_at)
        ))
        assert [item.provider for item in attempts] == [
            "dhan", "angel_one", "yfinance"
        ]
        assert [item.reason_category for item in attempts] == [
            "provider_request_failed",
            "provider_response_too_large",
            "provider_request_failed",
        ]
        assert all(
            item.reason_category in {category.value for category in ReasonCategory}
            for item in attempts
        )


def test_history_sync_falls_back_through_yfinance_and_records_categories(
    client: TestClient, test_engine
) -> None:
    register(client, "sync-yfinance-fallback@example.com")
    instrument_id = client.get(
        "/api/v1/market-data/instruments?query=ALPHA"
    ).json()[0]["id"]
    call_order: list[str] = []

    class SearchFailureProvider:
        is_mock = False

        def __init__(self, name: str, error_code: str) -> None:
            self.name = name
            self.error_code = error_code

        def search_instruments(self, _query: str, _limit: int):
            call_order.append(self.name)
            raise MarketDataProviderError(self.error_code)

    class YahooHistoryProvider:
        name = "yfinance"
        is_mock = False

        def get_historical_bars(
            self, _instrument, _interval, start, _end, _limit
        ):
            call_order.append(self.name)
            return [
                HistoricalBar(
                    timestamp=start + timedelta(days=index),
                    open=Decimal("10"),
                    high=Decimal("12"),
                    low=Decimal("9"),
                    close=Decimal("11"),
                    volume=Decimal("100"),
                )
                for index in range(2)
            ]

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [
            SearchFailureProvider("dhan", "provider_request_failed"),
            SearchFailureProvider("angel_one", "provider_response_too_large"),
            YahooHistoryProvider(),
        ]
    )
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": start.isoformat(),
            "end": (start + timedelta(days=2)).isoformat(),
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["freshness"]["source"] == "yfinance"
    assert {bar["source"] for bar in payload["bars"]} == {"yfinance"}
    assert [
        (attempt["provider"], attempt["outcome"], attempt["reason_category"])
        for attempt in payload["provider_attempts"]
    ] == [
        ("dhan", "unavailable", "provider_request_failed"),
        ("angel_one", "unavailable", "provider_response_too_large"),
        ("yfinance", "used", None),
    ]
    assert call_order == ["dhan", "angel_one", "yfinance"]
    with Session(test_engine) as session:
        attempts = list(session.scalars(
            select(MarketDataSyncAttempt).where(
                MarketDataSyncAttempt.instrument_id == UUID(instrument_id)
            )
        ))
        assert {item.provider: item.reason_category for item in attempts} == {
            "angel_one": "provider_response_too_large",
            "dhan": "provider_request_failed",
            "yfinance": None,
        }


def test_history_reports_unsupported_provider_interval(client: TestClient) -> None:
    register(client, "provider-interval@example.com")
    instrument_id = client.get(
        "/api/v1/market-data/instruments?query=ALPHA"
    ).json()[0]["id"]

    class YahooOnlyProvider:
        name = "yfinance"
        is_mock = False

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [YahooOnlyProvider()]
    )
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "5m",
            "start": "2026-04-01T00:00:00Z",
            "end": "2026-04-01T01:00:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["providers"] == [
        {"provider": "yfinance", "reason": "unsupported_interval"}
    ]


def test_malformed_provider_bars_are_rejected_without_partial_storage(
    client: TestClient, test_engine
) -> None:
    register(client, "malformed-market@example.com")

    class MalformedProvider(RecordedMarketDataProvider):
        def get_historical_bars(self, instrument, interval, start, end, limit):
            return [
                {
                    "timestamp": start,
                    "open": "10",
                    "high": "9",
                    "low": "8",
                    "close": "9",
                    "volume": "1",
                }
            ]

    app.dependency_overrides[get_market_data_provider] = MalformedProvider
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-03-03T00:00:00Z",
        },
    )

    assert response.status_code == 503
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketBar)) == 0
        sync = session.scalar(select(MarketDataSync))
        assert sync is not None
        assert sync.status == "unavailable"
        assert sync.safe_error_code == "provider_unavailable_or_invalid_response"


def test_provider_mapping_lookup_and_quote_are_instrument_specific(client: TestClient) -> None:
    register(client)
    instruments = client.get("/api/v1/market-data/instruments?query=ALPHA").json()
    us_instrument = next(item for item in instruments if item["venue"] == "DEMO_US")

    response = client.get(f"/api/v1/market-data/instruments/{us_instrument['id']}/quote")

    assert response.status_code == 200, response.text
    assert response.json()["symbol"] == "ALPHA"
    assert response.json()["currency"] == "USD"
    assert response.json()["is_synthetic"] is False
    assert response.json()["freshness"] == "stale"
    assert response.json()["freshness_status"] == "unverified"
    assert response.json()["delivery_mode"] == "on_demand_rest"
    assert response.json()["stream_status"] == "not_configured"
    assert response.json()["provider_configured"] is True
    assert response.json()["provider_request_status"] == "succeeded"
    assert response.json()["provider_connection_status"] == "not_applicable_rest"
    assert response.json()["feed_active"] is False
    assert response.json()["is_cached"] is False
    assert response.json()["is_verified"] is False


def test_provider_mapping_conflict_does_not_poison_session(client: TestClient, test_engine) -> None:
    register(client)
    instruments = client.get("/api/v1/market-data/instruments?query=ALPHA").json()
    us_id = next(item["id"] for item in instruments if item["venue"] == "DEMO_US")
    indian_id = next(item["id"] for item in instruments if item["venue"] == "DEMO_IN")

    class YahooProvider:
        name = "yfinance"
        is_mock = False

    with Session(test_engine) as session:
        us_instrument = session.scalar(select(Instrument).where(Instrument.venue == "DEMO_US"))
        indian_instrument = session.scalar(
            select(Instrument).where(Instrument.venue == "DEMO_IN")
        )
        assert us_instrument is not None
        assert indian_instrument is not None
        assert str(us_instrument.id) == us_id
        assert str(indian_instrument.id) == indian_id
        session.add(
            InstrumentProviderIdentifier(
                instrument_id=us_instrument.id,
                provider="yfinance",
                provider_instrument_id="ALPHA|DEMO_IN",
                provider_symbol="ALPHA",
                is_verified=False,
                last_seen_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

        with pytest.raises(MarketDataProviderError, match="provider_mapping_conflict"):
            _resolve_provider_candidate(session, indian_instrument, YahooProvider())

        assert session.scalar(
            select(func.count(InstrumentProviderIdentifier.id)).where(
                InstrumentProviderIdentifier.provider == "yfinance"
            )
        ) == 1
        session.commit()


def test_dhan_snapshot_quote_is_verified_persisted_and_attributed(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "dhan-snapshot@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)
    instrument = next(
        item
        for item in client.get("/api/v1/market-data/instruments?query=ALPHA").json()
        if item["venue"] == "DEMO_IN"
    )
    use_dhan_market_data_provider()
    now = datetime.now(timezone.utc)
    quote_requests = 0
    monkeypatch.setattr(
        DhanAdapter,
        "find_verified_equity_mapping",
        lambda _self, _symbol, _venue: ("11536", "NSE_EQ"),
    )

    def get_market_quote(_self, _token, _tokens, _segment, _security_id):
        nonlocal quote_requests
        quote_requests += 1
        return HistoricalBar(
            timestamp=now,
            open=Decimal("3890"),
            high=Decimal("3910"),
            low=Decimal("3880"),
            close=Decimal("3900.25"),
            volume=Decimal("1200"),
        )

    monkeypatch.setattr(DhanAdapter, "get_market_quote", get_market_quote)
    url = f"/api/v1/market-data/instruments/{instrument['id']}/quote"

    first = client.get(url)
    cached = client.get(url)

    assert first.status_code == 200, first.text
    assert first.json()["source"] == "dhan"
    assert first.json()["data_type"] == "provider_snapshot"
    assert first.json()["delivery_mode"] == "rest_snapshot"
    assert first.json()["stream_status"] == "not_configured"
    assert first.json()["is_verified"] is True
    assert first.json()["bar"]["close"] == "3900.25000000"
    assert cached.json()["is_cached"] is True
    assert quote_requests == 1
    with Session(test_engine) as session:
        stored_quote = session.scalar(
            select(MarketQuote).where(MarketQuote.source == "dhan")
        )
        mapping = session.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.provider == "dhan"
            )
        )
        assert stored_quote is not None
        assert stored_quote.timestamp == now.replace(tzinfo=None)
        assert stored_quote.retrieved_at is not None
        assert mapping is not None
        assert mapping.provider_instrument_id == "11536"
        assert mapping.is_verified is True


def test_dhan_history_sync_persists_verified_bars_and_reuses_cached_range(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "dhan-history@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)
    with Session(test_engine) as session:
        instrument = Instrument(
            symbol="SBIN",
            display_name="State Bank of India",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(instrument)
        session.flush()
        session.add(
            InstrumentProviderIdentifier(
                instrument_id=instrument.id,
                provider="dhan",
                provider_instrument_id="3045|NSE_EQ",
                provider_symbol="SBIN",
                is_verified=True,
                last_seen_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        instrument_id = instrument.id
    use_dhan_market_data_provider()
    timestamp = datetime(2026, 1, 5, tzinfo=timezone.utc)
    requests = []
    monkeypatch.setattr("app.broker_adapters._wait_for_dhan_market_data", lambda: None)

    def get_historical_bars(
        _self, segment, security_id, interval, start, end
    ):
        requests.append((start, end))
        assert segment == "NSE_EQ"
        assert security_id == "3045"
        assert interval == "1d"
        return [HistoricalBar(
            timestamp=timestamp,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1200"),
        )]

    monkeypatch.setattr(
        market_data.DhanV2Adapter,
        "get_historical_bars",
        get_historical_bars,
    )
    url = f"/api/v1/market-data/instruments/{instrument_id}/history/sync"
    params = {
        "interval": "1d",
        "start": "2026-01-05T00:00:00Z",
        "end": "2026-01-06T00:00:00Z",
        "limit": 10,
    }

    first = client.post(url, params=params)
    second = client.post(url, params=params)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["bars"][0]["source"] == "dhan"
    assert first.json()["bars"][0]["close"] == "101.00000000"
    stored_timestamp = datetime.fromisoformat(
        first.json()["bars"][0]["timestamp"].replace("Z", "+00:00")
    )
    retrieved_timestamp = datetime.fromisoformat(
        first.json()["bars"][0]["retrieved_at"].replace("Z", "+00:00")
    )
    if stored_timestamp.tzinfo is None:
        stored_timestamp = stored_timestamp.replace(tzinfo=timezone.utc)
    if retrieved_timestamp.tzinfo is None:
        retrieved_timestamp = retrieved_timestamp.replace(tzinfo=timezone.utc)
    assert stored_timestamp == timestamp
    assert retrieved_timestamp > stored_timestamp
    assert second.json()["bars"] == first.json()["bars"]
    assert len(requests) == 1


def test_dhan_v2_intraday_epoch_is_parsed_as_utc_for_option_candles(monkeypatch) -> None:
    adapter = market_data.DhanV2Adapter({"access_token": "test-token"})
    timestamp = int(datetime(2026, 10, 26, 3, 46, tzinfo=timezone.utc).timestamp())
    requests = []

    def post_payload(path, body):
        requests.append((path, body))
        return {
            "open": [100],
            "high": [102],
            "low": [99],
            "close": [101],
            "volume": [1200],
            "timestamp": [timestamp],
        }

    monkeypatch.setattr(adapter, "_post_payload", post_payload)
    bars = adapter.get_historical_bars(
        "NSE_EQ",
        "3045",
        "5m",
        datetime(2026, 10, 26, 3, 45, tzinfo=timezone.utc),
        datetime(2026, 10, 26, 3, 50, tzinfo=timezone.utc),
    )

    assert requests[0][0] == "/charts/intraday"
    assert requests[0][1]["interval"] == "5"
    assert bars[0].timestamp == datetime(2026, 10, 26, 3, 46, tzinfo=timezone.utc)
    assert bars[0].timestamp.astimezone(ZoneInfo("Asia/Kolkata")) == datetime(
        2026, 10, 26, 9, 16, tzinfo=ZoneInfo("Asia/Kolkata")
    )


def test_dhan_daily_history_requests_are_chunked_to_365_days(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "dhan-history-chunks@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)
    with Session(test_engine) as session:
        instrument = Instrument(
            symbol="SBIN",
            display_name="State Bank of India",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(instrument)
        session.flush()
        session.add(InstrumentProviderIdentifier(
            instrument_id=instrument.id,
            provider="dhan",
            provider_instrument_id="3045|NSE_EQ",
            provider_symbol="SBIN",
            is_verified=True,
            last_seen_at=datetime.now(timezone.utc),
        ))
        session.commit()
        instrument_id = instrument.id
    use_dhan_market_data_provider()
    requests = []
    monkeypatch.setattr("app.broker_adapters._wait_for_dhan_market_data", lambda: None)

    def get_historical_bars(_self, _segment, _security_id, _interval, start, end):
        requests.append((start, end))
        return [HistoricalBar(
            timestamp=start,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1200"),
        )]

    monkeypatch.setattr(
        market_data.DhanV2Adapter, "get_historical_bars", get_historical_bars
    )
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2024-01-01T00:00:00Z",
            "end": "2025-08-01T00:00:00Z",
            "limit": 10,
        },
    )

    assert response.status_code == 200, response.text
    assert len(requests) > 1
    assert all(end - start <= timedelta(days=365) for start, end in requests)


def test_dhan_empty_history_is_a_valid_empty_result(client: TestClient, test_engine, monkeypatch) -> None:
    register(client, "dhan-empty-history@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)
    with Session(test_engine) as session:
        instrument = Instrument(
            symbol="SBIN",
            display_name="State Bank of India",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(instrument)
        session.flush()
        session.add(InstrumentProviderIdentifier(
            instrument_id=instrument.id,
            provider="dhan",
            provider_instrument_id="3045|NSE_EQ",
            provider_symbol="SBIN",
            is_verified=True,
            last_seen_at=datetime.now(timezone.utc),
        ))
        session.commit()
        instrument_id = instrument.id
    use_dhan_market_data_provider()
    monkeypatch.setattr("app.broker_adapters._wait_for_dhan_market_data", lambda: None)
    monkeypatch.setattr(
        market_data.DhanV2Adapter,
        "get_historical_bars",
        lambda *_args, **_kwargs: [],
    )

    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-01-05T00:00:00Z",
            "end": "2026-01-06T00:00:00Z",
            "limit": 10,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["bars"] == []
    assert response.json()["freshness"]["status"] == "unavailable"
    assert response.json()["freshness"]["source"] == "dhan"
    with Session(test_engine) as session:
        attempt = session.scalar(
            select(MarketDataSyncAttempt).where(
                MarketDataSyncAttempt.instrument_id == instrument_id
            )
        )
        assert attempt is not None
        assert attempt.outcome == "unchanged"
        assert attempt.reason_category == "closed_session_no_new_bars"


def test_dhan_expired_market_data_auth_preserves_broker_credential(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "dhan-expired-history@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)
    with Session(test_engine) as session:
        instrument = Instrument(
            symbol="SBIN",
            display_name="State Bank of India",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(instrument)
        session.flush()
        session.add(InstrumentProviderIdentifier(
            instrument_id=instrument.id,
            provider="dhan",
            provider_instrument_id="3045|NSE_EQ",
            provider_symbol="SBIN",
            is_verified=True,
            last_seen_at=datetime.now(timezone.utc),
        ))
        session.commit()
        instrument_id = instrument.id
    use_dhan_market_data_provider()
    monkeypatch.setattr("app.broker_adapters._wait_for_dhan_market_data", lambda: None)

    def expired_history(*_args, **_kwargs):
        raise BrokerReconnectRequired("private token-related detail")

    monkeypatch.setattr(
        market_data.DhanV2Adapter,
        "get_historical_bars",
        expired_history,
    )

    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-01-05T00:00:00Z",
            "end": "2026-01-06T00:00:00Z",
            "limit": 10,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["providers"] == [
        {"provider": "dhan", "reason": "authentication_expired"}
    ]
    assert "private token-related detail" not in response.text
    with Session(test_engine) as session:
        credential = session.scalar(
            select(BrokerCredential).where(BrokerCredential.broker == "dhan")
        )
        assert credential is not None
        assert credential.connected_at is not None
        assert credential.expires_at is not None
        credential_expiry = (
            credential.expires_at.replace(tzinfo=timezone.utc)
            if credential.expires_at.tzinfo is None
            else credential.expires_at
        )
        assert credential_expiry > datetime.now(timezone.utc)


def test_dhan_websocket_route_authenticates_persists_and_reports_live_ticks(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "dhan-stream@example.com")
    prepare_dhan_quote(client, test_engine, monkeypatch)
    instrument = next(
        item
        for item in client.get("/api/v1/market-data/instruments?query=ALPHA").json()
        if item["venue"] == "DEMO_IN"
    )
    use_dhan_market_data_provider()
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as session:
        user_session = session.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        user_session.user.two_factor_enabled = True
        user_session.two_factor_verified = True
        session.commit()
    monkeypatch.setattr(
        DhanAdapter,
        "find_verified_equity_mapping",
        lambda _self, _symbol, _venue: ("11536", "NSE_EQ"),
    )
    monkeypatch.setattr(
        market_data,
        "_market_session_state",
        lambda *_args, **_kwargs: "open",
    )

    async def join_feed(_channel, _group_id, _subscriptions, _token, _tokens):
        events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        events.put_nowait({"type": "status", "status": "active"})
        events.put_nowait({
            "type": "tick",
            "bar": HistoricalBar(
                timestamp=datetime.now(timezone.utc),
                open=Decimal("3890"),
                high=Decimal("3910"),
                low=Decimal("3880"),
                close=Decimal("3900.25"),
                volume=Decimal("1200"),
            ),
        })
        return events

    async def leave_feed(_channel, _group_id):
        return None

    monkeypatch.setattr(market_data.dhan_live_feed_hub, "join", join_feed)
    monkeypatch.setattr(market_data.dhan_live_feed_hub, "leave", leave_feed)
    with client.websocket_connect(
        f"/api/v1/market-data/instruments/{instrument['id']}/stream",
        headers={"Origin": settings.frontend_origin},
    ) as stream:
        messages = [stream.receive_json()]
        while not any(message.get("status") == "active" for message in messages):
            messages.append(stream.receive_json())
        tick = stream.receive_json()
        assert tick["type"] == "tick"
        assert tick["quote"]["source"] == "dhan_ws"
        assert tick["quote"]["data_type"] == "live_tick"
        assert tick["quote"]["delivery_mode"] == "websocket_tick"
        assert tick["quote"]["provider_connection_status"] == "connected"
        assert tick["quote"]["feed_active"] is True

    with Session(test_engine) as session:
        quote = session.scalar(
            select(MarketQuote).where(MarketQuote.source == "dhan_ws")
        )
        assert quote is not None
        assert quote.close == Decimal("3900.25000000")


def test_angel_one_stream_persists_verified_ticks_and_quote_api_reuses_them(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, "angel-stream@example.com")
    monkeypatch.setattr(settings, "angel_one_api_key", SecretStr("server-api-key"))
    monkeypatch.setattr(settings, "angel_one_client_public_ip", "203.0.113.10")
    monkeypatch.setattr(
        settings, "broker_encryption_key", SecretStr(Fernet.generate_key().decode())
    )
    instrument = next(
        item
        for item in client.get("/api/v1/market-data/instruments?query=ALPHA").json()
        if item["venue"] == "DEMO_IN"
    )
    with Session(test_engine) as session:
        fixture_instrument = session.scalar(
            select(Instrument).where(
                Instrument.id == UUID(instrument["id"]),
                Instrument.venue == "DEMO_IN",
            )
        )
        assert fixture_instrument is not None
        fixture_instrument.venue = "NSE"
        fixture_instrument.timezone = "Asia/Kolkata"
        session.commit()
    timestamp = datetime.now(timezone.utc)
    monkeypatch.setattr(
        market_data,
        "_market_session_state",
        lambda *_args, **_kwargs: "open",
    )
    with Session(test_engine) as session:
        session.add(InstrumentProviderIdentifier(
            instrument_id=UUID(instrument["id"]),
            provider="angel_one",
            provider_instrument_id="3045|NSE",
            provider_symbol="ALPHA-EQ",
            is_verified=True,
            last_seen_at=timestamp,
        ))
        session.commit()
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as session:
        user_session = session.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        user_session.user.two_factor_enabled = True
        user_session.two_factor_verified = True
        session.add(UserMarketDataSettings(
            user_id=user_session.user_id,
            selected_provider="angel_one",
            encrypted_angel_tokens=brokers.encrypt_tokens({
                "client_code": "client-1",
                "access_token": "jwt",
                "refresh_token": "refresh",
                "feed_token": "feed",
            }),
            angel_expires_at=timestamp + timedelta(hours=1),
            angel_connected_at=timestamp,
            updated_at=timestamp,
        ))
        session.commit()

    stream_attempts = 0

    async def stream_quote(_self, _instrument):
        nonlocal stream_attempts
        stream_attempts += 1
        if stream_attempts == 1:
            raise OSError("mock stream interruption")
        yield HistoricalBar(
            timestamp=timestamp,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("103"),
            volume=Decimal("1000"),
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(AngelOneProvider, "stream_quote", stream_quote)
    with client.websocket_connect(
        f"/api/v1/market-data/instruments/{instrument['id']}/stream",
        headers={"Origin": settings.frontend_origin},
    ) as stream:
        messages = [stream.receive_json()]
        while not any(message["type"] == "tick" for message in messages):
            messages.append(stream.receive_json())
        assert messages[0]["type"] == "status"
        assert messages[0]["status"] == "connecting"
        assert any(
            message.get("status") == "reconnecting" for message in messages
        )
        tick = next(message for message in messages if message["type"] == "tick")
        assert tick["quote"]["source"] == "angel_one_ws"
        assert tick["quote"]["data_type"] == "live_tick"
        assert tick["quote"]["delivery_mode"] == "websocket_tick"
        assert tick["quote"]["feed_active"] is True
        assert stream_attempts >= 2

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [AngelOneProvider("server-api-key", {
            "client_code": "client-1",
            "access_token": "jwt",
            "refresh_token": "refresh",
            "feed_token": "feed",
        })]
    )
    cached = client.get(f"/api/v1/market-data/instruments/{instrument['id']}/quote")
    assert cached.status_code == 200, cached.text
    assert cached.json()["source"] == "angel_one_ws"
    assert cached.json()["delivery_mode"] == "websocket_tick"
    assert cached.json()["freshness_status"] == "fresh"
    with Session(test_engine) as session:
        quote = session.scalar(
            select(MarketQuote).where(MarketQuote.source == "angel_one_ws")
        )
        mapping = session.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.provider == "angel_one"
            )
        )
        assert quote is not None and quote.close == Decimal("103.00000000")
        assert mapping is not None and mapping.is_verified is True


def test_quote_endpoint_uses_fresh_cache_without_requerying_provider(
    client: TestClient, test_engine
) -> None:
    register(client, "cached-quote@example.com")

    class CountingProvider(RecordedMarketDataProvider):
        quote_requests = 0

        def get_latest_quote(self, instrument):
            self.quote_requests += 1
            return super().get_latest_quote(instrument)

    provider = CountingProvider()
    app.dependency_overrides[get_market_data_provider] = lambda: provider
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    url = f"/api/v1/market-data/instruments/{instrument_id}/quote"

    first = client.get(url)
    second = client.get(url)

    assert first.status_code == second.status_code == 200
    assert first.json()["is_cached"] is False
    assert second.json()["is_cached"] is True
    assert second.json()["freshness"] == "stale"
    assert second.json()["freshness_status"] == "unverified"
    assert second.json()["provider_request_status"] == "not_attempted"
    assert first.json()["bar"] == second.json()["bar"]
    assert provider.quote_requests == 1
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketQuote)) == 1


def test_quote_cache_uses_market_timestamp_not_only_recent_retrieval(
    client: TestClient, test_engine
) -> None:
    register(client, "market-timestamp-age@example.com")
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    url = f"/api/v1/market-data/instruments/{instrument_id}/quote"
    assert client.get(url).status_code == 200

    with Session(test_engine) as session:
        quote = session.scalar(select(MarketQuote))
        assert quote is not None
        quote.timestamp = datetime.now(timezone.utc) - timedelta(minutes=2)
        quote.retrieved_at = datetime.now(timezone.utc)
        session.commit()

    class UnavailableProvider(RecordedMarketDataProvider):
        def get_latest_quote(self, instrument):
            raise MarketDataProviderError("provider_network_error")

    app.dependency_overrides[get_market_data_provider] = UnavailableProvider
    response = client.get(url)

    assert response.status_code == 200, response.text
    assert response.json()["is_cached"] is True
    assert response.json()["freshness"] == "stale"
    assert response.json()["freshness_status"] == "stale"
    assert response.json()["market_age_seconds"] >= 120


def test_quote_refresh_failure_returns_cached_quote_as_stale(
    client: TestClient, test_engine
) -> None:
    register(client, "stale-quote@example.com")
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    url = f"/api/v1/market-data/instruments/{instrument_id}/quote"
    initial = client.get(url)
    assert initial.status_code == 200

    with Session(test_engine) as session:
        quote = session.scalar(select(MarketQuote))
        assert quote is not None
        quote.retrieved_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()

    class FailingProvider(RecordedMarketDataProvider):
        def get_latest_quote(self, instrument):
            raise MarketDataProviderError("upstream failure with private details")

    app.dependency_overrides[get_market_data_provider] = FailingProvider
    response = client.get(url)

    assert response.status_code == 200
    assert response.json()["freshness"] == "stale"
    assert response.json()["is_cached"] is True
    assert response.json()["provider_request_status"] == "failed"
    assert "last cached quote" in response.json()["freshness_message"]
    assert "private details" not in response.text
    assert response.json()["bar"] == initial.json()["bar"]


def test_instrument_and_bar_models_reject_invalid_contracts_and_ohlc() -> None:
    option = InstrumentCandidate(
        provider="mock",
        provider_instrument_id="OPTION-TEST",
        provider_symbol="DEMO:OPT1-2027-100-C",
        symbol="OPT1",
        display_name="Demo Equity Call Option",
        asset_class="option",
        venue="DEMO",
        currency="USD",
        country="US",
        timezone="America/New_York",
        multiplier=Decimal("100"),
        expiry=date(2027, 3, 19),
        strike=Decimal("100"),
        option_type="call",
        is_synthetic=True,
    )
    assert option.asset_class == "option"
    assert option.strike == Decimal("100")

    with pytest.raises(ValidationError):
        InstrumentCandidate(
            provider="mock",
            provider_instrument_id="invalid",
            provider_symbol="BAD",
            symbol="BAD",
            display_name="Bad Option",
            asset_class="option",
            venue="DEMO",
            currency="USD",
            country="US",
            timezone="UTC",
            is_synthetic=True,
        )
    with pytest.raises(ValidationError):
        HistoricalBar(
            timestamp=datetime.now(timezone.utc),
            open=Decimal("10"),
            high=Decimal("9"),
            low=Decimal("8"),
            close=Decimal("9"),
            volume=Decimal("1"),
        )
    with pytest.raises(ValidationError):
        HistoricalBar(
            timestamp=datetime.now(),
            open=Decimal("10"),
            high=Decimal("12"),
            low=Decimal("8"),
            close=Decimal("11"),
            volume=Decimal("1"),
        )


def test_freshness_calculation_transitions_to_stale(client: TestClient, test_engine) -> None:
    register(client)
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]
    client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-04-01T00:00:00Z",
            "end": "2026-04-03T00:00:00Z",
        },
    )
    with Session(test_engine) as session:
        sync = session.scalar(select(MarketDataSync))
        assert sync is not None
        sync.succeeded_at = datetime.now(timezone.utc) - timedelta(days=3)
        session.commit()

    response = client.get(
        f"/api/v1/market-data/instruments/{instrument_id}/history",
        params={"interval": "1d"},
    )
    assert response.json()["freshness"]["status"] == "stale"


def test_rest_providers_reject_quotes_without_market_timestamps() -> None:
    candidate = InstrumentCandidate(
        provider="test",
        provider_instrument_id="TEST",
        provider_symbol="TEST",
        symbol="TEST",
        display_name="Timestamp Test",
        asset_class="equity",
        venue="TEST",
        currency="USD",
        country="US",
        timezone="UTC",
        is_synthetic=False,
    )

    class TimestamplessDelta(DeltaExchangeProvider):
        def _get_json(self, url, params):
            return {"result": {"close": "10"}}

    class TimestamplessBinance(BinanceProvider):
        def _get_json(self, url, params):
            return {"lastPrice": "10"}

    class TimestamplessUpstox(UpstoxProvider):
        def _authorized_json(self, url, params=None):
            return {
                "data": {
                    "TEST": {
                        "last_price": "10",
                        "ohlc": {},
                    }
                }
            }

    providers = [
        (TimestamplessDelta(), candidate),
        (TimestamplessBinance(), candidate),
        (TimestamplessUpstox("test-token"), candidate),
    ]
    for provider, instrument in providers:
        with pytest.raises(MarketDataProviderError) as error:
            provider.get_latest_quote(instrument)
        assert error.value.code == "provider_quote_timestamp_unavailable"
