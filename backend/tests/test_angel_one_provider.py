import base64
import asyncio
import json
import struct
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.api import market_data as market_data_api
from app.config import settings
from app.market_data import InstrumentCandidate, MarketDataProviderError
from app.provider_adapters import AngelOneProvider


def instrument() -> InstrumentCandidate:
    return InstrumentCandidate(
        provider="angel_one",
        provider_instrument_id="3045|NSE",
        provider_symbol="SBIN-EQ",
        symbol="SBIN",
        display_name="State Bank of India",
        asset_class="equity",
        venue="NSE",
        currency="INR",
        country="IN",
        timezone="Asia/Kolkata",
        is_synthetic=False,
        is_verified=True,
    )


def test_angel_one_login_parses_tokens_without_exposing_provider_error_details(
    monkeypatch,
) -> None:
    expiry = int(time.time()) + 3600
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expiry}).encode()
    ).decode().rstrip("=")
    jwt = f"header.{encoded_payload}.signature"

    monkeypatch.setattr(settings, "angel_one_client_local_ip", "10.0.0.2")
    monkeypatch.setattr(settings, "angel_one_client_public_ip", "203.0.113.10")
    monkeypatch.setattr(settings, "angel_one_client_mac_address", "02:00:00:00:00:01")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == (
            "https://apiconnect.angelone.in/"
            "rest/auth/angelbroking/user/v1/loginByPassword"
        )
        assert request.headers["X-PrivateKey"] == "server-api-key"
        assert request.headers["X-UserType"] == "USER"
        assert request.headers["X-SourceID"] == "WEB"
        assert request.headers["X-ClientLocalIP"] == "10.0.0.2"
        assert request.headers["X-ClientPublicIP"] == "203.0.113.10"
        assert request.headers["X-MACAddress"] == "02:00:00:00:00:01"
        assert request.read() == b'{"clientcode":"client-1","password":"1234","totp":"123456"}'
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "jwtToken": jwt,
                    "refreshToken": "refresh",
                    "feedToken": "feed",
                },
            },
        )

    tokens, expires_at = AngelOneProvider.login(
        "server-api-key",
        "client-1",
        "1234",
        "123456",
        transport=httpx.MockTransport(respond),
    )

    assert tokens == {
        "client_code": "client-1",
        "access_token": jwt,
        "refresh_token": "refresh",
        "feed_token": "feed",
    }
    assert expires_at > datetime.now(timezone.utc)


def test_angel_one_login_status_false_is_classified_as_authentication_failure() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": False,
                "errorcode": "AB1010",
                "message": "Invalid TOTP",
            },
        )

    with pytest.raises(MarketDataProviderError) as captured:
        AngelOneProvider.login(
            "server-api-key",
            "client-1",
            "1234",
            "123456",
            transport=httpx.MockTransport(respond),
        )

    assert captured.value.code == "provider_authentication_failed"
    assert captured.value.status_code == 401
    assert "Invalid TOTP" not in str(captured.value)


def test_angel_one_history_uses_bearer_jwt_and_records_redacted_http_diagnostics(
    caplog,
) -> None:
    caplog.set_level("INFO", logger="app.provider_adapters")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            "/rest/secure/angelbroking/historical/v1/getCandleData"
        )
        assert request.headers["Authorization"] == "Bearer jwt-token"
        return httpx.Response(200, json={"status": True, "data": []})

    provider = AngelOneProvider(
        "private-api-key",
        {
            "access_token": "jwt-token",
            "refresh_token": "private-refresh-token",
            "feed_token": "private-feed-token",
            "client_code": "private-client",
        },
        transport=httpx.MockTransport(respond),
    )
    bars = provider.get_historical_bars(
        instrument(),
        "1d",
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 25, tzinfo=timezone.utc),
        500,
    )

    assert bars == []
    diagnostic = provider.last_http_diagnostics[0]
    assert diagnostic["url"].endswith(
        "/rest/secure/angelbroking/historical/v1/getCandleData"
    )
    assert diagnostic["http_status"] == 200
    assert set(diagnostic["request_headers"]) == {
        "Authorization",
        "X-PrivateKey",
        "X-UserType",
        "X-SourceID",
        "X-ClientLocalIP",
        "X-ClientPublicIP",
        "X-MACAddress",
        "Accept",
        "Content-Type",
    }
    assert all(value == "[REDACTED]" for value in diagnostic["request_headers"].values())
    assert "private-api-key" not in caplog.text
    assert "jwt-token" not in caplog.text
    assert "private-refresh-token" not in caplog.text
    assert "private-feed-token" not in caplog.text
    assert "private-client" not in caplog.text


def test_angel_one_login_timeout_is_normalized_without_transport_details() -> None:
    def time_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport detail")

    with pytest.raises(MarketDataProviderError) as captured:
        AngelOneProvider.login(
            "server-api-key",
            "client-1",
            "1234",
            "123456",
            transport=httpx.MockTransport(time_out),
        )

    assert captured.value.code == "provider_timeout"
    assert "private transport detail" not in str(captured.value)


def test_angel_one_rejects_expired_auth_without_leaking_provider_body() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid token secret-value"})

    with pytest.raises(MarketDataProviderError) as captured:
        AngelOneProvider.login(
            "server-api-key",
            "client-1",
            "1234",
            "123456",
            transport=httpx.MockTransport(respond),
        )

    assert captured.value.code == "provider_authentication_failed"
    assert "secret-value" not in str(captured.value)


@pytest.mark.parametrize(
    ("provider_error", "expected_status", "expected_detail", "logged_code"),
    [
        (
            MarketDataProviderError("provider_server_error", status_code=500),
            502,
            "Angel One SmartAPI returned an upstream server error (HTTP 500). Retry later.",
            "provider_server_error",
        ),
        (
            MarketDataProviderError("provider_timeout"),
            504,
            "Timed out connecting to Angel One SmartAPI. Check provider availability and retry.",
            "provider_timeout",
        ),
        (
            MarketDataProviderError("provider_network_error"),
            502,
            "Could not reach Angel One SmartAPI. Check outbound network access and retry.",
            "provider_network_error",
        ),
    ],
)
def test_angel_one_connect_logs_safe_failure_and_returns_actionable_error(
    monkeypatch, caplog, provider_error, expected_status, expected_detail, logged_code
) -> None:
    api_key = "private-api-key"
    pin = "private-pin"
    totp = "123456"
    monkeypatch.setattr(settings, "angel_one_api_key", SecretStr(api_key))
    monkeypatch.setattr(settings, "angel_one_client_public_ip", "203.0.113.10")

    def fail_login(*_args, **_kwargs):
        raise provider_error

    monkeypatch.setattr(AngelOneProvider, "login", fail_login)
    active_session = SimpleNamespace(
        user=SimpleNamespace(two_factor_enabled=True),
        two_factor_verified=True,
        user_id=uuid4(),
    )
    request = market_data_api.AngelOneConnectRequest(
        client_code="private-client",
        pin=pin,
        totp=totp,
    )

    with pytest.raises(HTTPException) as captured:
        market_data_api.connect_angel_one(
            request=request,
            active_session=active_session,
            _=active_session,
            db=None,
        )

    assert captured.value.status_code == expected_status
    assert captured.value.detail == expected_detail
    assert logged_code in caplog.text
    for secret in (api_key, pin, totp, "private-client"):
        assert secret not in caplog.text
        assert secret not in str(captured.value.detail)


def test_angel_one_quote_parsing_preserves_provider_market_timestamp() -> None:
    timestamp = "2026-06-12 10:15:00"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/market/v1/quote/")
        assert request.headers["Authorization"] == "Bearer jwt"
        body = json.loads(request.read())
        assert body == {"mode": "FULL", "exchangeTokens": {"NSE": ["3045"]}}
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "fetched": [{
                        "symbolToken": "3045",
                        "exchangeFeedTime": timestamp,
                        "open": 100,
                        "high": 105,
                        "low": 99,
                        "close": 102,
                        "tradeVolume": 1000,
                    }],
                },
            },
        )

    provider = AngelOneProvider(
        "server-api-key",
        {"access_token": "jwt", "client_code": "client-1", "feed_token": "feed"},
        transport=httpx.MockTransport(respond),
    )

    quote = provider.get_latest_quote(instrument())

    assert quote.timestamp == datetime(2026, 6, 12, 4, 45, tzinfo=timezone.utc)
    assert quote.close == Decimal("102")
    assert quote.volume == Decimal("1000")


def test_angel_one_malformed_quote_response_is_not_treated_as_data() -> None:
    provider = AngelOneProvider(
        "server-api-key",
        {"access_token": "jwt"},
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"status": True, "data": {"fetched": {"symbolToken": "3045"}}},
            )
        ),
    )

    with pytest.raises(MarketDataProviderError, match="provider_quote_unavailable"):
        provider.get_latest_quote(instrument())


def test_angel_one_scrip_master_mappings_are_verified_and_ids_are_validated(monkeypatch) -> None:
    rows = [{
        "exch_seg": "NSE",
        "instrumenttype": "EQ",
        "symbol": "SBIN-EQ",
        "token": "3045",
        "name": "State Bank of India",
    }]
    monkeypatch.setattr(AngelOneProvider, "_scrip_master_cache", (time.monotonic(), rows))
    provider = AngelOneProvider("server-api-key", {})

    candidate = provider.get_instrument("3045|NSE")

    assert candidate.provider_instrument_id == "3045|NSE"
    assert candidate.is_verified is True
    with pytest.raises(MarketDataProviderError, match="invalid_instrument_mapping"):
        provider.get_instrument("not-a-verified-id")


@pytest.mark.parametrize(
    ("content_length", "body", "maximum"),
    [
        ("11", b"[]", 10),
        ("invalid", b'[{"valid":"payload"}]', 10),
    ],
)
def test_angel_one_master_response_size_is_bounded(
    monkeypatch, content_length, body, maximum
) -> None:
    monkeypatch.setattr(AngelOneProvider, "_scrip_master_cache", None)
    monkeypatch.setattr(
        "app.provider_adapters.MAX_ANGEL_ONE_MASTER_BYTES", maximum
    )
    provider = AngelOneProvider(
        "server-api-key",
        {},
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Length": content_length},
                content=body,
            )
        ),
    )

    with pytest.raises(MarketDataProviderError) as captured:
        AngelOneProvider._master_rows(provider._transport)

    assert captured.value.code == "provider_response_too_large"


def test_angel_one_master_parses_and_reuses_valid_cached_response(monkeypatch) -> None:
    monkeypatch.setattr(AngelOneProvider, "_scrip_master_cache", None)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "token": "3045",
                    "symbol": "SBIN-EQ",
                    "name": "State Bank of India",
                    "exch_seg": "NSE",
                    "instrumenttype": "EQ",
                },
                {
                    "token": "999",
                    "symbol": "SBIN-FUT",
                    "name": "Ignored derivative",
                    "exch_seg": "NSE",
                    "instrumenttype": "FUT",
                },
            ],
        )

    transport = httpx.MockTransport(respond)
    first = AngelOneProvider._master_rows(transport)
    second = AngelOneProvider._master_rows(transport)

    assert len(first) == 1
    assert first[0]["token"] == "3045"
    assert second is first
    assert requests == [AngelOneProvider.scrip_master_url]


@pytest.mark.parametrize("body", [b"{malformed", b"[]"])
def test_angel_one_malformed_or_empty_master_fails_safely(monkeypatch, body) -> None:
    monkeypatch.setattr(AngelOneProvider, "_scrip_master_cache", None)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=body)
    )

    with pytest.raises(MarketDataProviderError) as captured:
        AngelOneProvider._master_rows(transport)

    assert captured.value.code == "invalid_provider_response"


def test_angel_one_search_uses_authenticated_scrip_lookup_not_full_master() -> None:
    requests: list[httpx.Request] = []
    exchanges: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path.endswith(
            "/rest/secure/angelbroking/order/v1/searchScrip"
        )
        assert request.headers["Authorization"] == "Bearer jwt"
        body = json.loads(request.read())
        exchanges.append(body["exchange"])
        assert body["searchscrip"] == "SBIN"
        if body["exchange"] == "NSE":
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": [{
                        "exchange": "NSE",
                        "tradingsymbol": "SBIN-EQ",
                        "symboltoken": "3045",
                    }],
                },
            )
        return httpx.Response(200, json={"status": True, "data": []})

    provider = AngelOneProvider(
        "server-api-key",
        {"access_token": "jwt"},
        transport=httpx.MockTransport(respond),
    )

    candidates = provider.search_instruments("SBIN", 10)

    assert [item.provider_instrument_id for item in candidates] == ["3045|NSE"]
    assert candidates[0].is_verified is True
    assert exchanges == ["NSE", "BSE"]


def test_angel_one_search_for_instrument_venue_does_not_query_other_exchanges() -> None:
    exchanges: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        exchanges.append(json.loads(request.read())["exchange"])
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [{
                    "exchange": "NSE",
                    "tradingsymbol": "SBIN-EQ",
                    "symboltoken": "3045",
                }],
            },
        )

    provider = AngelOneProvider(
        "server-api-key",
        {"access_token": "jwt"},
        transport=httpx.MockTransport(respond),
    )

    candidates = provider.search_instruments_for_exchange("SBIN", "NSE", 10)

    assert [item.provider_instrument_id for item in candidates] == ["3045|NSE"]
    assert exchanges == ["NSE"]


def test_angel_one_health_does_not_claim_live_connectivity_from_saved_session() -> None:
    health = AngelOneProvider("server-api-key", {"access_token": "jwt"}).health()

    assert health.healthy is False
    assert health.status == "authenticated_session_connectivity_unverified"


def test_angel_one_binary_tick_decoder_uses_scaled_prices_and_event_time() -> None:
    packet = bytearray(123)
    packet[0] = 3
    packet[1] = 1
    packet[2:27] = b"3045".ljust(25, b"\0")
    timestamp_ms = int(datetime(2026, 6, 12, 4, 45, tzinfo=timezone.utc).timestamp() * 1000)
    struct.pack_into("<q", packet, 35, timestamp_ms)
    struct.pack_into("<q", packet, 43, 12345)
    struct.pack_into("<q", packet, 67, 1000)
    struct.pack_into("<q", packet, 91, 12000)
    struct.pack_into("<q", packet, 99, 12500)
    struct.pack_into("<q", packet, 107, 11900)
    struct.pack_into("<q", packet, 115, 12200)

    tick = AngelOneProvider.decode_stream_tick(bytes(packet))

    assert tick["token"] == "3045"
    assert tick["timestamp"] == datetime(2026, 6, 12, 4, 45, tzinfo=timezone.utc)
    assert tick["ltp"] == Decimal("123.45")
    assert tick["open"] == Decimal("120")
    assert tick["volume"] == Decimal("1000")


def test_angel_one_stream_subscribes_to_verified_token_and_uses_ltp(monkeypatch) -> None:
    packet = bytearray(123)
    packet[0] = 3
    packet[1] = 1
    packet[2:27] = b"3045".ljust(25, b"\0")
    struct.pack_into(
        "<q",
        packet,
        35,
        int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    struct.pack_into("<q", packet, 43, 12345)
    struct.pack_into("<q", packet, 67, 1000)
    struct.pack_into("<q", packet, 91, 12000)
    struct.pack_into("<q", packet, 99, 12500)
    struct.pack_into("<q", packet, 107, 11900)
    struct.pack_into("<q", packet, 115, 12200)
    sent: list[dict[str, object]] = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, value: str) -> None:
            sent.append(json.loads(value))

        def __aiter__(self):
            async def messages():
                yield bytes(packet)
            return messages()

    monkeypatch.setattr(
        "app.provider_adapters.websocket_connect",
        lambda *_args, **_kwargs: Socket(),
    )
    provider = AngelOneProvider(
        "server-api-key",
        {"access_token": "jwt", "client_code": "client-1", "feed_token": "feed"},
    )

    async def collect_bars():
        return [bar async for bar in provider.stream_quote(instrument())]

    bars = asyncio.run(collect_bars())

    assert sent[0]["params"]["tokenList"] == [{"exchangeType": 1, "tokens": ["3045"]}]
    assert bars[0].close == Decimal("123.45")
