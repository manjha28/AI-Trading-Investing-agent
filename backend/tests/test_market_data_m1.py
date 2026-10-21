from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from pydantic import SecretStr

from app import provider_adapters
from app.api.market_data import (
    MarketDataProviderChain,
    create_market_data_provider_chain,
    get_market_data_provider,
)
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.market_data import InstrumentCandidate, MarketDataProviderError, MockMarketDataProvider
from app.models import Instrument, MarketBar, MarketDataSync, MarketQuote, PortfolioTransaction
from app.provider_adapters import (
    BinanceProvider,
    DeltaExchangeProvider,
    FinnhubProvider,
    YahooFinanceProvider,
    binance_provider_symbol,
    canonical_provider_symbol,
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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(client: TestClient, email: str = "m1-market@example.com") -> None:
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


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


class DeadFinnhub(MockMarketDataProvider):
    name = "finnhub"
    is_mock = False

    def search_instruments(self, query: str, limit: int):
        raise MarketDataProviderError("provider_rate_limit_exceeded")

    def get_latest_quote(self, instrument):
        raise MarketDataProviderError("provider_network_error")

    def get_historical_bars(self, instrument, interval, start, end, limit):
        raise MarketDataProviderError("provider_network_error")


def test_provider_chain_falls_back_for_search_quotes_and_history(client: TestClient) -> None:
    register(client)
    chain = MarketDataProviderChain([DeadFinnhub(), MockMarketDataProvider()])
    assert [item.symbol for item in chain.search_instruments("ALPHA", 10)] == ["ALPHA", "ALPHA"]
    app.dependency_overrides[get_market_data_provider] = lambda: chain
    instrument_id = client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]

    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")
    history = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-03T00:00:00Z",
        },
    )

    assert quote.status_code == 200, quote.text
    assert quote.json()["source"] == "mock"
    assert quote.json()["is_synthetic"] is True
    assert history.status_code == 200, history.text
    assert history.json()["freshness"]["source"] == "mock"
    assert all(bar["is_synthetic"] for bar in history.json()["bars"])


def test_missing_provider_credentials_degrade_to_mock(monkeypatch) -> None:
    monkeypatch.setattr(settings, "twelve_data_api_key", SecretStr(""))
    monkeypatch.setattr(settings, "finnhub_api_key", SecretStr(""))

    chain = create_market_data_provider_chain(("twelve_data", "finnhub", "yfinance", "mock"))

    assert [provider.name for provider in chain.providers] == [
        "yfinance",
        "mock",
    ]
    assert chain.providers[-1].is_mock is True


def test_upstox_is_skipped_until_a_connected_account_token_is_supplied() -> None:
    without_account = create_market_data_provider_chain(("upstox", "mock"))
    with_account = create_market_data_provider_chain(
        ("upstox", "mock"),
        upstox_access_token="connected-account-token",
    )

    assert [provider.name for provider in without_account.providers] == ["mock"]
    assert [provider.name for provider in with_account.providers] == ["upstox", "mock"]


def test_finnhub_rate_limit_is_normalized_without_retry() -> None:
    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": "API limit reached"})

    provider = FinnhubProvider("server-side-key", transport=httpx.MockTransport(respond))

    with pytest.raises(MarketDataProviderError, match="provider_rate_limit_exceeded"):
        provider.search_instruments("AAPL", 5)
    assert attempts == 1


def test_indian_provider_symbols_resolve_to_nse_and_bse_suffixes() -> None:
    assert canonical_provider_symbol("RELIANCE", "NSE", "IN") == "RELIANCE.NS"
    assert canonical_provider_symbol("RELIANCE", "XNSE", "IN") == "RELIANCE.NS"
    assert canonical_provider_symbol("RELIANCE", "BSE", "IN") == "RELIANCE.BO"
    assert canonical_provider_symbol("RELIANCE", "XBOM", "IN") == "RELIANCE.BO"
    assert binance_provider_symbol("BTCUSD") == "BTCUSDT"


def test_default_chain_is_free_first_and_does_not_read_paid_keys(monkeypatch) -> None:
    def fail_if_key_read(_self):
        raise AssertionError("paid provider credentials must not be read by the default chain")

    monkeypatch.setattr(SecretStr, "get_secret_value", fail_if_key_read)
    monkeypatch.setattr(
        settings,
        "market_data_providers",
        "delta_exchange,binance,yfinance,upstox,mock",
    )

    chain = create_market_data_provider_chain()

    assert [provider.name for provider in chain.providers] == [
        "delta_exchange",
        "binance",
        "yfinance",
        "mock",
    ]


def test_default_chain_skips_optional_paid_providers_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(settings, "twelve_data_api_key", SecretStr(""))
    monkeypatch.setattr(settings, "finnhub_api_key", SecretStr(""))

    chain = create_market_data_provider_chain(
        ("twelve_data", "finnhub", "delta_exchange", "binance", "yfinance", "mock")
    )

    assert [provider.name for provider in chain.providers] == [
        "delta_exchange",
        "binance",
        "yfinance",
        "mock",
    ]


def test_optional_paid_providers_are_enabled_only_when_named_and_credentialed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "twelve_data_api_key", SecretStr("configured-twelve-key"))
    monkeypatch.setattr(settings, "finnhub_api_key", SecretStr("configured-finnhub-key"))

    chain = create_market_data_provider_chain(("twelve_data", "finnhub", "yfinance", "mock"))

    assert [provider.name for provider in chain.providers] == [
        "twelve_data",
        "finnhub",
        "yfinance",
        "mock",
    ]


@pytest.mark.parametrize(
    ("twelve_key", "finnhub_key", "expected"),
    (
        ("twelve-key", "", ["twelve_data", "yfinance", "mock"]),
        ("", "finnhub-key", ["finnhub", "yfinance", "mock"]),
    ),
)
def test_paid_chain_providers_require_their_own_credentials(
    monkeypatch, twelve_key, finnhub_key, expected
) -> None:
    monkeypatch.setattr(settings, "twelve_data_api_key", SecretStr(twelve_key))
    monkeypatch.setattr(settings, "finnhub_api_key", SecretStr(finnhub_key))

    chain = create_market_data_provider_chain(("twelve_data", "finnhub", "yfinance", "mock"))

    assert [provider.name for provider in chain.providers] == expected


def test_delta_exchange_returns_real_candles_without_credentials() -> None:
    start = datetime(2026, 10, 20, tzinfo=timezone.utc)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.india.delta.exchange"
        if request.url.path == "/v2/products":
            return httpx.Response(
                200,
                json={"result": [{"id": 1, "symbol": "BTCUSD", "product_type": "spot"}]},
            )
        assert request.url.path == "/v2/history/candles"
        assert "api_key" not in request.url.params
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "time": int(start.timestamp()),
                        "open": "100",
                        "high": "110",
                        "low": "90",
                        "close": "105",
                        "volume": "12",
                    }
                ]
            },
        )

    DeltaExchangeProvider.clear_product_cache()
    provider = DeltaExchangeProvider(transport=httpx.MockTransport(respond))
    instrument = provider.get_instrument("BTCUSD")
    bars = provider.get_historical_bars(instrument, "1m", start, start + timedelta(minutes=1), 10)

    assert instrument.symbol == "BTCUSD"
    assert instrument.asset_class == "crypto"
    assert bars[0].close == Decimal("105")


def test_delta_exchange_history_paginates_beyond_2000_and_deduplicates() -> None:
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/v2/products":
            return httpx.Response(
                200,
                json={"result": [{"id": 1, "symbol": "BTCUSD", "product_type": "spot"}]},
            )
        calls += 1
        lower = int(request.url.params["start"])
        upper = int(request.url.params["end"])
        stamps = list(range(lower, upper, 60))[:2000]
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "time": stamp,
                        "open": "10",
                        "high": "11",
                        "low": "9",
                        "close": "10.5",
                        "volume": "1",
                    }
                    for stamp in stamps
                ]
            },
        )

    DeltaExchangeProvider.clear_product_cache()
    provider = DeltaExchangeProvider(transport=httpx.MockTransport(respond))
    instrument = provider.get_instrument("BTCUSD")
    bars = provider.get_historical_bars(
        instrument, "1m", start, start + timedelta(minutes=2100), 2100
    )

    assert calls == 2
    assert len(bars) == 2100
    assert len({bar.timestamp for bar in bars}) == 2100


def test_binance_history_paginates_after_1000_candles() -> None:
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"}
                    ]
                },
            )
        calls += 1
        cursor = int(request.url.params["startTime"])
        total = min(1000, int(request.url.params["limit"]))
        return httpx.Response(
            200,
            json=[
                [
                    cursor + index * 60_000,
                    "10",
                    "11",
                    "9",
                    "10.5",
                    "1",
                    cursor + index * 60_000 + 59_999,
                ]
                for index in range(total)
            ],
        )

    provider = BinanceProvider(transport=httpx.MockTransport(respond))
    instrument = provider.get_instrument("BTCUSDT")
    bars = provider.get_historical_bars(
        instrument, "1m", start, start + timedelta(minutes=1001), 1001
    )

    assert calls == 2
    assert len(bars) == 1001
    assert len({bar.timestamp for bar in bars}) == 1001


def test_binance_geo_or_access_restrictions_are_provider_unavailable() -> None:
    provider = BinanceProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(451, json={"msg": "Unavailable for legal reasons"})
        )
    )

    with pytest.raises(MarketDataProviderError, match="provider_request_failed"):
        provider.get_latest_quote(
            InstrumentCandidate(
                provider="binance",
                provider_instrument_id="BTCUSDT",
                provider_symbol="BTCUSDT",
                symbol="BTCUSDT",
                display_name="Bitcoin/Tether",
                asset_class="crypto",
                venue="BINANCE",
                currency="USDT",
                country="US",
                timezone="UTC",
                is_synthetic=False,
            )
        )


@pytest.mark.parametrize(
    ("interval", "days"),
    (("1m", 7), ("15m", 60), ("1h", 730)),
)
def test_yfinance_interval_lookback_errors_instead_of_truncating(interval, days) -> None:
    provider = YahooFinanceProvider()
    instrument = InstrumentCandidate(
        provider="yfinance",
        provider_instrument_id="AAPL|NASDAQ",
        provider_symbol="AAPL",
        symbol="AAPL",
        display_name="Apple Inc.",
        asset_class="equity",
        venue="NASDAQ",
        currency="USD",
        country="US",
        timezone="America/New_York",
        is_synthetic=False,
    )

    with pytest.raises(MarketDataProviderError, match=f"yfinance_{interval}_lookback_exceeded_{days}_days"):
        provider.get_historical_bars(
            instrument,
            interval,
            datetime.now(timezone.utc) - timedelta(days=days + 1),
            datetime.now(timezone.utc),
            100,
        )


def test_mock_catalog_is_limited_to_two_demo_instruments() -> None:
    provider = MockMarketDataProvider()

    demo_instruments = provider.search_instruments("ALPHA", 50)

    assert len(demo_instruments) == 2
    assert {item.symbol for item in demo_instruments} == {"ALPHA"}
    assert provider.search_instruments("INDEX1", 50) == []


def test_real_instruments_never_expose_legacy_synthetic_quote_or_history(
    client: TestClient, test_engine
) -> None:
    register(client, "real-symbol-unavailable@example.com")
    now = datetime.now(timezone.utc)
    with Session(test_engine) as db:
        instrument = Instrument(
            symbol="MSFT",
            display_name="Microsoft Corporation",
            asset_class="equity",
            venue="NASDAQ",
            currency="USD",
            country="US",
            timezone="America/New_York",
            is_synthetic=False,
            created_at=now,
        )
        db.add(instrument)
        db.flush()
        db.add(
            MarketQuote(
                instrument_id=instrument.id,
                timestamp=now,
                open=Decimal("9"),
                high=Decimal("12"),
                low=Decimal("8"),
                close=Decimal("11"),
                volume=Decimal("100"),
                source="mock",
                retrieved_at=now,
                is_synthetic=True,
            )
        )
        db.add(
            MarketBar(
                instrument_id=instrument.id,
                interval="1d",
                timestamp=now - timedelta(days=1),
                open=Decimal("9"),
                high=Decimal("12"),
                low=Decimal("8"),
                close=Decimal("11"),
                volume=Decimal("100"),
                source="mock",
                retrieved_at=now,
                is_synthetic=True,
            )
        )
        db.add(
            MarketDataSync(
                instrument_id=instrument.id,
                interval="1d",
                source="mock",
                attempted_at=now,
                succeeded_at=now,
                status="success",
                rows_received=1,
            )
        )
        instrument_id = instrument.id
        db.commit()

    class UnavailableProvider(MockMarketDataProvider):
        name = "yfinance"
        is_mock = False

        def search_instruments(self, query, limit):
            raise MarketDataProviderError("provider_network_error")

        def get_latest_quote(self, instrument):
            raise MarketDataProviderError("provider_network_error")

        def get_historical_bars(self, instrument, interval, start, end, limit):
            raise MarketDataProviderError("provider_network_error")

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [UnavailableProvider(), MockMarketDataProvider()]
    )
    quote = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")
    history = client.get(
        f"/api/v1/market-data/instruments/{instrument_id}/history",
        params={"interval": "1d"},
    )
    synced = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
        },
    )

    assert quote.status_code == 503
    assert history.status_code == 200
    assert history.json()["bars"] == []
    assert history.json()["freshness"]["status"] == "unavailable"
    assert history.json()["freshness"]["is_synthetic"] is False
    assert synced.status_code == 503


def test_stored_candle_range_is_not_refetched(
    client: TestClient, test_engine
) -> None:
    register(client, "history-cache@example.com")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(test_engine) as db:
        instrument = Instrument(
            symbol="AAPL",
            display_name="Apple Inc.",
            asset_class="equity",
            venue="NASDAQ",
            currency="USD",
            country="US",
            timezone="America/New_York",
            is_synthetic=False,
            created_at=now,
        )
        db.add(instrument)
        db.flush()
        instrument_id = instrument.id
        db.add(
            MarketBar(
                instrument_id=instrument_id,
                interval="1d",
                timestamp=now,
                open=Decimal("9"),
                high=Decimal("12"),
                low=Decimal("8"),
                close=Decimal("11"),
                volume=Decimal("100"),
                source="yfinance",
                retrieved_at=now,
                is_synthetic=False,
            )
        )
        db.commit()

    class MustNotFetch(MockMarketDataProvider):
        name = "yfinance"
        is_mock = False

        def get_historical_bars(self, instrument, interval, start, end, limit):
            raise AssertionError("a stored date range must not be fetched again")

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [MustNotFetch(), MockMarketDataProvider()]
    )
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument_id}/history/sync",
        params={
            "interval": "1d",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["bars"][0]["close"] == "11.00000000"


def test_yahoo_adapter_reads_indian_quote_and_candles_without_api_key(monkeypatch) -> None:
    timestamp = int(datetime(2026, 4, 3, 10, tzinfo=timezone.utc).timestamp())
    provider = YahooFinanceProvider()
    instrument = InstrumentCandidate(
        provider="yfinance",
        provider_instrument_id="RELIANCE.NS|NSE",
        provider_symbol="RELIANCE.NS",
        symbol="RELIANCE",
        display_name="Reliance Industries",
        asset_class="equity",
        venue="NSE",
        currency="INR",
        country="IN",
        timezone="Asia/Kolkata",
        is_synthetic=False,
    )
    frame = pd.DataFrame(
        {"Open": [9], "High": [11], "Low": [8], "Close": [10.5], "Volume": [100]},
        index=pd.to_datetime([timestamp], unit="s", utc=True),
    )

    def history_frame(ticker, *, interval, **_kwargs):
        assert ticker == "RELIANCE.NS"
        assert interval in {"1d", "60m"}
        return frame

    monkeypatch.setattr(provider, "_history_frame", history_frame)

    quote = provider.get_latest_quote(instrument)
    bars = provider.get_historical_bars(
        instrument,
        "1d",
        datetime.fromtimestamp(timestamp - 86400, tz=timezone.utc),
        datetime.fromtimestamp(timestamp + 86400, tz=timezone.utc),
        10,
    )

    assert quote.close == Decimal("10.5")
    assert len(bars) == 1
    assert bars[0].close == Decimal("10.5")


def test_yfinance_search_maps_nse_bse_and_us_names(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_adapters.yf,
        "Search",
        lambda *_args, **_kwargs: type(
            "SearchResult",
            (),
            {
                "quotes": [
                    {"symbol": "RELIANCE.NS", "exchange": "NSI", "shortname": "Reliance NSE"},
                    {"symbol": "RELIANCE.BO", "exchange": "BSE", "longname": "Reliance BSE"},
                    {"symbol": "AAPL", "exchange": "NMS", "shortname": "Apple Inc."},
                ]
            },
        )(),
    )

    results = YahooFinanceProvider().search_instruments("Reliance", 10)
    by_symbol = {item.provider_symbol: item for item in results}

    assert by_symbol["RELIANCE.NS"].venue == "NSE"
    assert by_symbol["RELIANCE.NS"].currency == "INR"
    assert by_symbol["RELIANCE.BO"].venue == "BSE"
    assert by_symbol["AAPL"].country == "US"
    assert by_symbol["AAPL"].display_name == "Apple Inc."


def _create_portfolio(client: TestClient, name: str = "M1 portfolio") -> str:
    response = client.post(
        "/api/v1/portfolios",
        json={"name": name, "base_currency": "USD"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _add_position(client: TestClient, portfolio_id: str, symbol: str) -> None:
    deposit = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"transaction_type": "deposit", "amount": "10000", "occurred_at": "2026-01-01T00:00:00Z"},
        headers=csrf_headers(client),
    )
    assert deposit.status_code == 201, deposit.text
    buy = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "transaction_type": "buy",
            "symbol": symbol,
            "quantity": "2",
            "unit_price": "10",
            "occurred_at": "2026-01-02T00:00:00Z",
        },
        headers=csrf_headers(client),
    )
    assert buy.status_code == 201, buy.text


def test_valuation_flags_mixed_fresh_stale_and_synthetic_prices(
    client: TestClient, test_engine
) -> None:
    register(client, "valuation-mixed@example.com")
    portfolio_id = _create_portfolio(client)
    _add_position(client, portfolio_id, "ALPHA")
    alpha = client.get(
        "/api/v1/market-data/instruments",
        params={"query": "ALPHA"},
    ).json()[0]
    attached = client.post(
        f"/api/v1/portfolios/{portfolio_id}/positions/ALPHA/instrument",
        json={"instrument_id": alpha["id"]},
        headers=csrf_headers(client),
    )
    assert attached.status_code == 200, attached.text

    instrument_ids: dict[str, UUID] = {}
    for symbol in ("MSFT", "ORCL"):
        _add_position(client, portfolio_id, symbol)
        with Session(test_engine) as db:
            instrument = Instrument(
                symbol=symbol,
                display_name=f"{symbol} Inc.",
                asset_class="equity",
                venue="NASDAQ",
                currency="USD",
                country="US",
                timezone="America/New_York",
                is_synthetic=False,
                created_at=datetime.now(timezone.utc),
            )
            db.add(instrument)
            db.flush()
            transaction = db.scalar(
                select(PortfolioTransaction).where(
                    PortfolioTransaction.portfolio_id == UUID(portfolio_id),
                    PortfolioTransaction.symbol == symbol,
                    PortfolioTransaction.transaction_type == "buy",
                )
            )
            assert transaction is not None
            transaction.instrument_id = instrument.id
            instrument_ids[symbol] = instrument.id
            db.commit()

    now = datetime.now(timezone.utc)
    with Session(test_engine) as db:
        for symbol, instrument_id in instrument_ids.items():
            retrieved_at = now - timedelta(minutes=5) if symbol == "ORCL" else now
            db.add(
                MarketQuote(
                    instrument_id=instrument_id,
                    timestamp=now,
                    open=Decimal("9"),
                    high=Decimal("12"),
                    low=Decimal("8"),
                    close=Decimal("11"),
                    volume=Decimal("100"),
                    source="yfinance",
                    retrieved_at=retrieved_at,
                    is_synthetic=False,
                )
            )
        db.commit()

    class UnavailableProvider(MockMarketDataProvider):
        name = "yfinance"
        is_mock = False

        def search_instruments(self, query, limit):
            raise MarketDataProviderError("provider_network_error")

        def get_latest_quote(self, instrument):
            raise MarketDataProviderError("provider_network_error")

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain(
        [UnavailableProvider(), MockMarketDataProvider()]
    )
    response = client.get(f"/api/v1/portfolios/{portfolio_id}/valuation")

    assert response.status_code == 200, response.text
    valuation = response.json()
    prices = {position["symbol"]: position for position in valuation["positions"]}
    assert prices["MSFT"]["freshness"] == "fresh"
    assert prices["ORCL"]["freshness"] == "stale"
    assert prices["ALPHA"]["is_synthetic"] is True
    assert valuation["confidence"] == "unreliable"
    assert any("stale" in reason for reason in valuation["reasons"])
    assert any("synthetic" in reason for reason in valuation["reasons"])


def test_valuation_does_not_convert_foreign_currency(client: TestClient, test_engine) -> None:
    register(client, "valuation-currency@example.com")
    portfolio_id = _create_portfolio(client, "USD only")
    _add_position(client, portfolio_id, "ALPHA")
    candidates = client.get(
        "/api/v1/market-data/instruments",
        params={"query": "ALPHA"},
    ).json()
    indian_instrument = next(item for item in candidates if item["venue"] == "DEMO_IN")
    with Session(test_engine) as db:
        transaction = db.scalar(
            select(PortfolioTransaction).where(
                PortfolioTransaction.portfolio_id == UUID(portfolio_id),
                PortfolioTransaction.transaction_type == "buy",
            )
        )
        assert transaction is not None
        transaction.instrument_id = UUID(indian_instrument["id"])
        db.commit()

    response = client.get(f"/api/v1/portfolios/{portfolio_id}/valuation")

    assert response.status_code == 200, response.text
    position = response.json()["positions"][0]
    assert position["freshness"] == "unavailable"
    assert position["market_value"] is None
    assert response.json()["market_value"] == "0.00"
    assert response.json()["confidence"] == "unreliable"
    assert any("no FX conversion" in reason for reason in response.json()["reasons"])


def test_synthetic_cleanup_preserves_linked_rows_and_marks_for_relinking(
    client: TestClient,
) -> None:
    register(client, "cleanup-synthetic@example.com")
    portfolio_id = _create_portfolio(client)
    _add_position(client, portfolio_id, "ALPHA")
    candidates = client.get(
        "/api/v1/market-data/instruments",
        params={"query": "ALPHA"},
    ).json()
    linked = candidates[0]
    response = client.post(
        f"/api/v1/portfolios/{portfolio_id}/positions/ALPHA/instrument",
        json={"instrument_id": linked["id"]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text

    cleanup = client.post(
        "/api/v1/market-data/instruments/cleanup-synthetic",
        headers=csrf_headers(client),
    )

    assert cleanup.status_code == 200, cleanup.text
    result = cleanup.json()
    assert result["marked_for_relink"] == 1
    assert result["deleted_unlinked"] == 1
    assert linked["id"] in result["relink_ids"]
    details = client.get(f"/api/v1/market-data/instruments/{linked['id']}")
    assert details.status_code == 200
    assert details.json()["relink_required"] is True
