from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import auth, market_data as market_data_api
from app.api.market_data import get_market_data_provider
from app.db import Base, get_db
from app.main import app
from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
    ProviderHealth,
)
from app.models import (
    Instrument,
    MarketBar,
    MarketDataSyncAttempt,
    MarketQuote,
    PortfolioTransaction,
)


class StubHistoryProvider:
    name = "stub"
    is_mock = False

    def __init__(self, *, fail_after_requests: int | None = None):
        self.fail_after_requests = fail_after_requests
        self.requests: list[tuple[datetime, datetime, int]] = []
        self.candidate = InstrumentCandidate(
            provider=self.name,
            provider_instrument_id="STUB-ALPHA-001",
            provider_symbol="ALPHA",
            symbol="ALPHA",
            display_name="Alpha Test Instrument",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
        )

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        return [self.candidate] if "alpha" in query.casefold() else []

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        if provider_instrument_id != self.candidate.provider_instrument_id:
            raise MarketDataProviderError("instrument_not_found")
        return self.candidate

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        self.requests.append((start, end, limit))
        if (
            self.fail_after_requests is not None
            and len(self.requests) > self.fail_after_requests
        ):
            raise MarketDataProviderError(
                "provider_rate_limit_exceeded",
                status_code=429,
                retry_after="0",
            )
        rows: list[HistoricalBar] = []
        session_day = start.astimezone(timezone.utc).date()
        end_day = end.astimezone(timezone.utc).date()
        while session_day < end_day and len(rows) < limit:
            if session_day.weekday() < 5:
                session_open = datetime.combine(
                    session_day, time.min, tzinfo=ZoneInfo(instrument.timezone)
                )
                if start.date() <= session_day < end.date():
                    rows.append(HistoricalBar(
                        timestamp=session_open,
                        open=Decimal("100"),
                        high=Decimal("103"),
                        low=Decimal("99"),
                        close=Decimal("102"),
                        volume=Decimal("1000"),
                    ))
            session_day += timedelta(days=1)
        return rows

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        raise MarketDataProviderError("quote_not_configured")

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=True,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv"),
            status="stubbed_test_provider",
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
def provider():
    return StubHistoryProvider()


@pytest.fixture
def client(test_engine, provider) -> Iterator[TestClient]:
    def override_get_db():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_market_data_provider] = lambda: provider
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(client: TestClient, monkeypatch) -> None:
    delivered: list[str] = []
    monkeypatch.setattr(auth, "send_verification_email", lambda _email, token: delivered.append(token))
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "backfill@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 202, response.text
    verified = client.post("/api/v1/auth/verify-email", json={"token": delivered[0]})
    assert verified.status_code == 200, verified.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "backfill@example.com", "password": "correct-horse-battery"},
    )
    assert login.status_code == 200, login.text


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def add_fixture_instrument(client: TestClient) -> str:
    response = client.get("/api/v1/market-data/instruments", params={"query": "ALPHA"})
    assert response.status_code == 200, response.text
    instrument_id = response.json()[0]["id"]
    added = client.post(
        f"/api/v1/market-data/watchlist/{instrument_id}",
        headers=csrf(client),
    )
    assert added.status_code == 204, added.text
    return instrument_id


def test_two_year_backfill_pages_under_500_and_is_idempotent(
    client: TestClient, provider: StubHistoryProvider, test_engine, monkeypatch
) -> None:
    register(client, monkeypatch)
    instrument_id = add_fixture_instrument(client)
    url = f"/api/v1/market-data/instruments/{instrument_id}/history/backfill?years=2"

    first = client.post(url, headers=csrf(client))
    second = client.post(url, headers=csrf(client))

    assert first.status_code == 200, first.text
    result = first.json()
    assert result["status"] == "success"
    assert 490 <= result["bars_after"] <= 540
    assert result["bars_before"] == 0
    assert result["chunks_attempted"] >= 2
    assert result["chunks_failed"] == 0
    assert provider.requests
    assert all(limit == 500 for _, _, limit in provider.requests)
    assert all(end - start <= timedelta(days=450) for start, end, _ in provider.requests)
    assert second.status_code == 200, second.text
    assert second.json()["bars_after"] == result["bars_after"]
    assert second.json()["bars_before"] == result["bars_after"]
    assert second.json()["status"] == "unchanged"
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(MarketBar)) == result["bars_after"]


def test_mid_run_rate_limit_returns_partial_and_records_reason(
    test_engine, monkeypatch
) -> None:
    provider = StubHistoryProvider(fail_after_requests=1)
    monkeypatch.setattr(market_data_api, "_wait_for_provider", lambda _host: None)
    monkeypatch.setattr(market_data_api, "_retry_after_seconds", lambda *_args: 0)
    with TestClient(app) as client:
        def override_get_db():
            with Session(test_engine) as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_market_data_provider] = lambda: provider
        register(client, monkeypatch)
        instrument_id = add_fixture_instrument(client)
        response = client.post(
            f"/api/v1/market-data/instruments/{instrument_id}/history/backfill?years=2",
            headers=csrf(client),
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "partial"
        assert result["chunks_failed"] == 1
        assert result["chunks"][-1]["reason"] == "provider_rate_limit_exceeded"
        with Session(test_engine) as db:
            attempt = db.scalar(
                select(MarketDataSyncAttempt)
                .where(MarketDataSyncAttempt.outcome == "unavailable")
                .order_by(MarketDataSyncAttempt.attempted_at.desc())
            )
            assert attempt is not None
            assert attempt.response_status == 429
            assert attempt.reason == "provider_rate_limit_exceeded"
        app.dependency_overrides.clear()


def test_synthetic_purge_removes_only_synthetic_rows(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, monkeypatch)
    instrument_id = UUID(add_fixture_instrument(client))
    now = datetime.now(timezone.utc)
    with test_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(insert(MarketBar.__table__), [
            {
                "instrument_id": instrument_id,
                "interval": "1d",
                "timestamp": now - timedelta(days=2),
                "open": Decimal("9"),
                "high": Decimal("12"),
                "low": Decimal("8"),
                "close": Decimal("11"),
                "volume": Decimal("100"),
                "source": "legacy-synthetic",
                "retrieved_at": now,
                "is_synthetic": True,
            },
            {
                "instrument_id": instrument_id,
                "interval": "1d",
                "timestamp": now - timedelta(days=1),
                "open": Decimal("9"),
                "high": Decimal("12"),
                "low": Decimal("8"),
                "close": Decimal("11"),
                "volume": Decimal("100"),
                "source": "recorded",
                "retrieved_at": now,
                "is_synthetic": False,
            },
        ])
        connection.execute(insert(MarketQuote.__table__).values(
            instrument_id=instrument_id,
            timestamp=now,
            open=Decimal("9"),
            high=Decimal("12"),
            low=Decimal("8"),
            close=Decimal("11"),
            volume=Decimal("100"),
            source="legacy-synthetic",
            retrieved_at=now,
            is_synthetic=True,
        ))
        connection.commit()

    first = client.post("/api/v1/market-data/purge-synthetic", headers=csrf(client))
    second = client.post("/api/v1/market-data/purge-synthetic", headers=csrf(client))

    assert first.status_code == 200, first.text
    assert first.json()["bars_deleted"] == 1
    assert first.json()["quotes_deleted"] == 1
    assert second.json()["bars_deleted"] == 0
    assert second.json()["quotes_deleted"] == 0
    with Session(test_engine) as db:
        remaining = db.scalars(select(MarketBar)).all()
        assert len(remaining) == 1
        assert remaining[0].is_synthetic is False
        assert db.scalar(select(func.count()).select_from(MarketQuote)) == 0


def test_storage_rejects_synthetic_bars_and_quotes(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, monkeypatch)
    instrument_id = UUID(add_fixture_instrument(client))
    now = datetime.now(timezone.utc)
    for model, values in (
        (MarketBar, {
            "interval": "1d",
            "timestamp": now,
        }),
        (MarketQuote, {
            "timestamp": now,
            "source": "attempted-synthetic",
        }),
    ):
        with Session(test_engine) as db:
            db.add(model(
                instrument_id=instrument_id,
                open=Decimal("9"),
                high=Decimal("12"),
                low=Decimal("8"),
                close=Decimal("11"),
                volume=Decimal("100"),
                retrieved_at=now,
                is_synthetic=True,
                **values,
            ))
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()


def test_cleanup_unlinks_alpha_transaction_without_deleting_transaction(
    client: TestClient, test_engine, monkeypatch
) -> None:
    register(client, monkeypatch)
    portfolio = client.post(
        "/api/v1/portfolios",
        json={"name": "Relink test", "base_currency": "INR"},
        headers=csrf(client),
    )
    assert portfolio.status_code == 201, portfolio.text
    portfolio_id = UUID(portfolio.json()["id"])
    deposit = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"transaction_type": "deposit", "amount": "10000", "occurred_at": "2026-01-01T00:00:00Z"},
        headers=csrf(client),
    )
    assert deposit.status_code == 201, deposit.text
    buy = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "transaction_type": "buy",
            "symbol": "ALPHA",
            "quantity": "2",
            "unit_price": "100",
            "occurred_at": "2026-01-02T00:00:00Z",
        },
        headers=csrf(client),
    )
    assert buy.status_code == 201, buy.text
    instrument_response = client.get(
        "/api/v1/market-data/instruments", params={"query": "ALPHA"}
    )
    instrument_id = UUID(instrument_response.json()[0]["id"])
    with Session(test_engine) as db:
        instrument = db.get(Instrument, instrument_id)
        assert instrument is not None
        instrument.is_synthetic = True
        instrument.venue = "DEMO_IN"
        transaction = db.scalar(
            select(PortfolioTransaction)
            .where(PortfolioTransaction.portfolio_id == portfolio_id)
            .where(PortfolioTransaction.transaction_type == "buy")
        )
        assert transaction is not None
        transaction.instrument_id = instrument_id
        db.commit()

    result = client.post(
        "/api/v1/market-data/instruments/cleanup-synthetic",
        headers=csrf(client),
    )

    assert result.status_code == 200, result.text
    assert str(instrument_id) in result.json()["relink_ids"]
    with Session(test_engine) as db:
        transaction = db.scalar(
            select(PortfolioTransaction)
            .where(PortfolioTransaction.portfolio_id == portfolio_id)
            .where(PortfolioTransaction.transaction_type == "buy")
        )
        assert transaction is not None
        assert transaction.instrument_id is None
        assert db.get(Instrument, instrument_id).relink_required is True


@pytest.mark.parametrize(
    ("timezone_name", "provider_timestamp", "expected_session"),
    [
        ("Asia/Kolkata", datetime(2026, 4, 2, 18, 30, tzinfo=timezone.utc), date(2026, 4, 3)),
        ("America/New_York", datetime(2026, 4, 3, 4, 0, tzinfo=timezone.utc), date(2026, 4, 3)),
        ("Asia/Kolkata", datetime(2026, 4, 3, 19, 0, tzinfo=timezone.utc), date(2026, 4, 4)),
        ("America/New_York", datetime(2026, 4, 4, 1, 0, tzinfo=timezone.utc), date(2026, 4, 3)),
    ],
)
def test_daily_bar_normalization_preserves_exchange_session_date(
    timezone_name: str, provider_timestamp: datetime, expected_session: date
) -> None:
    candidate = InstrumentCandidate(
        provider="stub",
        provider_instrument_id="timezone-test",
        provider_symbol="TEST",
        symbol="TEST",
        display_name="Timezone Test",
        asset_class="equity",
        venue="NSE" if timezone_name == "Asia/Kolkata" else "NASDAQ",
        currency="INR" if timezone_name == "Asia/Kolkata" else "USD",
        country="IN" if timezone_name == "Asia/Kolkata" else "US",
        timezone=timezone_name,
        is_synthetic=False,
    )
    bar = HistoricalBar(
        timestamp=provider_timestamp,
        open=Decimal("9"),
        high=Decimal("12"),
        low=Decimal("8"),
        close=Decimal("11"),
        volume=Decimal("100"),
    )

    normalized = market_data_api._normalize_bar_timestamp(bar, candidate, "1d")

    assert normalized.timestamp.date() == expected_session
    assert normalized.timestamp.hour == 0
    assert normalized.timestamp.tzinfo == timezone.utc
