from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)

    def override_get_db():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


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


def create_trade(client: TestClient, **payload):
    return client.post(
        "/api/v1/trades",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json=payload,
    )


def test_trade_net_pnl_supports_long_short_and_open_positions(client: TestClient) -> None:
    register(client, "journal@example.com")
    common = {
        "asset_class": "equity",
        "quantity": "10",
        "multiplier": "1",
        "entry_time": "2026-10-01T14:00:00Z",
    }
    long_trade = create_trade(
        client,
        **common,
        instrument="acme",
        side="long",
        entry_price="10.00",
        exit_price="13.50",
        exit_time="2026-10-01T15:00:00Z",
        fees="1.00",
        strategy="breakout",
        tags=["patient", "patient"],
    )
    short_trade = create_trade(
        client,
        **{**common, "quantity": "2", "entry_time": "2026-10-02T14:00:00Z"},
        instrument="ACME",
        side="short",
        entry_price="20.00",
        exit_price="21.00",
        exit_time="2026-10-02T15:00:00Z",
        fees="0.50",
    )
    open_trade = create_trade(
        client,
        **{**common, "quantity": "1", "entry_time": "2026-10-03T14:00:00Z"},
        instrument="XYZ",
        side="long",
        entry_price="5.00",
    )

    assert long_trade.status_code == 201, long_trade.text
    assert long_trade.json()["net_pnl"] == "34.00"
    assert long_trade.json()["instrument"] == "ACME"
    assert long_trade.json()["tags"] == ["patient"]
    assert short_trade.status_code == 201, short_trade.text
    assert short_trade.json()["net_pnl"] == "-2.50"
    assert open_trade.status_code == 201, open_trade.text
    assert open_trade.json()["net_pnl"] is None

    analytics = client.get("/api/v1/trades/analytics/summary").json()
    assert analytics == {
        "currency": "USD",
        "closed_trades": 2,
        "open_trades": 1,
        "wins": 1,
        "losses": 1,
        "win_rate": "50.00",
        "net_pnl": "31.50",
        "expectancy": "15.75",
        "profit_factor": "13.60",
        "max_drawdown": "2.50",
    }


def test_option_trade_requires_contract_details_and_applies_multiplier(client: TestClient) -> None:
    register(client, "options@example.com")
    incomplete = create_trade(
        client,
        asset_class="option",
        instrument="ACME 2026-12-18 100C",
        side="long",
        quantity="2",
        multiplier="100",
        entry_price="1.25",
        exit_price="1.75",
        entry_time="2026-10-01T14:00:00Z",
        exit_time="2026-10-01T15:00:00Z",
    )
    complete = create_trade(
        client,
        asset_class="option",
        instrument="ACME 2026-12-18 100C",
        side="long",
        quantity="2",
        multiplier="100",
        entry_price="1.25",
        exit_price="1.75",
        entry_time="2026-10-01T14:00:00Z",
        exit_time="2026-10-01T15:00:00Z",
        fees="5.00",
        option_type="call",
        strike="100",
        expiry="2026-12-18T21:00:00Z",
    )

    assert incomplete.status_code == 422
    assert complete.status_code == 201, complete.text
    assert complete.json()["net_pnl"] == "95.00"


def test_journal_filters_are_user_scoped(client: TestClient) -> None:
    register(client, "journal-owner@example.com")
    trade = create_trade(
        client,
        asset_class="crypto",
        instrument="btc-usd",
        side="long",
        quantity="0.5",
        entry_price="60000",
        entry_time="2026-10-01T14:00:00Z",
    )
    assert trade.status_code == 201
    assert len(client.get("/api/v1/trades?asset_class=crypto&instrument=BTC-USD").json()) == 1
    assert client.get("/api/v1/trades?instrument=ETH-USD").json() == []
    other_client = TestClient(app)
    register(other_client, "other-journal-user@example.com")
    assert other_client.get("/api/v1/trades").json() == []

    inr_trade = create_trade(
        client,
        asset_class="crypto",
        instrument="BTC-INR",
        currency="INR",
        side="long",
        quantity="1",
        entry_price="5000000",
        exit_price="5100000",
        entry_time="2026-10-02T14:00:00Z",
        exit_time="2026-10-02T15:00:00Z",
    )
    assert inr_trade.status_code == 201
    usd_summary = client.get("/api/v1/trades/analytics/summary?currency=USD").json()
    inr_summary = client.get("/api/v1/trades/analytics/summary?currency=INR").json()
    assert usd_summary["net_pnl"] == "0.00"
    assert inr_summary["net_pnl"] == "100000.00"
    assert inr_summary["currency"] == "INR"


def test_journal_rejects_timezone_naive_timestamps(client: TestClient) -> None:
    register(client, "naive-time@example.com")
    response = create_trade(
        client,
        asset_class="equity",
        instrument="ACME",
        side="long",
        quantity="1",
        entry_price="10",
        entry_time="2026-10-01T14:00:00",
    )
    assert response.status_code == 422


def test_trade_update_recalculates_pnl_and_is_user_scoped(client: TestClient) -> None:
    register(client, "update-trade@example.com")
    created = create_trade(
        client,
        asset_class="equity",
        instrument="ACME",
        side="long",
        quantity="2",
        entry_price="10",
        exit_price="12",
        entry_time="2026-10-01T14:00:00Z",
        exit_time="2026-10-01T15:00:00Z",
        fees="1.00",
    )
    assert created.status_code == 201
    trade_id = created.json()["id"]

    updated = client.put(
        f"/api/v1/trades/{trade_id}",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
        json={
            "asset_class": "equity",
            "instrument": "ACME",
            "side": "short",
            "quantity": "3",
            "entry_price": "15",
            "exit_price": "12",
            "entry_time": "2026-10-01T14:00:00Z",
            "exit_time": "2026-10-01T15:00:00Z",
            "fees": "2.00",
            "strategy": "reversal",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["side"] == "short"
    assert updated.json()["net_pnl"] == "7.00"
    assert client.get("/api/v1/trades/analytics/summary").json()["net_pnl"] == "7.00"

    other_client = TestClient(app)
    register(other_client, "trade-update-other@example.com")
    unauthorized_update = other_client.put(
        f"/api/v1/trades/{trade_id}",
        headers={"X-CSRF-Token": other_client.cookies.get("csrf_token")},
        json={
            "asset_class": "equity",
            "instrument": "ACME",
            "side": "long",
            "quantity": "1",
            "entry_price": "10",
            "entry_time": "2026-10-01T14:00:00Z",
        },
    )
    assert unauthorized_update.status_code == 404


def test_trade_analytics_breakdown_groups_by_strategy(client: TestClient) -> None:
    register(client, "breakdown@example.com")
    common = {
        "asset_class": "equity",
        "instrument": "ACME",
        "side": "long",
        "quantity": "1",
        "entry_price": "10",
        "entry_time": "2026-10-01T14:00:00Z",
        "exit_time": "2026-10-01T15:00:00Z",
    }
    assert create_trade(
        client, **common, exit_price="15", fees="1", strategy="breakout"
    ).status_code == 201
    assert create_trade(
        client,
        **{**common, "entry_time": "2026-10-02T14:00:00Z", "exit_time": "2026-10-02T15:00:00Z"},
        exit_price="8",
        fees="1",
        strategy="breakout",
    ).status_code == 201
    assert create_trade(
        client,
        **{**common, "entry_time": "2026-10-03T14:00:00Z", "exit_time": "2026-10-03T15:00:00Z"},
        exit_price="12",
        strategy="",
    ).status_code == 201

    response = client.get("/api/v1/trades/analytics/breakdown?currency=USD&group_by=strategy")

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "key": "breakout",
            "closed_trades": 2,
            "wins": 1,
            "losses": 1,
            "net_pnl": "1.00",
            "expectancy": "0.50",
            "profit_factor": "1.33",
        },
        {
            "key": "Unspecified",
            "closed_trades": 1,
            "wins": 1,
            "losses": 0,
            "net_pnl": "2.00",
            "expectancy": "2.00",
            "profit_factor": None,
        },
    ]