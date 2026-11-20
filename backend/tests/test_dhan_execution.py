"""Dhan live-execution safety. NOTHING here can reach a broker: the whole module blocks sockets, and every 'broker' is an httpx MockTransport.
All prices, bars and orders below are TEST DATA."""
import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.dhan_execution import policy, service as live
from app.dhan_execution.client import DhanOrderClient, ExecutionBlocked
from app.dhan_execution.guardrails import LiveContext, evaluate
from app.dhan_execution.intent import DhanOrderIntent, client_order_id
from app.models import Instrument, User
from app.models.execution import ExecutionAuditEvent
from app.models.forward_live import DhanLiveOrder, DhanLiveState, ForwardTest
from tests.test_signals_lab_api import API, add_inst, client, engine, h, login  # noqa: F401  (fixtures + helpers)

NOW = datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc)          # Monday 10:30 IST
D = Decimal


@pytest.fixture(autouse=True)
def no_dhan_network(monkeypatch):
    """Resolving or connecting to any Dhan host fails the test. (The app's own start-up makes unrelated outbound calls in every test that boots it; only Dhan matters here.)"""
    real_getaddrinfo = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if isinstance(host, (str, bytes)) and "dhan" in (host.decode() if isinstance(host, bytes) else host).lower():
            raise AssertionError(f"a test tried to reach a Dhan host: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def live_enabled(monkeypatch):
    """The deployment flag on and the pytest environment block lifted: only valid together with a MockTransport (never real HTTP)."""
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", True)
    monkeypatch.setattr(policy, "environment_block", lambda: None)


def intent(**over) -> DhanOrderIntent:
    base = dict(symbol="SBIN", security_id="3045", side="sell", quantity=1, strategy_key="five_ema_alert", signal_id="s1", signal_time=NOW - timedelta(minutes=1),
                reference_price=D("100"), stop_price=D("101"), target_price=D("98"))
    base.update(over)
    return DhanOrderIntent(**base)


def context(**over) -> LiveContext:
    base = dict(now=NOW, deployment_enabled=True, environment_block=None, armed=True, forward_test_execute_live=True, max_daily_loss_inr=D("500"), max_trades_per_day=10,
                max_open_positions=3, max_notional_inr=D("50000"), last_bar_close=NOW - timedelta(seconds=40), last_price=D("100"), orders_today=0, open_positions=0,
                realized_pnl_today=D("0"), instrument_has_open_order=False, duplicate_signal=False)
    base.update(over)
    return LiveContext(**base)


def mock_client(handler, token="SECRET-TOKEN") -> DhanOrderClient:
    return DhanOrderClient("https://api.dhan.co", token, "CLIENT1", transport=httpx.MockTransport(handler))


# ───────────────────────── hard limits ─────────────────────────
def test_the_hard_limits_are_exactly_what_the_owner_approved():
    assert policy.ALLOWED_STRATEGY_KEYS == {"five_ema_alert", "mfi_dip_30m"}
    assert policy.MAX_ORDER_QUANTITY == 1 and policy.PRODUCT_TYPE == "INTRADAY" and policy.EXCHANGE_SEGMENT == "NSE_EQ"
    assert policy.CONFIRMATION_PHRASE == "ENABLE LIVE ORDERS"
    from app.config import Settings
    assert Settings.model_fields["dhan_live_execution_enabled"].default is False          # off by default on a fresh deployment (whatever this machine's .env says)


def test_an_intent_cannot_exceed_the_cap_use_another_strategy_or_carry_nonsense_levels():
    with pytest.raises(ValueError):
        intent(quantity=2)
    with pytest.raises(ValueError):
        intent(quantity=0)
    for key in ("prev_day_break_30m", "psar_flip_trend_30m", "reg_five_ema_alert", ""):
        with pytest.raises(ValueError, match="allowlist"):
            intent(strategy_key=key)
    with pytest.raises(ValueError, match="never floats"):
        intent(reference_price=100.5)
    with pytest.raises(ValueError, match="sell needs target < price < stop"):
        intent(stop_price=D("99"), target_price=D("98"))
    with pytest.raises(ValueError, match="buy needs stop < price < target"):
        intent(side="buy")                                                                   # default levels are a sell's
    with pytest.raises(ValueError):
        intent(security_id="SBIN")                                                           # numeric Dhan security id only
    with pytest.raises(ValueError):
        DhanOrderIntent.model_validate({**intent().canonical(), "product": "CNC"})           # intraday only
    assert intent(side="buy", stop_price=D("99"), target_price=D("103")).side == "buy"


def test_client_order_ids_are_deterministic_short_and_owner_specific():
    owner, other = uuid4(), uuid4()
    a = client_order_id(intent(), owner)
    assert a == client_order_id(intent(), owner) and len(a) == 30 and a.startswith("dh")
    assert a != client_order_id(intent(), other) and a != client_order_id(intent(signal_id="s2"), owner)


# ───────────────────────── guard-rails fail closed ─────────────────────────
def test_the_baseline_context_is_allowed_and_every_check_is_recorded():
    decision = evaluate(intent(), context())
    assert decision.allowed and decision.reason is None and len(decision.checks) >= 15 and all(c.passed for c in decision.checks)


CASES = {
    "deployment_flag": dict(deployment_enabled=False), "environment": dict(environment_block="running under pytest"), "armed": dict(armed=False),
    "test_executes_live": dict(forward_test_execute_live=False),
    "market_window": dict(now=datetime(2026, 11, 23, 3, 30, tzinfo=timezone.utc)),                          # 09:00 IST: before the entry window
    "data_fresh": dict(last_bar_close=NOW - timedelta(minutes=10)), "price_known": dict(last_price=None),
    "notional_cap": dict(max_notional_inr=D("50")), "daily_loss_cap": dict(realized_pnl_today=D("-500")),
    "daily_trade_cap": dict(orders_today=10), "open_position_cap": dict(open_positions=3),
    "one_order_per_instrument": dict(instrument_has_open_order=True), "not_a_duplicate": dict(duplicate_signal=True),
    "price_inside_levels": dict(last_price=D("101.5")),                                                       # already through the stop
    "risk_per_trade": dict(max_daily_loss_inr=D("1")),
}


@pytest.mark.parametrize("name", list(CASES))
def test_each_guard_rail_blocks_on_its_own_and_names_itself(name):
    decision = evaluate(intent(), context(**CASES[name]))
    assert not decision.allowed
    failed = {c.name for c in decision.checks if not c.passed}
    assert name in failed and name in decision.reason


def test_weekends_late_entries_and_missing_data_are_blocked():
    sat = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 23, 9, 30, tzinfo=timezone.utc)                                               # 15:00 IST
    assert not evaluate(intent(), context(now=sat, last_bar_close=sat - timedelta(seconds=30))).allowed
    assert not evaluate(intent(), context(now=late, last_bar_close=late - timedelta(seconds=30))).allowed
    assert not evaluate(intent(), context(last_bar_close=None)).allowed
    assert not evaluate(intent(), context(last_bar_close=NOW + timedelta(minutes=5))).allowed             # a bar from the future is not trusted


# ───────────────────────── the client ─────────────────────────
def test_the_client_refuses_to_leave_the_process_under_pytest_even_if_the_deployment_flag_is_on(monkeypatch):
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", True)
    real = DhanOrderClient("https://api.dhan.co", "TOKEN", "C")                                              # no mock transport: this would be real HTTP
    with pytest.raises(ExecutionBlocked, match="pytest"):
        real.place_super_order(intent(), "dh" + "0" * 28)
    with pytest.raises(ExecutionBlocked):
        real.get_order("1")
    with pytest.raises(ExecutionBlocked):
        real.cancel_order("1")


def test_the_client_refuses_when_the_deployment_flag_is_off(monkeypatch):
    monkeypatch.setattr(policy, "environment_block", lambda: None)
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", False)
    with pytest.raises(ExecutionBlocked, match="DHAN_LIVE_EXECUTION_ENABLED"):
        mock_client(lambda request: httpx.Response(200, json={})).place_super_order(intent(), "dh" + "0" * 28)


def test_the_super_order_payload_is_one_intraday_nse_equity_share_with_stop_and_target_and_the_token_only_in_a_header(live_enabled):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"orderId": "1126", "orderStatus": "PENDING"})

    reply = mock_client(handler).place_super_order(intent(), "dh" + "a" * 28)
    request = seen[0]
    body = json.loads(request.content)
    assert request.method == "POST" and request.url.path == "/v2/super/orders"
    assert body["transactionType"] == "SELL" and body["exchangeSegment"] == "NSE_EQ" and body["productType"] == "INTRADAY" and body["quantity"] == 1
    assert body["securityId"] == "3045" and body["orderType"] == "LIMIT" and body["correlationId"] == "dh" + "a" * 28 and body["dhanClientId"] == "CLIENT1"
    assert body["stopLossPrice"] == 101.0 and body["targetPrice"] == 98.0
    assert body["price"] == 99.7                                                                             # a marketable limit 0.3% THROUGH the price for a sell
    assert request.headers["access-token"] == "SECRET-TOKEN" and "SECRET-TOKEN" not in request.content.decode()
    assert reply.ok and reply.order_id == "1126"
    buy = mock_client(handler).build_super_order(intent(side="buy", stop_price=D("99"), target_price=D("103")), "x", "C")
    assert buy["transactionType"] == "BUY" and buy["price"] == 100.3


def test_a_rejection_is_returned_verbatim_not_swallowed(live_enabled):
    def handler(request):
        return httpx.Response(401, json={"errorType": "Invalid_IP", "errorCode": "DH-905", "errorMessage": "IP not whitelisted for order APIs"})

    reply = mock_client(handler).place_super_order(intent(), "dh" + "b" * 28)
    assert not reply.ok and reply.status_code == 401 and reply.body["errorCode"] == "DH-905" and "whitelisted" in (reply.error or "")


def test_network_failures_become_a_reply_never_an_exception_that_could_leave_a_half_placed_order_unrecorded(live_enabled):
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    reply = mock_client(handler).place_super_order(intent(), "dh" + "c" * 28)
    assert not reply.ok and reply.status_code is None and "ConnectTimeout" in (reply.error or "")


# ───────────────────────── service: place / block / idempotency / audit ─────────────────────────
@pytest.fixture
def world(client, engine):
    login(client, engine, "live@example.com")
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "live@example.com"))
        inst_id = UUID(add_inst(engine, "SBIN"))
        ft = ForwardTest(user_id=user.id, name="t", strategy_key="five_ema_alert", definition={}, definition_hash="x", instrument_ids=[str(inst_id)], quantity=1,
                         start_date=NOW.date(), status="active", execute_live=True, slippage_bps=D("2"), created_at=NOW, updated_at=NOW)
        db.add(ft)
        state = live.get_state(db, user.id)
        state.armed = True
        db.commit()
        return {"user_id": user.id, "inst_id": inst_id, "ft_id": ft.id}


def attempt(engine, world, handler, *, it=None, execute_live=True, armed=True, now=NOW):
    with Session(engine) as db:
        ft = db.get(ForwardTest, world["ft_id"])
        ft.execute_live = execute_live
        live.get_state(db, world["user_id"]).armed = armed
        db.commit()
        inst = db.get(Instrument, world["inst_id"])
        row = live.attempt_order(db, world["user_id"], ft, inst, it or intent(), now=now, last_bar_close=now - timedelta(seconds=40), last_price=D("100"),
                                 client_factory=lambda uid: mock_client(handler))
        if row is not None:
            db.refresh(row)
            db.expunge(row)
        return row


def test_an_allowed_intent_is_placed_once_recorded_with_the_brokers_reply_and_fully_audited(client, engine, world, live_enabled):
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"orderId": "777", "orderStatus": "PENDING"})

    row = attempt(engine, world, handler)
    assert row.status == "placed" and row.dhan_order_id == "777" and row.quantity == 1 and row.side == "sell" and row.block_reason is None
    assert row.response["body"]["orderId"] == "777" and row.request["product"] == "INTRADAY" and "SECRET" not in json.dumps(row.request)
    again = attempt(engine, world, handler)
    assert again.id == row.id and calls == [1]                                              # a retried signal can never place a second order
    with Session(engine) as db:
        kinds = [e.event_type for e in db.scalars(select(ExecutionAuditEvent).where(ExecutionAuditEvent.user_id == world["user_id"]).order_by(ExecutionAuditEvent.created_at))]
        assert {"intent", "decision", "broker_reply"} <= set(kinds)
        events = list(db.scalars(select(ExecutionAuditEvent).where(ExecutionAuditEvent.client_order_id == row.client_order_id)))
        assert all(e.hypothetical is False and e.mode == "live" and e.venue == "dhan_nse_equity" for e in events)
        assert "SECRET-TOKEN" not in json.dumps([e.payload for e in events])
        with pytest.raises(Exception):
            events[0].payload = {"rewritten": True}
            db.commit()                                                                      # the audit trail is append-only


def test_a_blocked_intent_never_reaches_the_broker_and_says_why(client, engine, world, live_enabled):
    def handler(request):
        raise AssertionError("the broker must not be called")

    row = attempt(engine, world, handler, armed=False)
    assert row.status == "blocked" and "armed" in row.block_reason and row.dhan_order_id is None
    other = attempt(engine, world, handler, it=intent(signal_id="s2"), now=datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc))
    assert other.status == "blocked" and "market_window" in other.block_reason


def test_a_paper_only_test_records_nothing_in_the_live_ledger_even_when_blocked(client, engine, world, live_enabled):
    assert attempt(engine, world, lambda r: httpx.Response(200, json={}), execute_live=False) is None
    with Session(engine) as db:
        assert db.scalars(select(DhanLiveOrder)).all() == []
        assert db.scalars(select(ExecutionAuditEvent)).all()                                  # but the decision is still audited


def test_a_broker_rejection_is_stored_verbatim_and_the_order_is_not_treated_as_live(client, engine, world, live_enabled):
    row = attempt(engine, world, lambda r: httpx.Response(401, json={"errorCode": "DH-905", "errorMessage": "IP not whitelisted"}))
    assert row.status == "rejected" and "whitelisted" in row.block_reason and row.response["body"]["errorCode"] == "DH-905" and row.dhan_order_id is None


def test_the_daily_loss_cap_and_trade_cap_stop_further_orders(client, engine, world, live_enabled):
    with Session(engine) as db:
        db.add(DhanLiveOrder(user_id=world["user_id"], forward_test_id=world["ft_id"], strategy_key="five_ema_alert", instrument_id=uuid4_instrument(db, world), symbol="X", security_id="1",
                             side="sell", quantity=1, signal_time=NOW, reference_price=D("10"), client_order_id="dh" + "9" * 28, status="closed", realized_pnl=D("-500"), created_at=NOW, updated_at=NOW))
        db.commit()
    row = attempt(engine, world, lambda r: httpx.Response(200, json={"orderId": "1"}))
    assert row.status == "blocked" and "daily_loss_cap" in row.block_reason


def uuid4_instrument(db, world):
    return world["inst_id"]


# ───────────────────────── kill switch / reconcile ─────────────────────────
def seed_order(engine, world, status="placed", cid="dh" + "1" * 28, order_id="55"):
    with Session(engine) as db:
        db.add(DhanLiveOrder(user_id=world["user_id"], forward_test_id=world["ft_id"], strategy_key="five_ema_alert", instrument_id=world["inst_id"], symbol="SBIN", security_id="3045",
                             side="sell", quantity=1, signal_time=NOW, reference_price=D("100"), stop_price=D("101"), target_price=D("98"), client_order_id=cid, dhan_order_id=order_id,
                             status=status, created_at=NOW, updated_at=NOW))
        db.commit()


def test_the_kill_switch_disarms_first_cancels_unfilled_orders_and_reports_filled_positions(client, engine, world, live_enabled):
    seed_order(engine, world, "placed", "dh" + "1" * 28, "55")
    seed_order(engine, world, "filled", "dh" + "2" * 28, "56")
    cancelled: list[str] = []

    def handler(request):
        cancelled.append(request.url.path)
        return httpx.Response(200, json={"orderId": "55", "orderStatus": "CANCELLED"})

    with Session(engine) as db:
        result = live.kill_switch(db, world["user_id"], lambda uid: mock_client(handler), now=NOW)
        assert result["armed"] is False and result["cancelled"] == 1 and result["filled_positions_still_open"] == ["SBIN"] and "Dhan app" in result["note"]
        assert live.get_state(db, world["user_id"]).armed is False
        statuses = {r.dhan_order_id: r.status for r in db.scalars(select(DhanLiveOrder))}
        assert statuses == {"55": "cancelled", "56": "filled"} and cancelled == ["/v2/super/orders/55/ENTRY_LEG"]
        assert not evaluate(intent(), context(armed=live.get_state(db, world["user_id"]).armed)).allowed          # nothing new can be placed afterwards


def test_the_kill_switch_still_disarms_when_the_broker_cancel_fails(client, engine, world, live_enabled):
    seed_order(engine, world)
    with Session(engine) as db:
        result = live.kill_switch(db, world["user_id"], lambda uid: mock_client(lambda r: httpx.Response(500, json={"errorMessage": "down"})), now=NOW)
        assert result["armed"] is False and result["cancelled"] == 0 and result["cancel_failed"][0]["symbol"] == "SBIN"


def test_reconcile_maps_fills_target_and_stop_exits_rejections_and_leaves_unknown_payloads_alone(client, engine, world, live_enabled):
    seed_order(engine, world, "placed", "dh" + "1" * 28, "61")
    seed_order(engine, world, "placed", "dh" + "2" * 28, "62")
    seed_order(engine, world, "placed", "dh" + "3" * 28, "63")
    replies = {
        "61": {"orderStatus": "TRADED", "averageTradedPrice": 100.0, "legDetails": [{"legName": "TARGET_LEG", "orderStatus": "TRADED", "averageTradedPrice": 98.0}]},
        "62": {"orderStatus": "REJECTED"},
        "63": {"something": "unexpected"},
    }
    with Session(engine) as db:
        counts = live.reconcile(db, world["user_id"], lambda uid: mock_client(lambda r: httpx.Response(200, json=replies[r.url.path.rsplit("/", 1)[1]])), now=NOW)
        rows = {r.dhan_order_id: r for r in db.scalars(select(DhanLiveOrder))}
        assert counts["checked"] == 3
        assert rows["61"].status == "closed" and rows["61"].exit_reason == "target" and rows["61"].entry_price == D("100") and rows["61"].exit_price == D("98")
        assert D("0") < rows["61"].realized_pnl < D("2.00")                                                  # a SELL at 100 covered at 98 = +2.00 gross, less charges
        assert rows["62"].status == "rejected" and rows["63"].status == "placed" and rows["63"].response["last_status"] == {"something": "unexpected"}


def test_estimated_pnl_deducts_charges_and_signs_by_side():
    win_short = live.estimate_net_pnl("sell", 1, D("100"), D("98"))
    win_long = live.estimate_net_pnl("buy", 1, D("100"), D("102"))
    loss_short = live.estimate_net_pnl("sell", 1, D("100"), D("101"))
    assert D("0") < win_short < D("2") and D("0") < win_long < D("2") and loss_short < D("-1")
