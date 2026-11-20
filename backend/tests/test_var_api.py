"""VAR API: auth, email verification, CSRF, owner scoping, config versioning, hypothetical labelling. SQLite test DB only."""
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import User
from app.models.var_strategy import VarBacktestRun

API = "/api/v1/var"


@pytest.fixture
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


def _pf(rows):
    """Stub of service.preflight: symbol -> status. Real classification logic is tested separately via classify_symbol."""
    from app.services.var.service import classify_symbol

    def fake(db, segment, symbols, start, end):
        out = []
        for sym in [x.upper() for x in symbols]:
            st = rows.get(sym, "ok")
            out.append(classify_symbol({"symbol": sym, "exists": st != "not_in_storage", "sessions_available": 0 if st in ("not_in_storage", "no_bars_in_window") else 50,
                                        "sessions_usable": 0 if st in ("not_in_storage", "no_bars_in_window", "no_usable_sessions") else 50, "first": None, "last": None}))
        return out
    return fake


@pytest.fixture(autouse=True)
def _default_preflight(monkeypatch):
    from app.services.var import service
    monkeypatch.setattr(service, "preflight", _pf({}))


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client, engine, email, verified=True):
    pw = "correct-horse-battery"
    assert client.post("/api/v1/auth/register", json={"email": email, "password": pw}).status_code == 202
    assert client.post("/api/v1/auth/login", json={"email": email, "password": pw}).status_code == 200
    with Session(engine) as db:
        u = db.scalar(select(User).where(User.email == email))
        u.email_verified = verified
        db.commit()


def h(client):
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def test_unauthenticated_and_unverified_are_rejected(client, engine):
    assert client.get(f"{API}/config").status_code == 401
    login(client, engine, "u@example.com", verified=False)
    assert client.get(f"{API}/config").status_code == 403


def test_config_default_then_versioned_save_requires_csrf_and_validates(client, engine):
    login(client, engine, "a@example.com")
    g = client.get(f"{API}/config").json()
    assert g["saved"] is False and g["config"]["version"] == "1.0.0" and "not investment advice" in g["disclaimer"]
    body = g["config"]
    assert client.put(f"{API}/config", json=body).status_code in (401, 403)             # CSRF missing
    ok = client.put(f"{API}/config", json=body, headers=h(client))
    assert ok.status_code == 200 and ok.json()["version_no"] == 1
    assert client.put(f"{API}/config", json=body, headers=h(client)).json()["version_no"] == 2   # append-only versions
    bad = {**body, "risk": {**body["risk"], "max_daily_loss_pct": 0.1, "risk_per_trade_pct": 1.0}}
    assert client.put(f"{API}/config", json=bad, headers=h(client)).status_code == 422
    assert client.put(f"{API}/config", json={**body, "surprise": 1}, headers=h(client)).status_code == 422


def test_backtest_run_is_owner_scoped_hypothetical_and_snapshots_config(client, engine):
    login(client, engine, "a@example.com")
    r = client.post(f"{API}/backtests", json={"underlyings": ["NIFTY"]}, headers=h(client))
    assert r.status_code == 202 and r.json()["hypothetical"] is True
    rid = r.json()["id"]
    got = client.get(f"{API}/backtests/{rid}").json()
    assert got["hypothetical"] is True and got["config_snapshot"]["strategy"] == "VAR" and got["status"] == "queued"
    # a second user cannot see it
    other = TestClient(app)
    login(other, engine, "b@example.com")
    assert other.get(f"{API}/backtests/{rid}").status_code == 404
    assert other.get(f"{API}/backtests").json() == []
    # changing saved config later does not rewrite the stored snapshot
    cfg = client.get(f"{API}/config").json()["config"]
    cfg["risk"]["risk_per_trade_pct"] = 0.30
    assert client.put(f"{API}/config", json=cfg, headers=h(client)).status_code == 200
    assert client.get(f"{API}/backtests/{rid}").json()["config_snapshot"]["risk"]["risk_per_trade_pct"] == 0.25


def test_backtest_requires_csrf_and_rejects_bad_input(client, engine):
    login(client, engine, "a@example.com")
    assert client.post(f"{API}/backtests", json={"underlyings": ["NIFTY"]}).status_code in (401, 403)
    assert client.post(f"{API}/backtests", json={"underlyings": []}, headers=h(client)).status_code == 422
    assert client.post(f"{API}/backtests", json={"underlyings": ["NIFTY"], "place_order": True}, headers=h(client)).status_code == 422


def test_hypothetical_constraint_enforced_by_schema(engine):
    from datetime import datetime, timezone
    from sqlalchemy.exc import IntegrityError
    with Session(engine) as db:
        db.add(User(email="x@example.com", password_hash="x", created_at=datetime.now(timezone.utc)))
        db.commit()
        uid = db.scalar(select(User.id))
        db.add(VarBacktestRun(user_id=uid, status="queued", hypothetical=False, config_snapshot={}, request={}, created_at=datetime.now(timezone.utc)))
        with pytest.raises(IntegrityError):
            db.commit()


def test_charges_schedule_endpoint_lists_every_component_flags_unverified_and_refuses_cross_segment(client, engine):
    assert client.get(f"{API}/charges/schedule").status_code == 401
    login(client, engine, "c@example.com")
    r = client.get(f"{API}/charges/schedule", params={"segment": "futures", "instrument_type": "stock_future", "on": "2026-11-02"})
    assert r.status_code == 200
    j = r.json()
    assert set(j["components"]) == {"brokerage", "stt", "exchange", "ipft", "sebi", "stamp", "gst", "dp"} and j["components"]["stt"]["rate"] == "0.05"
    assert j["components"]["stamp"]["status"] == "owner_stated" and j["schedule_version"] and j["last_verified"] and "not investment advice" in j["disclaimer"]
    cash = client.get(f"{API}/charges/schedule", params={"segment": "equity_cash", "instrument_type": "cash", "on": "2026-11-02"}).json()
    assert cash["components"]["stt"]["rate"] == "0.025" and cash["components"]["dp"]["rate"] == "0"
    assert client.get(f"{API}/charges/schedule", params={"segment": "equity_cash", "instrument_type": "index_future"}).status_code == 422     # cross-segment
    gone = client.get(f"{API}/charges/schedule", params={"segment": "futures", "instrument_type": "index_future", "on": "2024-01-01"})
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "schedule_unavailable"                                          # never a partial schedule


def test_backtest_records_segment_broker_and_schedule_in_the_immutable_snapshot(client, engine):
    login(client, engine, "d@example.com")
    r = client.post(f"{API}/backtests", json={"underlyings": ["SBIN"], "segment": "equity_cash", "broker_profile": "dhan", "exchange": "NSE"}, headers=h(client))
    assert r.status_code == 202
    got = client.get(f"{API}/backtests/{r.json()['id']}").json()
    assert got["request"]["segment"] == "equity_cash" and got["config_snapshot"]["charges"]["broker_profile"] == "dhan"
    assert client.post(f"{API}/backtests", json={"underlyings": ["X"], "segment": "options"}, headers=h(client)).status_code == 422


def test_launch_is_rejected_when_no_requested_symbol_has_usable_data(client, engine, monkeypatch):
    from app.services.var import service
    monkeypatch.setattr(service, "preflight", _pf({"ITC": "not_in_storage", "RELIANCE": "no_usable_sessions"}))
    login(client, engine, "e@example.com")
    r = client.post(f"{API}/backtests", json={"underlyings": ["ITC", "RELIANCE"], "segment": "equity_cash"}, headers=h(client))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_usable_data"
    assert {x["symbol"]: x["status"] for x in r.json()["detail"]["symbols"]} == {"ITC": "not_in_storage", "RELIANCE": "no_usable_sessions"}
    assert client.get(f"{API}/backtests").json() == []                                  # nothing was queued


def test_partial_symbol_sets_need_an_explicit_opt_in_and_record_what_was_dropped(client, engine, monkeypatch):
    from app.services.var import service
    monkeypatch.setattr(service, "preflight", _pf({"ITC": "not_in_storage", "HDFCBANK": "no_bars_in_window"}))
    login(client, engine, "f@example.com")
    body = {"underlyings": ["SBIN", "ITC", "HDFCBANK"], "segment": "equity_cash"}
    r = client.post(f"{API}/backtests", json=body, headers=h(client))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unusable_symbols" and r.json()["detail"]["usable"] == ["SBIN"]
    ok = client.post(f"{API}/backtests", json={**body, "allow_partial": True}, headers=h(client))
    assert ok.status_code == 202
    got = client.get(f"{API}/backtests/{ok.json()['id']}").json()["request"]
    assert got["underlyings"] == ["SBIN"] and {d["symbol"] for d in got["dropped_symbols"]} == {"ITC", "HDFCBANK"} and len(got["preflight"]) == 3


def test_order_flow_variants_are_refused_at_launch_not_run_to_zero_trades(client, engine):
    login(client, engine, "g@example.com")
    cfg = client.get(f"{API}/config").json()["config"]
    r = client.post(f"{API}/backtests", json={"underlyings": ["SBIN"], "segment": "equity_cash", "config": {**cfg, "variant": "B"}}, headers=h(client))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "order_flow_unavailable"


def test_preflight_endpoint_reports_per_symbol_status(client, engine, monkeypatch):
    from app.services.var import service
    monkeypatch.setattr(service, "preflight", _pf({"ITC": "not_in_storage"}))
    assert client.get(f"{API}/preflight", params={"symbols": "SBIN"}).status_code == 401
    login(client, engine, "h@example.com")
    j = client.get(f"{API}/preflight", params={"symbols": "sbin, itc", "segment": "equity_cash"}).json()
    assert [(x["symbol"], x["status"]) for x in j["symbols"]] == [("SBIN", "ok"), ("ITC", "not_in_storage")] and j["usable"] == ["SBIN"] and j["order_flow_stored"] is False
    assert client.get(f"{API}/preflight", params={"symbols": "X", "segment": "options"}).status_code == 422
