import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from datetime import datetime, timezone
import struct
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import brokers
from app import broker_adapters
from app.broker_adapters import BrokerAdapterError, DhanAdapter, UpstoxAdapter
from app.config import settings
from app.db import Base, get_db
from app.main import BrokerCallbackAccessLogFilter, app
from app.models import BrokerCredential, BrokerMirror, PortfolioTransaction, UserSession
from app.security import hash_token
from app.two_factor import code_for_counter


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
def clients(test_engine) -> Iterator[tuple[TestClient, TestClient]]:
    def override_get_db():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as first, TestClient(
        app, follow_redirects=False
    ) as second:
        yield first, second
    app.dependency_overrides.clear()


@pytest.fixture
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "broker_encryption_key", SecretStr(key))
    return key


@pytest.fixture(autouse=True)
def two_factor_encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings,
        "two_factor_encryption_key",
        SecretStr(Fernet.generate_key().decode("ascii")),
    )


def register(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 202
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert login_response.status_code == 200


def complete_broker_gate(client: TestClient, test_engine) -> None:
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(UserSession.token_hash == hash_token(session_token))
        )
        assert user_session is not None
        user_session.user.email_verified = True
        db.commit()
    setup = client.post(
        "/api/v1/auth/2fa/setup",
        headers=csrf(client),
        json={},
    )
    assert setup.status_code == 201, setup.text
    counter = int(datetime.now(timezone.utc).timestamp() // 30)
    verified = client.post(
        "/api/v1/auth/2fa/verify",
        headers=csrf(client),
        json={"code": code_for_counter(setup.json()["secret"], counter)},
    )
    assert verified.status_code == 200, verified.text


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def connect_mock(client: TestClient) -> None:
    response = client.post(
        "/api/v1/brokers/mock_broker/connect",
        headers={**csrf(client), "Origin": settings.frontend_origin},
        json={},
    )
    assert response.status_code == 200, response.text
    callback_url = urlsplit(response.json()["redirect_url"])
    callback = client.get(
        f"{callback_url.path}?{callback_url.query}",
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert "broker_connected=mock_broker" in callback.headers["location"]
    assert "token" not in callback.headers["location"]


def prepare_dhan_sync(client: TestClient, test_engine, encryption_key, monkeypatch) -> None:
    register(client, "dhan-diagnostics@example.com")
    complete_broker_gate(client, test_engine)
    monkeypatch.setattr(settings, "broker_dhan_enabled", True)
    monkeypatch.setattr(settings, "dhan_api_key", SecretStr("DIAGNOSTIC_API_KEY_SENTINEL"))
    monkeypatch.setattr(settings, "dhan_api_secret", SecretStr("DIAGNOSTIC_API_SECRET_SENTINEL"))
    monkeypatch.setattr(
        settings,
        "dhan_redirect_uri",
        "https://app.example.com/api/v1/brokers/dhan/callback",
    )
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(UserSession.token_hash == hash_token(session_token))
        )
        assert user_session is not None
        now = datetime.now(timezone.utc)
        db.add(
            BrokerCredential(
                user_id=user_session.user_id,
                broker="dhan",
                encrypted_tokens=brokers.encrypt_tokens({
                    "access_token": "DIAGNOSTIC_ACCESS_TOKEN_SENTINEL",
                    "client_id": "DIAGNOSTIC_CLIENT_ID_SENTINEL",
                }),
                expires_at=now + timedelta(hours=1),
                connected_at=now,
            )
        )
        db.commit()


@pytest.mark.parametrize(
    ("failing_path", "expected_stage", "successful_calls"),
    [
        ("/v2/holdings", "holdings_api_request", 0),
        ("/v2/positions", "positions_api_request", 1),
    ],
)
def test_dhan_sync_http_failure_logs_endpoint_and_sanitized_provider_error(
    clients, test_engine, encryption_key, monkeypatch, caplog,
    failing_path, expected_stage, successful_calls,
) -> None:
    first, _ = clients
    prepare_dhan_sync(first, test_engine, encryption_key, monkeypatch)
    requested_paths: list[str] = []

    def mocked_request(method, url, **kwargs):
        path = httpx.URL(url).path
        requested_paths.append(path)
        if path == failing_path:
            return httpx.Response(
                401,
                json={
                    "errorCode": "DH-401",
                    "errorMessage": (
                        "Invalid access token DIAGNOSTIC_ACCESS_TOKEN_SENTINEL; "
                        "token=SHORT_TOKEN_SENTINEL; "
                        "api_secret=DIAGNOSTIC_API_SECRET_SENTINEL; "
                        "accountId=DIAGNOSTIC_CLIENT_ID_SENTINEL; "
                        "quantity=987654321"
                    ),
                },
                request=httpx.Request(method, url),
            )
        return httpx.Response(200, json=[], request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", mocked_request)
    caplog.set_level("WARNING", logger="app.broker_adapters")

    response = first.post(
        "/api/v1/brokers/dhan/sync",
        headers=csrf(first),
        json={},
    )

    if failing_path == "/v2/holdings":
        assert response.status_code == 401
        assert response.json()["detail"]["sync_status"] == "failed"
        assert requested_paths == [failing_path]
    else:
        assert response.status_code == 200
        assert response.json()["sync_status"] == "partial"
        assert response.json()["authentication_status"] == "expired"
        assert response.json()["endpoint_results"]["positions"]["reason"] == "authentication_expired"
        assert requested_paths[successful_calls] == failing_path
    diagnostic = caplog.text
    assert f"stage={expected_stage}" in diagnostic
    assert f"endpoint={failing_path}" in diagnostic
    assert "status_code=401" in diagnostic
    assert "error_class=HTTPStatusError" in diagnostic
    assert "provider_code=DH-401" in diagnostic
    assert "DIAGNOSTIC_ACCESS_TOKEN_SENTINEL" not in diagnostic
    assert "SHORT_TOKEN_SENTINEL" not in diagnostic
    assert "DIAGNOSTIC_API_SECRET_SENTINEL" not in diagnostic
    assert "DIAGNOSTIC_CLIENT_ID_SENTINEL" not in diagnostic
    assert "987654321" not in diagnostic
    assert "dhan-diagnostics@example.com" not in diagnostic


@pytest.mark.parametrize(
    ("kind", "expected_stage"),
    [
        ("holding", "holdings_response_normalization"),
        ("position", "positions_response_normalization"),
    ],
)
def test_dhan_sync_normalization_failures_have_distinct_safe_diagnostics(
    clients, test_engine, encryption_key, monkeypatch, caplog, kind, expected_stage
) -> None:
    first, _ = clients
    prepare_dhan_sync(first, test_engine, encryption_key, monkeypatch)
    monkeypatch.setattr(DhanAdapter, "get_holdings", lambda *_args: (
        [{"tradingsymbol": "HOLDING_VALUE_SENTINEL", "quantity": "not-a-number"}]
        if kind == "holding" else []
    ))
    monkeypatch.setattr(DhanAdapter, "get_positions", lambda *_args: (
        [{"tradingsymbol": "POSITION_VALUE_SENTINEL", "netQty": "not-a-number"}]
        if kind == "position" else []
    ))
    caplog.set_level("WARNING", logger="app.api.brokers")

    response = first.post(
        "/api/v1/brokers/dhan/sync",
        headers=csrf(first),
        json={},
    )

    assert response.status_code == 200
    assert response.json()["sync_status"] == "partial"
    assert response.json()["authentication_status"] == "connected"
    failed_endpoint = "holdings" if kind == "holding" else "positions"
    assert response.json()["endpoint_results"][failed_endpoint]["status"] == "failed"
    assert expected_stage in caplog.text
    assert "error_class=BrokerAdapterError" in caplog.text
    assert "could not be normalized" in caplog.text
    assert "HOLDING_VALUE_SENTINEL" not in caplog.text
    assert "POSITION_VALUE_SENTINEL" not in caplog.text
    assert "DIAGNOSTIC_ACCESS_TOKEN_SENTINEL" not in caplog.text
    assert "DIAGNOSTIC_CLIENT_ID_SENTINEL" not in caplog.text


def test_dhan_empty_holdings_provider_error_is_normalized_to_empty_list(monkeypatch) -> None:
    def mocked_request(method, url, **kwargs):
        if httpx.URL(url).path == "/v2/holdings":
            return httpx.Response(
                500,
                json={
                    "errorCode": "DH-1111",
                    "errorMessage": "No holdings found",
                },
                request=httpx.Request(method, url),
            )
        return httpx.Response(200, json=[], request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", mocked_request)

    assert DhanAdapter().get_holdings("access-token", {"client_id": "client"}) == []


def test_dhan_sync_accepts_empty_holdings_without_disconnecting(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    prepare_dhan_sync(first, test_engine, encryption_key, monkeypatch)

    def mocked_request(method, url, **kwargs):
        if httpx.URL(url).path == "/v2/holdings":
            return httpx.Response(
                500,
                json={
                    "errorCode": "DH-1111",
                    "errorMessage": "No holdings found",
                },
                request=httpx.Request(method, url),
            )
        return httpx.Response(200, json=[], request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", mocked_request)

    response = first.post(
        "/api/v1/brokers/dhan/sync",
        headers=csrf(first),
        json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["sync_status"] == "successful"
    assert response.json()["authentication_status"] == "connected"
    assert response.json()["holdings_count"] == 0
    assert response.json()["endpoint_results"] == {
        "holdings": {"status": "succeeded"},
        "positions": {"status": "succeeded"},
    }


def test_dhan_holdings_only_normalizes_the_documented_empty_case(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "request",
        lambda method, url, **kwargs: httpx.Response(
            500,
            json={"errorCode": "DH-1111", "errorMessage": "Other provider failure"},
            request=httpx.Request(method, url),
        ),
    )

    with pytest.raises(BrokerAdapterError):
        DhanAdapter().get_holdings("access-token", {"client_id": "client"})


def test_dhan_quote_parser_returns_exchange_timestamp_and_decimal_values(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def mocked_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "NSE_EQ": {
                        "11536": {
                            "last_price": 3900.25,
                            "last_trade_time": "15/04/2025 10:30:00",
                            "volume": 1200,
                            "ohlc": {
                                "open": 3890,
                                "high": 3910,
                                "low": 3880,
                            },
                        }
                    }
                },
            },
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", mocked_request)
    quote = DhanAdapter().get_market_quote(
        "access-token", {"client_id": "client"}, "NSE_EQ", "11536"
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.dhan.co/v2/marketfeed/quote"
    assert captured["json"] == {"NSE_EQ": [11536]}
    assert quote.close == Decimal("3900.25")
    assert quote.timestamp == datetime(2025, 4, 15, 5, 0, tzinfo=timezone.utc)


def test_dhan_historical_data_requests_one_minute_candles_and_converts_epoch(
    monkeypatch,
) -> None:
    start = datetime(2026, 10, 25, 3, 45, tzinfo=timezone.utc)
    end = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)
    epoch = int(datetime(2026, 10, 25, 3, 46, tzinfo=timezone.utc).timestamp())
    captured: dict[str, object] = {}

    def mocked_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(
            200,
            json={
                "open": [100],
                "high": [102],
                "low": [99],
                "close": [101],
                "volume": [1000],
                "timestamp": [epoch],
            },
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", mocked_request)
    bars = DhanAdapter().get_historical_bars(
        "token",
        {"client_id": "client"},
        "NSE_EQ",
        "2885",
        "1m",
        start,
        end,
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.dhan.co/v2/charts/intraday"
    assert captured["json"]["interval"] == "1"
    assert captured["json"]["fromDate"] == "2026-10-25 09:15:00"
    assert captured["json"]["toDate"] == "2026-10-25 09:30:00"
    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 10, 25, 3, 46, tzinfo=timezone.utc)


def test_dhan_historical_response_rejects_misaligned_arrays(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "request",
        lambda method, url, **kwargs: httpx.Response(
            200,
            json={
                "open": [100],
                "high": [],
                "low": [99],
                "close": [101],
                "volume": [1000],
                "timestamp": [1],
            },
            request=httpx.Request(method, url),
        ),
    )

    with pytest.raises(BrokerAdapterError, match="arrays do not align"):
        DhanAdapter().get_historical_bars(
            "token",
            {"client_id": "client"},
            "NSE_EQ",
            "2885",
            "1m",
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            datetime(1970, 1, 2, tzinfo=timezone.utc),
        )


def test_dhan_historical_response_accepts_empty_aligned_arrays(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "request",
        lambda method, url, **kwargs: httpx.Response(
            200,
            json={
                "open": [],
                "high": [],
                "low": [],
                "close": [],
                "volume": [],
                "timestamp": [],
            },
            request=httpx.Request(method, url),
        ),
    )

    bars = DhanAdapter().get_historical_bars(
        "token",
        {"client_id": "client"},
        "NSE_EQ",
        "2885",
        "1d",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert bars == []


def _dhan_quote_packet() -> bytes:
    packet = bytearray(50)
    packet[0] = 4
    packet[1:3] = (50).to_bytes(2, "little")
    packet[3] = 1
    packet[4:8] = (11536).to_bytes(4, "little", signed=True)
    struct.pack_into("<f", packet, 8, 3900.25)
    struct.pack_into("<i", packet, 14, int(datetime(2025, 4, 15, 5, tzinfo=timezone.utc).timestamp()))
    struct.pack_into("<i", packet, 22, 1200)
    struct.pack_into("<f", packet, 34, 3890)
    struct.pack_into("<f", packet, 42, 3910)
    struct.pack_into("<f", packet, 46, 3880)
    return bytes(packet)


def _dhan_disconnect_packet() -> bytes:
    packet = bytearray(8)
    packet[0] = 50
    packet[1:3] = (8).to_bytes(2, "little")
    packet[3] = 1
    packet[4:8] = (11536).to_bytes(4, "little", signed=True)
    return bytes(packet)


def test_dhan_websocket_quote_packet_uses_documented_little_endian_fields() -> None:
    quote = DhanAdapter.decode_market_feed_packet(
        _dhan_quote_packet(), "NSE_EQ", "11536"
    )

    assert quote is not None
    assert quote.timestamp == datetime(2025, 4, 15, 5, tzinfo=timezone.utc)
    assert quote.open == Decimal("3890.0")
    assert quote.high == Decimal("3910.0")
    assert quote.low == Decimal("3880.0")
    assert quote.close == Decimal("3900.25")
    assert quote.volume == Decimal("1200")


def test_dhan_websocket_resubscribe_after_disconnect(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self, messages: list[bytes]):
            self.messages = messages
            self.subscriptions: list[dict[str, object]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, message: str) -> None:
            self.subscriptions.append(json.loads(message))

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

    connections = [
        FakeConnection([_dhan_disconnect_packet()]),
        FakeConnection([_dhan_quote_packet()]),
    ]
    connection_iterator = iter(connections)
    monkeypatch.setattr(
        broker_adapters,
        "websocket_connect",
        lambda *_args, **_kwargs: next(connection_iterator),
    )
    statuses: list[str] = []

    async def capture_status(status: str) -> None:
        statuses.append(status)

    async def verify_stream() -> None:
        stream = DhanAdapter().stream_market_quotes(
            "access-token",
            {"client_id": "client"},
            "NSE_EQ",
            "11536",
            capture_status,
        )
        assert await anext(stream) is not None
        await stream.aclose()

    asyncio.run(verify_stream())

    assert statuses == ["active", "reconnecting", "active"]
    assert [connection.subscriptions for connection in connections] == [
        [{
            "RequestCode": 17,
            "InstrumentCount": 1,
            "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "11536"}],
        }]
    ] * 2


def test_dhan_instrument_master_mapping_requires_one_verified_equity(monkeypatch) -> None:
    monkeypatch.setattr(DhanAdapter, "_equity_index_cache", None)
    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", None)
    row = {
        "EXCH_ID": "NSE",
        "SEGMENT": "E",
        "SECURITY_ID": "11536",
        "SYMBOL_NAME": "TCS",
        "INSTRUMENT": "EQUITY",
        "SERIES": "EQ",
    }
    monkeypatch.setattr(DhanAdapter, "_scrip_master", classmethod(lambda _cls: [row]))
    adapter = DhanAdapter()

    assert adapter.find_verified_equity_mapping("TCS", "NSE") == ("11536", "NSE_EQ")
    assert adapter.find_verified_equity_mapping("UNKNOWN", "NSE") is None
    monkeypatch.setattr(
        DhanAdapter, "_scrip_master", classmethod(lambda _cls: [row, dict(row, SECURITY_ID="11537")])
    )
    assert adapter.find_verified_equity_mapping("TCS", "NSE") is None


def test_dhan_instrument_master_matches_reliance_underlying_symbol(monkeypatch) -> None:
    row = {
        "EXCH_ID": "NSE",
        "SEGMENT": "E",
        "SECURITY_ID": "2885",
        "SYMBOL_NAME": "RELIANCE INDUSTRIES LTD",
        "UNDERLYING_SYMBOL": "RELIANCE",
        "INSTRUMENT": "EQUITY",
        "SERIES": "EQ",
    }
    monkeypatch.setattr(DhanAdapter, "_scrip_master", classmethod(lambda _cls: [row]))

    assert DhanAdapter().find_verified_equity_mapping("RELIANCE", "NSE") == (
        "2885",
        "NSE_EQ",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": False, "data": []},
        {"status": True, "data": None},
        {"status": True, "errorCode": "DH-901", "data": []},
        {"status": True, "error_code": "DH-901", "data": []},
    ],
)
def test_dhan_success_http_error_payloads_are_rejected(payload) -> None:
    with pytest.raises(BrokerAdapterError) as error:
        broker_adapters._unwrap(payload)
    assert error.value.category == "provider_response_error"


def test_dhan_equity_index_does_not_readd_ambiguous_symbols(monkeypatch) -> None:
    monkeypatch.setattr(DhanAdapter, "_equity_index_cache", None)
    rows = [
        {
            "EXCH_ID": "NSE",
            "SEGMENT": "E",
            "SECURITY_ID": security_id,
            "SYMBOL_NAME": "ALPHA",
            "INSTRUMENT": "EQUITY",
            "SERIES": "EQ",
        }
        for security_id in ("100", "101", "102")
    ]
    monkeypatch.setattr(DhanAdapter, "_scrip_master", classmethod(lambda _cls: rows))

    assert DhanAdapter().find_verified_equity_mapping("ALPHA", "NSE") is None


def test_dhan_streams_large_instrument_master_and_maps_multiple_nse_equities(
    monkeypatch,
) -> None:
    class LargeLength:
        def __len__(self):
            return 33_000_001

    class StreamingResponse:
        content = LargeLength()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter([
                "EXCH_ID,SEGMENT,SECURITY_ID,SYMBOL_NAME,INSTRUMENT,SERIES,UNDERLYING_SYMBOL",
                "NSE,E,2885,RELIANCE INDUSTRIES LTD,EQUITY,EQ,RELIANCE",
                "NSE,E,11536,TCS LTD,EQUITY,EQ,TCS",
                "NSE,E,999999,RELIANCE FUT,FUTIDX,XX,RELIANCE",
            ])

    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", None)
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda method, url, **kwargs: StreamingResponse(),
    )

    assert DhanAdapter().find_verified_equity_mapping("RELIANCE", "NSE") == (
        "2885",
        "NSE_EQ",
    )
    assert DhanAdapter().find_verified_equity_mapping("TCS", "XNSE") == (
        "11536",
        "NSE_EQ",
    )


def test_dhan_sync_persistence_failure_logs_class_without_database_values(
    clients, test_engine, encryption_key, monkeypatch, caplog
) -> None:
    first, _ = clients
    prepare_dhan_sync(first, test_engine, encryption_key, monkeypatch)
    monkeypatch.setattr(DhanAdapter, "get_holdings", lambda *_args: [])
    monkeypatch.setattr(DhanAdapter, "get_positions", lambda *_args: [])

    def fail_commit(_session):
        raise IntegrityError("statement with DIAGNOSTIC_HOLDING_VALUE_SENTINEL", {}, RuntimeError("private row value"))

    monkeypatch.setattr(Session, "commit", fail_commit)
    caplog.set_level("WARNING", logger="app.api.brokers")

    response = first.post(
        "/api/v1/brokers/dhan/sync",
        headers=csrf(first),
        json={},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Broker account sync could not be saved"
    assert "stage=broker_mirror_persistence" in caplog.text
    assert "error_class=IntegrityError" in caplog.text
    assert "database details omitted" in caplog.text
    assert "DIAGNOSTIC_HOLDING_VALUE_SENTINEL" not in caplog.text
    assert "private row value" not in caplog.text
    assert "DIAGNOSTIC_ACCESS_TOKEN_SENTINEL" not in caplog.text


def test_fernet_token_round_trip_and_ciphertext_at_rest(encryption_key) -> None:
    tokens = {"access_token": "private-access-token", "refresh_token": "private-refresh-token"}
    encrypted = brokers.encrypt_tokens(tokens)

    assert encrypted != str(tokens)
    assert "private-access-token" not in encrypted
    assert brokers.decrypt_tokens(encrypted) == tokens


def test_broker_endpoints_fail_closed_until_email_and_two_factor_are_verified(
    clients, test_engine,
) -> None:
    first, second = clients
    register(first, "unverified@example.com")

    response = first.get("/api/v1/brokers")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "email_verification_required"
    with Session(test_engine) as db:
        user_session = db.scalar(select(UserSession))
        assert user_session is not None
        user_session.user.email_verified = True
        db.commit()
    response = first.get("/api/v1/brokers")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "two_factor_setup_required"

    setup = first.post(
        "/api/v1/auth/2fa/setup",
        headers=csrf(first),
        json={},
    )
    counter = int(datetime.now(timezone.utc).timestamp() // 30)
    challenge = first.post(
        "/api/v1/auth/2fa/verify",
        headers=csrf(first),
        json={"code": code_for_counter(setup.json()["secret"], counter)},
    )
    assert challenge.status_code == 200, challenge.text
    assert first.get("/api/v1/brokers").status_code == 200

    login = second.post(
        "/api/v1/auth/login",
        json={"email": "unverified@example.com", "password": "correct-horse-battery"},
    )
    assert login.status_code == 200
    incomplete_session = second.get("/api/v1/brokers")
    assert incomplete_session.status_code == 403
    assert incomplete_session.json()["detail"]["code"] == "two_factor_required"


def test_broker_list_route_is_mounted_and_returns_connection_status(clients, test_engine) -> None:
    first, _ = clients
    register(first, "broker-list@example.com")
    complete_broker_gate(first, test_engine)

    response = first.get("/api/v1/brokers")

    assert response.status_code == 200, response.text
    brokers_response = response.json()
    assert isinstance(brokers_response, list)
    mock_broker = next(broker for broker in brokers_response if broker["slug"] == "mock_broker")
    assert mock_broker["connected"] is False
    assert mock_broker["synthetic"] is True


@pytest.mark.parametrize(
    ("slug", "enabled_field", "credential_fields"),
    [
        ("zerodha", "broker_zerodha_enabled", ("zerodha_api_key", "zerodha_api_secret", "zerodha_redirect_url")),
        ("upstox", "broker_upstox_enabled", ("upstox_client_id", "upstox_client_secret", "upstox_redirect_uri")),
        ("dhan", "broker_dhan_enabled", ("dhan_api_key", "dhan_api_secret", "dhan_redirect_uri")),
    ],
)
def test_real_broker_adapters_fail_closed_without_credentials(
    clients, test_engine, encryption_key, monkeypatch, slug, enabled_field, credential_fields
) -> None:
    first, _ = clients
    register(first, f"{slug}@example.com")
    complete_broker_gate(first, test_engine)
    monkeypatch.setattr(settings, enabled_field, True)
    for field_name in credential_fields:
        monkeypatch.setattr(settings, field_name, None)

    response = first.post(f"/api/v1/brokers/{slug}/connect", headers=csrf(first), json={})

    assert response.status_code == 503
    assert "credentials" in response.json()["detail"]["message"]


def test_angel_one_is_pending_without_collecting_login_secrets(clients, test_engine) -> None:
    first, _ = clients
    register(first, "angel-pending@example.com")
    complete_broker_gate(first, test_engine)

    brokers_response = first.get("/api/v1/brokers")
    angel_one = next(item for item in brokers_response.json() if item["slug"] == "angel_one")
    connect_response = first.post(
        "/api/v1/brokers/angel_one/connect",
        headers=csrf(first),
        json={},
    )

    assert angel_one["integration_status"] == "pending"
    assert angel_one["enabled"] is False
    assert angel_one["direct_login"] is False
    assert connect_response.status_code == 503


def test_upstox_official_oauth_redirect_and_exchange_are_server_side(monkeypatch) -> None:
    monkeypatch.setattr(settings, "broker_upstox_enabled", True)
    monkeypatch.setattr(settings, "upstox_client_id", SecretStr("test-client-id"))
    monkeypatch.setattr(settings, "upstox_client_secret", SecretStr("test-client-secret"))
    monkeypatch.setattr(
        settings,
        "upstox_redirect_uri",
        "https://app.example.com/api/v1/brokers/upstox/callback",
    )
    adapter = UpstoxAdapter()
    redirect_url = adapter.authenticate(
        state="opaque-state",
        redirect_uri="https://app.example.com/api/v1/brokers/upstox/callback",
        credentials={},
    )
    parameters = parse_qs(urlsplit(redirect_url).query)
    assert parameters == {
        "response_type": ["code"],
        "client_id": ["test-client-id"],
        "redirect_uri": ["https://app.example.com/api/v1/brokers/upstox/callback"],
        "state": ["opaque-state"],
    }

    captured: dict[str, object] = {}

    def mocked_request(method: str, url: str, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(
            200,
            json={"access_token": "test-access-token", "expires_in": 3600},
        )

    monkeypatch.setattr(httpx, "request", mocked_request)
    tokens, expires_at = adapter.exchange_callback({"code": "single-use-code"}, {})

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.upstox.com/v2/login/authorization/token"
    assert captured["data"] == {
        "code": "single-use-code",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "redirect_uri": "https://app.example.com/api/v1/brokers/upstox/callback",
        "grant_type": "authorization_code",
    }
    assert tokens == {"access_token": "test-access-token"}
    assert expires_at > datetime.now(timezone.utc)


def test_upstox_rejected_authorization_fails_without_exposing_provider_body(monkeypatch) -> None:
    monkeypatch.setattr(settings, "broker_upstox_enabled", True)
    monkeypatch.setattr(settings, "upstox_client_id", SecretStr("test-client-id"))
    monkeypatch.setattr(settings, "upstox_client_secret", SecretStr("test-client-secret"))
    monkeypatch.setattr(
        settings,
        "upstox_redirect_uri",
        "https://app.example.com/api/v1/brokers/upstox/callback",
    )
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *_args, **_kwargs: httpx.Response(403, json={"error": "permission_denied"}),
    )

    with pytest.raises(BrokerAdapterError) as error:
        UpstoxAdapter().exchange_callback({"code": "bad-code"}, {})

    assert "permission_denied" not in str(error.value)


def test_connect_fails_closed_without_fernet_key(clients, test_engine, monkeypatch) -> None:
    first, _ = clients
    register(first, "nokey@example.com")
    complete_broker_gate(first, test_engine)
    monkeypatch.setattr(settings, "broker_encryption_key", None)

    response = first.post(
        "/api/v1/brokers/mock_broker/connect",
        headers={**csrf(first), "Origin": settings.frontend_origin},
        json={},
    )

    assert response.status_code == 503
    assert "encryption" in response.json()["detail"]["message"]


def test_expired_mock_token_refreshes_before_account_sync(
    clients, test_engine, encryption_key
) -> None:
    first, _ = clients
    register(first, "refresh@example.com")
    complete_broker_gate(first, test_engine)
    connect_mock(first)

    with Session(test_engine) as db:
        credential = db.scalar(select(BrokerCredential))
        assert credential is not None
        credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    response = first.post("/api/v1/brokers/mock_broker/sync", headers=csrf(first), json={})

    assert response.status_code == 200, response.text
    with Session(test_engine) as db:
        credential = db.scalar(select(BrokerCredential))
        assert credential is not None
        expiry = credential.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        assert expiry > datetime.now(timezone.utc)
        assert brokers.decrypt_tokens(credential.encrypted_tokens)["access_token"].endswith("-refreshed")


def test_sync_is_idempotent_and_reconciliation_reports_all_difference_kinds(
    clients, test_engine, encryption_key
) -> None:
    first, _ = clients
    register(first, "reconcile@example.com")
    complete_broker_gate(first, test_engine)
    connect_mock(first)
    portfolio_response = first.post(
        "/api/v1/portfolios",
        headers=csrf(first),
        json={"name": "Manual", "base_currency": "INR"},
    )
    assert portfolio_response.status_code == 201
    portfolio_id = portfolio_response.json()["id"]
    for payload in (
        {"transaction_type": "deposit", "amount": "10000"},
        {"transaction_type": "buy", "symbol": "DEMO-A", "quantity": "5", "unit_price": "20"},
        {"transaction_type": "buy", "symbol": "MANUAL-ONLY", "quantity": "3", "unit_price": "10"},
    ):
        response = first.post(
            f"/api/v1/portfolios/{portfolio_id}/transactions",
            headers=csrf(first),
            json=payload,
        )
        assert response.status_code == 201, response.text
    with Session(test_engine) as db:
        manual_transaction_count = len(
            db.scalars(
                select(PortfolioTransaction).where(
                    PortfolioTransaction.portfolio_id == UUID(portfolio_id)
                )
            ).all()
        )

    endpoint = "/api/v1/brokers/mock_broker/sync"
    assert first.post(endpoint, headers=csrf(first), json={}).status_code == 200
    assert first.post(endpoint, headers=csrf(first), json={}).status_code == 200
    with Session(test_engine) as db:
        assert len(
            db.scalars(
                select(PortfolioTransaction).where(
                    PortfolioTransaction.portfolio_id == UUID(portfolio_id)
                )
            ).all()
        ) == manual_transaction_count
    mirror_response = first.get("/api/v1/brokers/mock_broker/mirror?kind=holding")
    assert mirror_response.status_code == 200
    assert len(mirror_response.json()) == 2
    with Session(test_engine) as db:
        assert len(db.scalars(select(BrokerMirror)).all()) == 3

    response = first.get(
        f"/api/v1/brokers/mock_broker/reconciliation?portfolio_id={portfolio_id}"
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    results = {item["symbol"]: item for item in response.json()["differences"]}
    assert results["DEMO-A"]["status"] == "quantity_mismatch"
    assert Decimal(results["DEMO-A"]["difference"]) == Decimal("5")
    assert results["DEMO-B"]["status"] == "extra"
    assert results["MANUAL-ONLY"]["status"] == "missing"

    not_confirmed = first.post(
        "/api/v1/brokers/mock_broker/reconciliation/import",
        headers=csrf(first),
        json={"portfolio_id": portfolio_id, "confirmed": False, "rows": [{
            "symbol": "DEMO-A",
            "expected_manual_quantity": "5",
            "expected_broker_quantity": "10",
        }]},
    )
    assert not_confirmed.status_code == 400

    imported = first.post(
        "/api/v1/brokers/mock_broker/reconciliation/import",
        headers=csrf(first),
        json={
            "portfolio_id": portfolio_id,
            "confirmed": True,
            "rows": [{
                "symbol": "DEMO-A",
                "expected_manual_quantity": "5",
                "expected_broker_quantity": "10",
            }],
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported_count"] == 1
    assert first.get(
        f"/api/v1/brokers/mock_broker/reconciliation?portfolio_id={portfolio_id}"
    ).json()["differences"][0]["symbol"] != "DEMO-A"


def test_broker_credentials_and_mirror_are_isolated_between_users(
    clients, test_engine, encryption_key
) -> None:
    first, second = clients
    register(first, "owner@example.com")
    register(second, "other@example.com")
    complete_broker_gate(first, test_engine)
    complete_broker_gate(second, test_engine)
    connect_mock(first)
    assert first.post(
        "/api/v1/brokers/mock_broker/sync",
        headers=csrf(first),
        json={},
    ).status_code == 200

    other_brokers = second.get("/api/v1/brokers")
    assert other_brokers.status_code == 200
    mock_status = next(row for row in other_brokers.json() if row["slug"] == "mock_broker")
    assert mock_status["connected"] is False
    assert "synthetic-mock-access-token" not in other_brokers.text
    assert second.get("/api/v1/brokers/mock_broker/mirror").json() == []
    assert second.get("/api/v1/brokers/mock_broker/account").status_code == 409
    owner_account = first.get("/api/v1/brokers/mock_broker/account")
    assert owner_account.status_code == 200
    assert owner_account.headers["cache-control"] == "no-store"
    assert "synthetic-mock-access-token" not in owner_account.text

    disconnect = first.delete(
        "/api/v1/brokers/mock_broker",
        headers=csrf(first),
    )
    assert disconnect.status_code == 204
    with Session(test_engine) as db:
        assert db.scalar(select(BrokerCredential)) is None


def test_no_order_placement_or_mutation_route_exists() -> None:
    broker_routes = [
        route for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/brokers")
    ]
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}

    assert not any(
        "/orders" in route.path and route.methods & write_methods
        for route in broker_routes
    )
    adapter_source = Path(__file__).parents[1].joinpath("app", "broker_adapters.py").read_text()
    assert not any(
        forbidden in adapter_source.casefold()
        for forbidden in ("place_order", "modify_order", "cancel_order")
    )


def test_callback_access_logs_redact_request_tokens() -> None:
    import logging

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        1,
        '%s - "%s %s HTTP/%s" %s',
        ("127.0.0.1", "GET", "/api/v1/brokers/zerodha/callback?request_token=secret", "1.1", 200),
        None,
    )

    assert BrokerCallbackAccessLogFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "/api/v1/brokers/zerodha/callback?<redacted>" in record.getMessage()
