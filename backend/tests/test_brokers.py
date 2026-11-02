import asyncio
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from datetime import datetime, timezone
import struct
from types import SimpleNamespace
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import brokers, market_data
from app import broker_adapters
from app.broker_adapters import (
    BrokerAdapterError,
    DhanAdapter,
    HistoricalBar,
    UpstoxAdapter,
)
from app.config import settings
from app.db import Base, get_db
from app.main import BrokerCallbackAccessLogFilter, app
from app.models import (
    BrokerAccountMirror,
    BrokerCredential,
    BrokerMirror,
    Instrument,
    InstrumentProviderIdentifier,
    MarketBar,
    MarketDataSync,
    MarketQuote,
    PortfolioTransaction,
    UserSession,
)
from app.provider_adapters import DhanAccountRequestError
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
def dhan_position_mix():
    return [
        {
            "tradingSymbol": "OPEN_LONG",
            "securityId": "1001",
            "exchangeSegment": "NSE_FNO",
            "productType": "MARGIN",
            "positionType": "LONG",
            "netQty": 2,
            "costPrice": "125.50",
            "realizedProfit": "3.25",
            "unrealizedProfit": "4.50",
        },
        {
            "tradingSymbol": "OPEN_SHORT",
            "securityId": "1002",
            "exchangeSegment": "NSE_FNO",
            "productType": "MARGIN",
            "positionType": "SHORT",
            "netQty": -1,
            "costPrice": "75.00",
            "realizedProfit": "1.00",
            "unrealizedProfit": "-2.50",
        },
        {
            "tradingSymbol": "ZERO_LONG",
            "securityId": "1003",
            "exchangeSegment": "NSE_FNO",
            "productType": "MARGIN",
            "positionType": "LONG",
            "netQty": 0,
            "costPrice": "10.00",
            "realizedProfit": "0.00",
            "unrealizedProfit": "0.00",
        },
        {
            "tradingSymbol": "CLOSED_NONZERO",
            "securityId": "1004",
            "exchangeSegment": "NSE_FNO",
            "productType": "MARGIN",
            "positionType": "CLOSED",
            "netQty": 1,
            "costPrice": "20.00",
            "realizedProfit": "15.00",
            "unrealizedProfit": "0.00",
        },
    ]


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


def seed_dhan_portfolio_summary(
    client: TestClient,
    test_engine,
    encryption_key,
    monkeypatch,
) -> datetime:
    prepare_dhan_sync(client, test_engine, encryption_key, monkeypatch)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(brokers, "_now", lambda: now)
    monkeypatch.setattr(
        market_data, "_market_session_state", lambda *_args: "open"
    )
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        sbin = Instrument(
            symbol="SBIN",
            display_name="State Bank of India",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=now,
        )
        xyz = Instrument(
            symbol="XYZ",
            display_name="XYZ Limited",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=now,
        )
        db.add_all([sbin, xyz])
        db.flush()
        db.add_all([
            InstrumentProviderIdentifier(
                instrument_id=sbin.id,
                provider="dhan",
                provider_instrument_id="3045|NSE_EQ",
                provider_symbol="SBIN",
                is_verified=True,
                last_seen_at=now,
            ),
            InstrumentProviderIdentifier(
                instrument_id=xyz.id,
                provider="dhan",
                provider_instrument_id="9999|NSE_EQ",
                provider_symbol="XYZ",
                is_verified=True,
                last_seen_at=now,
            ),
            BrokerAccountMirror(
                user_id=user_session.user_id,
                broker="dhan",
                currency="INR",
                available_balance=Decimal("50"),
                synced_at=now,
            ),
            BrokerMirror(
                user_id=user_session.user_id,
                broker="dhan",
                kind="holding",
                symbol="SBIN",
                exchange="NSE",
                product="",
                security_id="3045",
                quantity=Decimal("2"),
                average_price=Decimal("90"),
                synced_at=now,
            ),
            BrokerMirror(
                user_id=user_session.user_id,
                broker="dhan",
                kind="position",
                symbol="SBIN",
                exchange="NSE",
                product="INTRADAY",
                security_id="3045",
                quantity=Decimal("1"),
                average_price=Decimal("100"),
                position_type="LONG",
                unrealized_profit=Decimal("7.50"),
                synced_at=now,
            ),
            BrokerMirror(
                user_id=user_session.user_id,
                broker="dhan",
                kind="holding",
                symbol="XYZ",
                exchange="NSE",
                product="",
                security_id="9999",
                quantity=Decimal("1"),
                average_price=Decimal("40"),
                synced_at=now,
            ),
            MarketBar(
                instrument_id=sbin.id,
                interval="1d",
                timestamp=now - timedelta(days=1),
                open=Decimal("94"),
                high=Decimal("96"),
                low=Decimal("93"),
                close=Decimal("95"),
                volume=Decimal("1000"),
                source="dhan",
                retrieved_at=now,
                is_synthetic=False,
            ),
        ])
        db.commit()
    return now


def _add_portfolio_close_bars(
    test_engine,
    session_dates: tuple[date, date],
    current_prices: dict[str, Decimal] | None = None,
) -> None:
    current_prices = current_prices or {
        "SBIN": Decimal("110"),
        "XYZ": Decimal("50"),
    }
    previous_prices = {"SBIN": Decimal("105"), "XYZ": Decimal("48")}
    zone = ZoneInfo("Asia/Kolkata")
    with Session(test_engine) as db:
        instruments = {
            instrument.symbol: instrument
            for instrument in db.scalars(select(Instrument))
        }
        instrument_ids = {instrument.id for instrument in instruments.values()}
        for existing_bar in db.scalars(
            select(MarketBar).where(
                MarketBar.instrument_id.in_(instrument_ids)
            )
        ):
            db.delete(existing_bar)
        for symbol, instrument in instruments.items():
            if symbol not in current_prices:
                continue
            for session_date, close in (
                (session_dates[0], current_prices[symbol]),
                (session_dates[1], previous_prices[symbol]),
            ):
                timestamp = datetime.combine(
                    session_date,
                    time(15, 30),
                    tzinfo=zone,
                ).astimezone(timezone.utc)
                db.add(MarketBar(
                    instrument_id=instrument.id,
                    interval="1d",
                    timestamp=timestamp,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=Decimal("1000"),
                    source="dhan",
                    retrieved_at=timestamp + timedelta(minutes=1),
                    is_synthetic=False,
                ))
        db.commit()


def test_broker_summary_uses_one_batched_quote_and_keeps_value_provenance(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    now = seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    session_token = first.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        delivery_holding = db.scalar(
            select(BrokerMirror).where(
                BrokerMirror.user_id == user_session.user_id,
                BrokerMirror.symbol == "XYZ",
                BrokerMirror.kind == "holding",
            )
        )
        assert delivery_holding is not None
        delivery_holding.product = "CNC"
        db.add(BrokerMirror(
            user_id=user_session.user_id,
            broker="dhan",
            kind="position",
            symbol="SBIN",
            exchange="NSE",
            product="CNC",
            security_id="3045",
            quantity=Decimal("-1"),
            average_price=Decimal("100"),
            position_type="SHORT",
            realized_profit=Decimal("0"),
            unrealized_profit=Decimal("0"),
            synced_at=now,
        ))
        db.commit()
    requests: list[dict[str, list[str]]] = []

    def get_market_quotes_batch(_self, _token, _tokens, instruments_by_segment):
        requests.append(instruments_by_segment)
        return {
            "NSE_EQ": {
                "3045": HistoricalBar(
                    timestamp=now,
                    open=Decimal("109"),
                    high=Decimal("111"),
                    low=Decimal("108"),
                    close=Decimal("110"),
                    volume=Decimal("1000"),
                ),
                "9999": HistoricalBar(
                    timestamp=now,
                    open=Decimal("49"),
                    high=Decimal("51"),
                    low=Decimal("48"),
                    close=Decimal("50"),
                    volume=Decimal("1000"),
                ),
            }
        }

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    assert len(requests) == 1
    assert requests[0] == {"NSE_EQ": ["3045", "9999"]}
    dhan = response.json()["brokers"][0]
    assert Decimal(dhan["market_value"]) == Decimal("380")
    assert Decimal(dhan["total_value"]) == Decimal("430")
    assert Decimal(dhan["invested_cost"]) == Decimal("220")
    assert Decimal(dhan["positions_exposure"]) == Decimal("100")
    assert Decimal(dhan["holdings_unrealized_pnl"]) == Decimal("50")
    assert Decimal(dhan["positions_unrealized_pnl"]) == Decimal("7.50")
    assert dhan["holdings_count"] == 2
    assert dhan["positions_count"] == 1
    assert Decimal(dhan["unrealized_pnl"]) == Decimal("57.50")
    assert dhan["day_change"] is None
    assert dhan["provenance"]["unrealized_pnl"]["type"] == "combined"
    assert "broker-supplied unrealizedProfit" in (
        dhan["provenance"]["unrealized_pnl"]["description"]
    )
    assert any(
        "XYZ: previous-session history"
        in item
        for item in dhan["provenance"]["day_change"]["exclusions"]
    )
    assert dhan["provenance"]["market_value"]["quote_observations"][0]["timestamp"]
    portfolio = first.get("/api/v1/brokers/dhan/portfolio").json()
    assert portfolio["positions"][0]["product"] == "INTRADAY"
    assert any(
        row["product"] == "CNC" for row in portfolio["holdings"]
    )
    assert len(portfolio["delivery_sales"]) == 1
    assert portfolio["delivery_sales"][0]["product"] == "CNC"
    assert Decimal(portfolio["delivery_sales"][0]["quantity"]) == Decimal("-1")


def test_broker_summary_reports_known_empty_positions_as_zero_with_evidence(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    client, _ = clients
    now = seed_dhan_portfolio_summary(
        client, test_engine, encryption_key, monkeypatch
    )
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        position = db.scalar(
            select(BrokerMirror).where(
                BrokerMirror.user_id == user_session.user_id,
                BrokerMirror.kind == "position",
            )
        )
        assert position is not None
        position.quantity = Decimal("0")
        position.realized_profit = Decimal("3.25")
        position.unrealized_profit = Decimal("0")
        db.commit()

    def get_market_quotes_batch(_self, _token, _tokens, _instruments_by_segment):
        return {"NSE_EQ": {
            "3045": HistoricalBar(
                timestamp=now, open=Decimal("110"), high=Decimal("110"),
                low=Decimal("110"), close=Decimal("110"), volume=Decimal("1"),
            ),
            "9999": HistoricalBar(
                timestamp=now, open=Decimal("50"), high=Decimal("50"),
                low=Decimal("50"), close=Decimal("50"), volume=Decimal("1"),
            ),
        }}

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    summary_response = client.get("/api/v1/brokers/portfolio-summary")
    assert summary_response.status_code == 200, summary_response.text
    summary = summary_response.json()
    dhan = summary["brokers"][0]
    assert Decimal(dhan["positions_exposure"]) == Decimal("0")
    assert Decimal(dhan["positions_unrealized_pnl"]) == Decimal("0")
    assert Decimal(summary["totals"]["positions_exposure"]) == Decimal("0")
    assert Decimal(summary["totals"]["positions_unrealized_pnl"]) == Decimal("0")
    assert dhan["unavailable_reasons"]["positions_exposure"] is None
    positions_evidence = dhan["provenance"]["positions_unrealized_pnl"]
    assert positions_evidence["type"] == "broker_supplied"
    assert "Dhan-supplied" in positions_evidence["description"]
    assert positions_evidence["coverage"] == {
        "priced": 0,
        "total": 0,
        "unit": "open positions",
    }
    assert positions_evidence["mirror_observations"][0]["source"] == (
        "Dhan broker mirror snapshot"
    )
    assert positions_evidence["mirror_observations"][0]["timestamp"]
    holdings_evidence = dhan["provenance"]["holdings_unrealized_pnl"]
    assert holdings_evidence["quote_observations"][0]["source"] == "dhan"
    assert holdings_evidence["quote_observations"][0]["timestamp"]
    assert holdings_evidence["mirror_observations"][0]["timestamp"]

    portfolio_response = client.get("/api/v1/brokers/dhan/portfolio")
    assert portfolio_response.status_code == 200, portfolio_response.text
    closed = portfolio_response.json()["closed_positions"]
    assert len(closed) == 1
    assert Decimal(closed[0]["realized_profit"]) == Decimal("3.25")


def test_dhan_stale_master_refreshes_and_creates_unverified_candidate(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    client, _ = clients
    now = seed_dhan_portfolio_summary(
        client, test_engine, encryption_key, monkeypatch
    )
    session_token = client.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        row = db.scalar(
            select(BrokerMirror).where(
                BrokerMirror.user_id == user_session.user_id,
                BrokerMirror.symbol == "XYZ",
                BrokerMirror.kind == "holding",
            )
        )
        assert row is not None
        instrument = db.scalar(select(Instrument).where(Instrument.symbol == "XYZ"))
        assert instrument is not None
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.instrument_id == instrument.id,
                InstrumentProviderIdentifier.provider == "dhan",
            )
        )
        assert mapping is not None
        mapping.provider_instrument_id = "9999|OLD_SEGMENT"
        mapping.provider_symbol = "OTHER"
        mapping.is_verified = False
        db.commit()

    stale_master = [{
        "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "0001",
        "INSTRUMENT": "EQUITY", "SERIES": "EQ", "ISIN": "OTHER",
    }]
    fresh_master = [
        {
            "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "3045",
            "INSTRUMENT": "EQUITY", "SERIES": "EQ", "ISIN": "SBINISIN",
        },
        {
            "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "9999",
            "INSTRUMENT": "EQUITY", "SERIES": "EQ", "ISIN": "XYZISIN",
            "LISTING_DATE": now.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat(),
        },
    ]
    calls = 0

    def cached_scrip_master(cls):
        nonlocal calls
        calls += 1
        if cls._scrip_master_cache is None:
            cls._scrip_master_cache = (monotonic(), fresh_master)
        return cls._scrip_master_cache[1]

    monkeypatch.setattr(
        DhanAdapter,
        "_scrip_master_cache",
        (monotonic(), stale_master),
    )
    monkeypatch.setattr(DhanAdapter, "_equity_index_cache", None)
    monkeypatch.setattr(brokers, "_dhan_master_miss_refresh_cache_time", None)
    monkeypatch.setattr(
        DhanAdapter, "_scrip_master", classmethod(cached_scrip_master)
    )

    response = client.get("/api/v1/brokers/dhan/portfolio")
    assert response.status_code == 200, response.text
    portfolio = response.json()
    candidate = next(
        item for item in portfolio["instrument_master"]["mapping_candidates"]
        if item["symbol"] == "XYZ"
    )
    assert calls == 2
    assert candidate["status"] == "pending_verification"
    assert candidate["provider_instrument_id"] == "9999|NSE_EQ"
    assert candidate["segment"] == "NSE_EQ"
    assert "verify the mapping" in candidate["verification_required"]
    assert portfolio["instrument_master"]["last_refreshed_at"] is not None
    with Session(test_engine) as db:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.provider == "dhan",
                InstrumentProviderIdentifier.provider_instrument_id
                == "9999|NSE_EQ",
            )
        )
        assert mapping is not None
        assert mapping.is_verified is False


def test_dhan_master_ambiguity_and_absence_have_specific_reasons(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    client, _ = clients
    seed_dhan_portfolio_summary(
        client, test_engine, encryption_key, monkeypatch
    )
    with Session(test_engine) as db:
        for mapping in db.scalars(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.provider == "dhan"
            )
        ):
            mapping.is_verified = False
            mapping.provider_symbol = "UNMATCHED"
        db.commit()
    master_rows = [
        {
            "EXCH_ID": exchange, "SEGMENT": "E", "SECURITY_ID": "9999",
            "INSTRUMENT": "EQUITY", "SERIES": "EQ", "ISIN": "XYZISIN",
        }
        for exchange in ("NSE", "BSE")
    ]

    def load_master(cls):
        cls._scrip_master_cache = (monotonic(), master_rows)
        return master_rows

    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", None)
    monkeypatch.setattr(
        DhanAdapter, "_scrip_master", classmethod(load_master)
    )
    response = client.get("/api/v1/brokers/dhan/portfolio")
    assert response.status_code == 200, response.text
    candidates = response.json()["instrument_master"]["mapping_candidates"]
    xyz = next(item for item in candidates if item["symbol"] == "XYZ")
    sbin = next(item for item in candidates if item["symbol"] == "SBIN")
    assert xyz["status"] == "ambiguous"
    assert "Multiple exact" in xyz["reason"]
    assert sbin["status"] == "not_found"
    assert "No exact security ID or ISIN match" in sbin["reason"]


def test_listing_day_without_previous_close_keeps_live_market_value(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    client, _ = clients
    now = seed_dhan_portfolio_summary(
        client, test_engine, encryption_key, monkeypatch
    )
    listing_date = now.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    master_rows = [
        {
            "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "3045",
            "INSTRUMENT": "EQUITY", "SERIES": "EQ", "ISIN": "SBINISIN",
        },
        {
            "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "9999",
            "INSTRUMENT": "EQUITY", "SERIES": "EQ", "ISIN": "XYZISIN",
            "LISTING_DATE": listing_date,
        },
    ]

    def load_master(cls):
        cls._scrip_master_cache = (monotonic(), master_rows)
        return master_rows

    monkeypatch.setattr(
        DhanAdapter, "_scrip_master_cache", None
    )
    monkeypatch.setattr(
        DhanAdapter, "_scrip_master", classmethod(load_master)
    )

    def get_market_quotes_batch(_self, _token, _tokens, _instruments_by_segment):
        return {"NSE_EQ": {
            "3045": HistoricalBar(
                timestamp=now, open=Decimal("110"), high=Decimal("110"),
                low=Decimal("110"), close=Decimal("110"), volume=Decimal("1"),
            ),
            "9999": HistoricalBar(
                timestamp=now, open=Decimal("50"), high=Decimal("50"),
                low=Decimal("50"), close=Decimal("50"), volume=Decimal("1"),
            ),
        }}

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    response = client.get("/api/v1/brokers/portfolio-summary")
    assert response.status_code == 200, response.text
    dhan = response.json()["brokers"][0]
    assert Decimal(dhan["market_value"]) == Decimal("380")
    assert dhan["day_change"] is None
    assert "no prior market session exists" in (
        dhan["unavailable_reasons"]["day_change"].lower()
    )
    assert any(
        "no prior market session exists" in exclusion.lower()
        for exclusion in dhan["provenance"]["day_change"]["exclusions"]
    )


def test_provider_empty_history_is_distinct_from_unfetched_history(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    client, _ = clients
    now = seed_dhan_portfolio_summary(
        client, test_engine, encryption_key, monkeypatch
    )
    timezone_info = ZoneInfo("Asia/Kolkata")
    quote_day = now.astimezone(timezone_info).date()
    session_reference = datetime.combine(
        quote_day, time.min, tzinfo=timezone_info
    ).astimezone(timezone.utc)
    with Session(test_engine) as db:
        xyz = db.scalar(select(Instrument).where(Instrument.symbol == "XYZ"))
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash
                == hash_token(client.cookies.get("session_token"))
            )
        )
        assert xyz is not None
        assert user_session is not None
        expected_previous_date = market_data._expected_market_session_date(
            xyz, session_reference
        )
        requested_start = datetime.combine(
            expected_previous_date, time.min, tzinfo=timezone_info
        ).astimezone(timezone.utc)
        requested_end = datetime.combine(
            expected_previous_date, time.max, tzinfo=timezone_info
        ).astimezone(timezone.utc)
        db.add(MarketDataSync(
            instrument_id=xyz.id,
            interval="1d",
            source="dhan",
            attempted_at=now,
            succeeded_at=now,
            status="success",
            rows_received=0,
            requested_start=requested_start,
            requested_end=requested_end,
            rows_written=0,
        ))
        db.commit()
    master_rows = [
        {
            "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "9999",
            "INSTRUMENT": "EQUITY", "SERIES": "EQ", "ISIN": "XYZISIN",
            "LISTING_DATE": (expected_previous_date - timedelta(days=30)).isoformat(),
        }
    ]

    def load_master(cls):
        cls._scrip_master_cache = (monotonic(), master_rows)
        return master_rows

    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", None)
    monkeypatch.setattr(
        DhanAdapter, "_scrip_master", classmethod(load_master)
    )

    def get_market_quotes_batch(_self, _token, _tokens, _instruments_by_segment):
        return {"NSE_EQ": {
            "3045": HistoricalBar(
                timestamp=now, open=Decimal("110"), high=Decimal("110"),
                low=Decimal("110"), close=Decimal("110"), volume=Decimal("1"),
            ),
            "9999": HistoricalBar(
                timestamp=now, open=Decimal("50"), high=Decimal("50"),
                low=Decimal("50"), close=Decimal("50"), volume=Decimal("1"),
            ),
        }}

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    response = client.get("/api/v1/brokers/portfolio-summary")
    assert response.status_code == 200, response.text
    dhan = response.json()["brokers"][0]
    assert "provider returned no daily data" in (
        dhan["unavailable_reasons"]["day_change"].lower()
    )


@pytest.mark.parametrize(
    ("quote_mode", "expected_reason"),
    [
        ("missing", "did not include LTP for XYZ"),
        ("stale", "quote is stale"),
    ],
)
def test_broker_summary_does_not_use_missing_or_stale_quote(
    clients,
    test_engine,
    encryption_key,
    monkeypatch,
    quote_mode,
    expected_reason,
) -> None:
    first, _ = clients
    now = seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)

    def get_market_quotes_batch(_self, _token, _tokens, _instruments_by_segment):
        if quote_mode == "missing":
            return {"NSE_EQ": {
                "3045": HistoricalBar(
                    timestamp=now,
                    open=Decimal("109"),
                    high=Decimal("111"),
                    low=Decimal("108"),
                    close=Decimal("110"),
                    volume=Decimal("1000"),
                )
            }}
        return {"NSE_EQ": {
            security_id: HistoricalBar(
                timestamp=now - timedelta(hours=2),
                open=Decimal("109"),
                high=Decimal("111"),
                low=Decimal("108"),
                close=Decimal("110"),
                volume=Decimal("1000"),
            )
            for security_id in ("3045", "9999")
        }}

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    monkeypatch.setattr(
        market_data,
        "market_data_provider_for_user",
        lambda _db, _user_id: market_data.MarketDataProviderChain([]),
    )
    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    dhan = response.json()["brokers"][0]
    assert dhan["market_value"] is None
    assert dhan["total_value"] is None
    assert dhan["unrealized_pnl"] is None
    assert dhan["day_change"] is None
    assert expected_reason in dhan["unavailable_reasons"]["market_value"]


def test_broker_summary_falls_back_in_configured_provider_priority(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    now = seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    with Session(test_engine) as db:
        instruments = list(db.scalars(select(Instrument)))
        for instrument in instruments:
            db.add(InstrumentProviderIdentifier(
                instrument_id=instrument.id,
                provider="angel_one",
                provider_instrument_id=instrument.symbol,
                provider_symbol=instrument.symbol,
                is_verified=True,
                last_seen_at=now,
            ))
        db.commit()

    def get_market_quotes_batch(_self, _token, _tokens, instruments_by_segment):
        return {
            "NSE_EQ": {
                security_id: HistoricalBar(
                    timestamp=now - timedelta(hours=2),
                    open=Decimal("109") if security_id == "3045" else Decimal("49"),
                    high=Decimal("111") if security_id == "3045" else Decimal("51"),
                    low=Decimal("108") if security_id == "3045" else Decimal("48"),
                    close=Decimal("110") if security_id == "3045" else Decimal("50"),
                    volume=Decimal("1000"),
                )
                for security_id in instruments_by_segment["NSE_EQ"]
            }
        }

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    fallback_chain = market_data.MarketDataProviderChain([
        SimpleNamespace(name="yfinance"),
        SimpleNamespace(name="angel_one"),
    ])
    monkeypatch.setattr(
        market_data,
        "market_data_provider_for_user",
        lambda _db, _user_id: fallback_chain,
    )
    attempts: list[list[str]] = []

    def resolve_quote(instrument_id, *, refresh, provider, _user, db):
        assert refresh is False
        provider_names = [item.name for item in provider.providers]
        attempts.append(provider_names)
        instrument = db.get(Instrument, instrument_id)
        quote = db.scalar(select(market_data.MarketQuote).where(
            market_data.MarketQuote.instrument_id == instrument_id,
            market_data.MarketQuote.source == "angel_one",
        ))
        if quote is None:
            quote = market_data.MarketQuote(
                instrument_id=instrument_id,
                source="angel_one",
            )
            db.add(quote)
        quote.timestamp = now
        quote.open = Decimal("109") if instrument.symbol == "SBIN" else Decimal("49")
        quote.high = Decimal("111") if instrument.symbol == "SBIN" else Decimal("51")
        quote.low = Decimal("108") if instrument.symbol == "SBIN" else Decimal("48")
        quote.close = Decimal("110") if instrument.symbol == "SBIN" else Decimal("50")
        quote.volume = Decimal("1000")
        quote.retrieved_at = now
        quote.is_synthetic = False
        db.commit()
        return SimpleNamespace(
            freshness_status="fresh",
            freshness_message="Fresh Angel One quote.",
            source="angel_one",
            provider_attempts=[
                SimpleNamespace(
                    provider="angel_one",
                    outcome="used",
                    reason=None,
                )
            ],
        )

    monkeypatch.setattr(market_data, "latest_quote", resolve_quote)
    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    assert attempts == [["angel_one", "yfinance"], ["angel_one", "yfinance"]]
    dhan = response.json()["brokers"][0]
    assert Decimal(dhan["market_value"]) == Decimal("380")
    observation = dhan["provenance"]["market_value"]["quote_observations"][0]
    assert observation["source"] == "angel_one"
    assert any(
        "dhan: quote stale" in attempt
        for attempt in observation["provider_attempts"]
    )


def test_broker_summary_uses_live_quote_during_nse_session(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    real_session_state = market_data._market_session_state
    seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    now = datetime(2026, 10, 27, 4, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(brokers, "_now", lambda: now)
    monkeypatch.setattr(market_data, "_market_session_state", real_session_state)
    with Session(test_engine) as db:
        for instrument in db.scalars(select(Instrument)):
            close = Decimal("110") if instrument.symbol == "SBIN" else Decimal("50")
            db.add(market_data.MarketQuote(
                instrument_id=instrument.id,
                source="dhan",
                timestamp=now,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("1000"),
                retrieved_at=now,
                is_synthetic=False,
            ))
        db.commit()
    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    evidence = response.json()["brokers"][0]["provenance"]["market_value"]
    assert {item["price_basis"] for item in evidence["quote_observations"]} == {
        "live_quote"
    }
    assert {item["basis_date"] for item in evidence["quote_observations"]} == {
        "2026-10-27"
    }


@pytest.mark.parametrize(
    ("current_time", "current_session", "previous_session"),
    [
        (
            datetime(2026, 10, 27, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
            date(2026, 10, 27),
            date(2026, 10, 26),
        ),
        (
            datetime(2026, 10, 26, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
            date(2026, 10, 23),
            date(2026, 10, 22),
        ),
        (
            datetime(2026, 10, 20, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
            date(2026, 10, 19),
            date(2026, 10, 16),
        ),
    ],
    ids=["after-close", "monday-pre-open", "nse-holiday"],
)
def test_broker_summary_uses_last_completed_session_close(
    clients,
    test_engine,
    encryption_key,
    monkeypatch,
    current_time,
    current_session,
    previous_session,
) -> None:
    first, _ = clients
    real_session_state = market_data._market_session_state
    seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    monkeypatch.setattr(brokers, "_now", lambda: current_time)
    monkeypatch.setattr(market_data, "_market_session_state", real_session_state)
    _add_portfolio_close_bars(
        test_engine,
        (current_session, previous_session),
    )
    quote_requests: list[str] = []
    monkeypatch.setattr(
        DhanAdapter,
        "get_market_quotes_batch",
        lambda *_args: quote_requests.append("unexpected"),
    )
    monkeypatch.setattr(
        market_data,
        "market_data_provider_for_user",
        lambda *_args: (_ for _ in ()).throw(AssertionError("live fallback attempted")),
    )

    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    assert quote_requests == []
    dhan = response.json()["brokers"][0]
    assert Decimal(dhan["market_value"]) == Decimal("380")
    assert Decimal(dhan["total_value"]) == Decimal("430")
    assert Decimal(dhan["unrealized_pnl"]) == Decimal("57.50")
    assert Decimal(dhan["day_change"]) == Decimal("17")
    market_evidence = dhan["provenance"]["market_value"]
    assert {item["price_basis"] for item in market_evidence["quote_observations"]} == {
        "eod_close"
    }
    assert {item["basis_date"] for item in market_evidence["quote_observations"]} == {
        current_session.isoformat()
    }
    day_evidence = dhan["provenance"]["day_change"]
    assert {
        item["basis_date"] for item in day_evidence["quote_observations"]
    } == {current_session.isoformat()}
    assert {
        item["basis_date"] for item in day_evidence["previous_close_observations"]
    } == {previous_session.isoformat()}


def test_broker_summary_does_not_partial_sum_without_last_session_close(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    real_session_state = market_data._market_session_state
    seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    current_time = datetime(2026, 10, 27, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(brokers, "_now", lambda: current_time)
    monkeypatch.setattr(market_data, "_market_session_state", real_session_state)
    _add_portfolio_close_bars(
        test_engine,
        (date(2026, 10, 27), date(2026, 10, 26)),
        current_prices={"SBIN": Decimal("110")},
    )
    monkeypatch.setattr(
        DhanAdapter,
        "get_market_quotes_batch",
        lambda *_args: (_ for _ in ()).throw(AssertionError("live quote attempted")),
    )

    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    dhan = response.json()["brokers"][0]
    assert dhan["market_value"] is None
    assert dhan["total_value"] is None
    assert dhan["day_change"] is None
    assert "XYZ" in dhan["unavailable_reasons"]["market_value"]
    assert "2026-10-27" in dhan["unavailable_reasons"]["market_value"]
    assert dhan["provenance"]["market_value"]["coverage"] == {
        "priced": 2,
        "total": 3,
        "unit": "open rows",
    }
    assert any("XYZ" in item for item in dhan["provenance"]["market_value"]["exclusions"])


def test_broker_holdings_are_in_coverage_and_backfill_selection(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    coverage = first.get("/api/v1/market-data/coverage")

    assert coverage.status_code == 200, coverage.text
    assert {
        row["symbol"] for row in coverage.json()["instruments"]
    } >= {"SBIN", "XYZ"}

    app.dependency_overrides[market_data.get_market_data_provider] = (
        lambda: market_data.MarketDataProviderChain([])
    )
    response = first.post(
        "/api/v1/market-data/backfill/holdings",
        headers=csrf(first),
    )

    assert response.status_code == 200, response.text
    assert {
        row["symbol"] for row in response.json()["instruments"]
    } >= {"SBIN", "XYZ"}
    with Session(test_engine) as session:
        sbin = session.scalar(
            select(market_data.Instrument).where(
                market_data.Instrument.symbol == "SBIN"
            )
        )
        assert sbin is not None
        attempt = session.scalar(
            select(market_data.MarketDataSyncAttempt)
            .where(
                market_data.MarketDataSyncAttempt.instrument_id == sbin.id,
                market_data.MarketDataSyncAttempt.interval == "1d",
            )
            .order_by(market_data.MarketDataSyncAttempt.attempted_at.desc())
        )
        assert attempt is not None
        assert attempt.outcome == "unavailable"
        assert attempt.reason == "no_real_provider_available"


def test_daily_backfill_records_provider_data_already_stored(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    with Session(test_engine) as session:
        instrument = session.scalar(
            select(market_data.Instrument).where(
                market_data.Instrument.symbol == "SBIN"
            )
        )
        assert instrument is not None
        monkeypatch.setattr(
            "app.broker_adapters.DhanAdapter.get_instrument_master_details",
            lambda *_args: None,
        )
        monkeypatch.setattr(
            market_data, "_providers",
            lambda _provider: [SimpleNamespace(name="yfinance")],
        )
        monkeypatch.setattr(
            market_data,
            "_resolve_provider_candidate",
            lambda *_args: (SimpleNamespace(), None),
        )
        monkeypatch.setattr(
            market_data,
            "_count_real_daily_bars",
            lambda *_args: 1,
        )

        def fetch_history(*_args, rows_returned, **_kwargs):
            rows_returned[0] = 1
            return [SimpleNamespace(timestamp=datetime.now(timezone.utc))]

        monkeypatch.setattr(
            market_data, "_fetch_provider_history", fetch_history
        )
        monkeypatch.setattr(
            market_data, "_persist_real_bars", lambda *_args: 0
        )
        result = market_data._backfill_instrument(
            session,
            instrument,
            SimpleNamespace(),
            years=1,
            request_budget=[10],
        )

        assert result["status"] == "unchanged"
        local_today = datetime.now(timezone.utc).astimezone(
            ZoneInfo(instrument.timezone)
        ).date()
        assert result["chunks"][0]["end"] == datetime.combine(
            local_today,
            datetime.min.time(),
            ZoneInfo(instrument.timezone),
        ).astimezone(timezone.utc)
        assert result["chunks"][0]["reason"] == "provider_data_already_stored"
        attempt = session.scalar(
            select(market_data.MarketDataSyncAttempt)
            .where(
                market_data.MarketDataSyncAttempt.instrument_id == instrument.id,
                market_data.MarketDataSyncAttempt.interval == "1d",
            )
            .order_by(market_data.MarketDataSyncAttempt.attempted_at.desc())
        )
        assert attempt is not None
        assert attempt.outcome == "unchanged"
        assert attempt.reason == "provider_data_already_stored"


def test_broker_summary_resolves_all_exchange_from_isin_and_symbol(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    now = seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    session_token = first.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        xyz_holding = db.scalar(
            select(BrokerMirror).where(
                BrokerMirror.user_id == user_session.user_id,
                BrokerMirror.symbol == "XYZ",
            )
        )
        assert xyz_holding is not None
        pcjeweller = Instrument(
            symbol="PCJEWELLER",
            display_name="PC Jeweller Limited",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=now,
        )
        db.add(pcjeweller)
        db.flush()
        db.add(InstrumentProviderIdentifier(
            instrument_id=pcjeweller.id,
            provider="dhan",
            provider_instrument_id="7777|NSE_EQ",
            provider_symbol="PCJEWELLER",
            is_verified=False,
            last_seen_at=now,
        ))
        xyz_holding.symbol = "PCJEWELLER"
        xyz_holding.security_id = "7777"
        xyz_holding.exchange = "ALL"
        xyz_holding.isin = "KNOWNISIN123"
        db.commit()
    monkeypatch.setattr(
        DhanAdapter,
        "_scrip_master",
        classmethod(lambda _cls: [{
            "EXCH_ID": "NSE",
            "SEGMENT": "E",
            "SECURITY_ID": "7777",
            "SYMBOL_NAME": "PCJEWELLER",
            "UNDERLYING_SYMBOL": "PCJEWELLER",
            "INSTRUMENT": "EQUITY",
            "SERIES": "EQ",
            "ISIN": "KNOWNISIN123",
        }]),
    )
    requests = []

    def get_market_quotes_batch(_self, _token, _tokens, instruments_by_segment):
        requests.append(instruments_by_segment)
        return {"NSE_EQ": {
            security_id: HistoricalBar(
                timestamp=now,
                open=Decimal("109") if security_id == "3045" else Decimal("49"),
                high=Decimal("111") if security_id == "3045" else Decimal("51"),
                low=Decimal("108") if security_id == "3045" else Decimal("48"),
                close=Decimal("110") if security_id == "3045" else Decimal("50"),
                volume=Decimal("1000"),
            )
            for security_id in ("3045", "7777")
        }}

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    assert requests == [{"NSE_EQ": ["3045", "7777"]}]
    dhan = response.json()["brokers"][0]
    assert Decimal(dhan["market_value"]) == Decimal("380")
    assert dhan["provenance"]["market_value"]["coverage"] == {
        "priced": 3,
        "total": 3,
        "unit": "open rows",
    }
    with Session(test_engine) as db:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.provider == "dhan",
                InstrumentProviderIdentifier.provider_instrument_id
                == "7777|NSE_EQ",
            )
        )
        assert mapping is not None
        assert mapping.is_verified is True
    portfolio = first.get("/api/v1/brokers/dhan/portfolio").json()
    pcjeweller_row = next(
        row for row in portfolio["holdings"] if row["symbol"] == "PCJEWELLER"
    )
    assert pcjeweller_row["exchange"] == "NSE_EQ"
    assert pcjeweller_row["exchange_raw"] == "ALL"
    assert pcjeweller_row["exchange_resolution_reason"] is None


def test_broker_summary_leaves_unresolved_all_exchange_holding_blank(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    first, _ = clients
    now = seed_dhan_portfolio_summary(first, test_engine, encryption_key, monkeypatch)
    session_token = first.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        unresolved = db.scalar(
            select(BrokerMirror).where(
                BrokerMirror.user_id == user_session.user_id,
                BrokerMirror.symbol == "XYZ",
            )
        )
        assert unresolved is not None
        unresolved.symbol = "PCJEWELLER"
        unresolved.exchange = "ALL"
        unresolved.isin = "UNMATCHEDISIN"
        db.commit()
    monkeypatch.setattr(DhanAdapter, "_scrip_master", classmethod(lambda _cls: []))
    requests = []

    def get_market_quotes_batch(_self, _token, _tokens, instruments_by_segment):
        requests.append(instruments_by_segment)
        return {"NSE_EQ": {
            "3045": HistoricalBar(
                timestamp=now,
                open=Decimal("109"),
                high=Decimal("111"),
                low=Decimal("108"),
                close=Decimal("110"),
                volume=Decimal("1000"),
            )
        }}

    monkeypatch.setattr(
        DhanAdapter, "get_market_quotes_batch", get_market_quotes_batch
    )
    response = first.get("/api/v1/brokers/portfolio-summary")

    assert response.status_code == 200, response.text
    assert requests == [{"NSE_EQ": ["3045"]}]
    dhan = response.json()["brokers"][0]
    assert dhan["market_value"] is None
    reason = dhan["unavailable_reasons"]["market_value"]
    assert reason.startswith("Unverified Dhan provider mapping")
    assert "Missing input:" not in reason
    assert "PCJEWELLER" in reason
    assert "UNMATCHEDISIN" in reason
    assert "instrument master" in reason
    portfolio = first.get("/api/v1/brokers/dhan/portfolio").json()
    pcjeweller_row = next(
        row for row in portfolio["holdings"] if row["symbol"] == "PCJEWELLER"
    )
    assert pcjeweller_row["exchange"] == "ALL"
    assert pcjeweller_row["exchange_raw"] == "ALL"
    assert "showing broker raw value" in pcjeweller_row["exchange_resolution_reason"]


def test_dhan_cached_live_quote_revalues_portfolio_without_broker_request(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    client, _ = clients
    now = seed_dhan_portfolio_summary(
        client, test_engine, encryption_key, monkeypatch
    )
    with Session(test_engine) as db:
        rows = db.scalars(
            select(BrokerMirror).where(
                BrokerMirror.broker == "dhan",
                BrokerMirror.quantity != 0,
                BrokerMirror.average_price.is_not(None),
                BrokerMirror.security_id.is_not(None),
            )
        ).all()
        selected = None
        for row in rows:
            if brokers._is_cnc_sale(row):
                continue
            mappings = db.scalars(
                select(InstrumentProviderIdentifier).where(
                    InstrumentProviderIdentifier.provider == "dhan",
                    InstrumentProviderIdentifier.is_verified.is_(True),
                    InstrumentProviderIdentifier.provider_instrument_id.like(
                        f"{row.security_id}|%"
                    ),
                )
            ).all()
            normalized_symbol = row.symbol.strip().upper().removesuffix("-EQ")
            mapping = next(
                (
                    candidate for candidate in mappings
                    if candidate.provider_symbol.strip().upper().removesuffix("-EQ")
                    == normalized_symbol
                ),
                None,
            )
            if mapping is not None:
                instrument = db.get(Instrument, mapping.instrument_id)
                if instrument is not None:
                    selected = row, instrument
                    break
        assert selected is not None
        row, instrument = selected
        instrument_id = instrument.id
        quote = db.scalar(
            select(MarketQuote).where(
                MarketQuote.instrument_id == instrument_id,
                MarketQuote.source == "dhan_ws",
            )
        )
        if quote is None:
            quote = MarketQuote(
                instrument_id=instrument.id,
                source="dhan_ws",
                timestamp=now,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=Decimal("0"),
                retrieved_at=now,
                is_synthetic=False,
            )
            db.add(quote)
        else:
            quote.timestamp = now
            quote.open = quote.high = quote.low = quote.close = Decimal("100")
            quote.retrieved_at = now
            quote.is_synthetic = False
        security_id = row.security_id
        db.commit()

    def no_broker_request(*_args, **_kwargs):
        pytest.fail("Portfolio valuation must use the cached live quote.")

    monkeypatch.setattr(brokers, "DhanReadOnlyAccountAdapter", no_broker_request)
    first = client.get("/api/v1/brokers/dhan/portfolio")
    assert first.status_code == 200, first.text
    first_payload = next(
        item
        for key in ("holdings", "positions")
        for item in first.json()[key]
        if item["security_id"] == security_id
    )
    assert first_payload["live_valuation"]["status"] == "live"
    first_value = Decimal(first_payload["live_valuation"]["market_value"])
    broker_pnl = first_payload.get("unrealized_profit")

    with Session(test_engine) as db:
        quote = db.scalar(
            select(MarketQuote).where(
                MarketQuote.source == "dhan_ws",
                MarketQuote.instrument_id == instrument_id,
            )
        )
        assert quote is not None
        quote.close = Decimal("101")
        quote.high = Decimal("101")
        db.commit()

    second = client.get("/api/v1/brokers/dhan/portfolio")
    assert second.status_code == 200, second.text
    second_payload = next(
        item
        for key in ("holdings", "positions")
        for item in second.json()[key]
        if item["security_id"] == security_id
    )
    assert Decimal(second_payload["live_valuation"]["market_value"]) != first_value
    assert second_payload.get("unrealized_profit") == broker_pnl
    assert second_payload["live_valuation"]["basis"] == "live_feed_ltp"


def test_dhan_automatic_snapshot_is_session_and_cadence_gated(
    clients, test_engine, encryption_key, monkeypatch
) -> None:
    client, _ = clients
    seed_dhan_portfolio_summary(client, test_engine, encryption_key, monkeypatch)
    calls: list[str] = []

    class SnapshotReader:
        last_http_diagnostics: list[dict[str, str]] = []

        def __init__(self, *_args) -> None:
            pass

        def get_holdings(self):
            calls.append("holdings")
            raise DhanAccountRequestError("provider_temporarily_unavailable")

        def get_positions(self):
            calls.append("positions")
            raise DhanAccountRequestError("provider_temporarily_unavailable")

        def get_fundlimit(self):
            calls.append("funds")
            raise DhanAccountRequestError("provider_temporarily_unavailable")

        def get_past_trades(self, *_args):
            pytest.fail("Automatic snapshots must not fetch historical trades.")

        def get_ledger(self, *_args):
            pytest.fail("Automatic snapshots must not fetch ledger history.")

    monkeypatch.setattr(brokers, "DhanReadOnlyAccountAdapter", SnapshotReader)
    monkeypatch.setattr(market_data, "_market_session_state", lambda *_args: "closed")
    brokers._dhan_snapshot_attempts.clear()
    closed = client.post(
        "/api/v1/brokers/dhan/sync?snapshot_only=true",
        headers=csrf(client),
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["reason"] == "market_closed"
    assert calls == []

    monkeypatch.setattr(market_data, "_market_session_state", lambda *_args: "open")
    attempted = client.post(
        "/api/v1/brokers/dhan/sync?snapshot_only=true",
        headers=csrf(client),
    )
    assert attempted.status_code == 502
    assert calls == ["holdings", "positions", "funds"]
    throttled = client.post(
        "/api/v1/brokers/dhan/sync?snapshot_only=true",
        headers=csrf(client),
    )
    assert throttled.status_code == 200, throttled.text
    assert throttled.json()["reason"] == "snapshot_cadence"
    assert calls == ["holdings", "positions", "funds"]


def test_dhan_portfolio_only_lists_nonzero_long_short_positions_as_open(
    clients, test_engine, dhan_position_mix
) -> None:
    first, _ = clients
    register(first, "position-mix@example.com")
    complete_broker_gate(first, test_engine)
    session_token = first.cookies.get("session_token")
    with Session(test_engine) as db:
        user_session = db.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_token(session_token)
            )
        )
        assert user_session is not None
        db.add_all(brokers._normalize_rows(
            dhan_position_mix,
            "position",
            datetime.now(timezone.utc),
            user_session.user_id,
            "dhan",
        ))
        db.commit()

    response = first.get("/api/v1/brokers/dhan/portfolio")

    assert response.status_code == 200
    portfolio = response.json()
    assert [row["symbol"] for row in portfolio["positions"]] == [
        "OPEN_LONG",
        "OPEN_SHORT",
    ]
    assert [row["symbol"] for row in portfolio["closed_positions"]] == [
        "CLOSED_NONZERO",
        "ZERO_LONG",
    ]
    assert portfolio["positions"][0]["position_type"] == "LONG"
    assert Decimal(portfolio["positions"][0]["realized_profit"]) == Decimal("3.25")
    assert Decimal(portfolio["positions"][0]["unrealized_profit"]) == Decimal("4.50")
    assert Decimal(portfolio["closed_positions"][0]["realized_profit"]) == Decimal("15.00")


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


def test_dhan_quote_batch_groups_segments_in_one_request(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(DhanAdapter, "_market_quote_cache", {})
    monkeypatch.setattr(
        "app.broker_adapters._wait_for_dhan_market_data", lambda: None
    )

    def mocked_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        record = {
            "last_price": 100,
            "last_trade_time": "15/04/2025 10:30:00",
            "volume": 1000,
            "ohlc": {"open": 99, "high": 101, "low": 98},
        }
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "NSE_EQ": {"11536": record},
                    "BSE_EQ": {"500112": record},
                },
            },
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", mocked_request)
    quotes = DhanAdapter().get_market_quotes_batch(
        "access-token",
        {"client_id": "client"},
        {"NSE_EQ": ["11536"], "BSE_EQ": ["500112"]},
    )

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.dhan.co/v2/marketfeed/quote"
    assert calls[0]["json"] == {"BSE_EQ": [500112], "NSE_EQ": [11536]}
    assert quotes["NSE_EQ"]["11536"].close == Decimal("100")
    assert quotes["BSE_EQ"]["500112"].close == Decimal("100")


def test_dhan_historical_data_requests_one_minute_candles_and_converts_epoch(
    monkeypatch,
) -> None:
    start = datetime(2026, 10, 26, 3, 45, tzinfo=timezone.utc)
    end = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
    epoch = int(datetime(2026, 10, 26, 3, 46, tzinfo=timezone.utc).timestamp())
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
    assert captured["json"]["fromDate"] == "2026-10-26 09:15:00"
    assert captured["json"]["toDate"] == "2026-10-26 09:30:00"
    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 10, 26, 3, 46, tzinfo=timezone.utc)
    assert bars[0].timestamp.astimezone(ZoneInfo("Asia/Kolkata")) == datetime(
        2026, 10, 26, 9, 16, tzinfo=ZoneInfo("Asia/Kolkata")
    )


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
    struct.pack_into("<i", packet, 14, int(datetime(2025, 4, 15, 10, 30, tzinfo=timezone.utc).timestamp()))
    struct.pack_into("<i", packet, 22, 1200)
    struct.pack_into("<f", packet, 34, 3890)
    struct.pack_into("<f", packet, 42, 3910)
    struct.pack_into("<f", packet, 46, 3880)
    return bytes(packet)


def _dhan_ticker_packet() -> bytes:
    packet = bytearray(16)
    packet[0] = 2
    packet[1:3] = (16).to_bytes(2, "little")
    packet[3] = 1
    packet[4:8] = (11536).to_bytes(4, "little", signed=True)
    struct.pack_into("<f", packet, 8, 3900.25)
    struct.pack_into(
        "<i",
        packet,
        12,
        int(datetime(2025, 4, 15, 10, 30, tzinfo=timezone.utc).timestamp()),
    )
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
    assert quote.timestamp.tzinfo is timezone.utc
    assert quote.open == Decimal("3890.0")
    assert quote.high == Decimal("3910.0")
    assert quote.low == Decimal("3880.0")
    assert quote.close == Decimal("3900.25")
    assert quote.volume == Decimal("1200")


def test_dhan_live_feed_stop_skips_tasks_from_another_event_loop() -> None:
    hub = market_data.DhanLiveFeedHub()
    foreign_loop = asyncio.new_event_loop()

    async def wait_forever() -> None:
        await asyncio.Future()

    foreign_task = foreign_loop.create_task(wait_forever())
    hub._counter_task = foreign_task
    hub._tasks["ticker"] = foreign_task
    try:
        asyncio.run(hub.stop())
        assert hub._counter_task is foreign_task
        assert hub._tasks["ticker"] is foreign_task
    finally:
        foreign_task.cancel()
        foreign_loop.run_until_complete(
            asyncio.gather(foreign_task, return_exceptions=True)
        )
        foreign_loop.close()


def test_dhan_live_feed_health_tracks_packets_and_failures(monkeypatch) -> None:
    hub = market_data.DhanLiveFeedHub()

    async def fake_stream(
        _access_token,
        _tokens,
        _initial,
        _updates,
        status_callback,
    ):
        await status_callback("active")
        await status_callback("connection_failure")
        await status_callback("packet_received")
        await status_callback("unknown_response")
        if False:
            yield "instrument", None

    monkeypatch.setattr(hub._adapter, "stream_watchlist_market_quotes", fake_stream)

    asyncio.run(
        hub._run_feed(
            "ticker",
            "",
            {},
            {},
            asyncio.Queue(),
        )
    )

    health = hub.health("ticker")
    assert health["connection_state"] == "idle"
    assert health["last_successful_packet_at"] is not None
    assert health["consecutive_failure_count"] == 0
    assert health["unknown_response_count"] == 1


def test_dhan_live_feed_hub_forwards_previous_close_and_market_status(
    monkeypatch,
) -> None:
    hub = market_data.DhanLiveFeedHub()
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    hub._groups["ticker"]["test-group"] = (
        {"instrument": ("NSE_EQ", "11536")},
        queue,
    )

    async def fake_stream(
        _access_token,
        _tokens,
        _initial,
        _updates,
        _status_callback,
    ):
        yield "instrument", "previous_close", Decimal("3890.25")
        yield "instrument", "market_status", b"\x07\x08\x00\x01\x10\x2d\x00\x00"

    monkeypatch.setattr(
        hub._adapter, "stream_watchlist_market_quotes", fake_stream
    )

    async def verify_feed() -> list[dict[str, object]]:
        await hub._run_feed("ticker", "", {}, {}, asyncio.Queue())
        messages = []
        while not queue.empty():
            messages.append(queue.get_nowait())
        return messages

    messages = asyncio.run(verify_feed())

    previous_close = next(message for message in messages if message["type"] == "previous_close")
    assert previous_close["previous_close"] == Decimal("3890.25")
    market_status = next(message for message in messages if message["type"] == "status")
    assert market_status["feed_health"]["last_market_status_packet_hex"] == (
        "07080001102d0000"
    )


def test_dhan_websocket_ticker_packet_decodes_ist_epoch_as_utc() -> None:
    tick = DhanAdapter.decode_market_feed_packet(
        _dhan_ticker_packet(), "NSE_EQ", "11536"
    )

    assert tick is not None
    assert tick.timestamp == datetime(2025, 4, 15, 5, 0, tzinfo=timezone.utc)
    assert tick.timestamp.tzinfo is timezone.utc


def test_dhan_ist_epoch_conversion_uses_zoneinfo(monkeypatch) -> None:
    raw_timestamp = int(
        datetime(2026, 10, 26, 14, 55, tzinfo=timezone.utc).timestamp()
    )
    encoded_wall_clock = datetime.fromtimestamp(raw_timestamp, timezone.utc)
    expected = encoded_wall_clock.replace(
        tzinfo=ZoneInfo("Asia/Kolkata")
    ).astimezone(timezone.utc)
    assert broker_adapters._dhan_ist_epoch_to_utc(raw_timestamp) == expected

    zone_database_offset = timezone(timedelta(hours=6, minutes=15))
    monkeypatch.setattr(
        broker_adapters,
        "ZoneInfo",
        lambda zone_name: (
            zone_database_offset if zone_name == "Asia/Kolkata" else None
        ),
    )
    dynamic_expected = encoded_wall_clock.replace(
        tzinfo=zone_database_offset
    ).astimezone(timezone.utc)
    assert broker_adapters._dhan_ist_epoch_to_utc(raw_timestamp) == dynamic_expected


def test_dhan_ist_epoch_conversion_handles_utc_midnight_rollover() -> None:
    expected_utc = datetime(2026, 10, 26, 0, 10, tzinfo=timezone.utc)
    encoded_ist_wall_clock = expected_utc.astimezone(
        ZoneInfo("Asia/Kolkata")
    ).replace(tzinfo=timezone.utc)
    raw_timestamp = int(encoded_ist_wall_clock.timestamp())

    parsed_utc = broker_adapters._dhan_ist_epoch_to_utc(raw_timestamp)

    assert parsed_utc == expected_utc
    assert parsed_utc.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat() == (
        "2026-10-26"
    )


def test_dhan_websocket_resubscribe_after_disconnect(monkeypatch) -> None:
    class FakeConnection:
        response = type("Response", (), {"status_code": 101, "reason_phrase": "OK"})()
        close_code = None
        close_reason = None

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


def test_dhan_live_feed_decodes_multiple_packets_in_one_frame() -> None:
    decoded, retained_packets, unknown_count = DhanAdapter.decode_market_feed_frame(
        _dhan_ticker_packet() + _dhan_quote_packet(),
        {("NSE_EQ", "11536"): "instrument"},
    )

    assert [instrument_id for instrument_id, _quote in decoded] == [
        "instrument",
        "instrument",
    ]
    assert [quote.close for _instrument_id, quote in decoded] == [
        Decimal("3900.25"),
        Decimal("3900.25"),
    ]
    assert retained_packets == []
    assert unknown_count == 0


def test_dhan_live_feed_disconnect_packet_is_not_a_header_error() -> None:
    with pytest.raises(broker_adapters.BrokerFeedDisconnected):
        DhanAdapter.decode_market_feed_frame(
            _dhan_disconnect_packet(),
            {("NSE_EQ", "11536"): "instrument"},
        )


def test_dhan_live_feed_previous_close_is_retained_and_not_unknown() -> None:
    packet = bytearray(16)
    packet[0] = 6
    packet[1:3] = (16).to_bytes(2, "little")
    packet[3] = 1
    packet[4:8] = (11536).to_bytes(4, "little", signed=True)
    struct.pack_into("<f", packet, 8, 3890.25)
    struct.pack_into("<i", packet, 12, 0)

    decoded, retained_packets, unknown_count = DhanAdapter.decode_market_feed_frame(
        bytes(packet),
        {("NSE_EQ", "11536"): "instrument"},
    )

    assert decoded == []
    assert retained_packets == [
        ("instrument", 6, Decimal("3890.25")),
    ]
    assert unknown_count == 0


def test_dhan_live_feed_market_status_packet_is_retained() -> None:
    packet = bytearray(8)
    packet[0] = 7
    packet[1:3] = (8).to_bytes(2, "little")
    packet[3] = 1
    packet[4:8] = (11536).to_bytes(4, "little", signed=True)

    decoded, retained_packets, unknown_count = DhanAdapter.decode_market_feed_frame(
        bytes(packet),
        {("NSE_EQ", "11536"): "instrument"},
    )

    assert decoded == []
    assert retained_packets == [("instrument", 7, bytes(packet))]
    assert unknown_count == 0


def test_dhan_live_feed_skips_and_counts_unknown_response_code() -> None:
    packet = bytearray(8)
    packet[0] = 99
    packet[1:3] = (8).to_bytes(2, "little")

    decoded, retained_packets, unknown_count = DhanAdapter.decode_market_feed_frame(
        bytes(packet),
        {},
    )

    assert decoded == []
    assert retained_packets == []
    assert unknown_count == 1


def test_dhan_live_feed_does_not_decode_text_frames(monkeypatch) -> None:
    class FakeConnection:
        response = type("Response", (), {"status_code": 101, "reason_phrase": "OK"})()
        close_code = None
        close_reason = None

        def __init__(self) -> None:
            self.messages: list[str | bytes] = ["not a binary packet", _dhan_ticker_packet()]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, _message: str) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

    connection = FakeConnection()
    monkeypatch.setattr(
        broker_adapters,
        "websocket_connect",
        lambda *_args, **_kwargs: connection,
    )

    async def verify_stream() -> None:
        stream = DhanAdapter().stream_market_quotes(
            "",
            {"client_id": ""},
            "NSE_EQ",
            "11536",
            lambda _status: asyncio.sleep(0),
        )
        quote = await anext(stream)
        assert quote is not None
        assert quote.close == Decimal("3900.25")
        await stream.aclose()

    asyncio.run(verify_stream())


def test_dhan_live_feed_stays_idle_without_reconnecting_when_unsubscribed(
    monkeypatch,
) -> None:
    class FakeConnection:
        response = type("Response", (), {"status_code": 101, "reason_phrase": "OK"})()
        close_code = None
        close_reason = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, _message: str) -> None:
            self.sent_messages.append(json.loads(_message))

        async def recv(self) -> bytes:
            await asyncio.Future()
            return b""

        def __init__(self) -> None:
            self.sent_messages: list[dict[str, object]] = []

    connection = FakeConnection()
    connect_count = 0

    def connect(*_args, **_kwargs):
        nonlocal connect_count
        connect_count += 1
        return connection

    monkeypatch.setattr(broker_adapters, "websocket_connect", connect)
    subscription_updates: asyncio.Queue[dict[str, tuple[str, str]]] = asyncio.Queue()
    active = asyncio.Event()
    idle = asyncio.Event()
    statuses: list[str] = []

    async def capture_status(status: str) -> None:
        statuses.append(status)
        if status == "active":
            active.set()
        if status == "idle":
            idle.set()

    async def verify_stream() -> None:
        stream = DhanAdapter().stream_watchlist_market_quotes(
            "",
            {"client_id": ""},
            {"instrument": ("NSE_EQ", "11536")},
            subscription_updates,
            capture_status,
        )
        consumer = asyncio.create_task(anext(stream))
        await asyncio.wait_for(active.wait(), timeout=1)
        assert connection.sent_messages[0]["RequestCode"] == 17
        subscription_updates.put_nowait({})
        await asyncio.wait_for(idle.wait(), timeout=1)
        assert connect_count == 1
        assert statuses[-1] == "idle"
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()

    asyncio.run(verify_stream())


def test_dhan_live_feed_backs_off_caps_failures_and_summarizes_repeats(
    monkeypatch, caplog
) -> None:
    class FakeConnection:
        response = type("Response", (), {"status_code": 101, "reason_phrase": "OK"})()
        close_code = None
        close_reason = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, _message: str) -> None:
            return None

        async def recv(self) -> bytes:
            return b"\x02\x00\x00\x01\x00\x00\x00\x00"

    connection = FakeConnection()
    monkeypatch.setattr(
        broker_adapters,
        "websocket_connect",
        lambda *_args, **_kwargs: connection,
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(broker_adapters.asyncio, "sleep", record_sleep)
    statuses: list[str] = []

    async def capture_status(status: str) -> None:
        statuses.append(status)

    async def verify_stream() -> None:
        stream = DhanAdapter().stream_watchlist_market_quotes(
            "",
            {"client_id": ""},
            {"instrument": ("NSE_EQ", "11536")},
            asyncio.Queue(),
            capture_status,
        )
        with pytest.raises(BrokerAdapterError, match="5 consecutive failures"):
            await anext(stream)

    caplog.set_level(30, logger="app.broker_adapters")
    asyncio.run(verify_stream())

    assert delays == [1, 2, 4, 8]
    assert statuses.count("connection_failure") == 5
    assert statuses[-1] == "failed"
    assert caplog.text.count(
        "Dhan live feed failure reason=Dhan live feed packet has an invalid header"
    ) == 1
    assert "Dhan live feed repeated failure count=5" in caplog.text


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
