from collections.abc import Iterator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    CorporateAction,
    CorporateActionApplication,
    PortfolioTransaction,
    User,
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


def register(client: TestClient, email: str) -> None:
    registration = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert registration.status_code == 202
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert login.status_code == 200


def verify_email(test_engine, email: str) -> None:
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.email_verified = True
        db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def create_position(client: TestClient, name: str) -> tuple[dict, dict]:
    portfolio_response = client.post(
        "/api/v1/portfolios",
        headers=csrf(client),
        json={"name": name, "base_currency": "USD"},
    )
    assert portfolio_response.status_code == 201, portfolio_response.text
    portfolio = portfolio_response.json()
    portfolio_id = portfolio["id"]
    deposit = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "deposit",
            "amount": "5000.00",
            "occurred_at": "2026-01-01T00:00:00Z",
        },
    )
    assert deposit.status_code == 201, deposit.text
    buy = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "buy",
            "symbol": "ALPHA",
            "quantity": "10",
            "unit_price": "100",
            "occurred_at": "2026-01-02T00:00:00Z",
        },
    )
    assert buy.status_code == 201, buy.text
    instruments = client.get("/api/v1/market-data/instruments?query=ALPHA").json()
    instrument = next(item for item in instruments if item["venue"] == "DEMO_US")
    linked = client.post(
        f"/api/v1/portfolios/{portfolio_id}/positions/ALPHA/instrument",
        headers=csrf(client),
        json={"instrument_id": instrument["id"]},
    )
    assert linked.status_code == 200, linked.text
    return portfolio, instrument


def create_action(
    client: TestClient,
    instrument_id: str,
    action_type: str,
    *,
    ratio: str | None = None,
    amount: str | None = None,
    ex_date: str = "2026-01-10",
) -> dict:
    response = client.post(
        "/api/v1/corporate-actions",
        headers=csrf(client),
        json={
            "instrument_id": instrument_id,
            "action_type": action_type,
            "ex_date": ex_date,
            "ratio": ratio,
            "amount": amount,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def apply_action(client: TestClient, portfolio_id: str, action_id: str):
    return client.post(
        f"/api/v1/corporate-actions/portfolios/{portfolio_id}/{action_id}/apply",
        headers=csrf(client),
    )


def test_split_application_is_idempotent_reversible_and_preserves_cost_and_start(
    client: TestClient, test_engine
) -> None:
    email = "split@example.com"
    register(client, email)
    verify_email(test_engine, email)
    portfolio, instrument = create_position(client, "Split portfolio")
    action = create_action(client, instrument["id"], "split", ratio="2")

    applied = apply_action(client, portfolio["id"], action["id"])
    assert applied.status_code == 200, applied.text
    adjustment = applied.json()
    assert adjustment["affected_quantity"] == "10.00000000"
    assert adjustment["quantity_adjustment"] == "10.00000000"
    assert adjustment["cash_adjustment"] == "0.00"
    repeated = apply_action(client, portfolio["id"], action["id"])
    assert repeated.status_code == 200
    assert repeated.json()["id"] == adjustment["id"]

    summary = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    position = summary["positions"][0]
    assert position["quantity"] == "20.00000000"
    assert position["cost_basis"] == "1000.00"
    assert position["average_cost"] == "50.00"
    assert position["holding_period_start"] == "2026-01-02T00:00:00Z"

    reversed_response = client.post(
        f"/api/v1/corporate-actions/portfolios/{portfolio['id']}/applications/{adjustment['id']}/reverse",
        headers=csrf(client),
    )
    assert reversed_response.status_code == 200, reversed_response.text
    assert reversed_response.json()["active"] is False
    restored = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    assert restored["positions"][0]["quantity"] == "10.00000000"
    assert restored["positions"][0]["cost_basis"] == "1000.00"
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(PortfolioTransaction)) == 2


def test_one_to_one_bonus_creates_new_holding_period_without_changing_total_cost(
    client: TestClient, test_engine
) -> None:
    email = "bonus@example.com"
    register(client, email)
    verify_email(test_engine, email)
    portfolio, instrument = create_position(client, "Bonus portfolio")
    action = create_action(client, instrument["id"], "bonus", ratio="1")
    applied = apply_action(client, portfolio["id"], action["id"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["quantity_adjustment"] == "10.00000000"

    sale = client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "sell",
            "symbol": "ALPHA",
            "quantity": "10",
            "unit_price": "60",
            "occurred_at": "2026-01-15T00:00:00Z",
        },
    )
    assert sale.status_code == 201, sale.text
    summary = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    position = summary["positions"][0]
    assert position["quantity"] == "10.00000000"
    assert position["cost_basis"] == "500.00"
    assert position["average_cost"] == "50.00"
    assert position["holding_period_start"] == "2026-01-10T00:00:00Z"


def test_cash_dividend_uses_ex_date_shares_and_updates_cash_and_realized_income(
    client: TestClient, test_engine
) -> None:
    email = "dividend@example.com"
    register(client, email)
    verify_email(test_engine, email)
    portfolio, instrument = create_position(client, "Dividend portfolio")
    action = create_action(client, instrument["id"], "dividend", amount="2")

    applied = apply_action(client, portfolio["id"], action["id"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["affected_quantity"] == "10.00000000"
    assert applied.json()["quantity_adjustment"] == "0E-8"
    assert applied.json()["cash_adjustment"] == "20.00"

    summary = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    assert summary["cash_balance"] == "4020.00"
    assert summary["realized_pnl"] == "20.00"
    assert summary["positions"][0]["quantity"] == "10.00000000"


def test_corporate_actions_require_verified_email_and_are_user_scoped(
    client: TestClient, test_engine
) -> None:
    register(client, "owner@example.com")
    portfolio, instrument = create_position(client, "Owner portfolio")

    assert client.get("/api/v1/corporate-actions").status_code == 403
    assert client.get("/api/v1/corporate-actions").json()["detail"]["code"] == "email_verification_required"

    verify_email(test_engine, "owner@example.com")
    action = create_action(client, instrument["id"], "split", ratio="2")
    assert client.get("/api/v1/corporate-actions").json()[0]["id"] == action["id"]

    other = TestClient(app)
    try:
        register(other, "other@example.com")
        verify_email(test_engine, "other@example.com")
        assert other.get("/api/v1/corporate-actions").json() == []
        other_portfolio_response = other.post(
            "/api/v1/portfolios",
            headers=csrf(other),
            json={"name": "Other portfolio", "base_currency": "USD"},
        )
        assert other_portfolio_response.status_code == 201
        attempted = apply_action(
            other,
            other_portfolio_response.json()["id"],
            action["id"],
        )
        assert attempted.status_code == 404
        hidden_applications = other.get(
            f"/api/v1/corporate-actions/portfolios/{portfolio['id']}/applications"
        )
        assert hidden_applications.status_code == 404
    finally:
        other.close()
