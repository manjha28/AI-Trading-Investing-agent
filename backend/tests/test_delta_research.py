from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.delta_research.collector import DeltaPublicCollector
from app.delta_research import collector as collector_module
from app.delta_research.store import DeltaResearchStore
from app.db import Base, get_db
from app.main import app
from app.models import User


@pytest.fixture
def test_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


def register(client: TestClient, test_engine, verified: bool = True) -> None:
    email, password = "delta@example.com", "correct-horse-battery"
    assert client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).status_code == 200
    if verified:
        with Session(test_engine) as db:
            db.scalar(select(User).where(User.email == email)).email_verified = True
            db.commit()


def test_status_requires_authentication_and_verified_email(client, test_engine) -> None:
    assert client.get("/api/v1/delta-research/status").status_code == 401
    register(client, test_engine, verified=False)
    response = client.get("/api/v1/delta-research/status")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "email_verification_required"


def test_disabled_by_default(client, test_engine) -> None:
    assert settings.delta_research_enabled is False
    register(client, test_engine)
    response = client.get("/api/v1/delta-research/status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["store_status"] == "disabled"
    assert body["live_feeds"] == "disabled"
    assert body["account_monitor"] == "disabled"
    assert "not investment advice" in body["disclaimer"]
    assert response.headers["cache-control"] == "no-store"


def test_disabled_collector_does_not_create_its_configured_store(
    client, test_engine, monkeypatch, tmp_path
) -> None:
    store_path = tmp_path / "must-not-be-created.sqlite3"
    monkeypatch.setattr(settings, "delta_research_enabled", False)
    monkeypatch.setattr(settings, "delta_research_db_path", str(store_path))
    register(client, test_engine)

    assert client.get("/api/v1/delta-research/status").status_code == 200
    assert not store_path.exists()


def test_missing_store_is_explicit_and_path_is_not_exposed(
    client, test_engine, monkeypatch, tmp_path
) -> None:
    secret_path = str(tmp_path / "private-dir" / "research.sqlite3")
    monkeypatch.setattr(settings, "delta_research_enabled", True)
    monkeypatch.setattr(settings, "delta_research_db_path", secret_path)
    register(client, test_engine)
    response = client.get("/api/v1/delta-research/status")
    assert response.json()["store_status"] == "not_found"
    assert "private-dir" not in response.text
    monkeypatch.setattr(settings, "delta_research_db_path", None)
    assert client.get("/api/v1/delta-research/status").json()["store_status"] == "not_configured"


def test_invalid_store_is_reported_without_exposing_its_path(
    client, test_engine, monkeypatch, tmp_path
) -> None:
    store = tmp_path / "research.sqlite3"
    store.write_bytes(b"not a database")
    monkeypatch.setattr(settings, "delta_research_enabled", True)
    monkeypatch.setattr(settings, "delta_research_db_path", str(store))
    register(client, test_engine)
    body = client.get("/api/v1/delta-research/status").json()
    assert body["store_status"] == "invalid"
    assert body["store_modified_at"] is not None
    assert body["store_age_seconds"] >= 0
    assert str(tmp_path) not in str(body)


def test_public_store_and_read_only_market_api(client, test_engine, monkeypatch, tmp_path) -> None:
    store_path = tmp_path / "delta-research.sqlite3"
    with DeltaResearchStore(store_path) as store:
        store.save_products([{
            "id": 123,
            "symbol": "BTCUSD",
            "contract_type": "perpetual_futures",
            "state": "live",
        }])
        store.save_quote(
            {"product_id": 123, "symbol": "BTCUSD", "close": "65000.25"},
            datetime.now(timezone.utc),
            "test_rest",
        )
    monkeypatch.setattr(settings, "delta_research_enabled", True)
    monkeypatch.setattr(settings, "delta_research_db_path", str(store_path))
    register(client, test_engine)

    response = client.get("/api/v1/delta-research/markets")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["symbol"] == "BTCUSD"
    assert item["last_price"] == "65000.25"
    assert client.get("/api/v1/delta-research/overview").json()["summary"]["products"] == 1


def test_candlestick_websocket_frame_is_stored_as_public_market_evidence(tmp_path) -> None:
    store_path = tmp_path / "delta-research.sqlite3"
    with DeltaResearchStore(store_path) as store:
        store.save_products([{
            "id": 123,
            "symbol": "BTCUSD",
            "contract_type": "perpetual_futures",
            "state": "live",
        }])
    collector = DeltaPublicCollector(store_path)
    frame = {
        "type": "candlestick_1m",
        "sy": "BTCUSD",
        "ts": 1775814834503627,
        "res": "1m",
        "o": "10",
        "h": "12",
        "l": "9",
        "c": "11",
        "v": "5",
    }

    asyncio.run(collector._handle_frame(json.dumps(frame)))
    queued = collector._write_queue.get_nowait()
    collector._persist_message(queued)
    collector._write_queue.task_done()

    with DeltaResearchStore(store_path, read_only=True) as store:
        detail = store.product_detail("123", 10)
        assert detail is not None
        assert detail["candles"][0]["close"] == "11"
        assert detail["candles"][0]["source"] == "delta_public_ws_candlestick"


def test_ticker_websocket_frame_updates_price_from_documented_ohlc_close(tmp_path) -> None:
    store_path = tmp_path / "delta-research.sqlite3"
    with DeltaResearchStore(store_path) as store:
        store.save_products([{
            "id": 123,
            "symbol": "BTCUSD",
            "contract_type": "perpetual_futures",
            "state": "live",
        }])
    collector = DeltaPublicCollector(store_path)
    frame = {
        "type": "ticker",
        "sy": "BTCUSD",
        "ts": 1775814834503627,
        "sp": "65001.00",
        "d": [{
            "i": 123,
            "m": "65002.00",
            "ohlc": ["64000.00", "66000.00", "63000.00", "65000.25"],
        }],
    }

    async def persist() -> None:
        await collector._handle_frame(json.dumps(frame))
        collector._persist_message(collector._write_queue.get_nowait())
        collector._write_queue.task_done()

    asyncio.run(persist())

    with DeltaResearchStore(store_path, read_only=True) as store:
        row = store.list_products("", 10, 0)["items"][0]
        assert row["last_price"] == "65000.25"
        assert row["mark_price"] == "65002.00"
        assert row["quote_source"] == "delta_public_ws_ticker_24h_close"


def test_research_store_read_connection_is_query_only(tmp_path) -> None:
    store_path = tmp_path / "delta-research.sqlite3"
    with DeltaResearchStore(store_path):
        pass
    with DeltaResearchStore(store_path, read_only=True) as store:
        with pytest.raises(sqlite3.OperationalError):
            store.connection.execute("DELETE FROM products")


def test_websocket_failures_back_off_with_jitter_report_unavailable_and_keep_retrying(
    tmp_path, monkeypatch, caplog
) -> None:
    attempts = 0
    delays: list[float] = []
    collector = DeltaPublicCollector(tmp_path / "not-created.sqlite3")
    states: list[str] = []

    class FailedConnection:
        async def __aenter__(self):
            raise OSError("connection unavailable")

        async def __aexit__(self, *_args):
            return None

    def failing_connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts >= 7:
            collector._stop.set()
        return FailedConnection()

    real_wait_for = asyncio.wait_for

    async def immediate_wait(awaitable, timeout):
        delays.append(timeout)
        states.append(collector.status()["state"])
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(collector_module, "connect", failing_connect)
    monkeypatch.setattr(collector_module.asyncio, "wait_for", immediate_wait)
    monkeypatch.setattr(collector_module, "MAX_CONSECUTIVE_FAILURES", 4)
    monkeypatch.setattr(collector_module, "MAX_RECONNECT_SECONDS", 2)

    asyncio.run(collector._websocket_loop())

    assert attempts == 7  # keeps retrying past the failure threshold instead of giving up
    assert len(delays) == 6
    for failures, delay in enumerate(delays, start=1):
        step = min(2 ** (failures - 1), 2)
        assert step * 0.5 <= delay <= step  # bounded exponential backoff with jitter
    assert states[:3] == ["reconnecting"] * 3 and states[3:] == ["unavailable"] * 3
    status = collector.status()
    assert status["stream_state"] == "unavailable" and status["is_live"] is False
    assert len([record for record in caplog.records if "Delta public collector error" in record.message]) == 1
    assert real_wait_for is not None


def test_idle_websocket_without_heartbeat_forces_a_reconnect(tmp_path, monkeypatch) -> None:
    collector = DeltaPublicCollector(tmp_path / "idle.sqlite3")
    connects = 0

    class SilentSocket:
        async def send(self, _data):
            return None

        async def recv(self):
            await asyncio.sleep(3600)

    class SilentConnection:
        async def __aenter__(self):
            return SilentSocket()

        async def __aexit__(self, *_args):
            return None

    def silent_connect(*_args, **_kwargs):
        nonlocal connects
        connects += 1
        if connects >= 2:
            collector._stop.set()
        return SilentConnection()

    monkeypatch.setattr(collector_module, "connect", silent_connect)
    monkeypatch.setattr(collector_module, "WS_IDLE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(collector_module, "reconnect_delay", lambda _n: 0.0)
    monkeypatch.setattr(collector, "_save_health", lambda force=False: None)
    asyncio.run(collector._websocket_loop())
    assert connects == 2 and collector._reconnects == 2
    assert collector.status()["connected"] is False


def test_stream_state_never_reports_live_when_disconnected_or_stale() -> None:
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(seconds=5)
    old = now - timedelta(seconds=600)
    assert collector_module.classify_stream_state(True, fresh, now, "connected") == "connected"
    assert collector_module.classify_stream_state(True, old, now, "connected") == "stale"
    assert collector_module.classify_stream_state(True, None, now, "connected") == "stale"
    assert collector_module.classify_stream_state(False, fresh, now, "reconnecting") == "reconnecting"
    assert collector_module.classify_stream_state(False, fresh, now, "unavailable") == "unavailable"


def test_sse_subscriber_queue_is_bounded_and_reports_dropped_events(tmp_path) -> None:
    collector = DeltaPublicCollector(tmp_path / "not-created.sqlite3")

    async def exercise_stream() -> None:
        stream = collector.subscribe()
        first = await anext(stream)
        assert first["event"] == "snapshot"
        for value in (1, 2, 3):
            await collector._publish({"event": "market_data", "data": {"value": value}})
        latest = await anext(stream)
        assert latest["data"] == {"value": 3}
        assert collector.status()["dropped_events"] == 2
        await stream.aclose()

    original_size = collector_module.SUBSCRIBER_QUEUE_SIZE
    collector_module.SUBSCRIBER_QUEUE_SIZE = 1
    try:
        asyncio.run(exercise_stream())
    finally:
        collector_module.SUBSCRIBER_QUEUE_SIZE = original_size
    assert not collector._subscribers


def test_delta_research_exposes_only_read_routes() -> None:
    paths = {
        path: operations
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/delta-research")
    }
    assert paths
    for path, operations in paths.items():
        assert set(operations) <= {"get", "head"}
        assert not any(word in path for word in ("order", "execute", "cancel", "position"))


def test_all_delta_read_routes_require_authentication(client) -> None:
    paths = (
        "/status",
        "/overview",
        "/markets",
        "/markets/123",
        "/events",
        "/strategies",
        "/evaluations",
        "/leaderboard",
        "/stream",
    )
    for path in paths:
        assert client.get(f"/api/v1/delta-research{path}").status_code == 401
