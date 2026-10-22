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
    MarketQuote,
    PositionThesis,
    Portfolio,
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
