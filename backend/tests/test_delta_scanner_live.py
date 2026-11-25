"""Per-scanner Delta Live switch (owner decision 2026-11-25). Candles, prices and broker replies are TEST DATA; the broker is a mocked transport only,
so no request leaves this process."""
from datetime import timedelta
from decimal import Decimal as D

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.crypto_delta.forward import Spec
from app.delta_execution import client as dclient, guardrails as gr, policy, service as live
from app.models import User
from app.models.crypto_delta import DeltaLiveOrder
from app.models.delta_scanner import DeltaScanner
from tests.test_crypto_delta import T0, _entry_and_candles, client, engine, login  # noqa: F401
from tests.test_delta_execution import ctx, failed, intent

SPEC = Spec("BTCUSD", D("0.001"), D("0.0005"), D("0.5"), 27, D("0.4"), D("0.2"))
OTHER = Spec("ETHUSD", D("0.01"), D("0.0005"), D("0.05"), 28, D("0.4"), D("0.2"))


class Broker:
    """Mocked Delta: answers the gate's signed reads, records every write (method, path, body)."""
    def __init__(self, positions=None):
        self.writes, self.positions = [], positions or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            reads = {"/v2/wallet/balances": [{"asset_symbol": "USD", "available_balance": "100", "balance": "100"}], "/v2/positions/margined": self.positions,
                     "/v2/orders": [], "/v2/wallet/transactions": []}
            return httpx.Response(200, json={"success": True, "result": reads[request.url.path]})
        self.writes.append((request.method, request.url.path, request.content.decode() or None))
        return httpx.Response(200, json={"success": True, "result": {"id": 777}})

    def factory(self):
        return lambda: dclient.DeltaOrderClient(transport=httpx.MockTransport(self.handler))


def _setup(engine, monkeypatch, *, strategy="five_ema_alert", execute_live=False, armed=True, email="sl@example.com"):
    monkeypatch.setattr(settings, "delta_live_execution_enabled", True)
    monkeypatch.setattr(settings, "delta_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "delta_api_secret", SecretStr("s"))
    monkeypatch.setattr(policy, "environment_block", lambda: None)
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, password_hash="x", email_verified=True, created_at=T0)
            db.add(user); db.flush()
        sc = DeltaScanner(user_id=user.id, name="s", strategy_key=strategy, resolution="15m", symbols=["BTCUSD"], params={"ema": 5, "rr": "3", "direction": -1},
                          wallet_usd=D(1000), risk_pct=D("0.005"), total_risk_cap_pct=D("0.02"), leverage_cap=D(5), label="paper", start_time=T0, status="active",
                          created_at=T0, updated_at=T0, **({"execute_live": True} if execute_live else {}))
        db.add(sc)
        live.get_state(db, user.id).armed = armed
        db.commit()
        return user.id, sc.id


def _place(engine, sid, broker):
    from datetime import datetime, timezone
    entries, candles, at = _entry_and_candles(datetime.now(timezone.utc))
    with Session(engine) as db:
        sc = db.get(DeltaScanner, sid)
        row = live.maybe_place_scanner(db, sc, SPEC, entries, candles, at, broker.factory())
        return (row.status, row.scanner_id, row.block_reason) if row else None


def test_the_switch_is_off_by_default_and_nothing_is_sent_while_off(engine, monkeypatch):
    _uid, sid = _setup(engine, monkeypatch)
    with Session(engine) as db:
        assert db.get(DeltaScanner, sid).execute_live is False
    broker = Broker()
    assert _place(engine, sid, broker) is None and broker.writes == []


def test_a_non_allowlisted_strategy_stays_inert_even_with_the_switch_forced_on(engine, monkeypatch):
    _uid, sid = _setup(engine, monkeypatch, strategy="funding_extreme", execute_live=True, email="sl2@example.com")
    broker = Broker()
    assert _place(engine, sid, broker) is None and broker.writes == []
    with Session(engine) as db:
        assert "not on the live allowlist" in live.scanner_live_block(db.get(DeltaScanner, sid))


def test_an_allowlisted_live_scanner_sends_one_bracket_through_the_same_gate(engine, monkeypatch):
    _uid, sid = _setup(engine, monkeypatch, execute_live=True, email="sl3@example.com")
    broker = Broker()
    status, scanner_id, _ = _place(engine, sid, broker)
    assert status == "placed" and scanner_id == sid
    assert [w[1] for w in broker.writes] == ["/v2/products/27/orders/leverage", "/v2/orders"]
    assert '"bracket_stop_loss_price"' in broker.writes[1][2] and '"size":1' in broker.writes[1][2]


def test_not_armed_blocks_the_scanner_too(engine, monkeypatch):
    _uid, sid = _setup(engine, monkeypatch, execute_live=True, armed=False, email="sl4@example.com")
    broker = Broker()
    status, _sid, reason = _place(engine, sid, broker)
    assert status == "blocked" and reason.startswith("armed:") and broker.writes == []


def test_another_position_not_opened_by_this_app_fails_the_portfolio_check_closed(engine, monkeypatch):
    _uid, sid = _setup(engine, monkeypatch, execute_live=True, email="sl5@example.com")
    broker = Broker(positions=[{"product_id": 99, "size": 1, "entry_price": "10"}])
    status, _sid, reason = _place(engine, sid, broker)
    assert status == "blocked" and "total_risk_cap" in reason and broker.writes == []


def test_total_risk_cap_one_per_coin_and_cross_margin_recheck():
    other = gr.OpenPosition("ETHUSD", D("0.01"), D("3000"), D("3050"), D("0.25"))         # 0.50 USD at risk
    # new BTC order risks 400 x 0.001 = 0.40 USD; with a 1000 USD wallet the 2% cap (20 USD) holds
    assert "total_risk_cap" not in failed(gr.evaluate(intent(), ctx(open_positions=1, other_positions=(other,))))
    # a 40 USD wallet: cap 0.80 USD < 0.90 USD in total -> refused
    assert "total_risk_cap" in failed(gr.evaluate(intent(), ctx(open_positions=1, other_positions=(other,), wallet_balance_usd=D(40))))
    # unknown risk of an open position: refused
    assert "total_risk_cap" in failed(gr.evaluate(intent(), ctx(open_positions=1, other_positions=None)))
    # one position per coin
    assert "one_order_per_contract" in failed(gr.evaluate(intent(), ctx(contract_has_open_order=True)))
    # cross margin: a far stop on the open position no longer sits within half of its liquidation distance once the wallet is small
    wide = gr.OpenPosition("ETHUSD", D("0.01"), D("3000"), D("3900"), D("0.25"))           # 900 away; room with a 10 USD wallet is about 1000 -> half is 500
    assert "liquidation_safety_all_positions" in failed(gr.evaluate(intent(), ctx(open_positions=1, other_positions=(wide,), wallet_balance_usd=D(10))))
    # a single position is not affected by the portfolio checks
    assert not {"total_risk_cap", "liquidation_safety_all_positions"} & failed(gr.evaluate(intent(), ctx(wallet_balance_usd=D(40))))


def test_scanner_kill_switch_turns_live_off_and_cancels_only_its_unfilled_orders(engine, monkeypatch):
    _uid, sid = _setup(engine, monkeypatch, execute_live=True, email="sl6@example.com")
    broker = Broker()
    assert _place(engine, sid, broker)[0] == "placed"
    broker.writes.clear()
    with Session(engine) as db:
        out = live.scanner_kill(db, db.get(DeltaScanner, sid), broker.factory())
        assert out["execute_live"] is False and out["cancelled"] == 1
        assert db.get(DeltaScanner, sid).execute_live is False
        assert db.scalar(select(DeltaLiveOrder).where(DeltaLiveOrder.scanner_id == sid)).status == "cancelled"
    assert [w[:2] for w in broker.writes] == [("DELETE", "/v2/orders")]


def test_close_all_sends_reduce_only_market_orders_for_this_scanners_contracts_only(engine, monkeypatch):
    _uid, sid = _setup(engine, monkeypatch, execute_live=True, email="sl7@example.com")
    broker = Broker()
    assert _place(engine, sid, broker)[0] == "placed"
    broker.writes.clear()
    broker.positions = [{"product_id": 27, "size": -1, "entry_price": "100000"}, {"product_id": 99, "size": 1, "entry_price": "5"}]   # 99 is not this scanner's
    with Session(engine) as db:
        out = live.scanner_close_all(db, db.get(DeltaScanner, sid), broker.factory())
    assert [c["symbol"] for c in out["closed"]] == ["BTCUSD"] and len(broker.writes) == 1
    method, path, body = broker.writes[0]
    assert (method, path) == ("POST", "/v2/orders") and '"reduce_only":true' in body and '"side":"buy"' in body and '"product_id":27' in body
    try:
        dclient.DeltaOrderClient(transport=httpx.MockTransport(broker.handler)).close_position(27, 3)
        raise AssertionError("closing more than one contract must be refused")
    except dclient.ExecutionBlocked:
        pass


def test_api_switch_needs_2fa_to_turn_on_refuses_non_allowlisted_and_never_needs_anything_to_turn_off(client, engine, monkeypatch):  # noqa: F811
    headers = login(client, engine, "slapi@example.com")
    _uid, sid = _setup(engine, monkeypatch, email="slapi@example.com")
    _uid, fid = _setup(engine, monkeypatch, strategy="funding_extreme", email="slapi@example.com")
    got = client.get(f"/api/v1/delta-scanner/{sid}").json()
    assert got["execute_live"] is False and got["paper_only"] is True and got["live_block"] is None
    assert client.put(f"/api/v1/delta-scanner/{sid}/live", headers=headers, json={"enabled": True}).status_code == 403        # no 2FA-verified session
    assert client.put(f"/api/v1/delta-scanner/{sid}/live", headers=headers, json={"enabled": False}).json()["execute_live"] is False
    assert "allowlist" in client.get(f"/api/v1/delta-scanner/{fid}").json()["live_block"]
    assert client.post(f"/api/v1/delta-scanner/{sid}/live/kill", headers=headers).json()["execute_live"] is False


def test_a_paper_scanner_run_never_builds_an_order_client(engine, monkeypatch):
    from app.delta_scanner import service as scan
    _uid, sid = _setup(engine, monkeypatch, email="sl8@example.com")

    def boom(*a, **k):
        raise AssertionError("a scanner with Live off must never reach the order path")
    monkeypatch.setattr(live, "maybe_place_scanner", boom)
    monkeypatch.setattr(dclient.DeltaOrderClient, "__init__", boom)
    _e, candles, _at = _entry_and_candles(T0 + timedelta(days=1))
    monkeypatch.setattr(scan, "fetch_spec", lambda s, transport=None: SPEC)
    monkeypatch.setattr(scan, "sync_candles", lambda *a, **k: {})
    monkeypatch.setattr(scan, "load_candles", lambda *a, **k: candles * 10)
    with Session(engine) as db:
        scan.run_once(db, db.get(DeltaScanner, sid), T0 + timedelta(days=1))


def test_freshness_is_judged_at_the_moment_of_sending_not_when_a_slow_pass_started(engine, monkeypatch):
    # a pass that began 20 minutes ago hands over a signal that was fresh THEN: by the time it would be sent it is stale (seen live on 25 Nov: a 4.5-minute pass)
    from datetime import datetime, timezone
    _uid, sid = _setup(engine, monkeypatch, execute_live=True, email="sl9@example.com")
    broker = Broker()
    entries, candles, at = _entry_and_candles(datetime.now(timezone.utc) - timedelta(minutes=20))
    with Session(engine) as db:
        row = live.maybe_place_scanner(db, db.get(DeltaScanner, sid), SPEC, entries, candles, at, broker.factory())
        assert row.status == "blocked" and "signal_fresh" in row.block_reason and broker.writes == []
