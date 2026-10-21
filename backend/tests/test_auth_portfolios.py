from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import User


@pytest.fixture
def test_engine():
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    yield test_engine
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def client(test_engine) -> Iterator[TestClient]:

    def override_get_db():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 201
    return response.json()


def create_portfolio(client: TestClient, name: str) -> dict:
    csrf_token = client.cookies.get("csrf_token")
    response = client.post(
        "/api/v1/portfolios",
        headers={"X-CSRF-Token": csrf_token},
        json={"name": name, "base_currency": "usd"},
    )
    assert response.status_code == 201
    return response.json()


def add_transaction(client: TestClient, portfolio_id: str, payload: dict):
    return client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json=payload,
    )


def test_registration_hashes_password_and_issues_protected_session(
    client: TestClient, test_engine
) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "person@example.com", "password": "correct-horse-battery"},
    )
    registered = response.json()

    assert response.status_code == 201
    assert registered["email"] == "person@example.com"
    assert "password" not in registered
    assert client.cookies.get("session_token")
    assert client.cookies.get("csrf_token")
    assert any(
        cookie.startswith("session_token=") and "httponly" in cookie.lower()
        for cookie in response.headers.get_list("set-cookie")
    )
    with Session(test_engine) as session:
        user = session.scalar(select(User).where(User.email == "person@example.com"))
        assert user is not None
        assert user.password_hash != "correct-horse-battery"
        assert user.password_hash.startswith("scrypt$")


def test_registration_rejects_duplicate_email_case_insensitively(client: TestClient) -> None:
    register(client, "person@example.com")

    response = client.post(
        "/api/v1/auth/register",
        json={"email": "PERSON@example.com", "password": "another-correct-password"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "An account with this email already exists"}


def test_login_and_csrf_protected_logout(client: TestClient) -> None:
    register(client, "trader@example.com")
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "trader@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    csrf_token = client.cookies.get("csrf_token")

    logout = client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
    )
    assert logout.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_portfolios_are_isolated_and_require_csrf(client: TestClient) -> None:
    first_client = client
    register(first_client, "first@example.com")
    first_portfolio = create_portfolio(first_client, "Long term")

    second_client = TestClient(app)
    register(second_client, "second@example.com")
    second_portfolio = create_portfolio(second_client, "Long term")

    assert first_portfolio["base_currency"] == "USD"
    assert [item["id"] for item in first_client.get("/api/v1/portfolios").json()] == [
        first_portfolio["id"]
    ]
    assert [item["id"] for item in second_client.get("/api/v1/portfolios").json()] == [
        second_portfolio["id"]
    ]
    assert first_client.get(f"/api/v1/portfolios/{second_portfolio['id']}").status_code == 404
    assert first_client.post(
        "/api/v1/portfolios",
        json={"name": "No CSRF", "base_currency": "USD"},
    ).status_code == 403


def test_registration_validates_email_and_password(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "short"},
    )
    assert response.status_code == 422


def test_weighted_average_cost_and_fees_are_accounted_for(client: TestClient) -> None:
    register(client, "accounting@example.com")
    portfolio = create_portfolio(client, "Ledger")
    portfolio_id = portfolio["id"]

    assert add_transaction(
        client, portfolio_id, {"transaction_type": "deposit", "amount": "1000.00", "fee": "1.00"}
    ).status_code == 201
    for price in ("20.01", "30.01"):
        assert add_transaction(
            client,
            portfolio_id,
            {
                "transaction_type": "buy",
                "symbol": "abc",
                "quantity": "10",
                "unit_price": price,
                "fee": "2.00",
            },
        ).status_code == 201
    assert add_transaction(
        client,
        portfolio_id,
        {
            "transaction_type": "sell",
            "symbol": "ABC",
            "quantity": "5",
            "unit_price": "30.01",
            "fee": "1.00",
        },
    ).status_code == 201

    summary = client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    assert summary["currency"] == "USD"
    assert summary["cash_balance"] == "643.85"
    assert summary["invested_cost"] == "378.15"
    assert summary["realized_pnl"] == "22.00"
    assert summary["unrealized_pnl"] is None
    assert summary["market_value"] is None
    assert summary["positions"] == [
        {"symbol": "ABC", "quantity": "15.00000000", "cost_basis": "378.15", "average_cost": "25.21"}
    ]


def test_oversell_is_rejected_without_persisting(client: TestClient) -> None:
    register(client, "oversell@example.com")
    portfolio_id = create_portfolio(client, "Cash equity")["id"]
    add_transaction(client, portfolio_id, {"transaction_type": "deposit", "amount": "1000.00"})
    add_transaction(
        client,
        portfolio_id,
        {"transaction_type": "buy", "symbol": "ABC", "quantity": "2", "unit_price": "10"},
    )

    response = add_transaction(
        client,
        portfolio_id,
        {"transaction_type": "sell", "symbol": "ABC", "quantity": "3", "unit_price": "10"},
    )

    assert response.status_code == 422
    assert len(client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json()) == 2


def test_deleting_funding_cannot_leave_negative_cash(client: TestClient) -> None:
    register(client, "delete@example.com")
    portfolio_id = create_portfolio(client, "Delete check")["id"]
    funding = add_transaction(
        client, portfolio_id, {"transaction_type": "deposit", "amount": "100.00"}
    ).json()
    add_transaction(
        client,
        portfolio_id,
        {"transaction_type": "buy", "symbol": "ABC", "quantity": "2", "unit_price": "20"},
    )

    response = client.delete(
        f"/api/v1/portfolios/{portfolio_id}/transactions/{funding['id']}",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )

    assert response.status_code == 409
    assert len(client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json()) == 2


def test_portfolio_currency_support_is_explicit(client: TestClient) -> None:
    register(client, "currency@example.com")
    response = client.post(
        "/api/v1/portfolios",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json={"name": "Unsupported", "base_currency": "EUR"},
    )
    assert response.status_code == 422


def test_transaction_csv_preview_import_export_and_duplicate_detection(client: TestClient) -> None:
    register(client, "csv-import@example.com")
    portfolio_id = create_portfolio(client, "CSV portfolio")["id"]
    csv_content = (
        "transaction_type,symbol,quantity,unit_price,amount,fee,occurred_at\n"
        "deposit,,,,1000.00,0.00,2026-10-01T18:30:00+05:30\n"
        "deposit,,,,1000.00,0.00,2026-10-01T13:00:00Z\n"
        "buy,ABC,10,20.00,,1.00,2026-10-01T14:00:00Z\n"
    )
    preview = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/import/preview",
        json={"csv_content": csv_content},
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["valid_count"] == 2
    assert preview.json()["duplicate_count"] == 1
    assert preview.json()["invalid_count"] == 0
    imported = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/import",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json={"csv_content": csv_content},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"imported_count": 2, "skipped_duplicate_count": 1}

    repeated_import = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/import",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json={"csv_content": csv_content},
    )
    assert repeated_import.status_code == 200
    assert repeated_import.json() == {"imported_count": 0, "skipped_duplicate_count": 3}

    summary = client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    assert summary["cash_balance"] == "799.00"
    assert summary["positions"][0]["quantity"] == "10.00000000"
    exported = client.get(f"/api/v1/portfolios/{portfolio_id}/transactions/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert exported.text.startswith("transaction_type,symbol,quantity,unit_price,amount,fee,occurred_at")
    round_trip = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/import/preview",
        json={"csv_content": exported.text},
    )
    assert round_trip.status_code == 200
    assert round_trip.json()["valid_count"] == 0
    assert round_trip.json()["duplicate_count"] == 2
    assert len(client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json()) == 2


def test_transaction_csv_import_rejects_invalid_rows_atomically(client: TestClient) -> None:
    register(client, "csv-invalid@example.com")
    portfolio_id = create_portfolio(client, "CSV validation")["id"]
    csv_content = (
        "transaction_type,symbol,quantity,unit_price,amount,fee,occurred_at\n"
        "deposit,,,,1000.00,0.00,2026-10-01T13:00:00Z\n"
        "buy,=1+1,1,10.00,,0.00,2026-10-01T14:00:00Z\n"
    )
    response = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/import",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json={"csv_content": csv_content},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["invalid_count"] == 1
    assert client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json() == []


def test_transaction_csv_preview_rejects_negative_cash_ledger(client: TestClient) -> None:
    register(client, "csv-ledger@example.com")
    portfolio_id = create_portfolio(client, "CSV ledger check")["id"]
    csv_content = (
        "transaction_type,symbol,quantity,unit_price,amount,fee,occurred_at\n"
        "buy,ABC,1,10.00,,0.00,2026-10-01T14:00:00Z\n"
    )

    preview = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/import/preview",
        json={"csv_content": csv_content},
    )

    assert preview.status_code == 200
    assert preview.json()["valid_count"] == 0
    assert preview.json()["invalid_count"] == 1
    assert "negative" in preview.json()["rows"][0]["errors"][0].lower()


def test_portfolio_transaction_update_revalidates_ledger_and_owner(client: TestClient) -> None:
    register(client, "transaction-update@example.com")
    portfolio_id = create_portfolio(client, "Update ledger")["id"]
    deposit = add_transaction(
        client, portfolio_id, {"transaction_type": "deposit", "amount": "100.00"}
    ).json()
    updated = client.put(
        f"/api/v1/portfolios/{portfolio_id}/transactions/{deposit['id']}",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json={
            "transaction_type": "deposit",
            "amount": "200.00",
            "occurred_at": "2026-10-01T18:30:00+05:30",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["occurred_at"] == "2026-10-01T13:00:00Z"

    buy = add_transaction(
        client,
        portfolio_id,
        {"transaction_type": "buy", "symbol": "ABC", "quantity": "2", "unit_price": "20"},
    ).json()
    invalid_update = client.put(
        f"/api/v1/portfolios/{portfolio_id}/transactions/{deposit['id']}",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json={"transaction_type": "deposit", "amount": "10.00"},
    )
    assert invalid_update.status_code == 422
    assert len(client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json()) == 2
    assert client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()["cash_balance"] == "160.00"

    other_client = TestClient(app)
    register(other_client, "transaction-update-other@example.com")
    other_portfolio_id = create_portfolio(other_client, "Other ledger")["id"]
    response = other_client.put(
        f"/api/v1/portfolios/{other_portfolio_id}/transactions/{buy['id']}",
        headers={"X-CSRF-Token": other_client.cookies.get("csrf_token")},
        json={"transaction_type": "deposit", "amount": "10.00"},
    )
    assert response.status_code == 404