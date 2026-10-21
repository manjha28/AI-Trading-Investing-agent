from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import brokers
from app.broker_adapters import BrokerAdapterError, UpstoxAdapter
from app.config import settings
from app.db import Base, get_db
from app.main import BrokerCallbackAccessLogFilter, app
from app.models import BrokerCredential, BrokerMirror, PortfolioTransaction, UserSession
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
