import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import market_data
from app.api.market_data import (
    MarketDataProviderChain,
    MarketDataSourceOption,
    get_market_data_provider,
)
from app.db import Base, get_db
from app.main import app
from app.market_data import MarketDataProviderError
from app.models import (
    Instrument,
    InstrumentProviderIdentifier,
    MarketBar,
    MarketQuote,
    PortfolioTransaction,
    User,
)
from app.services.price_freshness import IST, classify_quote, venue_session
from app.services.research_pipeline import _financial_trend_section, _technicals_section
from market_data_fixtures import RecordedMarketDataProvider


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
    app.dependency_overrides[get_market_data_provider] = RecordedMarketDataProvider
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def register(client: TestClient, test_engine, email: str) -> None:
    password = "correct-horse-battery"
    assert client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).status_code == 200
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.email_verified = True
        db.commit()


def create_holding(client: TestClient) -> tuple[dict, dict]:
    portfolio_response = client.post(
        "/api/v1/portfolios",
        headers=csrf(client),
        json={"name": "Freshness test", "base_currency": "USD"},
    )
    assert portfolio_response.status_code == 201, portfolio_response.text
    portfolio = portfolio_response.json()
    assert client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "deposit",
            "amount": "5000",
            "occurred_at": "2025-01-01T00:00:00Z",
        },
    ).status_code == 201
    assert client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "buy",
            "symbol": "ALPHA",
            "quantity": "10",
            "unit_price": "100",
            "occurred_at": "2025-01-02T00:00:00Z",
        },
    ).status_code == 201
    results = client.get("/api/v1/market-data/instruments?query=ALPHA")
    assert results.status_code == 200, results.text
    instrument = next(
        item for item in results.json()
        if item["symbol"] == "ALPHA" and item["venue"] == "DEMO_US"
    )
    attach = client.post(
        f"/api/v1/portfolios/{portfolio['id']}/positions/ALPHA/instrument",
        headers=csrf(client),
        json={"instrument_id": instrument["id"]},
    )
    assert attach.status_code == 200, attach.text
    return portfolio, instrument


def add_quote(
    test_engine,
    instrument_id: str,
    *,
    retrieved_age: timedelta = timedelta(0),
    timestamp_age: timedelta = timedelta(0),
    synthetic: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    with Session(test_engine) as db:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.instrument_id == UUID(instrument_id),
                InstrumentProviderIdentifier.provider == "recorded",
            )
        )
        if mapping is not None:
            mapping.is_verified = True
        db.add(
            MarketQuote(
                instrument_id=UUID(instrument_id),
                timestamp=now - timestamp_age,
                open=Decimal("99"),
                high=Decimal("101"),
                low=Decimal("98"),
                close=Decimal("100"),
                volume=Decimal("1000"),
                source="recorded",
                retrieved_at=now - retrieved_age,
                is_synthetic=synthetic,
            )
        )
        db.commit()


def test_ten_minute_quote_is_usable_and_portfolio_risk_is_available(
    client, test_engine
):
    register(client, test_engine, "usable-price@example.com")
    portfolio, instrument = create_holding(client)
    add_quote(
        test_engine,
        instrument["id"],
        retrieved_age=timedelta(seconds=10),
        timestamp_age=timedelta(seconds=10),
    )

    response = client.get(
        f"/api/v1/portfolios/{portfolio['id']}/risk-metrics"
    )
    assert response.status_code == 200, response.text
    metrics = response.json()
    position = next(
        item for item in metrics["position_weights"] if item["symbol"] == "ALPHA"
    )
    assert position["weight_pct"] is not None
    assert metrics["hhi"] is not None
    assert metrics["cash_pct"] is not None
    exposure = next(
        item for item in metrics["currency_exposure"] if item["currency"] == "USD"
    )
    assert exposure["complete"] is True
    assert exposure["market_value"] is not None


def test_portfolio_valuation_suppresses_unverified_provider_mapping(
    client, test_engine
):
    register(client, test_engine, "unverified-valuation@example.com")
    portfolio, instrument = create_holding(client)
    add_quote(test_engine, instrument["id"])
    with Session(test_engine) as db:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.instrument_id == UUID(instrument["id"]),
                InstrumentProviderIdentifier.provider == "recorded",
            )
        )
        assert mapping is not None
        mapping.is_verified = False
        db.commit()

    response = client.get(f"/api/v1/portfolios/{portfolio['id']}/valuation")

    assert response.status_code == 200, response.text
    position = response.json()["positions"][0]
    assert position["last_price"] is None
    assert position["market_value"] is None
    assert position["freshness"] == "stale"
    assert "mapping is unverified" in position["message"]


def test_quote_classification_uses_age_synthetic_and_currency_reasons():
    now = datetime.now(timezone.utc)
    instrument = SimpleNamespace(currency="USD")
    fresh_quote = SimpleNamespace(
        retrieved_at=now - timedelta(seconds=10),
        timestamp=now - timedelta(seconds=10),
        source="test",
        is_synthetic=False,
    )
    instrument.provider_identifiers = [SimpleNamespace(provider="test", is_verified=True)]
    usable, reason, evidence = classify_quote(fresh_quote, instrument, "USD", now, last_completed_session=None)
    assert usable is True and reason is None
    assert evidence["price_freshness_basis"] == "market_timestamp_and_retrieved_at"
    assert evidence["quote_timestamp_age_seconds"] < 60

    stale_quote = SimpleNamespace(
        retrieved_at=now - timedelta(seconds=5),
        timestamp=now - timedelta(minutes=2),
        source="test",
        is_synthetic=False,
    )
    assert classify_quote(stale_quote, instrument, "USD", now, last_completed_session=None)[1] == "stale_price"

    future_quote = SimpleNamespace(
        retrieved_at=now,
        timestamp=now + timedelta(minutes=1),
        source="test",
        is_synthetic=False,
    )
    assert classify_quote(future_quote, instrument, "USD", now, last_completed_session=None)[1] == "future_dated_price"

    historical_quote = SimpleNamespace(
        retrieved_at=now,
        timestamp=now,
        source="yfinance",
        is_synthetic=False,
    )
    assert classify_quote(historical_quote, instrument, "USD", now, last_completed_session=None)[1] == (
        "historical_close_not_current"
    )

    synthetic_quote = SimpleNamespace(
        retrieved_at=now - timedelta(days=20),
        timestamp=now,
        source="test",
        is_synthetic=True,
    )
    assert classify_quote(synthetic_quote, instrument, "USD", now, last_completed_session=None)[1] == "synthetic_price"

    foreign_instrument = SimpleNamespace(currency="INR")
    assert classify_quote(fresh_quote, foreign_instrument, "USD", now, last_completed_session=None)[1] == (
        "foreign_currency_no_fx"
    )


def test_angel_one_live_ticks_use_verified_mapping_and_staleness_limits():
    now = datetime.now(timezone.utc)
    instrument = SimpleNamespace(
        currency="INR",
        provider_identifiers=[
            SimpleNamespace(provider="angel_one", is_verified=True)
        ],
    )
    tick = SimpleNamespace(
        retrieved_at=now,
        timestamp=now,
        source="angel_one_ws",
        is_synthetic=False,
    )

    usable, reason, evidence = classify_quote(tick, instrument, "INR", now, last_completed_session=None)

    assert usable is True
    assert reason is None
    assert evidence["quote_data_type"] == "live_tick"
    assert evidence["quote_mapping_verified"] is True

    stale_tick = SimpleNamespace(
        retrieved_at=now,
        timestamp=now - timedelta(minutes=2),
        source="angel_one_ws",
        is_synthetic=False,
    )
    stale_usable, stale_reason, stale_evidence = classify_quote(
        stale_tick, instrument, "INR", now, last_completed_session=None
    )
    assert stale_usable is False
    assert stale_reason == "stale_price"
    assert stale_evidence["quote_freshness_status"] == "stale"


def test_currency_exposure_is_incomplete_when_any_position_is_unvalued(test_engine):
    instrument = SimpleNamespace(id=UUID(int=1), currency="USD")
    positions = {
        "ALPHA": SimpleNamespace(quantity=Decimal("1")),
        "BETA": SimpleNamespace(quantity=Decimal("2")),
    }
    instruments = {"ALPHA": instrument, "BETA": SimpleNamespace(id=UUID(int=2), currency="USD")}
    quote = SimpleNamespace(
        close=Decimal("100"),
        retrieved_at=datetime.now(timezone.utc),
        timestamp=datetime.now(timezone.utc) - timedelta(hours=4),
        source="test",
        is_synthetic=False,
    )

    from app.services.risk_metrics import position_currency_exposure

    with Session(test_engine) as db:
        exposure = position_currency_exposure(
            db,
            positions,
            instruments,
            {instrument.id: quote},
            datetime.now(timezone.utc),
        )
    usd = next(item for item in exposure if item["currency"] == "USD")
    assert usd["complete"] is False
    assert usd["market_value"] is None


def test_alert_rsi_matches_research_brief_indicators(client, test_engine):
    register(client, test_engine, "indicator-parity@example.com")
    portfolio, instrument = create_holding(client)
    now = datetime.now(timezone.utc)
    with Session(test_engine) as db:
        db.add_all(
            MarketBar(
                instrument_id=UUID(instrument["id"]),
                interval="1d",
                timestamp=now - timedelta(days=500 - index),
                open=Decimal(100 + index),
                high=Decimal(101 + index),
                low=Decimal(99 + index),
                close=Decimal(100 + index),
                volume=Decimal("1000"),
                source="test",
                retrieved_at=now,
                is_synthetic=False,
            )
            for index in range(500)
        )
        db.commit()
    add_quote(test_engine, instrument["id"])
    create_rule = client.post(
        "/api/v1/alerts/rules",
        headers=csrf(client),
        json={
            "scope": "position",
            "portfolio_id": portfolio["id"],
            "instrument_id": instrument["id"],
            "rule_type": "rsi_threshold",
            "params": {"period": 14, "threshold": 0, "direction": "above"},
            "severity": "attention",
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    assert create_rule.status_code == 201, create_rule.text
    evaluated = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert evaluated.status_code == 200, evaluated.text
    evidence = evaluated.json()["fired"][0]["evidence"]
    assert evidence["bars_used"] == 500

    with Session(test_engine) as db:
        quality: list[dict[str, str]] = []
        technicals = _technicals_section(db, UUID(instrument["id"]), quality)
    brief_rsi = technicals["indicators"]["rsi_14"]["value"]
    assert evidence["rsi"] == brief_rsi


def test_days_held_min_and_max_reflect_separate_lots(client, test_engine):
    register(client, test_engine, "lot-age-range@example.com")
    portfolio, _instrument = create_holding(client)
    now = datetime.now(timezone.utc)
    second_buy = client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "buy",
            "symbol": "ALPHA",
            "quantity": "1",
            "unit_price": "100",
            "occurred_at": (now - timedelta(days=300)).isoformat(),
        },
    )
    assert second_buy.status_code == 201, second_buy.text
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == "lot-age-range@example.com"))
        transactions = list(
            db.scalars(
                select(PortfolioTransaction).where(
                    PortfolioTransaction.portfolio_id == UUID(portfolio["id"])
                )
            )
        )
        deposit = next(item for item in transactions if item.transaction_type == "deposit")
        buys = [item for item in transactions if item.transaction_type == "buy"]
        deposit.occurred_at = now - timedelta(days=501)
        buys[0].occurred_at = now - timedelta(days=500)
        buys[1].occurred_at = now - timedelta(days=300)
        db.commit()
        assert user is not None

    response = client.get(f"/api/v1/portfolios/{portfolio['id']}/risk-metrics")
    assert response.status_code == 200, response.text
    position = next(
        item for item in response.json()["position_weights"] if item["symbol"] == "ALPHA"
    )
    assert position["days_held_min"] == 300
    assert position["days_held_max"] == 500


def test_financial_growth_from_nonpositive_base_is_not_meaningful():
    from app.models import InstrumentFundamental

    def fundamental(period_end: date, net_income: Decimal) -> InstrumentFundamental:
        return InstrumentFundamental(
            instrument_id=UUID(int=1),
            period_type="annual",
            period_end=period_end,
            currency="USD",
            source="test",
            fetched_at=datetime.now(timezone.utc),
            revenue=Decimal("100"),
            net_income=net_income,
            eps=Decimal("1"),
            free_cash_flow=Decimal("10"),
        )

    prior_date = date(2024, 12, 31)
    current_date = date(2025, 12, 31)
    rows = [
        fundamental(prior_date, Decimal("-10")),
        fundamental(current_date, Decimal("5")),
    ]
    quality: list[dict[str, str]] = []

    result = _financial_trend_section(rows, quality)
    current_row = next(
        item for item in result["periods"]
        if item["period_end"] == current_date.isoformat()
    )
    assert current_row["values"]["net_income"]["year_on_year_growth_pct"] is None
    assert any(
        "non-positive prior-period base" in item["reason"]
        for item in quality
        if item["field_path"].endswith(".net_income")
    )


def _in_instrument() -> SimpleNamespace:
    return SimpleNamespace(
        country="IN",
        currency="INR",
        provider_identifiers=[SimpleNamespace(provider="dhan", is_verified=True)],
    )


def _ist(*args: int) -> datetime:
    return datetime(*args, tzinfo=IST)


def _dhan_quote(timestamp: datetime, retrieved_at: datetime, close: str = "987.00") -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=timestamp,
        retrieved_at=retrieved_at,
        close=Decimal(close),
        source="dhan",
        is_synthetic=False,
    )


def test_p0h2_continuous_crypto_venue_never_returns_session_close_or_unknown():
    # Saturday 2026-11-07: the NSE calendar would report a closed market here.
    now = _ist(2026, 11, 7, 12, 0)
    crypto = SimpleNamespace(
        country="IN",
        asset_class="crypto",
        currency="INR",
        provider_identifiers=[SimpleNamespace(provider="dhan", is_verified=True)],
    )
    fresh = _dhan_quote(now - timedelta(seconds=10), now - timedelta(seconds=10))
    old = _dhan_quote(now - timedelta(hours=2), now - timedelta(hours=2))

    for quote, last_session in (
        (fresh, date(2026, 11, 6)),
        (fresh, None),
        (old, date(2026, 11, 6)),
        (old, None),
    ):
        usable, reason, evidence = classify_quote(
            quote, crypto, None, now, last_completed_session=last_session
        )
        assert evidence["quote_freshness_status"] in {"live", "delayed"}
        assert reason not in {"last_completed_session_unavailable", "session_state_unknown"}
        assert evidence["quote_session_model"] == "continuous_age"
        assert evidence["venue_session_state"] is None
        assert evidence["quote_freshness_status"] != "session_close"

    usable, reason, evidence = classify_quote(
        fresh, crypto, None, now, last_completed_session=None
    )
    assert (usable, reason) == (True, None)
    assert evidence["quote_freshness_status"] == "live"

    usable, reason, evidence = classify_quote(
        old, crypto, None, now, last_completed_session=date(2026, 11, 6)
    )
    assert (usable, reason) == (False, "delayed_price")
    assert evidence["quote_freshness_status"] == "delayed"


def test_classify_quote_live_during_open_session():
    now = _ist(2026, 11, 2, 10, 0)
    quote = _dhan_quote(now - timedelta(seconds=10), now - timedelta(seconds=10))

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 10, 30)
    )

    assert (usable, reason) == (True, None)
    assert evidence["quote_freshness_status"] == "live"


def test_classify_quote_delayed_when_open_session_quote_is_old():
    now = _ist(2026, 11, 2, 10, 0)
    quote = _dhan_quote(now - timedelta(seconds=300), now - timedelta(seconds=300))

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 10, 30)
    )

    assert (usable, reason) == (False, "delayed_price")
    assert evidence["quote_freshness_status"] == "delayed"


def test_classify_quote_session_close_for_closing_price_after_post_close_window():
    now = _ist(2026, 11, 2, 20, 8)
    quote = _dhan_quote(_ist(2026, 11, 2, 15, 59, 50), now)

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 11, 2)
    )

    assert (usable, reason) == (True, None)
    assert evidence["quote_freshness_status"] == "session_close"
    assert evidence["venue_session_state"] == "closed"
    assert evidence["quote_session_date"] == "2026-11-02"


def test_classify_quote_stale_when_closed_market_quote_predates_last_session():
    now = _ist(2026, 11, 2, 20, 8)
    quote = _dhan_quote(_ist(2026, 10, 29, 15, 25), now)

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 11, 2)
    )

    assert (usable, reason) == (False, "stale_price")
    assert evidence["quote_freshness_status"] == "stale"


def test_classify_quote_unavailable_without_quote():
    now = _ist(2026, 11, 2, 10, 0)

    usable, reason, evidence = classify_quote(
        None, _in_instrument(), None, now, last_completed_session=date(2026, 10, 30)
    )

    assert (usable, reason) == (False, "quote_unavailable")
    assert evidence["quote_freshness_status"] == "unavailable"


def test_classify_quote_unknown_when_closed_market_has_no_last_session():
    now = _ist(2026, 11, 2, 20, 8)
    quote = _dhan_quote(_ist(2026, 11, 2, 15, 59, 50), now)

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=None
    )

    assert (usable, reason) == (False, "last_completed_session_unavailable")
    assert evidence["quote_freshness_status"] == "unknown"


def test_classify_quote_unknown_on_special_session_date():
    now = _ist(2026, 11, 8, 18, 0)
    quote = _dhan_quote(now - timedelta(seconds=5), now - timedelta(seconds=5))

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 11, 2)
    )

    assert venue_session(_in_instrument(), now).state == "unknown"
    assert (usable, reason) == (False, "session_state_unknown")
    assert evidence["quote_freshness_status"] == "unknown"


def test_classify_quote_holiday_weekday_is_closed_not_live():
    now = _ist(2026, 11, 10, 11, 0)
    quote = _dhan_quote(_ist(2026, 11, 9, 15, 20), now)

    session = venue_session(_in_instrument(), now)
    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 11, 9)
    )

    assert session.state == "closed"
    assert session.reason == "weekend_or_holiday"
    assert (usable, reason) == (True, None)
    assert evidence["quote_freshness_status"] == "session_close"


def test_classify_quote_unknown_beyond_last_covered_year():
    now = _ist(2027, 1, 4, 10, 0)
    quote = _dhan_quote(now - timedelta(seconds=5), now - timedelta(seconds=5))

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 12, 31)
    )

    assert venue_session(_in_instrument(), now).reason == "calendar_not_covered"
    assert (usable, reason) == (False, "session_state_unknown")
    assert evidence["quote_freshness_status"] == "unknown"


def test_classify_quote_unknown_on_unverified_holiday():
    now = _ist(2026, 1, 15, 10, 0)

    session = venue_session(_in_instrument(), now)

    assert session.state == "unknown"
    assert session.reason == "holiday_unverified"


def test_classify_quote_unverified_mapping_never_reports_usable_status():
    now = _ist(2026, 11, 2, 20, 8)
    instrument = _in_instrument()
    instrument.provider_identifiers[0].is_verified = False
    quote = _dhan_quote(_ist(2026, 11, 2, 15, 59, 50), now)

    usable, reason, evidence = classify_quote(
        quote, instrument, None, now, last_completed_session=date(2026, 11, 2)
    )

    assert (usable, reason) == (False, "unverified_provider_mapping")
    assert evidence["quote_freshness_status"] == "unverified"


def test_session_close_is_downgraded_to_stale_beyond_two_trading_session_cap():
    from app.config import settings

    assert settings.nse_session_close_max_trading_sessions == 2
    now = _ist(2026, 11, 2, 20, 8)
    # Oct 29 -> Oct 30 and Nov 2 are 2 trading sessions: within the cap.
    within_cap = _dhan_quote(_ist(2026, 10, 29, 15, 25), now)
    # Oct 28 -> Oct 29, Oct 30 and Nov 2 are 3 trading sessions: beyond the cap.
    beyond_cap = _dhan_quote(_ist(2026, 10, 28, 15, 25), now)

    within = classify_quote(
        within_cap, _in_instrument(), None, now, last_completed_session=date(2026, 10, 29)
    )
    beyond = classify_quote(
        beyond_cap, _in_instrument(), None, now, last_completed_session=date(2026, 10, 28)
    )

    assert within[:2] == (True, None)
    assert within[2]["quote_freshness_status"] == "session_close"
    assert beyond[:2] == (False, "stale_price")
    assert beyond[2]["quote_freshness_status"] == "stale"


def test_diwali_weekend_and_holidays_do_not_count_as_trading_sessions():
    from app.services.price_freshness import nse_trading_sessions_elapsed

    # Fri Nov 6 close. Nov 8 (Sun) is a Muhurat special session and trades.
    # Nov 7 is Saturday and Nov 10 is a holiday, so neither counts.
    assert nse_trading_sessions_elapsed(date(2026, 11, 6), _ist(2026, 11, 10, 10, 0)) == 2
    assert nse_trading_sessions_elapsed(date(2026, 11, 6), _ist(2026, 11, 11, 10, 0)) == 3
    assert nse_trading_sessions_elapsed(date(2026, 11, 6), _ist(2026, 11, 12, 10, 0)) == 4


def test_friday_close_viewed_wednesday_across_diwali_counts_muhurat_session():
    # Fri Nov 6 close viewed Wed Nov 11. Five calendar days would fail the old cap.
    # Under the exchange calendar Nov 8 Muhurat, Nov 9 and Nov 11 are three sessions,
    # so this is stale. Excluding Muhurat would make it session_close. See the report.
    now = _ist(2026, 11, 11, 20, 0)
    quote = _dhan_quote(_ist(2026, 11, 6, 15, 25), now)

    usable, reason, evidence = classify_quote(
        quote, _in_instrument(), None, now, last_completed_session=date(2026, 11, 6)
    )

    assert (usable, reason) == (False, "stale_price")
    assert evidence["quote_freshness_status"] == "stale"


def test_missing_last_completed_session_is_unknown_and_logged(caplog):
    now = _ist(2026, 11, 2, 20, 8)
    quote = _dhan_quote(_ist(2026, 10, 30, 15, 25), now)

    with caplog.at_level("WARNING", logger="app.services.price_freshness"):
        usable, reason, evidence = classify_quote(
            quote, _in_instrument(), None, now, last_completed_session=None
        )

    assert (usable, reason) == (False, "last_completed_session_unavailable")
    assert evidence["quote_freshness_status"] == "unknown"
    assert "reason=last_completed_session_unavailable" in caplog.text


def test_daily_close_change_uses_decimal_against_prior_session_close(monkeypatch):
    from app.api import market_data

    monkeypatch.setattr(
        market_data,
        "_adjust_previous_daily_close",
        lambda db, user_id, instrument, close, price_date: (close, "no_adjustment"),
    )
    previous_bar = SimpleNamespace(
        timestamp=datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc),
        close=Decimal("961.85"),
    )
    change, percent, basis, unavailable = market_data._daily_close_change(
        None,
        UUID(int=9),
        SimpleNamespace(timezone="Asia/Kolkata"),
        Decimal("987.00"),
        date(2026, 11, 2),
        previous_bar,
        basis="quote",
    )

    assert change == Decimal("25.15")
    assert isinstance(change, Decimal) and isinstance(percent, Decimal)
    assert percent == Decimal("2.6148")
    assert unavailable is None
    assert "2026-10-30" in basis


def test_daily_close_change_is_unavailable_without_prior_session_close():
    from app.api import market_data

    change, percent, basis, reason = market_data._daily_close_change(
        None,
        UUID(int=9),
        SimpleNamespace(timezone="Asia/Kolkata"),
        Decimal("987.00"),
        date(2026, 11, 2),
        None,
        basis="quote",
    )

    assert (change, percent) == (None, None)
    assert reason == "stored real previous-session daily close unavailable"


FROZEN_NOW_IST = datetime(2026, 11, 2, 20, 8, tzinfo=IST)


class _FrozenMarketDataClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW_IST.astimezone(tz) if tz is not None else FROZEN_NOW_IST.replace(tzinfo=None)


def test_sbin_post_close_quote_on_closed_market_resolves_to_session_close(
    client, test_engine, monkeypatch
):
    monkeypatch.setattr(market_data, "datetime", _FrozenMarketDataClock)
    register(client, test_engine, "sbin-session-close@example.com")
    with Session(test_engine) as db:
        instrument = Instrument(
            symbol="SBIN",
            display_name="State Bank of India",
            asset_class="equity",
            venue="NSE",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=False,
            relink_required=False,
            created_at=FROZEN_NOW_IST,
        )
        db.add(instrument)
        db.flush()
        db.add(
            InstrumentProviderIdentifier(
                instrument_id=instrument.id,
                provider="dhan",
                provider_instrument_id="3045|NSE_EQ",
                provider_symbol="SBIN",
                is_verified=True,
                last_seen_at=FROZEN_NOW_IST,
            )
        )
        db.add(
            MarketBar(
                instrument_id=instrument.id,
                interval="1d",
                timestamp=datetime(2026, 10, 29, 18, 30, tzinfo=timezone.utc),
                open=Decimal("960"),
                high=Decimal("965"),
                low=Decimal("955"),
                close=Decimal("961.85"),
                volume=Decimal("1000"),
                source="dhan",
                retrieved_at=FROZEN_NOW_IST,
                is_synthetic=False,
            )
        )
        db.add(
            MarketQuote(
                instrument_id=instrument.id,
                # 15:59:50 IST stored as UTC; SQLite drops tzinfo so storage must be UTC.
                timestamp=datetime(2026, 11, 2, 10, 29, 50, tzinfo=timezone.utc),
                open=Decimal("980"),
                high=Decimal("990"),
                low=Decimal("975"),
                close=Decimal("987.00"),
                volume=Decimal("1000"),
                source="dhan",
                retrieved_at=FROZEN_NOW_IST,
                is_synthetic=False,
            )
        )
        db.commit()
        instrument_id = str(instrument.id)

    monkeypatch.setattr(market_data, "_available_market_data_sources", lambda db, user_id: [
        MarketDataSourceOption(name="dhan", label="dhan", available=True, status="connected")
    ])
    class _DhanRestUnavailable:
        name = "dhan"
        is_mock = False

        def get_latest_quote(self, candidate):  # noqa: ARG002
            raise MarketDataProviderError("provider_unavailable")

    app.dependency_overrides[get_market_data_provider] = lambda: MarketDataProviderChain([
        _DhanRestUnavailable()
    ])
    response = client.get(f"/api/v1/market-data/instruments/{instrument_id}/quote")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["freshness_status"] == "session_close", json.dumps(body, default=str)
    assert float(body["bar"]["close"]) == 987.0
