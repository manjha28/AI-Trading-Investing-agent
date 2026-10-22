from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    Instrument,
    MarketBar,
    MarketQuote,
    PositionThesis,
    Portfolio,
    PortfolioTransaction,
    User,
)
from app.models.alerts import AlertEvent, AlertRule
from app.services.alert_engine import evaluate_rules


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
        user.email_verified = True
        db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def portfolio_and_instrument(client: TestClient) -> tuple[dict, dict]:
    portfolio = client.post(
        "/api/v1/portfolios", headers=csrf(client),
        json={"name": "Main", "base_currency": "USD"},
    ).json()
    assert client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={"transaction_type": "deposit", "amount": "5000", "occurred_at": "2026-01-01T00:00:00Z"},
    ).status_code == 201
    assert client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={"transaction_type": "buy", "symbol": "ALPHA", "quantity": "10",
              "unit_price": "100", "occurred_at": "2026-01-02T00:00:00Z"},
    ).status_code == 201
    instruments = client.get("/api/v1/market-data/instruments?query=ALPHA").json()
    instrument = next(item for item in instruments if item["venue"] == "DEMO_US")
    assert client.post(
        f"/api/v1/portfolios/{portfolio['id']}/positions/ALPHA/instrument",
        headers=csrf(client), json={"instrument_id": instrument["id"]},
    ).status_code == 200
    return portfolio, instrument


def add_quote(test_engine, instrument_id: str, *, age: timedelta = timedelta(0), synthetic=False):
    now = datetime.now(timezone.utc) - age
    with Session(test_engine) as db:
        db.add(
            MarketQuote(
                instrument_id=UUID(instrument_id), timestamp=now,
                open=Decimal("99"), high=Decimal("101"), low=Decimal("98"),
                close=Decimal("100"), volume=Decimal("1000"), source="test",
                retrieved_at=now, is_synthetic=synthetic,
            )
        )
        db.commit()


def add_daily_bars(test_engine, instrument_id: str, closes: list[Decimal]) -> None:
    retrieved_at = datetime.now(timezone.utc)
    latest_timestamp = retrieved_at - timedelta(days=1)
    with Session(test_engine) as db:
        db.add_all(
            MarketBar(
                instrument_id=UUID(instrument_id),
                interval="1d",
                timestamp=latest_timestamp - timedelta(days=len(closes) - index - 1),
                open=close,
                high=close + Decimal("50"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1000"),
                source="test",
                retrieved_at=retrieved_at,
                is_synthetic=False,
            )
            for index, close in enumerate(closes)
        )
        db.commit()


def create_rule(client: TestClient, portfolio: dict, instrument: dict, **changes) -> dict:
    body = {
        "scope": "position",
        "portfolio_id": portfolio["id"],
        "instrument_id": instrument["id"],
        "rule_type": "price_above",
        "params": {"price": "90"},
        "severity": "urgent",
        "cooldown_seconds": 0,
        "enabled": True,
    }
    body.update(changes)
    response = client.post("/api/v1/alerts/rules", headers=csrf(client), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_repeat_fire_is_deduplicated_and_evidence_is_self_contained(client, test_engine):
    register(client, test_engine, "a@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    add_quote(test_engine, instrument["id"])
    create_rule(client, portfolio, instrument)
    first = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert first.status_code == 200, first.text
    assert len(first.json()["fired"]) == 1
    evidence = first.json()["fired"][0]["evidence"]
    assert evidence["quote_price"] == "100.00000000"
    assert evidence["threshold_price"] == "90"
    assert evidence["rule_type"] == "price_above"
    second = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert second.status_code == 200
    assert second.json()["fired"] == []
    assert len(second.json()["deduplicated"]) == 1
    assert len(client.get("/api/v1/alerts/events").json()) == 1


def test_cooldown_blocks_another_fire_even_on_a_later_day(client, test_engine):
    register(client, test_engine, "a@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    add_quote(test_engine, instrument["id"])
    rule = create_rule(
        client, portfolio, instrument, cooldown_seconds=3600,
        params={"price": "90"},
    )
    now = datetime.now(timezone.utc)
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        assert len(evaluate_rules(db, user.id, now=now)["fired"]) == 1
        next_run = evaluate_rules(db, user.id, now=now + timedelta(seconds=30))
        assert next_run["fired"] == []
        assert next_run["cooling_down"] == [rule["id"]]


def test_stale_quote_suppresses_thesis_target_with_reason(client, test_engine):
    register(client, test_engine, "a@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    add_quote(test_engine, instrument["id"], age=timedelta(days=2))
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        db.add(
            PositionThesis(
                user_id=user.id,
                portfolio_id=UUID(portfolio["id"]),
                instrument_id=UUID(instrument["id"]),
                symbol="ALPHA",
                status="active",
                entry_reason="Test thesis",
                horizon_days=365,
                conviction=3,
                target_levels=[{"price": "110", "trim_pct": "25"}],
                stop_level=Decimal("90"),
                invalidation_conditions=[],
                invalidation_notes=None,
                max_loss_pct=None,
                review_at=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    create_rule(
        client, portfolio, instrument, rule_type="thesis_target_hit",
        params={"target_index": 0},
    )
    response = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert response.status_code == 200, response.text
    assert response.json()["fired"] == []
    suppressed = response.json()["suppressed"][0]
    assert suppressed["reason"] == "stale_price"
    assert suppressed["evidence"]["suppression_reason"] == "stale_price"
    assert client.get("/api/v1/alerts/events").json() == []


def test_cross_user_rule_and_event_isolation(client, test_engine):
    register(client, test_engine, "a@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    add_quote(test_engine, instrument["id"])
    rule = create_rule(client, portfolio, instrument)
    client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    event = client.get("/api/v1/alerts/events").json()[0]

    client.cookies.clear()
    register(client, test_engine, "b@example.com")
    assert client.get("/api/v1/alerts/rules").json() == []
    assert client.get("/api/v1/alerts/events").json() == []
    assert client.patch(
        f"/api/v1/alerts/rules/{rule['id']}", headers=csrf(client), json={"enabled": False}
    ).status_code == 404
    assert client.delete(
        f"/api/v1/alerts/rules/{rule['id']}", headers=csrf(client)
    ).status_code == 404
    assert client.patch(
        f"/api/v1/alerts/events/{event['id']}", headers=csrf(client),
        json={"status": "dismissed"},
    ).status_code == 404
    assert client.post("/api/v1/alerts/evaluate", headers=csrf(client)).json()["fired"] == []
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(AlertRule)) == 1
        assert db.scalar(select(func.count()).select_from(AlertEvent)) == 1


def test_rule_crud_and_event_status(client, test_engine):
    register(client, test_engine, "a@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    rule = create_rule(client, portfolio, instrument)
    assert len(client.get("/api/v1/alerts/rules").json()) == 1
    updated = client.patch(
        f"/api/v1/alerts/rules/{rule['id']}", headers=csrf(client), json={"enabled": False}
    )
    assert updated.status_code == 200 and updated.json()["enabled"] is False
    assert client.post("/api/v1/alerts/evaluate", headers=csrf(client)).json()["fired"] == []
    assert client.delete(
        f"/api/v1/alerts/rules/{rule['id']}", headers=csrf(client)
    ).status_code == 204


def test_rule_validation_and_email_verification(client, test_engine):
    register(client, test_engine, "a@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    invalid = client.post(
        "/api/v1/alerts/rules", headers=csrf(client),
        json={
            "scope": "position", "portfolio_id": portfolio["id"],
            "instrument_id": instrument["id"], "rule_type": "ma_cross",
            "params": {"fast_period": 20, "slow_period": 10, "direction": "up"},
            "severity": "attention", "cooldown_seconds": 1, "enabled": True,
        },
    )
    assert invalid.status_code == 422

    client.cookies.clear()
    password = "correct-horse-battery"
    assert client.post(
        "/api/v1/auth/register", json={"email": "unverified@example.com", "password": password}
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login", json={"email": "unverified@example.com", "password": password}
    ).status_code == 200
    response = client.get("/api/v1/alerts/rules")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "email_verification_required"


def test_quote_freshness_uses_retrieved_at_and_alert_age_limit(client, test_engine):
    register(client, test_engine, "freshness@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    add_quote(test_engine, instrument["id"], age=timedelta(minutes=10))
    with Session(test_engine) as db:
        quote = db.scalar(select(MarketQuote).where(
            MarketQuote.instrument_id == UUID(instrument["id"])
        ))
        quote.timestamp = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    create_rule(client, portfolio, instrument)

    fresh_response = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert fresh_response.status_code == 200, fresh_response.text
    fired = fresh_response.json()["fired"]
    assert len(fired) == 1
    evidence = fired[0]["evidence"]
    assert evidence["quote_age_seconds"] < 900
    assert evidence["quote_timestamp_age_seconds"] >= 2 * 60 * 60

    with Session(test_engine) as db:
        quote = db.scalar(select(MarketQuote).where(
            MarketQuote.instrument_id == UUID(instrument["id"])
        ))
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        quote.timestamp = old_time
        quote.retrieved_at = old_time
        db.commit()
    stale_response = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert stale_response.status_code == 200, stale_response.text
    assert stale_response.json()["fired"] == []
    assert stale_response.json()["suppressed"][0]["reason"] == "stale_price"


def test_portfolio_rule_cooldown_is_scoped_per_instrument(client, test_engine):
    register(client, test_engine, "portfolio-cooldown@example.com")
    portfolio, first_instrument = portfolio_and_instrument(client)
    now = datetime.now(timezone.utc)
    more_instruments: list[tuple[str, UUID]] = []
    with Session(test_engine) as db:
        for symbol in ("BETA", "GAMMA"):
            instrument = Instrument(
                symbol=symbol,
                display_name=f"{symbol} Test Corporation",
                asset_class="equity",
                venue="TEST",
                currency="USD",
                country="US",
                timezone="UTC",
                created_at=now,
            )
            db.add(instrument)
            db.commit()
            db.refresh(instrument)
            more_instruments.append((instrument.symbol, instrument.id))
    for symbol, instrument_id in more_instruments:
        transaction_response = client.post(
            f"/api/v1/portfolios/{portfolio['id']}/transactions",
            headers=csrf(client),
            json={
                "transaction_type": "buy",
                "symbol": symbol,
                "quantity": "1",
                "unit_price": "100",
                "occurred_at": "2026-01-03T00:00:00Z",
            },
        )
        assert transaction_response.status_code == 201, transaction_response.text
        assert client.post(
            f"/api/v1/portfolios/{portfolio['id']}/positions/{symbol}/instrument",
            headers=csrf(client),
            json={"instrument_id": str(instrument_id)},
        ).status_code == 200
        add_quote(test_engine, str(instrument_id))
    add_quote(test_engine, first_instrument["id"])
    rule = create_rule(
        client,
        portfolio,
        first_instrument,
        scope="portfolio",
        instrument_id=None,
        cooldown_seconds=3600,
    )

    response = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert response.status_code == 200, response.text
    fired = response.json()["fired"]
    assert len(fired) == 3
    assert {event["instrument_id"] for event in fired} == {
        first_instrument["id"],
        *(str(instrument_id) for _, instrument_id in more_instruments),
    }
    assert all(event["rule_id"] == rule["id"] for event in fired)


def test_alert_rsi_matches_instrument_indicators_endpoint(client, test_engine):
    register(client, test_engine, "rsi-match@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    closes = [Decimal("100") + Decimal(index) / Decimal("10") for index in range(250)]
    add_daily_bars(test_engine, instrument["id"], closes)
    add_quote(test_engine, instrument["id"])
    create_rule(
        client,
        portfolio,
        instrument,
        rule_type="rsi_threshold",
        params={"period": 14, "threshold": 0, "direction": "above"},
    )

    alert_response = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert alert_response.status_code == 200, alert_response.text
    assert len(alert_response.json()["fired"]) == 1
    alert_evidence = alert_response.json()["fired"][0]["evidence"]
    indicators_response = client.get(
        f"/api/v1/instruments/{instrument['id']}/indicators?interval=1d"
    )
    assert indicators_response.status_code == 200, indicators_response.text
    indicator_rsi = indicators_response.json()["indicators"]["rsi_14"]["value"]
    assert alert_evidence["rsi"] == indicator_rsi
    assert alert_evidence["bars_used"] == 250


def test_ltcg_crossing_five_days_ago_fires_once(client, test_engine):
    register(client, test_engine, "ltcg-crossing@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    now = datetime.now(timezone.utc)
    create_rule(
        client,
        portfolio,
        instrument,
        rule_type="holding_period_crosses_365d",
        params={},
    )
    with Session(test_engine) as db:
        transactions = list(
            db.scalars(
                select(PortfolioTransaction).where(
                    PortfolioTransaction.portfolio_id == UUID(portfolio["id"]),
                    PortfolioTransaction.transaction_type == "buy",
                )
            )
        )
        transaction = transactions[0]
        transaction.occurred_at = now - timedelta(days=370)
        deposit = db.scalar(
            select(PortfolioTransaction).where(
                PortfolioTransaction.portfolio_id == UUID(portfolio["id"]),
                PortfolioTransaction.transaction_type == "deposit",
            )
        )
        deposit.occurred_at = now - timedelta(days=371)
        db.commit()
        user = db.scalar(select(User).where(User.email == "ltcg-crossing@example.com"))
        first = evaluate_rules(db, user.id, now=now)
        assert len(first["fired"]) == 1
        assert first["fired"][0].evidence["days_since_crossing"] == 5
        assert first["fired"][0].evidence["crossed_at"]

        second = evaluate_rules(db, user.id, now=now + timedelta(days=1))
        assert second["fired"] == []
        assert len(second["deduplicated"]) == 1
        assert db.scalar(select(func.count()).select_from(AlertEvent)) == 1


def test_drawdown_sign_matches_indicators_and_alert_evidence(client, test_engine):
    register(client, test_engine, "drawdown-match@example.com")
    portfolio, instrument = portfolio_and_instrument(client)
    closes = [Decimal("100")] * 249 + [Decimal("90")]
    add_daily_bars(test_engine, instrument["id"], closes)
    add_quote(test_engine, instrument["id"])
    create_rule(
        client,
        portfolio,
        instrument,
        rule_type="trailing_drawdown_pct",
        params={"days": 20, "threshold_pct": "5"},
    )

    alert_response = client.post("/api/v1/alerts/evaluate", headers=csrf(client))
    assert alert_response.status_code == 200, alert_response.text
    assert len(alert_response.json()["fired"]) == 1
    alert_drawdown = Decimal(
        alert_response.json()["fired"][0]["evidence"]["drawdown_pct"]
    )
    indicators_response = client.get(
        f"/api/v1/instruments/{instrument['id']}/indicators?interval=1d"
    )
    assert indicators_response.status_code == 200, indicators_response.text
    indicator_drawdown = Decimal(
        indicators_response.json()["indicators"]["drawdown_from_20d_peak"]["value"]
    )
    assert alert_drawdown > 0
    assert alert_drawdown == indicator_drawdown == Decimal("10")
