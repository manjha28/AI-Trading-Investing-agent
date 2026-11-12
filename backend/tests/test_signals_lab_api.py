"""Signals workspace API: legacy preservation, versioning/immutability, imports, backtests, live signals, auth."""
import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Instrument, MarketBar, User
from app.models.strategies import BacktestRun, IntradaySignal, IntradaySignalEvent, ScanRejection, Strategy, StrategyVersion
from app.services import intraday_python as ip

NOW = datetime.now(timezone.utc)
API = "/api/v1/signals-lab"


@pytest.fixture
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    _ENGINE[:] = [e]
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client, engine, email="lab@example.com"):
    pw = "correct-horse-battery"
    assert client.post("/api/v1/auth/register", json={"email": email, "password": pw}).status_code == 202
    assert client.post("/api/v1/auth/login", json={"email": email, "password": pw}).status_code == 200
    with Session(engine) as db:
        u = db.scalar(select(User).where(User.email == email))
        u.email_verified = True
        db.commit()


def h(client):
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def add_inst(engine, symbol, asset="equity", country="IN", venue="NSE", tz="Asia/Kolkata", cur="INR") -> str:
    with Session(engine) as db:
        i = Instrument(symbol=symbol, display_name=symbol, asset_class=asset, venue=venue, currency=cur, country=country, timezone=tz, created_at=NOW)
        db.add(i)
        db.commit()
        return str(i.id)


def add_bars(engine, iid: str, start: datetime, closes: list[float]) -> None:
    with Session(engine) as db:
        db.add_all(MarketBar(instrument_id=UUID(iid), interval="1m", timestamp=start + timedelta(minutes=k), open=Decimal(repr(c)), high=Decimal(repr(c + 0.05)),
                             low=Decimal(repr(c - 0.05)), close=Decimal(repr(c)), volume=Decimal("500"), source="test", retrieved_at=NOW, is_synthetic=False)
                   for k, c in enumerate(closes))
        db.commit()


def nse_sessions(engine, iid, days=("2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10")):
    rng = np.random.default_rng(5)
    for d in days:
        c = 100 + np.cumsum(rng.normal(0, 0.12, 375))
        add_bars(engine, iid, datetime.fromisoformat(f"{d}T03:45:00+00:00"), [round(float(x), 2) for x in c])


def definition(iid, **over):
    d = {"schema_version": "intraday-strategy/1", "name": "EMA cross", "description": "test", "market": "equity_in", "universe": [iid], "decision_timeframe": "5m",
         "constraints": {"allow_long": True, "allow_short": True}, "sizing": {"method": "fixed_quantity", "value": 10},
         "indicators": [{"alias": "f", "indicator": "ema", "params": {"length": 5}, "timeframe": "5m"}, {"alias": "s", "indicator": "ema", "params": {"length": 12}, "timeframe": "5m"}],
         "long_entry": {"type": "compare", "left": {"kind": "indicator", "ref": "f"}, "op": "crosses_above", "right": {"kind": "indicator", "ref": "s"}},
         "short_entry": {"type": "compare", "left": {"kind": "indicator", "ref": "f"}, "op": "crosses_below", "right": {"kind": "indicator", "ref": "s"}}}
    d.update(over)
    return d


def post(client, path, body, status=None):
    r = client.post(f"{API}{path}", json=body, headers=h(client))
    if status:
        assert r.status_code == status, r.text
    if path == "/backtests" and r.status_code == 202:   # the production worker runs queued backtests; run them inline here
        drain_backtests(_ENGINE[0])
    return r


_ENGINE: list = []


def drain_backtests(engine):
    from app.api import signals_lab
    from app.services import job_queue

    with Session(engine) as db:
        while (job := job_queue.claim_next(db, "test-worker", job_types=("backtest",))) is not None:
            signals_lab._run_in_background(engine, UUID(job.payload["run_id"]), [UUID(x) for x in job.payload["instrument_ids"]])
            job_queue.succeed(db, job.id, "test-worker", {})


def scan_now(client, engine, status=202):
    """Queue a scan through the API, then run the queued scan jobs inline against the test database (the real worker runs them in production)."""
    from app.services import job_queue
    from app.services.job_handlers import PermanentJobError  # noqa: F401
    from app.services.scan_service import run_strategy_scan

    queued = post(client, "/signals/scan", {}, status).json()
    with Session(engine) as db:
        while (job := job_queue.claim_next(db, "test-worker")) is not None:
            p = job.payload
            run_strategy_scan(db, user_id=job.user_id, strategy_id=UUID(p["strategy_id"]), scan_run_id=UUID(p["scan_run_id"]), trigger=p["trigger"], job_id=job.id, now=datetime.now(timezone.utc))
            job_queue.succeed(db, job.id, "test-worker", {})
    view = client.get(f"{API}/scan-runs/{queued['scan_run_id']}")
    if view.status_code == 404:
        return {"scan_run_id": queued["scan_run_id"], "scanned": 0, "new_rows": 0, "errors": [], "instruments_scanned": 0, "rejections": {"total": 0, "by_reason": {}}}
    v = view.json()
    return {**v, "scanned": v["pairs_scanned"], "new_rows": v["new_signals"]}


# ───────────── access control ─────────────
def test_requires_auth_csrf_and_is_owner_scoped(client, engine):
    assert client.get(f"{API}/strategies").status_code == 401
    login(client, engine)
    iid = add_inst(engine, "AAA")
    assert client.post(f"{API}/strategies", json={"definition": definition(iid)}).status_code == 403   # no CSRF header
    sid = post(client, "/strategies", {"definition": definition(iid)}, 201).json()["id"]
    other = TestClient(app)
    login(other, engine, "other@example.com")
    assert other.get(f"{API}/strategies/{sid}").status_code == 404
    assert other.get(f"{API}/strategies").json() == []
    assert other.post(f"{API}/backtests", json={"strategy_id": sid, "instruments": [iid]}, headers=h(other)).status_code == 404


# ───────────── legacy preservation ─────────────
def test_legacy_strategies_are_listed_with_identity_and_untouched(client, engine):
    login(client, engine)
    iid = add_inst(engine, "LEG")
    created = client.post("/api/v1/strategies", json={"name": "Legacy MA", "instrument_id": iid, "params": {"strategy_id": "fast_slow_ma", "fast_period": 20, "slow_period": 50}}, headers=h(client))
    assert created.status_code == 201
    lid = created.json()["id"]
    client.post(f"/api/v1/strategies/{lid}/suspend", json={}, headers=h(client))
    before = client.get(f"/api/v1/strategies/{lid}").json()
    inv = {x["id"]: x for x in client.get(f"{API}/strategies").json()}
    assert inv[lid]["kind"] == "legacy_daily" and inv[lid]["params"]["fast_period"] == 20 and inv[lid]["status"] == "suspended"
    new = post(client, "/strategies", {"definition": definition(iid, name="Intraday")}, 201).json()
    legacy_list = [x["id"] for x in client.get("/api/v1/strategies").json()]
    assert lid in legacy_list and new["id"] not in legacy_list                    # old panel keeps showing only its own rows
    assert client.get(f"/api/v1/strategies/{lid}").json() == before               # nothing changed
    assert client.post(f"/api/v1/strategies/{new['id']}/evaluate", json={}, headers=h(client)).status_code in (409, 422)
    draft = client.get(f"{API}/strategies/{lid}/convert-draft?decision_timeframe=15m").json()
    assert draft["definition"]["source"]["type"] == "legacy" and draft["definition"]["indicators"][0]["params"]["length"] == 20
    assert client.get(f"{API}/strategies/{lid}").json()["versions"][0]["definition"] is None
    dup = post(client, f"/strategies/{lid}/duplicate", {"name": "Legacy MA copy"}, 201).json()
    assert dup["kind"] == "legacy_daily" and dup["id"] != lid and client.get(f"/api/v1/strategies/{lid}").json() == before


def test_retired_legacy_cannot_be_versioned_but_can_be_converted(client, engine):
    login(client, engine)
    iid = add_inst(engine, "RET")
    lid = client.post("/api/v1/strategies", json={"name": "Old", "instrument_id": iid, "params": {"strategy_id": "sma_rsi", "fast_period": 5, "slow_period": 10, "ma_type": "SMA", "rsi_max": "70"}}, headers=h(client)).json()["id"]
    client.post(f"/api/v1/strategies/{lid}/retire", json={}, headers=h(client))
    assert post(client, f"/strategies/{lid}/versions", {"definition": definition(iid)}).status_code == 409
    d = client.get(f"{API}/strategies/{lid}/convert-draft").json()["definition"]
    assert d["long_entry"]["type"] == "group" and d["long_entry"]["children"][1]["right"]["value"] == 70.0
    assert post(client, "/strategies", {"definition": {**d, "name": "Old (intraday)"}}, 201).status_code == 201
    assert client.get(f"/api/v1/strategies/{lid}").json()["status"] == "retired"


# ───────────── creation, validation, versioning ─────────────
def test_visual_strategy_is_validated_and_versioned_immutably(client, engine):
    login(client, engine)
    iid = add_inst(engine, "VER")
    bad = post(client, "/strategies", {"definition": definition(iid, long_entry={"type": "compare", "left": {"kind": "indicator", "ref": "ghost"}, "op": ">", "right": {"kind": "const", "value": 1}})})
    assert bad.status_code == 422 and "unknown indicator alias" in str(bad.json())
    assert post(client, "/strategies", {"definition": definition(iid, decision_timeframe="7m")}).status_code == 422
    assert post(client, "/strategies", {"definition": definition(iid, bogus=1)}).status_code == 422
    assert post(client, "/strategies", {"definition": definition("00000000-0000-0000-0000-000000000000")}).status_code == 422
    v = post(client, "/validate", {"definition": definition(iid)}, 200).json()
    assert v["valid"] and v["requirements"]["timeframes"] == ["5m"] and v["requirements"]["warmup_base_minutes"] >= 60
    s = post(client, "/strategies", {"definition": definition(iid)}, 201).json()
    assert post(client, "/strategies", {"definition": definition(iid)}).status_code == 409   # duplicate name
    v1 = s["versions"][0]
    edited = definition(iid, indicators=[{"alias": "f", "indicator": "ema", "params": {"length": 8}, "timeframe": "5m"}, {"alias": "s", "indicator": "ema", "params": {"length": 21}, "timeframe": "5m"}])
    s2 = post(client, f"/strategies/{s['id']}/versions", {"definition": edited}, 201).json()
    assert s2["current_version"] == 2 and len(s2["versions"]) == 2
    old = [x for x in s2["versions"] if x["version"] == 1][0]
    assert old["definition"] == v1["definition"] and old["definition_hash"] == v1["definition_hash"]
    with Session(engine) as db:                       # DB-level immutability guard
        row = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == UUID(s["id"]), StrategyVersion.version == 1))
        row.definition = {"tampered": True}
        with pytest.raises(ValueError, match="immutable"):
            db.commit()


def test_lifecycle_endpoints_still_work_for_intraday_strategies(client, engine):
    login(client, engine)
    iid = add_inst(engine, "LIFE")
    sid = post(client, "/strategies", {"definition": definition(iid)}, 201).json()["id"]
    assert client.post(f"/api/v1/strategies/{sid}/activate", json={}, headers=h(client)).status_code == 200
    assert client.get(f"{API}/strategies/{sid}").json()["status"] == "active"
    assert client.post(f"/api/v1/strategies/{sid}/suspend", json={}, headers=h(client)).status_code == 200


# ───────────── imports ─────────────
def test_pine_and_python_import_endpoints_round_trip(client, engine):
    login(client, engine)
    iid = add_inst(engine, "IMP")
    pine = "//@version=5\nstrategy('Pine X')\nf = ta.ema(close, 9)\ns = ta.ema(close, 21)\nif ta.crossover(f, s)\n    strategy.entry('L', strategy.long)\n"
    r = post(client, "/import/pine", {"filename": "x.pine", "text": pine}, 200).json()
    assert r["can_import"] and r["compatibility"] == "full"
    d = {**r["definition"], "universe": [iid]}
    assert post(client, "/strategies", {"definition": d}).status_code == 422                       # source text must accompany the artifact
    assert post(client, "/strategies", {"definition": d, "source_text": pine + "x"}).status_code == 422   # and match its hash
    s = post(client, "/strategies", {"definition": d, "source_text": pine}, 201).json()
    assert s["source_type"] == "pine" and s["versions"][0]["has_source"] and s["versions"][0]["source_filename"] == "x.pine"
    bad = post(client, "/import/pine", {"filename": "x.pine", "text": "//@version=5\nstrategy('x')\nvar a = 1\n"}, 200).json()
    assert not bad["can_import"] and bad["errors"][0]["line"] == 3
    assert post(client, "/import/pine", {"filename": "x.exe", "text": pine}).status_code == 422
    py = post(client, "/import/python", {"filename": "t.py", "text": ip.TEMPLATE}, 200).json()
    assert py["can_import"] and py["definition"]["python"]["source_sha256"] == hashlib.sha256(ip.TEMPLATE.encode()).hexdigest()
    evil = post(client, "/import/python", {"filename": "evil.py", "text": "import os\nclass Strategy:\n  pass\n"}, 200).json()
    assert not evil["can_import"] and evil["screen_errors"][0]["line"] == 1 and evil["definition"] is None
    sp = post(client, "/strategies", {"definition": {**py["definition"], "universe": [iid], "name": "Py"}, "source_text": ip.TEMPLATE}, 201).json()
    assert sp["source_type"] == "python"
    tampered = ip.TEMPLATE.replace("rsi_min", "rsi_max")
    assert post(client, "/strategies", {"definition": {**py["definition"], "universe": [iid], "name": "Py2"}, "source_text": tampered}).status_code == 422


# ───────────── backtests ─────────────
def run_backtest(client, sid, iid, **cfg):
    body = {"strategy_id": sid, "instruments": [iid], "config": {"start": "2025-01-06T00:00:00Z", "end": "2025-01-11T00:00:00Z", "slippage_bps": "2", **cfg}}
    r = post(client, "/backtests", body, 202).json()
    return client.get(f"{API}/backtests/{r['id']}").json()


def test_backtest_runs_on_one_minute_data_and_is_reproducible(client, engine):
    login(client, engine)
    iid = add_inst(engine, "BT")
    nse_sessions(engine, iid)
    sid = post(client, "/strategies", {"definition": definition(iid, stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2})}, 201).json()["id"]
    a = run_backtest(client, sid, iid)
    assert a["status"] == "completed", a.get("error")
    res = a["result"]
    assert res["metrics"]["total_trades"] > 0 and res["dataset"]["instruments"][0]["bars"] == 375 * 5
    assert res["assumptions"]["cost_profile"]["assumed"] and res["versions"]["engine"] and a["definition_snapshot"]["name"] == "EMA cross" and a["definition_hash"]
    b = run_backtest(client, sid, iid)
    assert b["result"]["trades"] == a["result"]["trades"] and b["result"]["dataset"]["sha256"] == a["result"]["dataset"]["sha256"]
    detail = client.get(f"{API}/strategies/{sid}").json()
    assert detail["versions"][0]["locked"] is True and detail["last_backtest"] if "last_backtest" in detail else True
    listing = client.get(f"{API}/backtests?strategy_id={sid}").json()
    assert len(listing) == 2 and "trades" not in str(listing[0].keys())
    # a later version never rewrites what v1's run recorded
    edited = definition(iid, indicators=[{"alias": "f", "indicator": "ema", "params": {"length": 3}, "timeframe": "5m"}, {"alias": "s", "indicator": "ema", "params": {"length": 9}, "timeframe": "5m"}])
    post(client, f"/strategies/{sid}/versions", {"definition": edited}, 201)
    again = client.get(f"{API}/backtests/{a['id']}").json()
    assert again["strategy_version"] == 1 and again["result"]["trades"] == a["result"]["trades"]
    with Session(engine) as db:
        assert db.scalar(select(BacktestRun).where(BacktestRun.id == UUID(a["id"]))).definition_hash == a["definition_hash"]


def test_backtest_refuses_instruments_with_insufficient_or_bad_data(client, engine):
    login(client, engine)
    iid = add_inst(engine, "THIN")
    add_bars(engine, iid, datetime.fromisoformat("2025-01-06T03:45:00+00:00"), [100.0 + (k % 3) for k in range(40)])   # 40 of 375 minutes
    sid = post(client, "/strategies", {"definition": definition(iid)}, 201).json()["id"]
    run = run_backtest(client, sid, iid)
    assert run["status"] == "failed" and "insufficient" in run["error"] or "missing" in run["error"]
    empty = add_inst(engine, "NONE")
    sid2 = post(client, "/strategies", {"definition": definition(empty, name="empty")}, 201).json()["id"]
    assert run_backtest(client, sid2, empty)["status"] == "failed"
    dq = client.get(f"{API}/data-quality/{iid}?days=800").json()
    assert dq["status"] == "refuse" and dq["missing_pct"] > 80


def test_market_mismatch_and_bad_config_are_rejected_up_front(client, engine):
    login(client, engine)
    eq = add_inst(engine, "EQ")
    crypto = add_inst(engine, "BTCUSD", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    sid = post(client, "/strategies", {"definition": definition(eq)}, 201).json()["id"]
    assert post(client, "/backtests", {"strategy_id": sid, "instruments": [crypto]}).status_code == 422
    assert post(client, "/backtests", {"strategy_id": sid, "instruments": [eq], "config": {"slippage_bps": "-1"}}).status_code == 422
    assert post(client, "/backtests", {"strategy_id": sid, "instruments": [eq], "config": {"cost_profile": "nope"}}).status_code == 422
    assert post(client, "/backtests", {"strategy_id": sid, "instruments": [eq], "config": {"unknown": 1}}).status_code == 422
    usa = add_inst(engine, "AAPL", country="US", venue="NMS", tz="America/New_York", cur="USD")
    assert post(client, "/backtests", {"strategy_id": sid, "instruments": [usa]}).status_code == 422


def test_walk_forward_in_out_of_sample_and_sensitivity_via_api(client, engine):
    login(client, engine)
    iid = add_inst(engine, "WF")
    days = [f"2025-01-{d:02d}" for d in range(6, 32) if datetime(2025, 1, d).weekday() < 5]
    nse_sessions(engine, iid, tuple(days))
    sid = post(client, "/strategies", {"definition": definition(iid, constraints={"allow_long": True, "allow_short": False})}, 201).json()["id"]
    for kind, opts in (("is_oos", {}), ("sensitivity", {"parameters": ["f.length"]}), ("walk_forward", {"train_days": 8, "test_days": 4, "parameters": ["f.length"]})):
        body = {"strategy_id": sid, "instruments": [iid], "kind": kind, "options": opts, "config": {"start": "2025-01-06T00:00:00Z", "end": "2025-02-01T00:00:00Z"}}
        r = post(client, "/backtests", body, 202).json()
        got = client.get(f"{API}/backtests/{r['id']}").json()
        assert got["status"] == "completed", got.get("error")
        assert got["result"]["mode"] == {"is_oos": "in_out_of_sample"}.get(kind, kind)
    wf = got["result"]
    assert wf["folds"] and "train windows only" in wf["optimised_on"]


# ───────────── live signals ─────────────
def test_live_signals_are_explainable_deduplicated_and_have_a_lifecycle(client, engine):
    login(client, engine)
    iid = add_inst(engine, "BTC-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    end = (NOW - timedelta(minutes=1)).replace(second=0, microsecond=0)
    n = 90
    closes = [100.0] * (n - 1) + [101.0]
    add_bars(engine, iid, end - timedelta(minutes=n - 1), closes)
    d = definition(iid, name="Breakout", market="crypto", decision_timeframe="1m", indicators=[{"alias": "e", "indicator": "ema", "params": {"length": 10}, "timeframe": "1m"}],
                   long_entry={"type": "group", "op": "and", "children": [
                       {"type": "compare", "left": {"kind": "price", "field": "close"}, "op": "crosses_above", "right": {"kind": "const", "value": 100.5}},
                       {"type": "compare", "left": {"kind": "price", "field": "close"}, "op": ">", "right": {"kind": "indicator", "ref": "e"}}]},
                   short_entry=None)
    sid = post(client, "/strategies", {"definition": d}, 201).json()["id"]
    assert scan_now(client, engine)["scanned"] == 0                    # suspended strategies are not scanned
    client.post(f"/api/v1/strategies/{sid}/activate", json={}, headers=h(client))
    scan = scan_now(client, engine)
    assert scan["scanned"] == 1 and not scan["errors"]
    sigs = client.get(f"{API}/signals").json()
    assert len(sigs) == 1
    s = sigs[0]
    assert s["direction"] == "long" and s["status"] == "ready" and s["symbol"] == "BTC-PERP" and s["strategy_version"] == 1 and s["timeframe"] == "1m"
    assert Decimal(s["reference_price"]) == Decimal("101.0") and s["data_freshness"]["age_minutes"] < 3
    assert any("crosses above" in c["label"] for c in s["conditions_met"]) and s["indicators"]["e"]["value"] is not None
    assert s["rationale"] and s["expires_at"] > s["signal_time"] and "not investment advice" in s["disclaimer"]
    scan_now(client, engine)                                              # same candle again: no duplicate, new -> active
    sigs = client.get(f"{API}/signals").json()
    assert len(sigs) == 1 and sigs[0]["status"] == "active"
    with Session(engine) as db:                                                         # expiry
        row = db.scalar(select(IntradaySignal))
        row.expires_at = NOW - timedelta(minutes=1)
        db.commit()
    assert client.get(f"{API}/signals?status=expired").json()[0]["status"] == "expired"


def test_stale_data_suppresses_signals_with_a_reason(client, engine):
    login(client, engine)
    iid = add_inst(engine, "OLD-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    end = NOW - timedelta(hours=3)
    add_bars(engine, iid, end - timedelta(minutes=89), [100.0] * 89 + [101.0])
    d = definition(iid, name="Stale", market="crypto", decision_timeframe="1m", indicators=[], short_entry=None,
                   long_entry={"type": "compare", "left": {"kind": "price", "field": "close"}, "op": "crosses_above", "right": {"kind": "const", "value": 100.5}})
    sid = post(client, "/strategies", {"definition": d}, 201).json()["id"]
    client.post(f"/api/v1/strategies/{sid}/activate", json={}, headers=h(client))
    scan_now(client, engine)
    assert client.get(f"{API}/signals").json() == []                                    # a suppressed candidate never becomes a signal
    rej = client.get(f"{API}/signals/rejections").json()
    assert len(rej) == 1 and rej[0]["reason_code"] == "stale_data" and "stale" in rej[0]["reason_detail"] and rej[0]["observed_data_time"]


def _active_breakout(client, engine, iid, name):
    d = definition(iid, name=name, market="crypto", decision_timeframe="1m", indicators=[], short_entry=None,
                   long_entry={"type": "compare", "left": {"kind": "price", "field": "close"}, "op": "crosses_above", "right": {"kind": "const", "value": 100.5}})
    sid = post(client, "/strategies", {"definition": d}, 201).json()["id"]
    client.post(f"/api/v1/strategies/{sid}/activate", json={}, headers=h(client))
    scan_now(client, engine)


def test_signal_age_is_derived_at_read_time_from_the_stored_as_of_timestamp(client, engine):
    login(client, engine)
    iid = add_inst(engine, "AGE-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    end = (NOW - timedelta(minutes=1)).replace(second=0, microsecond=0)
    add_bars(engine, iid, end - timedelta(minutes=89), [100.0] * 89 + [101.0])
    _active_breakout(client, engine, iid, "Age")
    fresh = client.get(f"{API}/signals").json()[0]["data_freshness"]
    assert fresh["state"] == "fresh" and fresh["age_minutes"] < 3
    with Session(engine) as db:
        row = db.scalars(select(IntradaySignal)).one()
        assert "age_minutes" not in row.data_freshness                      # no frozen age is stored
        old = dict(row.data_freshness)
        old["last_bar_close"] = (NOW - timedelta(days=3)).isoformat()
        row.data_freshness = {**old, "age_minutes": 1.2}                    # legacy frozen age must be ignored
        db.commit()
    aged = client.get(f"{API}/signals?status=ready").json() + client.get(f"{API}/signals?status=expired").json()
    f = aged[0]["data_freshness"]
    assert f["state"] == "stale" and f["age_minutes"] > 4000
    with Session(engine) as db:
        row = db.scalars(select(IntradaySignal)).one()
        row.data_freshness = {"stale_after_minutes": 5}                    # no as-of timestamp: unknown, never fresh
        db.commit()
    f = client.get(f"{API}/signals").json()[0]["data_freshness"]
    assert f["state"] == "unknown" and f["age_minutes"] is None


def test_live_scan_refuses_data_over_the_missing_candle_limit_with_a_reason(client, engine):
    login(client, engine)
    iid = add_inst(engine, "GAP-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    end = (NOW - timedelta(minutes=1)).replace(second=0, microsecond=0)
    add_bars(engine, iid, end - timedelta(minutes=99), [100.0] * 50)                  # 10 candles missing in a 100-minute window = 10% > 5%
    add_bars(engine, iid, end - timedelta(minutes=39), [100.0] * 39 + [101.0])
    _active_breakout(client, engine, iid, "Gap")
    assert client.get(f"{API}/signals").json() == []
    rej = client.get(f"{API}/signals/rejections").json()
    assert len(rej) == 1 and rej[0]["reason_code"] == "missing_data_limit" and "5.0% limit" in rej[0]["reason_detail"]


def test_option_spot_must_pass_the_same_quality_gate_as_the_signal_inputs(engine):
    from app.services import intraday_runner as runner
    fresh_id = add_inst(engine, "SPOT-OK", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    stale_id = add_inst(engine, "SPOT-OLD", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    empty_id = add_inst(engine, "SPOT-NONE", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    end = (NOW - timedelta(minutes=1)).replace(second=0, microsecond=0)
    add_bars(engine, fresh_id, end - timedelta(minutes=59), [100.0] * 59 + [102.0])
    add_bars(engine, stale_id, end - timedelta(hours=3, minutes=59), [100.0] * 60)
    with Session(engine) as db:
        close, reason, code = runner.spot_gate(db, fresh_id, NOW)
        assert close == 102.0 and reason is None and code is None
        close, reason, code = runner.spot_gate(db, stale_id, NOW)
        assert close is None and "stale" in reason and code == "stale_spot"
        close, reason, code = runner.spot_gate(db, empty_id, NOW)
        assert close is None and "no real" in reason and code == "no_history"


# ───────────── append-only lifecycle events, state machine, scan rejections ─────────────
ALL_STATES = ("forming", "ready", "active", "partially_realised", "target", "stopped", "invalidated", "expired", "cancelled")


def _bare_signal(status: str):
    from uuid import uuid4
    return IntradaySignal(id=uuid4(), user_id=uuid4(), strategy_id=uuid4(), strategy_version=1, instrument_id=uuid4(), market="crypto", direction="long", status=status,
                          timeframe="1m", signal_time=NOW, reference_price=Decimal("1"), indicators={}, conditions_met=[], conditions_unmet=[], rationale="r",
                          data_freshness={}, dedupe_key=f"k{uuid4()}", expires_at=NOW + timedelta(hours=1), created_at=NOW, updated_at=NOW)


def test_all_nine_states_exist_and_the_database_rejects_any_other(engine):
    from app.models.strategies import SIGNAL_STATES
    assert SIGNAL_STATES == ALL_STATES
    from sqlalchemy.exc import IntegrityError
    with Session(engine) as db:
        db.add(_bare_signal("suppressed"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_every_invalid_transition_raises_and_every_allowed_one_is_recorded(engine):
    from uuid import uuid4
    from app.services import signal_lifecycle as lc
    terminal = {"target", "stopped", "invalidated", "expired", "cancelled"}
    allowed = {("forming", "ready"), ("forming", "expired"), ("forming", "invalidated"), ("ready", "active"), ("ready", "expired"), ("ready", "invalidated"),
               ("active", "partially_realised"), ("active", "target"), ("active", "stopped"), ("active", "invalidated"), ("active", "expired"),
               ("partially_realised", "target"), ("partially_realised", "stopped"), ("partially_realised", "invalidated"), ("partially_realised", "expired")}
    allowed |= {(f, "cancelled") for f in ALL_STATES if f not in terminal}                # explicit owner action from any non-terminal state
    vid = uuid4()
    checked = 0
    for frm in ALL_STATES:
        for to in ALL_STATES:
            with Session(engine) as db:
                sig = _bare_signal(frm)
                db.add(sig)
                db.flush()
                code = "owner_cancel" if to == "cancelled" else "time_expired"
                kw = dict(reason_code=code, observed_data_time=NOW, event_time=NOW, source="test", strategy_version_id=vid)
                if (frm, to) in allowed:
                    ev = lc.transition_signal(db, sig, to, **kw)
                    db.commit()
                    assert sig.status == to and ev.from_state == frm and ev.to_state == to
                else:
                    with pytest.raises(lc.InvalidTransition):
                        lc.transition_signal(db, sig, to, **kw)
                    assert sig.status == frm
                    assert db.scalars(select(IntradaySignalEvent).where(IntradaySignalEvent.signal_id == sig.id)).all() == []   # a rejected transition leaves no event
                checked += 1
    assert checked == 81


def test_transition_requires_both_times_and_enumerated_reasons_and_blocks_direct_writes(engine):
    from uuid import uuid4
    from app.services import signal_lifecycle as lc
    with Session(engine) as db:
        sig = _bare_signal("ready")
        db.add(sig)
        db.flush()
        base = dict(event_time=NOW, source="test", strategy_version_id=uuid4())
        with pytest.raises(lc.InvalidTransition):                                           # data time is mandatory and never defaulted from event time
            lc.transition_signal(db, sig, "active", reason_code="confirmed_next_scan", observed_data_time=None, **base)
        with pytest.raises(lc.InvalidTransition):                                           # free text is not a reason code
            lc.transition_signal(db, sig, "active", reason_code="because", observed_data_time=NOW, **base)
        with pytest.raises(lc.InvalidTransition):                                           # backfill reason is migration-only
            lc.transition_signal(db, sig, "active", reason_code="backfill_genesis", observed_data_time=NOW, **base)
        sig.status = "active"                                                              # direct write bypassing the service
        with pytest.raises(ValueError, match="signal_lifecycle"):
            db.flush()


def test_event_and_rejection_rows_are_insert_only(engine):
    from uuid import uuid4
    from app.services import signal_lifecycle as lc
    with Session(engine) as db:
        sig = _bare_signal("ready")
        db.add(sig)
        db.flush()
        lc.transition_signal(db, sig, "active", reason_code="confirmed_next_scan", observed_data_time=NOW - timedelta(minutes=1), event_time=NOW, source="test", strategy_version_id=uuid4())
        lc.record_rejection(db, user_id=sig.user_id, scan_run_id=uuid4(), strategy_id=sig.strategy_id, strategy_version_id=uuid4(), instrument_id=sig.instrument_id,
                            direction=None, reason_code="no_history", recorded_at=NOW, observed_data_time=None)
        db.commit()
    for model in (IntradaySignalEvent, ScanRejection):
        with Session(engine) as db:
            row = db.scalars(select(model)).one()
            row.reason_code = "other"
            with pytest.raises(ValueError, match="append-only"):
                db.commit()
        with Session(engine) as db:
            db.delete(db.scalars(select(model)).one())
            with pytest.raises(ValueError, match="append-only"):
                db.commit()
    with Session(engine) as db:
        ev = db.scalars(select(IntradaySignalEvent)).one()
        assert ev.event_time != ev.observed_data_time                                       # processing time and data time are separate fields


def test_real_scan_events_carry_distinct_event_and_data_times_and_expiry_is_appended(client, engine):
    login(client, engine)
    iid = add_inst(engine, "EVT-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    end = (NOW - timedelta(minutes=1)).replace(second=0, microsecond=0)
    add_bars(engine, iid, end - timedelta(minutes=89), [100.0] * 89 + [101.0])
    _active_breakout(client, engine, iid, "Evt")
    sig = client.get(f"{API}/signals").json()[0]
    ev = client.get(f"{API}/signals/{sig['id']}/events").json()
    assert [(e["from_state"], e["to_state"], e["reason_code"]) for e in ev] == [(None, "ready", "signal_created")]
    assert ev[0]["observed_data_time"] == sig["signal_time"] and ev[0]["event_time"] != ev[0]["observed_data_time"] and ev[0]["source"].startswith("scan:")
    assert ev[0]["strategy_version_id"] and not ev[0]["backfilled"]
    scan_now(client, engine)                                                 # next scan: ready -> active
    ev = client.get(f"{API}/signals/{sig['id']}/events").json()
    assert [e["to_state"] for e in ev] == ["ready", "active"] and ev[1]["observed_data_time"] and ev[1]["reason_code"] == "confirmed_next_scan"
    with Session(engine) as db:                                                             # force expiry; the lazy path must append, not overwrite
        row = db.scalars(select(IntradaySignal)).one()
        row.expires_at = NOW - timedelta(minutes=5)
        db.commit()
    assert client.get(f"{API}/signals?status=expired").json()[0]["status"] == "expired"
    ev = client.get(f"{API}/signals/{sig['id']}/events").json()
    assert [e["to_state"] for e in ev] == ["ready", "active", "expired"] and ev[2]["source"] == "lazy_expiry" and ev[2]["reason_code"] == "time_expired"
    r = client.post(f"{API}/signals/{sig['id']}/cancel", json={}, headers=h(client))       # terminal: cannot be cancelled
    assert r.status_code == 409


def test_owner_cancel_appends_a_cancelled_event(client, engine):
    login(client, engine)
    iid = add_inst(engine, "CAN-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    end = (NOW - timedelta(minutes=1)).replace(second=0, microsecond=0)
    add_bars(engine, iid, end - timedelta(minutes=89), [100.0] * 89 + [101.0])
    _active_breakout(client, engine, iid, "Can")
    sig = client.get(f"{API}/signals").json()[0]
    assert client.post(f"{API}/signals/{sig['id']}/cancel", json={}).status_code in (401, 403)   # CSRF required
    out = client.post(f"{API}/signals/{sig['id']}/cancel", json={}, headers=h(client)).json()
    assert out["status"] == "cancelled"
    ev = client.get(f"{API}/signals/{sig['id']}/events").json()
    assert ev[-1]["to_state"] == "cancelled" and ev[-1]["reason_code"] == "owner_cancel" and ev[-1]["source"] == "manual"


def test_every_suppression_writes_a_rejection_and_the_summary_counts_them(client, engine):
    login(client, engine)
    empty = add_inst(engine, "NOBARS-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")          # no bars at all
    short = add_inst(engine, "SHORT-PERP", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    floored = NOW.replace(second=0, microsecond=0)
    anchor = floored - timedelta(minutes=floored.minute % 15)                                                           # 15m bucket start: 3 bars inside it never complete a 15m candle
    add_bars(engine, short, anchor, [100.0] * 3)
    for iid, name, tf in ((empty, "R1", "1m"), (short, "R2", "15m")):
        d = definition(iid, name=name, market="crypto", decision_timeframe=tf, indicators=[], short_entry=None,
                       long_entry={"type": "compare", "left": {"kind": "price", "field": "close"}, "op": ">", "right": {"kind": "const", "value": 1}})
        sid = post(client, "/strategies", {"definition": d}, 201).json()["id"]
        client.post(f"/api/v1/strategies/{sid}/activate", json={}, headers=h(client))
    scan = scan_now(client, engine)
    assert scan["rejections"]["total"] == 2 and scan["rejections"]["by_reason"] == {"insufficient_bars": 1, "no_history": 1}
    summary = client.get(f"{API}/signals/rejections/summary").json()
    assert summary["scan_run_id"] == scan["scan_run_id"] and summary["total"] == 2 and summary["strategies_with_rejections"] == 2 and summary["instruments_with_rejections"] == 2
    assert summary["by_reason"] == {"insufficient_bars": 1, "no_history": 1}
    assert client.get(f"{API}/signals?status=suppressed").json() == []                                                 # legacy filter value: nothing is a "suppressed signal"


# ───────────── meta / dashboard ─────────────
def test_meta_lists_the_full_indicator_library_timeframes_and_assumptions(client, engine):
    login(client, engine)
    m = client.get(f"{API}/meta").json()
    ids = {i["id"] for i in m["indicators"]}
    for required in ("sma", "ema", "wma", "vwma", "hma", "supertrend", "adx", "psar", "ichimoku", "donchian", "aroon", "keltner", "rsi", "stoch", "stochrsi", "macd", "cci", "roc",
                     "momentum", "willr", "mfi", "trix", "atr", "bbands", "stdev", "hist_vol", "choppiness", "vwap", "anchored_vwap", "obv", "vol_sma", "vol_ema", "vol_roc", "ad",
                     "cmf", "rvol", "prev_candle", "prev_day", "opening_range", "candle_shape", "inside_bar", "outside_bar", "engulfing", "breakout", "swing"):
        assert required in ids, required
    assert m["timeframes"] == ["1m", "3m", "5m", "10m", "15m", "30m", "60m"] and all(i["formula"] for i in m["indicators"])
    assert m["pine"]["unsupported"] and m["python"]["template"] and any(p["assumed"] for p in m["cost_profiles"])


def test_dashboard_health_and_instrument_availability(client, engine):
    login(client, engine)
    iid = add_inst(engine, "DASH")
    nse_sessions(engine, iid, ("2025-01-06",))
    post(client, "/strategies", {"definition": definition(iid)}, 201)
    health = client.get(f"{API}/health").json()
    assert health["counts"]["total"] == 1 and health["data_health"][0]["symbol"] == "DASH"
    inst = client.get(f"{API}/instruments?market=equity_in").json()
    assert inst[0]["symbol"] == "DASH" and inst[0]["bars_1m"] == 375
