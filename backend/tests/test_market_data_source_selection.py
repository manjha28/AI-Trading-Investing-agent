from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import auth, market_data
from app.api.market_data import MarketDataProviderChain, get_market_data_provider
from app.db import Base, get_db
from app.main import app
from app.market_data import HistoricalBar, InstrumentCandidate, MarketDataProviderError
from app.models import MarketBar, MarketQuote


@pytest.fixture(autouse=True)
def _isolate_provider_state(monkeypatch):
    monkeypatch.setattr(
        market_data, "_provider_circuit_breaker", market_data.ProviderCircuitBreaker()
    )
    monkeypatch.setattr(market_data, "_provider_next_request", {})


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


def register(client: TestClient, email: str) -> None:
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


class CallRecorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


def _candidate(provider: str, provider_instrument_id: str) -> InstrumentCandidate:
    return InstrumentCandidate(
        provider=provider,
        provider_instrument_id=provider_instrument_id,
        provider_symbol="DEMO_US:ALPHA",
        symbol="ALPHA",
        display_name="Alpha Fixture Corporation",
        asset_class="equity",
        venue="DEMO_US",
        currency="USD",
        country="US",
        timezone="UTC",
        is_synthetic=False,
        is_verified=True,
    )


class FixtureDhanProvider:
    name = "dhan"
    is_mock = False

    def __init__(self, recorder: CallRecorder, quote_timestamp: datetime) -> None:
        self.recorder = recorder
        self.quote_timestamp = quote_timestamp

    def search_instruments(self, query, limit):
        return [_candidate(self.name, "9001|NSE_EQ")]

    def get_latest_quote(self, instrument):
        self.recorder.calls.append("dhan:quote")
        return HistoricalBar(
            timestamp=self.quote_timestamp,
            open="9",
            high="11",
            low="8",
            close="10",
            volume="100",
        )

    def get_historical_bars(self, instrument, interval, start, end, limit):
        raise MarketDataProviderError("provider_network_error")


class FixtureYahooProvider:
    name = "yfinance"
    is_mock = False

    def __init__(self, recorder: CallRecorder) -> None:
        self.recorder = recorder

    def search_instruments(self, query, limit):
        return [_candidate(self.name, "FIXTURE-ALPHA-001")]

    def get_latest_quote(self, instrument):
        self.recorder.calls.append("yfinance:quote")
        return HistoricalBar(
            timestamp=datetime.now(timezone.utc),
            open="10",
            high="12",
            low="9",
            close="11",
            volume="120",
        )

    def get_historical_bars(self, instrument, interval, start, end, limit):
        self.recorder.calls.append("yfinance:history")
        return []


def _instrument_id(client: TestClient) -> str:
    return client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]


def test_selected_dhan_quote_never_substitutes_yfinance(
    client: TestClient, monkeypatch
) -> None:
    register(client, "selected-dhan@example.com")
    recorder = CallRecorder()
    stale_timestamp = datetime.now(timezone.utc) - timedelta(hours=2)
    chain = MarketDataProviderChain([
        FixtureDhanProvider(recorder, stale_timestamp),
        FixtureYahooProvider(recorder),
    ])
    chain.unavailable_providers = []
    app.dependency_overrides[get_market_data_provider] = lambda: chain
    monkeypatch.setattr(
        market_data, "_selected_market_data_source", lambda *_args, **_kwargs: "dhan"
    )
    instrument_id = _instrument_id(client)

    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")

    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert body["source"] != "yfinance"
    assert "yfinance:quote" not in recorder.calls
    outcomes = {attempt["provider"]: attempt for attempt in body["provider_attempts"]}
    assert outcomes["dhan"]["outcome"] == "stale"
    assert outcomes["yfinance"]["outcome"] == "skipped"
    assert outcomes["yfinance"]["reason"] == "not_selected_source"


def test_fresh_selected_dhan_quote_is_returned_as_dhan(
    client: TestClient, monkeypatch
) -> None:
    register(client, "fresh-dhan@example.com")
    recorder = CallRecorder()
    chain = MarketDataProviderChain([
        FixtureDhanProvider(recorder, datetime.now(timezone.utc)),
        FixtureYahooProvider(recorder),
    ])
    chain.unavailable_providers = []
    app.dependency_overrides[get_market_data_provider] = lambda: chain
    monkeypatch.setattr(
        market_data, "_selected_market_data_source", lambda *_args, **_kwargs: "dhan"
    )
    instrument_id = _instrument_id(client)

    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")

    assert quote.status_code == 200, quote.text
    assert quote.json()["source"] == "dhan"
    assert recorder.calls == ["dhan:quote"]


def test_yfinance_fallback_is_preserved_when_yfinance_is_not_dhan_selected(
    client: TestClient, monkeypatch
) -> None:
    register(client, "yahoo-selected@example.com")
    recorder = CallRecorder()
    stale_timestamp = datetime.now(timezone.utc) - timedelta(hours=2)
    chain = MarketDataProviderChain([
        FixtureDhanProvider(recorder, stale_timestamp),
        FixtureYahooProvider(recorder),
    ])
    chain.unavailable_providers = []
    app.dependency_overrides[get_market_data_provider] = lambda: chain
    monkeypatch.setattr(
        market_data, "_selected_market_data_source", lambda *_args, **_kwargs: "yfinance"
    )
    instrument_id = _instrument_id(client)

    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")

    assert quote.status_code == 200, quote.text
    assert quote.json()["source"] == "yfinance"
    assert recorder.calls == ["dhan:quote", "yfinance:quote"]


CSRF_HEADER = "X-CSRF-Token"


def _csrf(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get("csrf_token", "")}


def _seed_yfinance_history(test_engine, instrument_id: str, days: int = 40) -> None:
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with Session(test_engine) as session:
        for offset in range(1, days + 1):
            session.add(MarketBar(
                instrument_id=UUID(instrument_id),
                interval="1d",
                timestamp=now - timedelta(days=offset),
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=Decimal("11"),
                volume=Decimal("120"),
                source="yfinance",
                retrieved_at=now,
                is_synthetic=False,
            ))
        session.commit()


def _seed_dhan_history(test_engine, instrument_id: str, days: int = 40) -> None:
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with Session(test_engine) as session:
        for offset in range(3, days + 1):
            session.add(MarketBar(
                instrument_id=UUID(instrument_id),
                interval="1d",
                timestamp=now - timedelta(days=offset),
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=Decimal("11"),
                volume=Decimal("120"),
                source="dhan",
                retrieved_at=now,
                is_synthetic=False,
            ))
        session.commit()


def _seed_yfinance_quote(test_engine, instrument_id: str) -> None:
    now = datetime.now(timezone.utc)
    with Session(test_engine) as session:
        session.add(MarketQuote(
            instrument_id=UUID(instrument_id),
            timestamp=now,
            open=Decimal("10"),
            high=Decimal("12"),
            low=Decimal("9"),
            close=Decimal("11"),
            volume=Decimal("120"),
            source="yfinance",
            retrieved_at=now,
            is_synthetic=False,
        ))
        session.commit()


def _stored_yfinance_bar_count(test_engine, instrument_id: str) -> int:
    with Session(test_engine) as session:
        return len(session.scalars(
            select(MarketBar).where(
                MarketBar.instrument_id == UUID(instrument_id),
                MarketBar.source == "yfinance",
            )
        ).all())


def _select_dhan_with_failing_dhan_history(
    client: TestClient, monkeypatch, recorder: CallRecorder
) -> None:
    chain = MarketDataProviderChain([
        FixtureDhanProvider(recorder, datetime.now(timezone.utc)),
        FixtureYahooProvider(recorder),
    ])
    chain.unavailable_providers = []
    app.dependency_overrides[get_market_data_provider] = lambda: chain
    monkeypatch.setattr(
        market_data, "_selected_market_data_source", lambda *_args, **_kwargs: "dhan"
    )


def test_dhan_selected_history_never_returns_yfinance_bars(
    client: TestClient, monkeypatch, test_engine
) -> None:
    register(client, "history-dhan@example.com")
    recorder = CallRecorder()
    _select_dhan_with_failing_dhan_history(client, monkeypatch, recorder)
    instrument_id = _instrument_id(client)
    _seed_yfinance_history(test_engine, instrument_id)

    history = client.get(
        f"/api/v1/market-data/instruments/{instrument_id}/history?interval=1d"
    )

    assert history.status_code == 200, history.text
    bars = history.json()["bars"]
    assert bars == []
    assert all(bar["source"] == "dhan" for bar in bars)
    assert "yfinance:quote" not in recorder.calls


def test_yfinance_selected_history_returns_yfinance_bars_control(
    client: TestClient, monkeypatch, test_engine
) -> None:
    register(client, "history-yahoo@example.com")
    recorder = CallRecorder()
    chain = MarketDataProviderChain([FixtureYahooProvider(recorder)])
    chain.unavailable_providers = []
    app.dependency_overrides[get_market_data_provider] = lambda: chain
    monkeypatch.setattr(
        market_data, "_selected_market_data_source", lambda *_args, **_kwargs: "yfinance"
    )
    instrument_id = _instrument_id(client)
    _seed_yfinance_history(test_engine, instrument_id)

    history = client.get(
        f"/api/v1/market-data/instruments/{instrument_id}/history?interval=1d"
    )

    assert history.status_code == 200, history.text
    bars = history.json()["bars"]
    assert bars, history.text
    assert all(bar["source"] == "yfinance" for bar in bars)


def test_dhan_selected_history_sync_never_calls_yfinance(
    client: TestClient, monkeypatch, test_engine
) -> None:
    register(client, "sync-dhan@example.com")
    recorder = CallRecorder()
    _select_dhan_with_failing_dhan_history(client, monkeypatch, recorder)
    instrument_id = _instrument_id(client)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)

    sync = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={"interval": "1d", "start": start.isoformat(), "end": end.isoformat()},
        headers=_csrf(client),
    )

    assert sync.status_code == 503, sync.text
    detail = sync.json()["detail"]
    assert detail["stage"] == "provider_history_fetch"
    assert [item["provider"] for item in detail["providers"]] == ["dhan"]
    assert [attempt["provider"] for attempt in detail["provider_attempts"]] == ["dhan"]
    assert "yfinance:history" not in recorder.calls
    assert _stored_yfinance_bar_count(test_engine, instrument_id) == 0


def test_dhan_selected_indicators_reject_yfinance_source_and_ignore_its_bars(
    client: TestClient, monkeypatch, test_engine
) -> None:
    register(client, "indicators-dhan@example.com")
    recorder = CallRecorder()
    _select_dhan_with_failing_dhan_history(client, monkeypatch, recorder)
    instrument_id = _instrument_id(client)
    _seed_yfinance_history(test_engine, instrument_id)

    conflicting = client.get(
        f"/api/v1/instruments/{instrument_id}/indicators?interval=1d&source=yfinance"
    )
    assert conflicting.status_code == 409, conflicting.text

    indicators = client.get(
        f"/api/v1/instruments/{instrument_id}/indicators?interval=1d"
    )
    assert indicators.status_code == 200, indicators.text
    body = indicators.json()
    assert body["source"] != "yfinance"
    assert body["bars_available"] == 0
    assert "yfinance:quote" not in recorder.calls


def test_dhan_selected_watchlist_ignores_yfinance_quote(
    client: TestClient, monkeypatch, test_engine
) -> None:
    register(client, "watchlist-dhan@example.com")
    recorder = CallRecorder()
    _select_dhan_with_failing_dhan_history(client, monkeypatch, recorder)
    instrument_id = _instrument_id(client)
    _seed_yfinance_quote(test_engine, instrument_id)

    added = client.post(
        f"/api/v1/market-data/watchlist/{instrument_id}",
        headers=_csrf(client),
    )
    assert added.status_code == 204, added.text

    watchlist = client.get("/api/v1/market-data/watchlist")
    assert watchlist.status_code == 200, watchlist.text
    entries = [entry for entry in watchlist.json() if entry["instrument_id"] == instrument_id]
    assert entries
    assert entries[0]["quote"] is None
    assert "yfinance:quote" not in recorder.calls


def test_dhan_breaker_skip_is_reported_as_provider_outage_not_capability_error(
    client: TestClient, monkeypatch, test_engine
) -> None:
    register(client, "breaker-dhan@example.com")
    recorder = CallRecorder()
    _select_dhan_with_failing_dhan_history(client, monkeypatch, recorder)
    instrument_id = _instrument_id(client)
    _seed_yfinance_history(test_engine, instrument_id)
    seeded_bars = _stored_yfinance_bar_count(test_engine, instrument_id)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    url = f"/api/v1/market-data/instruments/{instrument_id}/history/sync"
    params = {"interval": "1d", "start": start.isoformat(), "end": end.isoformat()}

    for _ in range(3):
        failed = client.post(url, params=params, headers=_csrf(client))
        assert failed.status_code == 503, failed.text

    skipped = client.post(url, params=params, headers=_csrf(client))

    assert skipped.status_code == 503, skipped.text
    detail = skipped.json()["detail"]
    assert detail["stage"] == "provider_history_fetch"
    assert [item["reason"] for item in detail["providers"]] == ["circuit_breaker_open"]
    assert "yfinance:history" not in recorder.calls
    assert _stored_yfinance_bar_count(test_engine, instrument_id) == seeded_bars


def test_circuit_breaker_state_is_isolated_per_owner() -> None:
    breaker = market_data.ProviderCircuitBreaker(threshold=2, cooldown_seconds=30)
    breaker.record_failure("dhan", owner="user-a")
    breaker.record_failure("dhan", owner="user-a")

    assert breaker.before_attempt("dhan", owner="user-a") == "circuit_breaker_open"
    assert breaker.before_attempt("dhan", owner="user-b") is None
    assert breaker.before_attempt("dhan") is None

    breaker.reset("dhan")
    assert breaker.before_attempt("dhan", owner="user-a") is None


def test_saved_dhan_bars_are_marked_unavailable_when_sync_refresh_fails(
    client: TestClient, monkeypatch, test_engine
) -> None:
    register(client, "saved-dhan@example.com")
    recorder = CallRecorder()
    _select_dhan_with_failing_dhan_history(client, monkeypatch, recorder)
    instrument_id = _instrument_id(client)
    _seed_dhan_history(test_engine, instrument_id)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    url = f"/api/v1/market-data/instruments/{instrument_id}/history/sync"
    params = {"interval": "1d", "start": start.isoformat(), "end": end.isoformat()}

    response = client.post(url, params=params, headers=_csrf(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sync_status"] == "unavailable"
    assert body["is_cached"] is True
    assert body["fallback_reason"]
    assert body["bars"]
    assert all(bar["source"] == "dhan" for bar in body["bars"])
    expected_last_session = (
        end.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
    ).date().isoformat()
    assert body["bars"][-1]["session_date"] == expected_last_session
    assert "yfinance:history" not in recorder.calls
