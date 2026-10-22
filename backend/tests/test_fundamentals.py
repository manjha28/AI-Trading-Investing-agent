from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.market_data import MarketDataProviderError
from app.models import Instrument, InstrumentFundamental, InstrumentSnapshot, User
from app.provider_adapters import YahooFinanceProvider


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


def login_verified(client: TestClient, test_engine) -> None:
    client.post(
        "/api/v1/auth/register",
        json={"email": "fundamentals@example.com", "password": "correct-horse-battery"},
    )
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "fundamentals@example.com", "password": "correct-horse-battery"},
    ).status_code == 200
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == "fundamentals@example.com"))
        assert user is not None
        user.email_verified = True
        db.commit()


def add_real_instrument(test_engine) -> UUID:
    from uuid import uuid4

    instrument = Instrument(
        id=uuid4(),
        symbol="ACME",
        display_name="Acme Corporation",
        asset_class="equity",
        venue="NYSE",
        currency="USD",
        country="US",
        timezone="America/New_York",
        multiplier=None,
        expiry=None,
        strike=None,
        option_type=None,
        is_synthetic=False,
        relink_required=False,
        created_at=datetime.now(timezone.utc),
    )
    instrument_id = instrument.id
    with Session(test_engine) as db:
        db.add(instrument)
        db.commit()
    return instrument_id


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def provider_fixture():
    period = pd.Timestamp("2024-12-31")
    return {
        "info": {
            "currency": "USD",
            "marketCap": 123456,
            "trailingPE": 12.5,
            "priceToBook": 2.2,
            "dividendYield": 0.015,
            "sector": "Technology",
            "industry": "Software",
        },
        "annual_income": pd.DataFrame(
            {
                period: {
                    "Total Revenue": 1000,
                    "Net Income": 100,
                    "Basic EPS": 2.5,
                    "Operating Income": 200,
                }
            }
        ),
        "quarterly_income": pd.DataFrame(),
        "annual_balance": pd.DataFrame(
            {
                period: {
                    "Total Assets": 2000,
                    "Total Liabilities Net Minority Interest": 900,
                    "Stockholders Equity": 1100,
                    "Total Debt": 100,
                    "Ordinary Shares Number": 40,
                }
            }
        ),
        "quarterly_balance": pd.DataFrame(),
        "annual_cashflow": pd.DataFrame(
            {period: {"Operating Cash Flow": 150, "Free Cash Flow": 120}}
        ),
        "quarterly_cashflow": pd.DataFrame(),
    }


def patch_provider_fixture(monkeypatch, fixture=None) -> None:
    fixture = fixture or provider_fixture()

    class RecordedTicker:
        def __init__(self, _symbol: str):
            pass

        def get_info(self):
            return fixture["info"]

        @property
        def income_stmt(self):
            return fixture["annual_income"]

        @property
        def quarterly_income_stmt(self):
            return fixture["quarterly_income"]

        @property
        def balance_sheet(self):
            return fixture["annual_balance"]

        @property
        def quarterly_balance_sheet(self):
            return fixture["quarterly_balance"]

        @property
        def cashflow(self):
            return fixture["annual_cashflow"]

        @property
        def quarterly_cashflow(self):
            return fixture["quarterly_cashflow"]

    monkeypatch.setattr("app.services.fundamentals.yf.Ticker", RecordedTicker)
    monkeypatch.setattr(
        YahooFinanceProvider,
        "_thread_call",
        staticmethod(lambda function, *args, **kwargs: function(*args, **kwargs)),
    )


def test_provider_mapping_and_missing_fields_are_null_not_zero(
    client: TestClient, test_engine, monkeypatch
) -> None:
    login_verified(client, test_engine)
    instrument = add_real_instrument(test_engine)
    fixture = provider_fixture()
    fixture["annual_income"].loc["Basic EPS"] = float("nan")
    fixture["annual_balance"] = pd.DataFrame()
    patch_provider_fixture(monkeypatch, fixture)

    response = client.post(
        f"/api/v1/market-data/instruments/{instrument}/fundamentals/sync",
        headers=csrf(client),
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "available"
    result = client.get(
        f"/api/v1/market-data/instruments/{instrument}/fundamentals?period_type=annual"
    )
    assert result.status_code == 200, result.text
    row = result.json()["fundamentals"][0]
    assert row["revenue"] == "1000.00000000"
    assert row["net_income"] == "100.00000000"
    assert row["operating_margin"] == "20.00000000"
    assert row["eps"] is None
    assert row["total_assets"] is None
    assert row["total_debt"] is None
    assert row["shares_outstanding"] is None
    assert result.json()["snapshot"]["market_cap"] == "123456.00000000"


def test_stale_flag_uses_95_day_cutoff(client: TestClient, test_engine, monkeypatch) -> None:
    login_verified(client, test_engine)
    instrument = add_real_instrument(test_engine)
    fixed_now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.api.market_data.fundamentals_service.is_stale",
        lambda value: (fixed_now - value.replace(tzinfo=timezone.utc)).total_seconds() > 95 * 86400,
    )
    with Session(test_engine) as db:
        db.add(InstrumentSnapshot(
            instrument_id=instrument,
            market_cap=Decimal("100"),
            pe=None,
            pb=None,
            dividend_yield=None,
            sector=None,
            industry=None,
            source="yfinance",
            fetched_at=fixed_now - timedelta(days=95),
        ))
        db.commit()
    response = client.get(f"/api/v1/market-data/instruments/{instrument}/fundamentals")
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["stale"] is False
    with Session(test_engine) as db:
        snapshot = db.scalar(select(InstrumentSnapshot).where(InstrumentSnapshot.instrument_id == instrument))
        assert snapshot is not None
        snapshot.fetched_at = fixed_now - timedelta(days=95, seconds=1)
        db.commit()
    response = client.get(f"/api/v1/market-data/instruments/{instrument}/fundamentals")
    assert response.json()["snapshot"]["stale"] is True


def test_provider_failure_returns_unavailable_without_fallback(
    client: TestClient, test_engine, monkeypatch
) -> None:
    login_verified(client, test_engine)
    instrument = add_real_instrument(test_engine)

    def failed_call(_function, *_args, **_kwargs):
        raise MarketDataProviderError("provider_request_failed")

    monkeypatch.setattr(YahooFinanceProvider, "_thread_call", staticmethod(failed_call))
    response = client.post(
        f"/api/v1/market-data/instruments/{instrument}/fundamentals/sync",
        headers=csrf(client),
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unavailable"
    assert "No synthetic fallback" in response.json()["message"]
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(InstrumentFundamental)) == 0
        assert db.scalar(select(func.count()).select_from(InstrumentSnapshot)) == 0
    result = client.get(f"/api/v1/market-data/instruments/{instrument}/fundamentals")
    assert result.json()["unavailable"] is True


def test_resync_updates_period_rows_without_duplicates(
    client: TestClient, test_engine, monkeypatch
) -> None:
    login_verified(client, test_engine)
    instrument = add_real_instrument(test_engine)
    patch_provider_fixture(monkeypatch)
    path = f"/api/v1/market-data/instruments/{instrument}/fundamentals/sync"
    assert client.post(path, headers=csrf(client), json={}).json()["status"] == "available"
    assert client.post(path, headers=csrf(client), json={}).json()["status"] == "available"
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(InstrumentFundamental)) == 1
        assert db.scalar(select(func.count()).select_from(InstrumentSnapshot)) == 1
