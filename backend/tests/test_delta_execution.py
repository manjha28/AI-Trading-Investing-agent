"""Delta live execution: hard limits, guard-rails, signing and the arming rules. Candles and prices are TEST DATA; no request ever leaves this process
(the client is exercised only against a mocked transport, and refuses to run under pytest otherwise)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.config import settings
from app.crypto_delta.candles import Candle
from app.crypto_delta.forward import Spec
from app.delta_execution import client as dclient, guardrails as gr, policy, service
from app.delta_execution.intent import DeltaOrderIntent, client_order_id

D = Decimal
NOW = datetime(2026, 11, 23, 12, 0, 20, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
SPEC = Spec("BTCUSD", D("0.001"), D("0.0005"), D("0.5"), 27, D("0.4"))      # initial margin 0.4% -> exchange max 250x


def intent(**over):
    base = dict(symbol="BTCUSD", product_id=27, side="sell", contracts=1, leverage=25, strategy_key="five_ema_alert", signal_id="s1",
                signal_time=CLOSE - timedelta(minutes=15), reference_price=D("100000"), stop_price=D("100400"), target_price=D("98800"))
    return DeltaOrderIntent(**{**base, **over})


def ctx(**over):
    base = dict(now=NOW, deployment_enabled=True, credentials_present=True, environment_block=None, armed=True, test_executes_live=True, max_daily_loss_usd=D("25"),
                max_trades_per_day=6, max_open_positions=2, max_notional_usd=D("2000"), contract_value=D("0.001"), exchange_max_leverage=250, last_candle_close=CLOSE,
                last_price=D("100000"), orders_today=0, open_positions=0, realized_pnl_today=D(0), contract_has_open_order=False, duplicate_signal=False)
    return gr.LiveContext(**{**base, **over})


def failed(decision):
    return {c.name for c in decision.checks if not c.passed}


# ---- hard limits ----
def test_one_contract_and_25x_are_hard_limits_in_the_intent():
    with pytest.raises(ValidationError):
        intent(contracts=2)
    with pytest.raises(ValidationError):
        intent(leverage=26)
    with pytest.raises(ValidationError):
        intent(strategy_key="ema_cross")
    with pytest.raises(ValidationError):
        intent(stop_price=D("99000"))                              # a sell's stop must be above the price


def test_leverage_is_the_lower_of_25_and_the_exchange_maximum():
    assert policy.effective_leverage(D("0.4")) == 25               # exchange max 250x -> capped at 25x
    assert policy.effective_leverage(D("5")) == 20                 # exchange max 20x is lower than 25x
    assert policy.effective_leverage(D("3")) == 25                 # 33x -> 25x
    assert policy.effective_leverage(None) is None and policy.effective_leverage(D("0")) is None


# ---- guard-rails ----
def test_a_clean_context_is_allowed_and_each_layer_blocks_on_its_own():
    assert gr.evaluate(intent(), ctx()).allowed
    assert "deployment_flag" in failed(gr.evaluate(intent(), ctx(deployment_enabled=False)))
    assert "armed" in failed(gr.evaluate(intent(), ctx(armed=False)))
    assert "test_executes_live" in failed(gr.evaluate(intent(), ctx(test_executes_live=False)))
    assert "credentials" in failed(gr.evaluate(intent(), ctx(credentials_present=False)))
    assert "environment" in failed(gr.evaluate(intent(), ctx(environment_block="running under pytest")))


def test_missing_or_stale_inputs_fail_closed():
    assert "signal_fresh" in failed(gr.evaluate(intent(), ctx(last_candle_close=None)))
    assert "signal_fresh" in failed(gr.evaluate(intent(), ctx(now=NOW + timedelta(minutes=5))))
    assert "price_known" in failed(gr.evaluate(intent(), ctx(last_price=None)))
    assert "leverage_cap" in failed(gr.evaluate(intent(), ctx(exchange_max_leverage=None)))          # unknown margin data: refuse
    assert "leverage_cap" in failed(gr.evaluate(intent(leverage=25), ctx(exchange_max_leverage=10)))  # the exchange allows less than 25x
    assert "price_inside_levels" in failed(gr.evaluate(intent(), ctx(last_price=D("98000"))))


def test_caps_and_duplicates_block():
    assert "notional_cap" in failed(gr.evaluate(intent(), ctx(max_notional_usd=D("50"))))
    assert "risk_per_trade" in failed(gr.evaluate(intent(), ctx(max_daily_loss_usd=D("0.5"))))
    assert "daily_trade_cap" in failed(gr.evaluate(intent(), ctx(orders_today=6)))
    assert "open_position_cap" in failed(gr.evaluate(intent(), ctx(open_positions=2)))
    assert "one_order_per_contract" in failed(gr.evaluate(intent(), ctx(contract_has_open_order=True)))
    assert "not_a_duplicate" in failed(gr.evaluate(intent(), ctx(duplicate_signal=True)))


def test_client_order_id_is_deterministic_and_within_limits():
    from uuid import uuid4
    owner = uuid4()
    assert client_order_id(intent(), owner) == client_order_id(intent(), owner) and len(client_order_id(intent(), owner)) == 30
    assert client_order_id(intent(), owner) != client_order_id(intent(signal_id="s2"), owner)


# ---- the client ----
@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setattr(settings, "delta_live_execution_enabled", True)
    monkeypatch.setattr(settings, "delta_api_key", SecretStr("test-key"))
    monkeypatch.setattr(settings, "delta_api_secret", SecretStr("test-secret"))


def test_client_refuses_the_real_network_under_pytest_and_without_the_flag(creds, monkeypatch):
    with pytest.raises(dclient.ExecutionBlocked, match="pytest"):
        dclient.DeltaOrderClient().set_leverage(27, 25)                                  # no mocked transport: blocked before any request
    monkeypatch.setattr(settings, "delta_live_execution_enabled", False)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with pytest.raises(dclient.ExecutionBlocked, match="DELTA_LIVE_EXECUTION_ENABLED"):
        dclient.DeltaOrderClient(transport=transport).set_leverage(27, 25)
    monkeypatch.setattr(settings, "delta_live_execution_enabled", True)
    monkeypatch.setattr(settings, "delta_api_secret", None)
    with pytest.raises(dclient.ExecutionBlocked, match="credentials"):
        dclient.DeltaOrderClient(transport=transport).set_leverage(27, 25)


def test_requests_are_signed_and_never_carry_the_secret(creds):
    seen = []
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "result": {"id": 987}})
    c = dclient.DeltaOrderClient(transport=httpx.MockTransport(handler), clock=lambda: 1_700_000_000)
    reply = c.place_bracket(intent(), "dlabc", D("0.5"))
    req = seen[0]
    body = req.content.decode()
    expected = dclient.DeltaOrderClient.sign("test-secret", "POST", "1700000000", "/v2/orders", "", body)
    assert req.headers["signature"] == expected and req.headers["api-key"] == "test-key" and req.headers["timestamp"] == "1700000000"
    assert "test-secret" not in body and "test-secret" not in str(req.headers)
    sent = json.loads(body)
    assert sent["size"] == 1 and sent["side"] == "sell" and sent["order_type"] == "limit_order" and sent["client_order_id"] == "dlabc"
    assert D(sent["limit_price"]) == D("99700.0")                                        # 0.3% through the reference, on the 0.5 tick
    assert sent["bracket_stop_loss_price"] == "100400" and sent["bracket_take_profit_price"] == "98800"
    assert reply.ok and reply.order_id == "987"


def test_leverage_call_is_capped_at_25(creds):
    c = dclient.DeltaOrderClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True})))
    with pytest.raises(dclient.ExecutionBlocked):
        c.set_leverage(27, 50)
    assert c.set_leverage(27, 25).ok


# ---- the service on a real (in-memory) database is exercised through the API tests; here the pure flow ----
def test_paper_only_test_places_nothing():
    class FT:
        execute_live = False
    assert service.maybe_place(None, FT(), SPEC, [], [Candle(CLOSE, D(1), D(1), D(1), D(1), D(1))], NOW) is None
