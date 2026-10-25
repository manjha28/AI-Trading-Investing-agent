from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.api.market_data import create_market_data_provider_chain, get_market_data_provider
from app.config import settings
from app.market_data import InstrumentCandidate, MarketDataProviderError
from app.twelve_data import TwelveDataProvider


def _instrument_record(**updates):
    return {
        "symbol": "AAPL",
        "instrument_name": "Apple Inc",
        "exchange": "NASDAQ",
        "mic_code": "XNGS",
        "exchange_timezone": "America/New_York",
        "instrument_type": "Common Stock",
        "country": "United States",
        "currency": "USD",
        **updates,
    }


def test_search_returns_venue_qualified_unverified_instruments() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/symbol_search")
        assert request.url.params["symbol"] == "AAPL"
        assert request.url.params["outputsize"] == "5"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": [
                    _instrument_record(),
                    _instrument_record(
                        exchange="BATS",
                        mic_code="BATS",
                        instrument_name="Apple Inc alternate listing",
                    ),
                    _instrument_record(country="Unsupported Country"),
                ],
            },
        )

    provider = TwelveDataProvider(
        "server-side-test-key", transport=httpx.MockTransport(respond)
    )

    results = provider.search_instruments("AAPL", 5)

    assert len(results) == 2
    assert results[0].provider == "twelve_data"
    assert results[0].provider_instrument_id == "AAPL|mic_code:XNGS"
    assert results[0].venue == "XNGS"
    assert results[0].country == "US"
    assert results[0].is_synthetic is False
    assert results[0].is_verified is False
    assert results[0].provider_instrument_id != results[1].provider_instrument_id


def test_historical_bars_are_parsed_sorted_bounded_and_requested_in_utc() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert request.url.path.endswith("/time_series")
        assert params["interval"] == "1day"
        assert params["timezone"] == "UTC"
        assert params["order"] == "ASC"
        assert params["outputsize"] == "2"
        assert params["mic_code"] == "XNGS"
        assert params["start_date"] == "2026-04-01 00:00:00"
        assert params["end_date"] == "2026-04-04 00:00:00"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "values": [
                    {
                        "datetime": "2026-04-03",
                        "open": "12",
                        "high": "14",
                        "low": "11",
                        "close": "13",
                        "volume": "1200",
                    },
                    {
                        "datetime": "2026-04-01",
                        "open": "10",
                        "high": "12",
                        "low": "9",
                        "close": "11",
                        "volume": "1000",
                    },
                    {
                        "datetime": "2026-04-04",
                        "open": "13",
                        "high": "14",
                        "low": "12",
                        "close": "13",
                        "volume": "900",
                    },
                ],
            },
        )

    provider = TwelveDataProvider("server-side-test-key", transport=httpx.MockTransport(respond))
    instrument = InstrumentCandidate(
        provider="twelve_data",
        provider_instrument_id="AAPL|mic_code:XNGS",
        provider_symbol="AAPL:XNGS",
        symbol="AAPL",
        display_name="Apple Inc",
        asset_class="equity",
        venue="XNGS",
        currency="USD",
        country="US",
        timezone="America/New_York",
        is_synthetic=False,
    )

    bars = provider.get_historical_bars(
        instrument,
        "1d",
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 4, 4, tzinfo=timezone.utc),
        2,
    )

    assert len(bars) == 2
    assert [bar.timestamp for bar in bars] == [
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 4, 3, tzinfo=timezone.utc),
    ]
    assert bars[0].close == Decimal("11")


def test_quote_reads_latest_bar_without_claiming_synthetic_data() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/quote")
        assert request.url.params["symbol"] == "AAPL"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "datetime": "2026-04-03 15:00:00",
                "open": "12",
                "high": "14",
                "low": "11",
                "close": "13",
                "volume": "1200",
            },
        )

    provider = TwelveDataProvider("server-side-test-key", transport=httpx.MockTransport(respond))
    candidate = InstrumentCandidate(
        provider="twelve_data",
        provider_instrument_id="AAPL|mic_code:XNGS",
        provider_symbol="AAPL:XNGS",
        symbol="AAPL",
        display_name="Apple Inc",
        asset_class="equity",
        venue="XNGS",
        currency="USD",
        country="US",
        timezone="America/New_York",
        is_synthetic=False,
    )

    quote = provider.get_latest_quote(candidate)

    assert quote.timestamp == datetime(2026, 4, 3, 15, tzinfo=timezone.utc)
    assert quote.close == Decimal("13")


def test_rate_limit_is_reported_without_retry_or_secret_leak(monkeypatch) -> None:
    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"status": "error", "message": "rate limit exceeded"})

    monkeypatch.setattr("app.twelve_data.time.sleep", lambda _seconds: None)
    provider = TwelveDataProvider(
        "server-side-test-key", transport=httpx.MockTransport(respond)
    )

    with pytest.raises(MarketDataProviderError, match="provider_rate_limit_exceeded") as error:
        provider.search_instruments("AAPL", 5)

    assert attempts == 1
    assert "server-side-test-key" not in str(error.value)


def test_transient_server_error_is_retried_once(monkeypatch) -> None:
    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "ok", "data": [_instrument_record()]})

    monkeypatch.setattr("app.twelve_data.time.sleep", lambda _seconds: None)
    provider = TwelveDataProvider(
        "server-side-test-key", transport=httpx.MockTransport(respond)
    )

    results = provider.search_instruments("AAPL", 1)

    assert attempts == 2
    assert len(results) == 1


def test_provider_error_payload_does_not_echo_upstream_message() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "message": "private details and server-side-test-key"},
        )

    provider = TwelveDataProvider(
        "server-side-test-key", transport=httpx.MockTransport(respond)
    )

    with pytest.raises(MarketDataProviderError, match="provider_request_failed") as error:
        provider.search_instruments("AAPL", 1)

    assert "private details" not in str(error.value)
    assert "server-side-test-key" not in str(error.value)


def test_oversized_provider_response_is_rejected() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b" " * 2_000_001)

    provider = TwelveDataProvider(
        "server-side-test-key", transport=httpx.MockTransport(respond)
    )

    with pytest.raises(MarketDataProviderError, match="provider_response_too_large"):
        provider.search_instruments("AAPL", 1)


def test_health_does_not_claim_verified_live_connectivity() -> None:
    provider = TwelveDataProvider("server-side-test-key")

    health = provider.health()

    assert health.healthy is False
    assert health.is_mock is False
    assert health.status == "credentials_configured_connectivity_unverified"


def test_provider_factory_missing_credentials_falls_through_to_unkeyed_provider(monkeypatch) -> None:
    monkeypatch.setattr(settings, "twelve_data_api_key", SecretStr(""))
    monkeypatch.setattr(settings, "finnhub_api_key", SecretStr(""))

    provider = create_market_data_provider_chain(
        ("twelve_data", "finnhub", "yfinance", "mock")
    )

    assert [item.name for item in provider.providers] == ["yfinance"]


def test_provider_factory_does_not_select_mock(monkeypatch) -> None:
    provider = get_market_data_provider()

    assert "mock" not in [item.name for item in provider.providers]
    assert provider.is_mock is False


def test_provider_factory_selects_twelve_data_with_server_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "twelve_data_api_key", SecretStr("server-side-test-key"))

    provider = create_market_data_provider_chain(("twelve_data", "mock"))

    assert isinstance(provider.providers[0], TwelveDataProvider)
