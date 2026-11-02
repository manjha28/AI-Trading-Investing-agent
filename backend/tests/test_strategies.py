from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Instrument, MarketBar, User
from app.models.strategies import StrategySignal
from app.services.strategy_engine import StrategyParams, evaluate_bars

FAST_SLOW = {"fast_period": 3, "slow_period": 5}
NOW = datetime.now(timezone.utc)


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


def bar(timestamp: datetime, close: str, *, synthetic: bool = False) -> SimpleNamespace:
    value = Decimal(close)
    return SimpleNamespace(
        timestamp=timestamp, open=value, high=value, low=value, close=value,
        volume=Decimal("1000"), is_synthetic=synthetic,
    )


def series(closes: list[str], last: datetime = NOW - timedelta(days=1)) -> list[SimpleNamespace]:
    start = last - timedelta(days=len(closes) - 1)
    return [bar(start + timedelta(days=i), close) for i, close in enumerate(closes)]


FLAT_THEN_JUMP = ["100"] * 30 + ["200"]
FLAT_THEN_DROP = ["100"] * 30 + ["50"]


def params(**overrides) -> StrategyParams:
    return StrategyParams(**{**FAST_SLOW, **overrides})


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


def add_instrument(test_engine, symbol: str = "STRAT") -> str:
    with Session(test_engine) as db:
        instrument = Instrument(
            symbol=symbol, display_name=f"{symbol} Test Corporation", asset_class="equity",
            venue="TEST", currency="USD", country="US", timezone="UTC", created_at=NOW,
        )
        db.add(instrument)
        db.commit()
        db.refresh(instrument)
        return str(instrument.id)


def add_daily_bars(
    test_engine, instrument_id: str, closes: list[str], last: datetime | None = None
) -> None:
    with Session(test_engine) as db:
        db.add_all(
            MarketBar(
                instrument_id=UUID(instrument_id), interval="1d",
                timestamp=bar_point.timestamp, open=bar_point.close, high=bar_point.close,
                low=bar_point.close, close=bar_point.close, volume=Decimal("1000"),
                source="test", retrieved_at=NOW, is_synthetic=False,
            )
            for bar_point in series(closes, last or NOW - timedelta(days=1))
        )
        db.commit()


def create_strategy(client: TestClient, instrument_id: str, name="Trend", **overrides) -> dict:
    response = client.post(
        "/api/v1/strategies", headers=csrf(client),
        json={"name": name, "instrument_id": instrument_id,
              "params": {**FAST_SLOW, **overrides}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_params_reject_inverted_periods_and_unknown_fields():
    with pytest.raises(ValidationError):
        StrategyParams(fast_period=20, slow_period=10)
    with pytest.raises(ValidationError):
        StrategyParams(fast_period=3, slow_period=5, stop_loss=1)
    with pytest.raises(ValidationError):
        StrategyParams(fast_period=3, slow_period=5, rsi_max=Decimal("101"))


def test_entry_crossover_is_detected_with_evidence_and_disclaimer():
    now = NOW
    result = evaluate_bars(series(FLAT_THEN_JUMP), params(), now)
    assert (result.state, result.direction, result.status) == ("signal", "entry", "active")
    assert result.trigger_price == Decimal("200")
    assert result.evidence["disclaimer"] == "Decision support, not investment advice."
    assert result.evidence["sma_fast"] == str(
        (Decimal("100") + Decimal("100") + Decimal("200")) / Decimal("3")
    )


def test_exit_crossover_is_detected():
    result = evaluate_bars(series(FLAT_THEN_DROP), params(), NOW)
    assert (result.state, result.direction, result.status) == ("signal", "exit", "active")


def test_no_crossover_and_insufficient_history_produce_no_signal():
    assert evaluate_bars(series(["100"] * 40), params(), NOW).state == "no_crossover"
    assert evaluate_bars(series(["100"] * 10), params(), NOW).state == "insufficient_history"


def test_rsi_filter_rejects_overbought_entry_with_reason():
    result = evaluate_bars(series(FLAT_THEN_JUMP), params(rsi_max=Decimal("50")), NOW)
    assert (result.direction, result.status, result.rejection_reason) == (
        "entry", "rejected", "rsi_above_max",
    )


def test_stale_bars_are_rejected_not_estimated():
    later = NOW + timedelta(days=30)
    result = evaluate_bars(series(FLAT_THEN_JUMP), params(), later)
    assert (result.status, result.rejection_reason) == ("rejected", "stale_bars")


def test_synthetic_bars_are_rejected():
    bars = series(FLAT_THEN_JUMP)
    bars[0] = bar(bars[0].timestamp, "100", synthetic=True)
    result = evaluate_bars(bars, params(), NOW)
    assert (result.status, result.rejection_reason) == ("rejected", "synthetic_data")


def test_owner_can_version_activate_evaluate_and_dedupe(client, test_engine):
    register(client, test_engine, "owner@example.com")
    instrument_id = add_instrument(test_engine)
    add_daily_bars(test_engine, instrument_id, FLAT_THEN_JUMP)
    strategy = create_strategy(client, instrument_id)
    assert strategy["status"] == "suspended" and strategy["current_version"] == 1

    blocked = client.post(f"/api/v1/strategies/{strategy['id']}/evaluate", headers=csrf(client))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "strategy_not_active"

    versioned = client.post(
        f"/api/v1/strategies/{strategy['id']}/versions", headers=csrf(client),
        json={"params": {**FAST_SLOW, "expiry_hours": 72}},
    )
    assert versioned.status_code == 201 and versioned.json()["current_version"] == 2
    assert len(client.get(f"/api/v1/strategies/{strategy['id']}/versions").json()) == 2

    activated = client.post(f"/api/v1/strategies/{strategy['id']}/activate", headers=csrf(client))
    assert activated.json()["status"] == "active"

    first = client.post(f"/api/v1/strategies/{strategy['id']}/evaluate", headers=csrf(client)).json()
    assert first["result"] == "signal"
    assert first["signal"]["strategy_version"] == 2
    assert first["signal"]["direction"] == "entry"

    second = client.post(f"/api/v1/strategies/{strategy['id']}/evaluate", headers=csrf(client)).json()
    assert second["result"] == "duplicate"
    assert second["signal"]["id"] == first["signal"]["id"]
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(StrategySignal)) == 1

    signals = client.get(f"/api/v1/strategies/{strategy['id']}/signals").json()
    assert [row["id"] for row in signals] == [first["signal"]["id"]]


def test_expired_signal_is_marked_and_retired_strategy_is_terminal(client, test_engine):
    register(client, test_engine, "expiry@example.com")
    instrument_id = add_instrument(test_engine)
    add_daily_bars(test_engine, instrument_id, FLAT_THEN_JUMP)
    strategy = create_strategy(client, instrument_id, expiry_hours=1)
    client.post(f"/api/v1/strategies/{strategy['id']}/activate", headers=csrf(client))
    client.post(f"/api/v1/strategies/{strategy['id']}/evaluate", headers=csrf(client))

    signals = client.get(f"/api/v1/strategies/{strategy['id']}/signals?status=expired").json()
    assert len(signals) == 1

    client.post(f"/api/v1/strategies/{strategy['id']}/retire", headers=csrf(client))
    reactivate = client.post(f"/api/v1/strategies/{strategy['id']}/activate", headers=csrf(client))
    assert reactivate.status_code == 409


def test_stale_crossover_is_recorded_as_rejected_signal(client, test_engine):
    register(client, test_engine, "stale@example.com")
    instrument_id = add_instrument(test_engine)
    add_daily_bars(test_engine, instrument_id, FLAT_THEN_JUMP, last=NOW - timedelta(days=11))
    strategy = create_strategy(client, instrument_id, max_bar_age_days=1)
    client.post(f"/api/v1/strategies/{strategy['id']}/activate", headers=csrf(client))
    result = client.post(
        f"/api/v1/strategies/{strategy['id']}/evaluate", headers=csrf(client)
    ).json()
    assert result["result"] == "signal"
    assert result["signal"]["status"] == "rejected"
    assert result["signal"]["rejection_reason"] == "stale_bars"


def test_mutations_require_csrf_and_strategies_are_owner_scoped(client, test_engine):
    register(client, test_engine, "first@example.com")
    instrument_id = add_instrument(test_engine)
    strategy = create_strategy(client, instrument_id)

    no_token = client.post(
        "/api/v1/strategies", json={"name": "x", "instrument_id": instrument_id,
                                    "params": FAST_SLOW},
    )
    assert no_token.status_code == 403

    client.cookies.clear()
    register(client, test_engine, "second@example.com")
    assert client.get("/api/v1/strategies").json() == []
    assert client.get(f"/api/v1/strategies/{strategy['id']}").status_code == 404
    assert client.post(
        f"/api/v1/strategies/{strategy['id']}/activate", headers=csrf(client)
    ).status_code == 404
    assert client.get(f"/api/v1/strategies/{strategy['id']}/signals").status_code == 404


def test_duplicate_strategy_name_is_rejected(client, test_engine):
    register(client, test_engine, "names@example.com")
    instrument_id = add_instrument(test_engine)
    create_strategy(client, instrument_id, name="Same")
    duplicate = client.post(
        "/api/v1/strategies", headers=csrf(client),
        json={"name": "Same", "instrument_id": instrument_id, "params": FAST_SLOW},
    )
    assert duplicate.status_code == 409
