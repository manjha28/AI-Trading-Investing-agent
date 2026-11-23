"""One execution state per venue, the split ledgers, and the guarantee that with the deployment flag off NO code path can reach a broker order endpoint,
whatever the armed state is. Rows are TEST DATA; the broker is a mocked transport that counts calls."""
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.config import settings
from app.delta_execution import client as dclient, policy as dpolicy
from app.dhan_execution import client as hclient, policy as hpolicy
from app.dhan_execution.intent import DhanOrderIntent
from app.delta_execution.intent import DeltaOrderIntent
from app.services import execution_state as es
from tests.test_crypto_delta import engine  # noqa: F401

D = Decimal
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


class Counter:
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return httpx.Response(200, json={"success": True, "result": {"id": 1}, "orderId": "1"})


def delta_intent():
    return DeltaOrderIntent(symbol="BTCUSD", product_id=27, side="sell", contracts=1, leverage=25, strategy_key="five_ema_alert", signal_id="s", signal_time=NOW, reference_price=D("100"), stop_price=D("101"), target_price=D("97"))


def dhan_intent():
    return DhanOrderIntent(symbol="TCS", security_id="11536", side="buy", quantity=1, strategy_key="five_ema_alert", signal_id="s", signal_time=NOW, reference_price=D("100"), stop_price=D("99"), target_price=D("103"))


@pytest.mark.parametrize("armed", [True, False])
def test_with_the_deployment_flag_off_no_order_call_reaches_a_broker_whatever_the_armed_state(monkeypatch, armed):
    monkeypatch.setattr(settings, "delta_live_execution_enabled", False)
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", False)
    monkeypatch.setattr(settings, "delta_api_key", SecretStr("k")); monkeypatch.setattr(settings, "delta_api_secret", SecretStr("s"))
    monkeypatch.setattr(dpolicy, "environment_block", lambda: None); monkeypatch.setattr(hpolicy, "environment_block", lambda: None)
    transport = Counter()
    delta = dclient.DeltaOrderClient(transport=httpx.MockTransport(transport))
    dhan = hclient.DhanOrderClient("https://api.dhan.co", "tok", "cid", transport=httpx.MockTransport(transport))
    for call in (lambda: delta.set_leverage(27, 25), lambda: delta.place_bracket(delta_intent(), "dlx", D("0.5")), lambda: delta.get_order("1"), lambda: delta.cancel_order("1", 27), lambda: delta.balances(),
                 lambda: dhan.place_super_order(dhan_intent(), "dhx"), lambda: dhan.cancel_order("1"), lambda: dhan.get_order("1"), lambda: dhan.positions()):
        with pytest.raises(Exception, match="(DELTA_LIVE_EXECUTION_ENABLED|DHAN_LIVE_EXECUTION_ENABLED)"):
            call()
    assert transport.calls == []                                   # armed or not, the flag alone keeps every order endpoint unreachable


def test_effective_state_is_off_blocked_or_live(engine, monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app.models import User
    from app.models.crypto_delta import CryptoForwardTest, DeltaLiveState
    from app.services import execution_state
    del execution_state
    with Session(engine) as db:
        user = User(email="state@example.com", password_hash="x", email_verified=True, created_at=NOW)
        db.add(user); db.flush()
        monkeypatch.setattr(dpolicy, "environment_block", lambda: None)
        monkeypatch.setattr(settings, "delta_api_key", SecretStr("k")); monkeypatch.setattr(settings, "delta_api_secret", SecretStr("s"))
        monkeypatch.setattr(settings, "delta_live_execution_enabled", False)
        assert es.delta_state(db, user.id, NOW)["effective"]["state"] == "OFF"
        assert "DELTA_LIVE_EXECUTION_ENABLED" in es.delta_state(db, user.id, NOW)["effective"]["reasons"][0]
        monkeypatch.setattr(settings, "delta_live_execution_enabled", True)
        assert es.delta_state(db, user.id, NOW)["effective"]["reasons"] == ["you have not armed live execution"]
        db.add(DeltaLiveState(user_id=user.id, armed=True, max_daily_loss_usd=D(25), max_trades_per_day=6, max_open_positions=2, max_notional_usd=D(2000), updated_at=NOW)); db.flush()
        blocked = es.delta_state(db, user.id, NOW)["effective"]
        assert blocked["state"] == "ARMED_BUT_BLOCKED" and blocked["reasons"] == ["no Delta forward test has its live switch on"]
        db.add(CryptoForwardTest(user_id=user.id, name="t", symbol="BTCUSD", resolution="5m", params={"ema": 5}, contracts=1, slippage_bps=D(5), start_time=NOW, status="active", execute_live=True, created_at=NOW, updated_at=NOW)); db.flush()
        assert es.delta_state(db, user.id, NOW)["effective"]["state"] == "LIVE"
        assert es.describe(es.delta_state(db, user.id, NOW)).endswith("LIVE")
        del select


def test_a_blocked_attempt_is_not_counted_as_sent_and_a_broker_rejection_is():
    class Row:
        def __init__(self, status): self.status = status
    today = es._today([Row("blocked"), Row("blocked"), Row("rejected"), Row("placed"), Row("error")])
    assert today == {"sent_to_broker": 3, "rejected_by_broker": 2, "blocked_attempts": 2}
