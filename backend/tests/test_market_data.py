from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.market_data import get_market_data_provider
from app.db import Base, get_db
from app.main import app
from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
    MockMarketDataProvider,
)
from app.models import MarketBar, MarketDataSync, MarketQuote


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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(client: TestClient, email: str = "market@example.com") -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 202, response.text
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
            "is_synthetic": True,
            "relink_required": False,
            "quote": None,
        }
    ]

    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")
    assert quote.status_code == 200, quote.text
    entry = client.get("/api/v1/market-data/watchlist").json()[0]
    assert entry["quote"]["source"] == "mock"
    assert entry["quote"]["is_synthetic"] is True
    assert entry["quote"]["is_verified"] is False
    assert entry["quote"]["close"] == quote.json()["bar"]["close"]

    with TestClient(app) as other_client:
        register(other_client, "other-market@example.com")
        assert other_client.get("/api/v1/market-data/watchlist").json() == []
    assert len(client.get("/api/v1/market-data/watchlist").json()) == 1


def test_search_keeps_same_symbol_in_different_venues_distinct(client: TestClient) -> None:
    register(client)

    response = client.get("/api/v1/market-data/instruments?query=ALPHA&limit=10")

    assert response.status_code == 200, response.text
    instruments = response.json()
    assert len(instruments) == 2
    assert {item["symbol"] for item in instruments} == {"ALPHA"}
    assert {item["venue"] for item in instruments} == {"DEMO_US", "DEMO_IN"}
    assert len({item["id"] for item in instruments}) == 2
    assert all(item["is_synthetic"] for item in instruments)
    assert all(item["provider_identifiers"][0]["is_verified"] is False for item in instruments)


def test_mock_provider_reports_capabilities_and_synthetic_status(client: TestClient) -> None:
    register(client)

    response = client.get("/api/v1/market-data/provider")

    assert response.status_code == 200
    assert response.json()["provider"]["healthy"] is True
    assert response.json()["provider"]["is_mock"] is True
    assert "historical_ohlcv" in response.json()["provider"]["capabilities"]
    assert response.json()["configuration"] == "synthetic fixture provider"


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
    assert first.json()["freshness"]["source"] == "mock"
    assert first.json()["freshness"]["is_synthetic"] is True
    assert first.json()["freshness"]["latest_bar_timestamp"] == "2026-01-03T00:00:00Z"
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketBar)) == 3
        assert session.scalar(select(func.count()).select_from(MarketDataSync)) == 1


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
    assert too_long.status_code == 422
    assert too_many.status_code == 422


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

    class FailingProvider(MockMarketDataProvider):
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


def test_malformed_provider_bars_are_rejected_without_partial_storage(
    client: TestClient, test_engine
) -> None:
    register(client, "malformed-market@example.com")

    class MalformedProvider(MockMarketDataProvider):
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
    assert response.json()["is_synthetic"] is True
    assert response.json()["freshness"] == "fresh"
    assert response.json()["is_cached"] is False
    assert response.json()["is_verified"] is False


def test_quote_endpoint_uses_fresh_cache_without_requerying_provider(
    client: TestClient, test_engine
) -> None:
    register(client, "cached-quote@example.com")

    class CountingProvider(MockMarketDataProvider):
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
    assert second.json()["freshness"] == "fresh"
    assert first.json()["bar"] == second.json()["bar"]
    assert provider.quote_requests == 1
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketQuote)) == 1


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

    class FailingProvider(MockMarketDataProvider):
        def get_latest_quote(self, instrument):
            raise MarketDataProviderError("upstream failure with private details")

    app.dependency_overrides[get_market_data_provider] = FailingProvider
    response = client.get(url)

    assert response.status_code == 200
    assert response.json()["freshness"] == "stale"
    assert response.json()["is_cached"] is True
    assert "last cached quote" in response.json()["freshness_message"]
    assert "private details" not in response.text
    assert response.json()["bar"] == initial.json()["bar"]


def test_instrument_and_bar_models_reject_invalid_contracts_and_ohlc() -> None:
    provider = MockMarketDataProvider()
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
