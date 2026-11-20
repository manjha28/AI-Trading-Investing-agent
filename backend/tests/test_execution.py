"""Execution groundwork: mode, intent, guard-rails, simulated fills, audit, API.

The Delta store used by the API tests is a TEST fixture written to a temp file; it never flows into a
runtime path. Nothing here (or in the app) can transmit an order.
"""
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.delta_research.store import DeltaResearchStore
from app.execution.adapter import BacktestFillAdapter, Bar
from app.execution.guardrails import Limits, MarketInputs, evaluate
from app.execution.intent import OrderIntent, client_order_id
from app.execution.mode import ExecutionMode, current_mode
from app.main import app
from app.models import User
from app.models.execution import ExecutionAuditEvent

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
LIMITS = Limits(max_order_notional=Decimal("1000"), max_total_exposure=Decimal("5000"), max_price_age_seconds=120)


def intent(**changes) -> OrderIntent:
    base = dict(symbol="BTCUSD", side="buy", quantity="2", order_type="limit", limit_price="60000",
                strategy_id="scalp_hold", strategy_version="1", signal_id="sig-1")
    base.update(changes)
    return OrderIntent(**base)


def inputs(**changes) -> MarketInputs:
    base = dict(mark_price=Decimal("60000"), price_as_of=NOW - timedelta(seconds=10), price_is_synthetic=False,
                quote_currency="USD", contract_value=Decimal("0.001"), product_state="live",
                current_exposure_usd=Decimal("100"))
    base.update(changes)
    return MarketInputs(**base)


# ---- mode -----------------------------------------------------------------------------------

def test_mode_defaults_to_research_and_live_never_resolves(monkeypatch):
    assert current_mode().effective is ExecutionMode.RESEARCH
    monkeypatch.setattr(settings, "execution_mode", "monitor")
    assert current_mode().effective is ExecutionMode.MONITOR
    monkeypatch.setattr(settings, "execution_mode", "live")
    state = current_mode()
    assert state.effective is ExecutionMode.RESEARCH and state.live_blocked and "not available" in state.reason


# ---- intent ---------------------------------------------------------------------------------

def test_intent_rejects_floats_and_inconsistent_prices():
    with pytest.raises(ValidationError):
        intent(quantity=2.5)
    with pytest.raises(ValidationError):
        intent(order_type="limit", limit_price=None)
    with pytest.raises(ValidationError):
        intent(order_type="market")  # market order with a limit price
    with pytest.raises(ValidationError):
        intent(order_type="stop_market", limit_price=None, stop_price=None)
    with pytest.raises(ValidationError):
        intent(valid_until=datetime(2026, 12, 1))  # naive timestamp
    assert intent(order_type="market", limit_price=None).order_type == "market"


def test_client_order_id_is_deterministic_and_owner_scoped():
    owner, other = uuid4(), uuid4()
    first = client_order_id(intent(), owner)
    assert first == client_order_id(intent(), owner) and len(first) == 30
    assert first != client_order_id(intent(quantity="3"), owner)
    assert first != client_order_id(intent(), other)


# ---- guard-rails ----------------------------------------------------------------------------

def test_guardrails_approve_only_when_every_input_is_verified_and_within_caps():
    decision = evaluate(intent(), inputs(), LIMITS, NOW)
    assert decision.approved and decision.notional == Decimal("120.000") and decision.reasons == []


@pytest.mark.parametrize("changes,failed", [
    ({"mark_price": None}, "price_available"),
    ({"price_is_synthetic": None}, "price_not_synthetic"),
    ({"price_is_synthetic": True}, "price_not_synthetic"),
    ({"price_as_of": None}, "price_fresh"),
    ({"price_as_of": NOW - timedelta(minutes=10)}, "price_fresh"),
    ({"price_as_of": NOW + timedelta(minutes=1)}, "price_fresh"),
    ({"quote_currency": "INR"}, "currency_compatible"),
    ({"quote_currency": None}, "currency_compatible"),
    ({"product_state": "expired"}, "product_tradable"),
    ({"product_state": None}, "product_tradable"),
    ({"contract_value": None}, "notional_computable"),
    ({"current_exposure_usd": None}, "exposure_verifiable"),
])
def test_guardrails_fail_closed_on_missing_stale_synthetic_or_incompatible_inputs(changes, failed):
    decision = evaluate(intent(), inputs(**changes), LIMITS, NOW)
    assert not decision.approved
    assert failed in {check.name for check in decision.checks if not check.passed}


def test_guardrails_enforce_size_and_exposure_caps_and_reduce_only():
    assert not evaluate(intent(quantity="20"), inputs(), LIMITS, NOW).approved  # 1200 > 1000 cap
    near_cap = inputs(current_exposure_usd=Decimal("4950"))
    over = evaluate(intent(), near_cap, LIMITS, NOW)
    assert not over.approved and "total_exposure_within_cap" in {c.name for c in over.checks if not c.passed}
    assert evaluate(intent(reduce_only=True), near_cap, LIMITS, NOW).approved  # adds no exposure
    expired = evaluate(intent(valid_until=NOW - timedelta(seconds=1)), inputs(), LIMITS, NOW)
    assert "order_not_expired" in {c.name for c in expired.checks if not c.passed}


# ---- simulated fills (backtest adapter) -----------------------------------------------------

def bar(**changes) -> Bar:
    base = dict(timestamp=NOW, open=Decimal("100"), high=Decimal("105"), low=Decimal("95"), close=Decimal("101"), completed=True)
    base.update(changes)
    return Bar(**base)


def test_backtest_adapter_only_replays_completed_bars_and_labels_fills_hypothetical():
    adapter = BacktestFillAdapter(fee_rate=Decimal("0.001"), slippage_bps=Decimal("10"))
    cv = Decimal("1")
    assert adapter.fill(intent(order_type="market", limit_price=None), bar(completed=False), contract_value=cv).reason == "bar_not_completed"
    market = adapter.fill(intent(order_type="market", limit_price=None, quantity="2"), bar(), contract_value=cv)
    assert market.filled and market.price == Decimal("100.1") and market.hypothetical  # open + 10 bps
    assert market.fee == Decimal("2") * cv * Decimal("100.1") * Decimal("0.001")
    buy_limit = adapter.fill(intent(limit_price="97"), bar(), contract_value=cv)
    assert buy_limit.filled and buy_limit.price == Decimal("97")
    assert adapter.fill(intent(limit_price="90"), bar(), contract_value=cv).reason == "price_not_reached"
    sell_limit = adapter.fill(intent(side="sell", limit_price="103"), bar(), contract_value=cv)
    assert sell_limit.filled and sell_limit.price == Decimal("103")
    stop = adapter.fill(intent(order_type="stop_market", limit_price=None, stop_price="104"), bar(), contract_value=cv)
    assert stop.filled and stop.price >= Decimal("104")
    assert adapter.fill(intent(order_type="stop_limit", stop_price="104"), bar(), contract_value=cv).reason == "stop_limit_not_modelled"


# ---- API ------------------------------------------------------------------------------------

@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, engine, email: str) -> dict[str, str]:
    creds = {"email": email, "password": "correct-horse-battery"}
    assert client.post("/api/v1/auth/register", json=creds).status_code == 202
    assert client.post("/api/v1/auth/login", json=creds).status_code == 200
    with Session(engine) as db:
        db.scalar(select(User).where(User.email == email)).email_verified = True
        db.commit()
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


@pytest.fixture
def delta_store(tmp_path: Path, monkeypatch) -> Path:
    """TEST DATA ONLY: a throwaway Delta store with one perpetual and a fresh quote."""
    path = tmp_path / "delta_test.sqlite3"
    with DeltaResearchStore(path) as store:
        store.save_products([{
            "id": 27, "symbol": "BTCUSD", "contract_type": "perpetual_futures", "state": "live",
            "contract_value": "0.001", "quoting_asset": {"symbol": "USD"},
        }])
        store.save_quote({"product_id": 27, "close": "60000", "mark_price": "60000"}, datetime.now(timezone.utc), "delta_ws_test_fixture")
    monkeypatch.setattr(settings, "delta_research_enabled", True)
    monkeypatch.setattr(settings, "delta_research_db_path", str(path))
    return path


BODY = {"intent": {"symbol": "BTCUSD", "side": "buy", "quantity": "2", "order_type": "limit", "limit_price": "60000",
                   "strategy_id": "scalp_hold", "strategy_version": "1", "signal_id": "sig-1"}}


def test_status_states_that_no_order_can_be_transmitted(client, engine):
    login(client, engine, "exec@example.com")
    body = client.get("/api/v1/execution/status").json()
    assert body["mode"]["effective"] == "research" and body["capabilities"]["order_transmission"] is False
    assert body["capabilities"]["live_adapter"] is False and "not stored" in body["capabilities"]["execution_credentials"]
    assert body["limits"]["currency"] == "USD"


def test_dry_run_checks_real_stored_data_blocks_on_unknown_exposure_and_audits(client, engine, delta_store):
    headers = login(client, engine, "exec@example.com")
    response = client.post("/api/v1/execution/dry-run", headers=headers, json=BODY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_transmission"] == "not_available" and body["mode"] == "research"
    by_name = {check["name"]: check for check in body["decision"]["checks"]}
    assert by_name["price_available"]["passed"] and by_name["price_fresh"]["passed"] and by_name["product_tradable"]["passed"]
    assert by_name["currency_compatible"]["passed"] and by_name["order_notional_within_cap"]["passed"]
    assert not by_name["exposure_verifiable"]["passed"]  # unknown is never treated as zero
    assert body["decision"]["approved"] is False and body["decision"]["notional"] == "120.000"
    assert len(body["client_order_id"]) == 30
    # the same intent yields the same ID; both calls are audited as intent + decision pairs
    again = client.post("/api/v1/execution/dry-run", headers=headers, json=BODY).json()
    assert again["client_order_id"] == body["client_order_id"] and again["correlation_id"] != body["correlation_id"]
    trail = client.get("/api/v1/execution/audit").json()
    assert len(trail) == 4 and {row["event_type"] for row in trail} == {"intent", "decision"}
    assert all(row["hypothetical"] for row in trail)
    assert client.get("/api/v1/execution/status").json()["audit_events"] == 4


def test_dry_run_without_stored_market_data_fails_closed(client, engine, monkeypatch):
    monkeypatch.setattr(settings, "delta_research_enabled", False)
    headers = login(client, engine, "exec@example.com")
    body = client.post("/api/v1/execution/dry-run", headers=headers, json=BODY).json()
    assert body["decision"]["approved"] is False
    assert {c["name"] for c in body["decision"]["checks"] if not c["passed"]} >= {"price_available", "price_fresh", "currency_compatible"}


def test_dry_run_rejects_invalid_intents_and_requires_csrf(client, engine, delta_store):
    headers = login(client, engine, "exec@example.com")
    bad = {"intent": {**BODY["intent"], "quantity": 2.5}}
    assert client.post("/api/v1/execution/dry-run", headers=headers, json=bad).status_code == 422
    assert client.post("/api/v1/execution/dry-run", json=BODY).status_code == 403


def test_audit_is_owner_scoped_and_append_only(client, engine, delta_store):
    headers = login(client, engine, "owner@example.com")
    client.post("/api/v1/execution/dry-run", headers=headers, json=BODY)
    assert len(client.get("/api/v1/execution/audit").json()) == 2
    client.post("/api/v1/auth/logout", headers=headers)
    login(client, engine, "other@example.com")
    assert client.get("/api/v1/execution/audit").json() == []  # another owner sees nothing
    with Session(engine) as db:
        row = db.scalar(select(ExecutionAuditEvent))
        row.symbol = "TAMPERED"
        with pytest.raises(RuntimeError, match="append-only"):
            db.flush()
        db.rollback()
        row = db.scalar(select(ExecutionAuditEvent))
        db.delete(row)
        with pytest.raises(RuntimeError, match="append-only"):
            db.flush()


# ---- policy guard ---------------------------------------------------------------------------

def test_no_code_path_can_reach_a_broker_order_endpoint():
    """AGENTS.md: no place/modify/cancel path, no live adapter, no execution credentials."""
    import ast

    root = Path(__file__).resolve().parents[1] / "app"
    sources = [*(root / "execution").rglob("*.py"), root / "api" / "execution.py", root / "models" / "execution.py"]
    network_modules = {"httpx", "requests", "aiohttp", "websocket", "websockets", "urllib", "socket", "http"}
    for path in sources:
        text = path.read_text()
        for node in ast.walk(ast.parse(text)):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not [n for n in names if n.split(".")[0] in network_modules], f"{path.name} imports a network client"
        assert "/v2/orders" not in text and not any(
            isinstance(n, ast.FunctionDef) and any(word in n.name for word in ("place_order", "cancel_order", "modify_order"))
            for n in ast.walk(ast.parse(text))
        ), f"{path.name} defines an order path"

    paths = list(app.openapi()["paths"])
    assert not [path for path in paths if "order" in path.lower() and "/execution/" not in path]
    assert {path for path in paths if "/execution/" in path} == {
        "/api/v1/execution/status", "/api/v1/execution/dry-run", "/api/v1/execution/audit"}
