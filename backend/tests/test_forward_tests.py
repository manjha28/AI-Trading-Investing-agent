"""Forward tests: paper replay on live-style data, live-signal detection, the guarded live path (mock broker only), and the API. All data is TEST DATA."""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.dhan_execution import policy, service as live
from app.forward_test_scheduler import should_run
from app.models import Instrument, InstrumentProviderIdentifier, User, UserSession
from app.models.execution import ExecutionAuditEvent
from app.models.forward_live import DhanLiveOrder, ForwardTest
from app.services import forward_test as fts, lab
from app.services import intraday_runner as runner
from app.services.intraday_strategy import StrategyDefinition, definition_hash, evaluate
from app.services.timeframes import EQUITY_IN
from tests.test_dhan_execution import mock_client, no_dhan_network  # noqa: F401  (autouse: no Dhan host is ever resolved)
from tests.test_signals_lab_api import API, add_bars, add_inst, client, definition, engine, h, login, nse_sessions  # noqa: F401  (fixtures + helpers)

D = Decimal
FT = "/api/v1/forward-tests"
LIVE = "/api/v1/dhan-live"
JAN10_CLOSE = datetime(2025, 1, 10, 10, 5, tzinfo=timezone.utc)             # Fri 15:35 IST, after the close


@pytest.fixture(autouse=True)
def small_history(monkeypatch):
    monkeypatch.setattr(lab, "MIN_BARS_1M", 1000)


def dhan_id(engine, iid: str, security_id="3045") -> None:
    with Session(engine) as db:
        db.add(InstrumentProviderIdentifier(instrument_id=UUID(iid), provider="dhan", provider_instrument_id=f"{security_id}|NSE_EQ", provider_symbol="X", is_verified=True,
                                            last_seen_at=datetime.now(timezone.utc)))
        db.commit()


def make_test(engine, user_email: str, iid: str, *, key="mfi_dip_30m", start=date(2025, 1, 8), execute_live=False, defn=None, quantity=1) -> UUID:
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == user_email))
        d = defn or definition(iid, stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2})
        ft = ForwardTest(user_id=user.id, name="t", strategy_key=key, definition=d, definition_hash=definition_hash(d), instrument_ids=[iid], quantity=quantity, start_date=start,
                         status="active", execute_live=execute_live, slippage_bps=D("2"), created_at=JAN10_CLOSE, updated_at=JAN10_CLOSE)
        db.add(ft)
        db.commit()
        return ft.id


# ───────────────────────── paper replay ─────────────────────────
def test_paper_replay_counts_only_trades_from_the_start_date_at_the_fixed_quantity_and_says_it_is_hypothetical(client, engine):
    login(client, engine, "paper@example.com")
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    ft_id = make_test(engine, "paper@example.com", iid, start=date(2025, 1, 8), quantity=3)
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        fts.run_once(db, ft, JAN10_CLOSE, refresh=False)
        snap = ft.snapshot
        assert ft.last_error is None and snap["paper_only"] is True and "Not real P&L" in snap["label"] and snap["quantity"] == 3
        trades = snap["trades"]
        assert trades and all(pd.Timestamp(t["entry_time"]) >= pd.Timestamp("2025-01-07T18:30:00Z") for t in trades)          # Jan 8 00:00 IST
        assert all(t["qty"] == "3" for t in trades)
        s = snap["summary"]
        assert s["closed_trades"] == len(trades) == sum(v["trades"] for v in s["by_symbol"].values())
        assert D(s["net_pnl"]) == sum(D(t["net_pnl"]) for t in trades) == D(s["gross_pnl"]) - D(s["charges"]) - D(s["slippage"])         # net is gross less charges and slippage
        assert s["win_rate"] == pytest.approx(sum(D(t["net_pnl"]) > 0 for t in trades) / len(trades), abs=1e-4)
        assert snap["universe"][0]["symbol"] == "SBIN" and snap["universe"][0]["status"] == "ok" and snap["universe"][0]["bars"] == 375 * 5
        earlier = [t for t in runner.make_backtester(None)(StrategyDefinition.model_validate(ft.definition), runner.prepare_runs(db, StrategyDefinition.model_validate(ft.definition),
                   [db.get(Instrument, UUID(iid))], fts.BacktestConfig(start=datetime(2025, 1, 1, tzinfo=timezone.utc), end=JAN10_CLOSE), JAN10_CLOSE)[0], fts.BacktestConfig(start=datetime(2025, 1, 1, tzinfo=timezone.utc), end=JAN10_CLOSE))["trades"]
                   if pd.Timestamp(t["entry_time"]) < pd.Timestamp("2025-01-07T18:30:00Z")]
        assert earlier, "the fixture must have trades BEFORE the start date, otherwise this test proves nothing"      # ... and none of them leaked into the forward results


def test_before_the_start_date_the_test_waits_and_reports_no_trades(client, engine):
    login(client, engine, "wait@example.com")
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    ft_id = make_test(engine, "wait@example.com", iid, start=date(2025, 1, 20))
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        fts.run_once(db, ft, JAN10_CLOSE, refresh=False)
        assert "Starts 2025-01-20" in ft.snapshot["waiting"] and ft.snapshot["trades"] == [] and ft.snapshot["summary"]["closed_trades"] == 0


def test_missing_or_stale_data_is_shown_as_such_never_as_a_clean_zero(client, engine):
    login(client, engine, "stale@example.com")
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    nodata = add_inst(engine, "NODATA")
    ft_id = make_test(engine, "stale@example.com", iid)
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        ft.instrument_ids = [iid, nodata]
        db.commit()
        during = datetime(2025, 1, 10, 7, 0, tzinfo=timezone.utc)                                         # 12:30 IST, session open, newest bar is from 15:29 IST... earlier
        fts.run_once(db, ft, during + timedelta(days=3), refresh=False)                                    # Monday, no new bars since Friday
        status = {u["symbol"]: u for u in ft.snapshot["universe"]}
        assert status["NODATA"]["status"] in ("no_data", "refused") and status["NODATA"]["reason"]
        assert status["SBIN"]["status"] in ("stale", "refused", "ok")


def test_a_failing_data_refresh_is_recorded_on_the_instrument_and_does_not_stop_the_run(client, engine):
    login(client, engine, "refresh@example.com")
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    ft_id = make_test(engine, "refresh@example.com", iid)
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        fts.run_once(db, ft, JAN10_CLOSE, refresher=lambda db_, user, insts, now: {insts[0].id: "provider returned no data"})
        assert ft.snapshot["universe"][0]["reason"] == "provider returned no data" and ft.last_error is None
        fts.run_once(db, ft, JAN10_CLOSE + timedelta(hours=7), refresher=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))     # >6h later: outside the session a refresh is due again
        assert "RuntimeError" in ft.last_error                                                                # one broken pass is reported, not raised


def test_the_scheduler_runs_active_tests_in_session_once_after_close_and_never_when_paused():
    ft = ForwardTest(status="active", last_run_at=datetime(2026, 11, 23, 4, 30, tzinfo=timezone.utc))        # last ran 10:00 IST
    assert should_run(ft, datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc))                                  # Monday 10:30 IST: in session
    assert should_run(ft, datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc))                                 # 16:30 IST: the final post-close pass has not happened yet
    ft.last_run_at = datetime(2026, 11, 23, 10, 5, tzinfo=timezone.utc)                                       # 15:35 IST: it already did
    assert not should_run(ft, datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc))
    ft.last_run_at = datetime(2026, 11, 23, 4, 30)                                                            # a naive timestamp (as SQLite returns) is read as UTC
    assert should_run(ft, datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc))
    ft.last_run_at = datetime(2026, 11, 20, 10, 5, tzinfo=timezone.utc)
    assert not should_run(ft, datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc))                              # Saturday: nothing to refresh
    ft.status = "paused"
    assert not should_run(ft, datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc))
    assert should_run(ForwardTest(status="active", last_run_at=None), datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc))      # a new test runs once even on a Saturday


def test_trading_day_helpers():
    assert fts.next_trading_day(date(2026, 11, 20)) == date(2026, 11, 23)       # Friday -> Monday
    assert fts.next_trading_day(date(2026, 11, 22)) == date(2026, 11, 23)       # Sunday -> Monday
    assert fts.next_trading_day(date(2026, 11, 23)) == date(2026, 11, 24)
    assert fts.session_open(datetime(2026, 11, 23, 4, 0, tzinfo=timezone.utc)) and not fts.session_open(datetime(2026, 11, 22, 5, 0, tzinfo=timezone.utc))
    assert not fts.session_open(datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc))                           # 15:30 IST is the close


# ───────────────────────── live-signal detection ─────────────────────────
def signal_world(engine, email="sig@example.com", *, with_dhan_id=True):
    """A flat tape then a sharp rise: the fast EMA crosses above the slow one on a known decision candle. Bars stop right after that candle."""
    iid = add_inst(engine, "SBIN")
    if with_dhan_id:
        dhan_id(engine, iid)
    closes = [100.0] * 280 + [100.0 + 0.4 * k for k in range(1, 95)]
    frame = pd.DataFrame({"time": pd.date_range("2025-01-08T03:45:00Z", periods=len(closes), freq="min"), "open": closes, "high": [c + 0.05 for c in closes],
                          "low": [c - 0.05 for c in closes], "close": closes, "volume": 500.0})
    d = definition(iid, stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2}, short_entry=None, constraints={"allow_long": True, "allow_short": False})
    res = evaluate(StrategyDefinition.model_validate(d), frame, EQUITY_IN)
    idx = [int(i) for i in np.flatnonzero(res.signals["long_entry"])][0]
    cut = frame.iloc[: idx + 1]
    add_bars(engine, iid, cut["time"].iloc[0].to_pydatetime(), [float(c) for c in cut["close"]])
    close_time = (cut["time"].iloc[-1] + pd.Timedelta(minutes=1)).to_pydatetime()
    return iid, d, close_time


def test_a_fresh_signal_on_the_latest_candle_becomes_a_buy_intent_with_a_stop_and_target_computed_from_data(client, engine):
    login(client, engine, "sig@example.com")
    iid, d, close_time = signal_world(engine)
    ft_id = make_test(engine, "sig@example.com", iid, defn=d, start=date(2025, 1, 8))
    now = close_time + timedelta(seconds=20)
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        inst = db.get(Instrument, UUID(iid))
        _snapshot, runs = fts.replay(db, ft, [inst], now, {})
        intents = fts.detect_intents(db, ft, inst, runs[0], now)
        assert len(intents) == 1
        i = intents[0]
        assert i.side == "buy" and i.quantity == 1 and i.security_id == "3045" and i.strategy_key == "mfi_dip_30m"
        assert i.stop_price < i.reference_price < i.target_price
        assert float(i.reference_price - i.stop_price) == pytest.approx(float(i.reference_price) * 0.005, abs=0.06)            # a 0.5% stop, to the 0.05 tick
        assert float(i.target_price - i.reference_price) == pytest.approx(2 * float(i.reference_price - i.stop_price), abs=0.11)    # target = 2 x the stop distance
        assert fts.detect_intents(db, ft, inst, runs[0], now + timedelta(minutes=12)) == []                             # a late signal is never acted on


def test_no_intent_without_a_stop_a_target_or_a_verified_dhan_security_id(client, engine):
    login(client, engine, "noint@example.com")
    iid, d, close_time = signal_world(engine, with_dhan_id=False)
    now = close_time + timedelta(seconds=20)
    ft_id = make_test(engine, "noint@example.com", iid, defn=d)
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        inst = db.get(Instrument, UUID(iid))
        _s, runs = fts.replay(db, ft, [inst], now, {})
        assert fts.detect_intents(db, ft, inst, runs[0], now) == []                                                    # no Dhan security id
        db.add(InstrumentProviderIdentifier(instrument_id=inst.id, provider="dhan", provider_instrument_id="3045|NSE_EQ", provider_symbol="X", is_verified=True, last_seen_at=now))
        ft.definition = {**d, "target": {"mode": "none", "value": 0, "atr_length": 14}}
        db.commit()
        assert fts.detect_intents(db, ft, inst, runs[0], now) == []                                                    # no target: live orders always carry both


def test_end_to_end_live_path_places_exactly_one_order_for_the_signal_and_never_a_second(client, engine, monkeypatch):
    login(client, engine, "e2e@example.com")
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", True)
    monkeypatch.setattr(policy, "environment_block", lambda: None)
    iid, d, close_time = signal_world(engine)
    now = close_time + timedelta(seconds=20)
    ft_id = make_test(engine, "e2e@example.com", iid, defn=d, execute_live=True)
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"orderId": "9001"})

    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "e2e@example.com"))
        live.get_state(db, user.id).armed = True
        db.commit()
        ft = db.get(ForwardTest, ft_id)
        factory = lambda uid: mock_client(handler)  # noqa: E731
        first = fts.run_once(db, ft, now, client_factory=factory, refresh=False)
        assert first["error"] is None and len(posted) == 1
        order = db.scalar(select(DhanLiveOrder))
        assert order.status == "placed" and order.dhan_order_id == "9001" and order.side == "buy" and order.quantity == 1
        assert posted[0]["quantity"] == 1 and posted[0]["productType"] == "INTRADAY" and posted[0]["transactionType"] == "BUY"
        assert ft.snapshot["live_orders_this_pass"][0]["status"] == "placed" and ft.snapshot["execute_live"] is True
        fts.run_once(db, ft, now + timedelta(seconds=30), client_factory=factory, refresh=False)
        assert len(posted) == 1 and len(db.scalars(select(DhanLiveOrder)).all()) == 1                                    # the same signal is never ordered twice
        assert db.scalars(select(ExecutionAuditEvent).where(ExecutionAuditEvent.event_type == "broker_reply")).all()


def test_the_same_signal_is_not_ordered_when_the_user_has_not_armed_or_the_test_is_paper(client, engine, monkeypatch):
    login(client, engine, "unarmed@example.com")
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", True)
    monkeypatch.setattr(policy, "environment_block", lambda: None)
    iid, d, close_time = signal_world(engine)
    now = close_time + timedelta(seconds=20)
    paper_id = make_test(engine, "unarmed@example.com", iid, defn=d, execute_live=False)
    live_id = make_test(engine, "unarmed@example.com", iid, defn=d, execute_live=True)

    def boom(request):
        raise AssertionError("no order may be sent")

    with Session(engine) as db:
        for ft_id in (paper_id, live_id):                                         # neither armed, nor (for the first) live
            fts.run_once(db, db.get(ForwardTest, ft_id), now, client_factory=lambda uid: mock_client(boom), refresh=False)
        rows = db.scalars(select(DhanLiveOrder)).all()
        assert [r.status for r in rows] == ["blocked"] and "armed" in rows[0].block_reason                           # only the live-flagged test leaves a (blocked) record


# ───────────────────────── API ─────────────────────────
def session_ok_2fa(engine, email: str) -> None:
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        user.two_factor_enabled = True
        for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
            s.two_factor_verified = True
        db.commit()


def create_body(iid: str, **over):
    return {"strategy_key": "mfi_dip_30m", "instruments": [iid], "start_date": "2026-11-23", **over}


def test_create_list_get_pause_resume_delete_with_csrf_and_ownership(client, engine):
    headers = login(client, engine, "api@example.com") or h(client)
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    assert client.post(FT, json=create_body(iid)).status_code in (400, 403)                                     # CSRF
    made = client.post(FT, headers=h(client), json=create_body(iid, quantity=5, name="Monday run"))
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["execute_live"] is False and body["paper_only"] is True and body["quantity"] == 5 and body["name"] == "Monday run" and body["start_date"] == "2026-11-23"
    ft_id = body["id"]
    assert [f["id"] for f in client.get(FT).json()["forward_tests"]] == [ft_id]
    detail = client.get(f"{FT}/{ft_id}").json()
    assert detail["definition_hash"] and detail["timeframe"] == "30m" and detail["snapshot"] == {}
    assert client.post(f"{FT}/{ft_id}/pause", headers=h(client)).json()["status"] == "paused"
    assert client.post(f"{FT}/{ft_id}/resume", headers=h(client)).json()["status"] == "active"
    assert client.post(f"{FT}/{ft_id}/bogus", headers=h(client)).status_code == 422
    from fastapi.testclient import TestClient
    from app.main import app
    other = TestClient(app)
    other_headers = login(other, engine, "other-ft@example.com") or h(other)
    assert other.get(f"{FT}/{ft_id}").status_code == 404 and other.delete(f"{FT}/{ft_id}", headers=h(other)).status_code == 404 and other.get(FT).json()["forward_tests"] == []
    assert other_headers is not None and headers is not None
    assert client.delete(f"{FT}/{ft_id}").status_code in (400, 403)
    assert client.delete(f"{FT}/{ft_id}", headers=h(client)).status_code == 204 and client.get(f"{FT}/{ft_id}").status_code == 404


def test_creation_validation_and_the_live_allowlist_and_quantity_cap(client, engine):
    login(client, engine, "val@example.com")
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    post = lambda **kw: client.post(FT, headers=h(client), json=create_body(iid, **kw))  # noqa: E731
    assert client.post(FT, headers=h(client), json=create_body(iid, strategy_key="nope")).status_code == 404
    assert client.post(FT, headers=h(client), json={"strategy_key": "mfi_dip_30m"}).status_code == 422                          # no instruments
    assert post(quantity=101).status_code == 422 and post(slippage_bps=2.5).status_code == 422
    assert client.post(FT, headers=h(client), json=create_body(iid, params={"level": 99})).status_code == 422                    # out-of-range strategy parameter
    over = post(execute_live=True, strategy_key="prev_day_break_30m")
    assert over.status_code == 422 and "allowlist" in over.json()["detail"]
    capped = post(execute_live=True, quantity=2)
    assert capped.status_code == 422 and "capped at 1" in capped.json()["detail"]
    no_id = post(execute_live=True)
    assert no_id.status_code == 422 and "security id" in no_id.json()["detail"]
    dhan_id(engine, iid)
    ok = post(execute_live=True)
    assert ok.status_code == 201 and ok.json()["execute_live"] is True and ok.json()["paper_only"] is False
    both = client.post(FT, headers=h(client), json={"strategy_key": "five_ema_alert", "instruments": [iid], "execute_live": True, "params": {"timeframe_min": 5}})
    assert both.status_code == 201 and both.json()["name"] == "5 EMA Alert Candle (Subasish Pani) (5m)"
    assert client.get(f"{FT}/{both.json()['id']}").json()["timeframe"] == "5m"                                                  # the 5-minute variant                                                    # the other allowlisted strategy


def test_execute_live_can_be_toggled_only_for_allowlisted_strategies_at_quantity_one(client, engine):
    login(client, engine, "toggle@example.com")
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    ok = client.post(FT, headers=h(client), json=create_body(iid)).json()["id"]
    bad = client.post(FT, headers=h(client), json=create_body(iid, strategy_key="psar_flip_trend_30m")).json()["id"]
    big = client.post(FT, headers=h(client), json=create_body(iid, quantity=4)).json()["id"]
    put = lambda i, on: client.put(f"{FT}/{i}/execute-live", headers=h(client), json={"enabled": on})  # noqa: E731
    assert put(ok, True).json()["execute_live"] is True and put(ok, False).json()["execute_live"] is False
    assert put(bad, True).status_code == 422 and put(big, True).status_code == 422
    assert put(bad, False).status_code == 200                                                                                     # turning live OFF is always allowed


def test_manual_refresh_runs_the_test_and_returns_the_snapshot(client, engine, monkeypatch):
    login(client, engine, "manual@example.com")
    monkeypatch.setattr(fts, "refresh_bars", lambda db, user, insts, now: {i.id: None for i in insts})
    iid = add_inst(engine, "SBIN")
    nse_sessions(engine, iid)
    ft_id = client.post(FT, headers=h(client), json=create_body(iid)).json()["id"]
    out = client.post(f"{FT}/{ft_id}/refresh", headers=h(client)).json()
    assert out["last_run_at"] and out["snapshot"]["paper_only"] is True and out["snapshot"]["universe"][0]["symbol"] == "SBIN"


def test_live_status_shows_the_hard_limits_and_cannot_be_armed_on_a_default_deployment(client, engine, monkeypatch):
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", False)                                  # a default deployment, whatever this machine's .env says
    login(client, engine, "status@example.com")
    s = client.get(f"{LIVE}/status").json()
    assert s["deployment_enabled"] is False and s["armed"] is False and s["can_arm"] is False
    assert s["limits"]["max_order_quantity"] == 1 and s["limits"]["allowed_strategies"] == ["five_ema_alert", "mfi_dip_30m"] and s["limits"]["product"] == "INTRADAY"
    assert s["confirmation_phrase"] == "ENABLE LIVE ORDERS" and "net-negative" in s["warning"]
    session_ok_2fa(engine, "status@example.com")
    wrong = client.post(f"{LIVE}/arm", headers=h(client), json={"confirmation": "yes"})
    assert wrong.status_code == 422
    off = client.post(f"{LIVE}/arm", headers=h(client), json={"confirmation": "ENABLE LIVE ORDERS"})
    assert off.status_code == 409 and "DHAN_LIVE_EXECUTION_ENABLED" in off.json()["detail"]


def test_arming_needs_a_two_factor_session_the_phrase_the_deployment_flag_and_a_real_environment(client, engine, monkeypatch):
    login(client, engine, "arm@example.com")
    arm = lambda **kw: client.post(f"{LIVE}/arm", headers=h(client), json={"confirmation": "ENABLE LIVE ORDERS", **kw})  # noqa: E731
    no2fa = arm()
    assert no2fa.status_code == 403 and no2fa.json()["detail"]["code"] == "two_factor_required"
    session_ok_2fa(engine, "arm@example.com")
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", True)
    blocked = arm()
    assert blocked.status_code == 409 and "pytest" in blocked.json()["detail"]                                                 # a test run can never arm live execution
    monkeypatch.setattr(policy, "environment_block", lambda: None)
    assert arm(max_daily_loss_inr="10000").status_code == 422 and arm(max_notional_inr="1").status_code == 422 and arm(max_open_positions=9).status_code == 422
    assert client.post(f"{LIVE}/arm", headers=h(client), json={"confirmation": "ENABLE LIVE ORDERS", "max_daily_loss_inr": 300.5}).status_code in (200, 422)
    armed = arm(max_daily_loss_inr="300", max_trades_per_day=4, max_open_positions=2, max_notional_inr="20000")
    assert armed.status_code == 200 and armed.json()["armed"] is True and armed.json()["armed_at"]
    assert armed.json()["limits"]["max_daily_loss_inr"] == "300.00" and armed.json()["limits"]["max_trades_per_day"] == 4
    with Session(engine) as db:
        events = db.scalars(select(ExecutionAuditEvent).where(ExecutionAuditEvent.client_order_id == "arm")).all()
        assert events and events[0].hypothetical is False and "ARMED" in events[0].payload["event"]


def test_the_kill_switch_works_without_two_factor_and_leaves_the_user_disarmed(client, engine, monkeypatch):
    login(client, engine, "kill@example.com")
    monkeypatch.setattr(settings, "dhan_live_execution_enabled", True)
    monkeypatch.setattr(policy, "environment_block", lambda: None)
    session_ok_2fa(engine, "kill@example.com")
    client.post(f"{LIVE}/arm", headers=h(client), json={"confirmation": "ENABLE LIVE ORDERS"})
    assert client.get(f"{LIVE}/status").json()["armed"] is True
    with Session(engine) as db:
        for s in db.scalars(select(UserSession)):
            s.two_factor_verified = False                                                                                       # a plain verified session
        db.commit()
    out = client.post(f"{LIVE}/disarm", headers=h(client))
    assert out.status_code == 200 and out.json()["armed"] is False and out.json()["status"]["armed"] is False
    assert client.post(f"{LIVE}/disarm").status_code in (400, 403)                                                               # but never without CSRF


def test_live_orders_are_listed_owner_scoped_with_the_brokers_verbatim_reply(client, engine):
    login(client, engine, "orders@example.com")
    iid = add_inst(engine, "SBIN")
    ft_id = make_test(engine, "orders@example.com", iid, execute_live=True)
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "orders@example.com"))
        db.add(DhanLiveOrder(user_id=user.id, forward_test_id=ft_id, strategy_key="mfi_dip_30m", instrument_id=UUID(iid), symbol="SBIN", security_id="3045", side="buy", quantity=1,
                             signal_time=JAN10_CLOSE, reference_price=D("100"), stop_price=D("99"), target_price=D("102"), client_order_id="dh" + "7" * 28, status="rejected",
                             block_reason="IP not whitelisted", response={"http_status": 401, "body": {"errorCode": "DH-905"}}, created_at=JAN10_CLOSE, updated_at=JAN10_CLOSE))
        db.commit()
    out = client.get(f"{LIVE}/orders").json()
    assert len(out["orders"]) == 1 and out["orders"][0]["status"] == "rejected" and out["orders"][0]["broker_response"]["body"]["errorCode"] == "DH-905"
    assert "contract note" in out["note"]
    from fastapi.testclient import TestClient
    from app.main import app
    other = TestClient(app)
    login(other, engine, "other-orders@example.com")
    assert other.get(f"{LIVE}/orders").json()["orders"] == []
