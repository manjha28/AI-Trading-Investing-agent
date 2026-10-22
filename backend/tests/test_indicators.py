from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import inspect
import textwrap
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    CorporateAction,
    CorporateActionApplication,
    Instrument,
    MarketBar,
    Portfolio,
    PortfolioTransaction,
    User,
)
from app.services.indicators import atr, compute_indicators, rsi, sma


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("100")
    is_synthetic: bool = False


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


def authenticate_verified(
    client: TestClient, test_engine, email: str = "indicators@example.com"
) -> None:
    password = "correct-horse-battery"
    assert client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    ).status_code == 200
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.email_verified = True
        db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def make_bar(close: int, *, synthetic: bool = False) -> Bar:
    price = Decimal(close)
    return Bar(
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        is_synthetic=synthetic,
    )


def test_indicators_match_hand_computed_golden_values() -> None:
    closes = [Decimal("100")]
    closes.extend(Decimal(value) for value in [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 109, 108, 107, 106])
    rsi_bars = [
        Bar(open=close, high=close + 1, low=close - 1, close=close)
        for close in closes
    ]
    assert rsi(rsi_bars, 14).value.quantize(Decimal("0.000001")) == Decimal("71.428571")

    atr_bars = [
        Bar(
            open=Decimal("100"),
            high=Decimal("100") + Decimal(index),
            low=Decimal("100"),
            close=Decimal("100"),
        )
        for index in range(15)
    ]
    assert atr(atr_bars, 14).value == Decimal("7.5")

    sma_bars = [make_bar(value) for value in range(1, 21)]
    assert sma(sma_bars, 20).value == Decimal("10.5")


def test_insufficient_history_returns_none() -> None:
    snapshot = compute_indicators([make_bar(value) for value in range(1, 15)])
    for name in (
        "sma_20",
        "sma_50",
        "sma_200",
        "ema_20",
        "ema_50",
        "ema_200",
        "rsi_14",
        "atr_14",
        "realized_volatility_20d",
        "distance_to_52w_high",
        "distance_to_52w_low",
    ):
        assert getattr(snapshot, name).value is None


def test_synthetic_flag_propagates_from_bars_used() -> None:
    bars = [make_bar(value) for value in range(1, 21)]
    bars[-1] = make_bar(20, synthetic=True)

    result = sma(bars, 20)

    assert result.value == Decimal("10.5")
    assert result.is_synthetic is True


def test_instrument_indicators_endpoint_uses_stored_bars_only(
    client: TestClient, test_engine
) -> None:
    authenticate_verified(client, test_engine)
    observed_at = datetime.now(timezone.utc)
    with Session(test_engine) as db:
        instrument = Instrument(
            symbol="TEST",
            display_name="Indicator fixture",
            asset_class="equity",
            venue="NYSE",
            currency="USD",
            country="US",
            timezone="America/New_York",
            is_synthetic=False,
            relink_required=False,
            created_at=observed_at,
        )
        db.add(instrument)
        db.flush()
        instrument_id = instrument.id
        for index in range(20):
            close = Decimal(index + 100)
            db.add(
                MarketBar(
                    instrument_id=instrument.id,
                    interval="1d",
                    timestamp=observed_at - timedelta(days=19 - index),
                    open=close,
                    high=close + 1,
                    low=close - 1,
                    close=close,
                    volume=Decimal("100"),
                    source="test_fixture",
                    retrieved_at=observed_at,
                    is_synthetic=False,
                )
            )
        db.commit()

    response = client.get(f"/api/v1/instruments/{instrument_id}/indicators?interval=1d")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "test_fixture"
    assert Decimal(payload["indicators"]["sma_20"]["value"]) == Decimal("109.5")
    assert payload["indicators"]["sma_20"]["status"] == "available"
    assert payload["indicators"]["rsi_14"]["is_synthetic"] is False


def test_split_adjusted_lot_preserves_ltcg_boundary_date(
    client: TestClient, test_engine
) -> None:
    authenticate_verified(client, test_engine, "split-indicator@example.com")
    headers = csrf(client)
    now = datetime.now(timezone.utc)
    acquired_date = now.date() - timedelta(days=360)
    split_date = acquired_date + timedelta(days=30)

    portfolio_response = client.post(
        "/api/v1/portfolios",
        headers=headers,
        json={"name": "Boundary portfolio", "base_currency": "USD"},
    )
    assert portfolio_response.status_code == 201, portfolio_response.text
    portfolio = portfolio_response.json()
    deposit = client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=headers,
        json={
            "transaction_type": "deposit",
            "amount": "1000",
            "occurred_at": datetime.combine(
                acquired_date - timedelta(days=1), time.min, tzinfo=timezone.utc
            ).isoformat(),
        },
    )
    assert deposit.status_code == 201, deposit.text

    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == "split-indicator@example.com"))
        assert user is not None
        instrument = Instrument(
            symbol="SPLT",
            display_name="Split fixture",
            asset_class="equity",
            venue="NYSE",
            currency="USD",
            country="US",
            timezone="America/New_York",
            is_synthetic=False,
            relink_required=False,
            created_at=now,
        )
        db.add(instrument)
        db.flush()
        transaction = PortfolioTransaction(
            portfolio_id=UUID(portfolio["id"]),
            instrument_id=instrument.id,
            transaction_type="buy",
            symbol="SPLT",
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            amount=Decimal("1000"),
            fee=Decimal("0"),
            occurred_at=datetime.combine(acquired_date, time.min, tzinfo=timezone.utc),
            created_at=now,
        )
        action = CorporateAction(
            user_id=user.id,
            instrument_id=instrument.id,
            action_type="split",
            ex_date=split_date,
            ratio=Decimal("2"),
            amount=None,
            source="test_fixture",
            created_at=now,
        )
        db.add_all([transaction, action])
        db.flush()
        db.add(
            CorporateActionApplication(
                user_id=user.id,
                portfolio_id=UUID(portfolio["id"]),
                corporate_action_id=action.id,
                applied_at=now,
                reversed_at=None,
                affected_quantity=Decimal("10"),
                quantity_adjustment=Decimal("10"),
                cash_adjustment=Decimal("0"),
            )
        )
        db.commit()

    response = client.get(f"/api/v1/portfolios/{portfolio['id']}/risk-metrics")

    assert response.status_code == 200, response.text
    metrics = response.json()
    position = next(item for item in metrics["position_weights"] if item["symbol"] == "SPLT")
    assert position["quantity"] == "20.00000000"
    assert position["lots"][0]["quantity"] == "20.00000000"
    assert datetime.fromisoformat(position["lots"][0]["acquired_at"]).date() == acquired_date
    assert position["days_held"] == 360
    assert position["ltcg_boundary_flag"] is True
    assert metrics["ltcg_boundary_positions"][0]["symbol"] == "SPLT"


def test_alert_engine_uses_shared_indicators_without_local_helpers() -> None:
    from app.services import alert_engine, indicators

    assert alert_engine.calculate_rsi is indicators.rsi
    assert alert_engine.calculate_sma is indicators.sma
    syntax_tree = ast.parse(textwrap.dedent(inspect.getsource(alert_engine)))
    local_helpers = {
        node.name.lower()
        for node in ast.walk(syntax_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not any(
        "rsi" in name or "moving_average" in name or name in {"sma", "ma"}
        for name in local_helpers
    )
