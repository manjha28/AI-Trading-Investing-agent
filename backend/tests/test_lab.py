"""Backtest lab (M3): symbol sets, batch runs through the real queue + engine, ranking, CSV. All bars below are TEST DATA."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.services import lab
from tests.test_signals_lab_api import API, add_inst, client, definition, drain_backtests, engine, h, login, nse_sessions, post  # noqa: F401  (fixtures + helpers)


@pytest.fixture(autouse=True)
def small_history_requirement(monkeypatch):
    monkeypatch.setattr(lab, "MIN_BARS_1M", 1000)   # test frames are 5 sessions (1,875 bars), not a year


WINDOW = {"start": "2025-01-06T00:00:00Z", "end": "2025-01-11T00:00:00Z", "slippage_bps": "2"}   # the test bars are from this week


def two_strategies(client, iid):
    ema = definition(iid, name="EMA cross", stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2})
    fast = definition(iid, name="EMA fast", indicators=[{"alias": "f", "indicator": "ema", "params": {"length": 3}, "timeframe": "5m"},
                                                          {"alias": "s", "indicator": "ema", "params": {"length": 8}, "timeframe": "5m"}],
                      stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2})
    return [post(client, "/strategies", {"definition": d}, 201).json()["id"] for d in (ema, fast)]


def test_universes_report_what_is_usable_and_exactly_what_was_left_out(client, engine):
    login(client, engine)
    rich, thin = add_inst(engine, "HDFCBANK"), add_inst(engine, "TCS")
    nse_sessions(engine, rich)
    nse_sessions(engine, thin, days=("2025-01-06",))                  # 375 bars: below the requirement
    body = client.get(f"{API}/lab/universes").json()
    nifty = next(p for p in body["presets"] if p["key"] == "nifty50")
    assert nifty["symbols"] == ["HDFCBANK"] and nifty["available"] == 1
    excluded = {e["symbol"]: e["reason"] for e in nifty["excluded"]}
    assert "only 375 stored 1-minute bars" in excluded["TCS"]
    assert excluded["RELIANCE"] == "not in the instrument master"      # a Nifty name that is not even an instrument here
    assert "never filled in" in body["note"] and "DISCLAIMER" not in body["note"]
    has1m = next(p for p in body["presets"] if p["key"] == "has_1m")
    assert has1m["symbols"] == ["HDFCBANK"]


def test_batch_runs_every_strategy_on_the_same_symbols_and_ranks_them_after_charges(client, engine):
    login(client, engine)
    a, b = add_inst(engine, "HDFCBANK"), add_inst(engine, "ICICIBANK")
    nse_sessions(engine, a)
    nse_sessions(engine, b)
    s1, s2 = two_strategies(client, a)
    started = client.post(f"{API}/lab/batches", json={"name": "EMA test", "strategy_ids": [s1, s2], "universe": "nifty50", "config": WINDOW}, headers=h(client))
    assert started.status_code == 202, started.text
    batch = started.json()
    assert len(batch["runs"]) == 2 and {i["symbol"] for i in batch["instruments"]} == {"HDFCBANK", "ICICIBANK"}
    drain_backtests(engine)

    view = client.get(f"{API}/lab/batches/{batch['batch_id']}?min_trades=1").json()
    assert view["finished"] and view["progress"] == {"done": 2, "total": 2} and "Hypothetical" in view["label"]
    assert len(view["rows"]) == 2 and {r["status"] for r in view["rows"]} == {"completed"}
    first, second = view["rows"]
    assert first["rank"] == 1 and second["rank"] == 2
    assert Decimal(str(first["metrics"]["profit_factor_net"])) >= Decimal(str(second["metrics"]["profit_factor_net"]))
    for row in view["rows"]:
        m = row["metrics"]
        assert Decimal(m["net_profit"]) == Decimal(m["gross_pnl_before_costs"]) - Decimal(m["total_charges"]) - Decimal(m["total_slippage"])   # costs are in the result
        assert row["symbols_tested"] == 2 and set(row["by_instrument"]) == {"HDFCBANK", "ICICIBANK"} and row["cost_profile"]
        assert row["dataset"]["instruments"][0]["bars"] == 375 * 5
    listing = client.get(f"{API}/lab/batches").json()["batches"]
    assert listing[0]["batch_id"] == batch["batch_id"] and listing[0]["name"] == "EMA test" and listing[0]["completed"] == 2

    csv_text = client.get(f"{API}/lab/batches/{batch['batch_id']}/export.csv").text
    assert csv_text.splitlines()[0].startswith("rank,strategy,status,trades") and "hypothetical backtest, not real P&L" in csv_text


def test_in_out_of_sample_batches_report_both_windows(client, engine):
    login(client, engine)
    a = add_inst(engine, "HDFCBANK")
    nse_sessions(engine, a)
    s1, _ = two_strategies(client, a)
    batch = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1], "universe": "has_1m", "kind": "is_oos", "split_ratio": 0.6, "config": WINDOW}, headers=h(client)).json()
    drain_backtests(engine)
    row = client.get(f"{API}/lab/batches/{batch['batch_id']}?min_trades=1").json()["rows"][0]
    assert row["kind"] == "is_oos" and row["in_sample"] and row["out_of_sample"] and row["split_at"]
    assert row["metrics"]["net_profit"] == row["out_of_sample"]["net_profit"]      # the ranked figure is the out-of-sample one


def test_batch_validation_ownership_and_csrf(client, engine):
    login(client, engine)
    iid = add_inst(engine, "HDFCBANK")
    nse_sessions(engine, iid)
    s1, _ = two_strategies(client, iid)
    assert client.post(f"{API}/lab/batches", json={"strategy_ids": [s1], "universe": "nifty50"}).status_code in (400, 403)      # CSRF
    none = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1]}, headers=h(client))
    assert none.status_code == 422 and none.json()["detail"]["code"] == "no_instruments"
    assert client.post(f"{API}/lab/batches", json={"strategy_ids": [], "universe": "nifty50"}, headers=h(client)).status_code == 422
    assert client.post(f"{API}/lab/batches", json={"strategy_ids": [s1] * 13, "universe": "nifty50"}, headers=h(client)).status_code == 422
    assert client.post(f"{API}/lab/batches", json={"strategy_ids": [s1], "universe": "bogus"}, headers=h(client)).status_code == 422
    assert client.post(f"{API}/lab/batches", json={"strategy_ids": [str(uuid4())], "universe": "nifty50"}, headers=h(client)).status_code == 404

    batch = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1], "universe": "nifty50", "config": WINDOW}, headers=h(client)).json()
    from fastapi.testclient import TestClient
    from app.main import app
    other = TestClient(app)
    login(other, engine, "other-lab@example.com")
    assert other.get(f"{API}/lab/batches/{batch['batch_id']}").status_code == 404                      # another user's batch is invisible
    assert other.get(f"{API}/lab/batches/{batch['batch_id']}/export.csv").status_code == 404
    assert other.get(f"{API}/lab/batches").json()["batches"] == []
    assert other.post(f"{API}/lab/batches", json={"strategy_ids": [s1], "universe": "has_1m"}, headers=h(other)).status_code == 404   # not their strategy


# ───────────────────────── pure ranking / export ─────────────────────────
def row(name, pf, trades=100, status="completed", net="1000"):
    return {"strategy": name, "status": status, "metrics": {"profit_factor_net": pf, "total_trades": trades, "net_profit": net}}


def test_rank_orders_by_net_profit_factor_and_flags_thin_or_unfinished_rows():
    rows = lab.rank([row("low", 0.8), row("best", 1.6), row("thin", 9.9, trades=5), row("running", None, status="running"), row("mid", 1.1)], min_trades=30)
    assert [r["strategy"] for r in rows[:3]] == ["best", "mid", "low"] and [r["rank"] for r in rows[:3]] == [1, 2, 3]
    thin = next(r for r in rows if r["strategy"] == "thin")
    assert thin["rank"] is None and thin["low_sample"] is True          # a 9.9 factor on 5 trades must never top the table
    assert next(r for r in rows if r["strategy"] == "running")["low_sample"] is False


def test_csv_neutralises_spreadsheet_formulas_and_labels_every_row_hypothetical():
    text = lab.batch_csv(lab.rank([row("=HYPERLINK(\"http://x\")", 1.2)], min_trades=1))
    assert "'=HYPERLINK" in text and text.count("hypothetical backtest, not real P&L") == 1


# ───────────────────────── M4: walk-forward, sweeps, comparison ─────────────────────────
DAYS = tuple(f"2025-01-{d:02d}" for d in (6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23))   # 14 test sessions
LONG_WINDOW = {"start": "2025-01-06T00:00:00Z", "end": "2025-01-25T00:00:00Z", "slippage_bps": "2"}


def test_walk_forward_batch_reports_out_of_sample_folds_and_ranks_by_test_window_net(client, engine):
    login(client, engine)
    a = add_inst(engine, "HDFCBANK")
    nse_sessions(engine, a, days=DAYS)
    s1, s2 = two_strategies(client, a)
    batch = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1, s2], "universe": "has_1m", "kind": "walk_forward", "train_days": 5, "test_days": 3,
                                                     "config": LONG_WINDOW}, headers=h(client))
    assert batch.status_code == 202, batch.text
    drain_backtests(engine)
    view = client.get(f"{API}/lab/batches/{batch.json()['batch_id']}?min_trades=1").json()
    assert view["kind"] == "walk_forward" and view["finished"]
    for row in view["rows"]:
        wf = row["walk_forward"]
        assert row["status"] == "completed" and wf["folds"] >= 2 and wf["train_days"] == 5 and wf["test_days"] == 3
        assert wf["folds_profitable"] <= wf["folds"] and "train windows only" in wf["optimised_on"]
        assert all(f["test"] for f in wf["fold_results"])
    nets = [Decimal(str(r["metrics"]["net_profit"])) for r in view["rows"] if r["rank"]]
    assert nets == sorted(nets, reverse=True)                  # ranked by out-of-sample net, best first


def test_sensitivity_batch_summarises_the_parameter_grid_and_ranks_by_the_median(client, engine):
    login(client, engine)
    a = add_inst(engine, "HDFCBANK")
    nse_sessions(engine, a, days=DAYS)
    s1, _ = two_strategies(client, a)
    batch = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1], "universe": "has_1m", "kind": "sensitivity", "steps": 1, "config": LONG_WINDOW}, headers=h(client)).json()
    drain_backtests(engine)
    row = client.get(f"{API}/lab/batches/{batch['batch_id']}?min_trades=1").json()["rows"][0]
    sweep = row["sweep"]
    assert row["status"] == "completed" and sweep["points"] >= 3 and 0 <= sweep["points_profitable"] <= sweep["points"]
    assert Decimal(sweep["worst"]) <= Decimal(sweep["median"]) <= Decimal(sweep["best"])
    assert row["metrics"]["net_profit"] == sweep["median"]       # the ranked figure is the median, not the best point
    assert "in-sample" in (sweep["window"] or "")


def test_compare_two_single_runs_side_by_side(client, engine):
    login(client, engine)
    a, b = add_inst(engine, "HDFCBANK"), add_inst(engine, "ICICIBANK")
    nse_sessions(engine, a)
    nse_sessions(engine, b)
    s1, s2 = two_strategies(client, a)
    batch = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1, s2], "universe": "has_1m", "config": WINDOW}, headers=h(client)).json()
    drain_backtests(engine)
    run_ids = [r["run_id"] for r in batch["runs"]]
    body = client.get(f"{API}/lab/compare?runs={','.join(run_ids)}").json()
    assert [c["run_id"] for c in body["columns"]] == run_ids and body["shared_symbols"] == ["HDFCBANK", "ICICIBANK"]
    assert "net_profit" in body["metric_keys"] and body["best"]["net_profit"] in run_ids and body["months"]
    for column in body["columns"]:
        assert column["metrics"]["total_charges"] is not None and set(column["by_instrument"]) == {"HDFCBANK", "ICICIBANK"}
    assert "like-for-like" in body["note"]


def test_compare_validation_and_ownership(client, engine):
    login(client, engine)
    a = add_inst(engine, "HDFCBANK")
    nse_sessions(engine, a)
    s1, s2 = two_strategies(client, a)
    batch = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1, s2], "universe": "has_1m", "config": WINDOW}, headers=h(client)).json()
    ids = [r["run_id"] for r in batch["runs"]]
    assert client.get(f"{API}/lab/compare?runs={ids[0]},{ids[1]}").status_code == 409           # still queued: not comparable yet
    drain_backtests(engine)
    assert client.get(f"{API}/lab/compare?runs={'x' * 40}").status_code == 422
    assert client.get(f"{API}/lab/compare?runs={ids[0]}{'0' * 36}").status_code == 422          # not a list of uuids
    assert client.get(f"{API}/lab/compare?runs={ids[0]}," + str(uuid4())).status_code == 404
    from fastapi.testclient import TestClient
    from app.main import app
    other = TestClient(app)
    login(other, engine, "other-compare@example.com")
    assert other.get(f"{API}/lab/compare?runs={ids[0]},{ids[1]}").status_code == 404            # another user's runs are invisible
    wf = client.post(f"{API}/lab/batches", json={"strategy_ids": [s1], "universe": "has_1m", "kind": "is_oos", "config": WINDOW}, headers=h(client)).json()
    drain_backtests(engine)
    assert client.get(f"{API}/lab/compare?runs={ids[0]},{wf['runs'][0]['run_id']}").status_code == 409   # only single runs compare


def test_rank_uses_the_right_score_for_each_kind():
    wf = [{"kind": "walk_forward", "status": "completed", "strategy": "a", "metrics": {"net_profit": "500", "total_trades": 40}},
          {"kind": "walk_forward", "status": "completed", "strategy": "b", "metrics": {"net_profit": "-100", "total_trades": 40}},
          {"kind": "walk_forward", "status": "completed", "strategy": "c", "metrics": {"net_profit": "9000", "total_trades": 3}}]
    ranked = lab.rank(wf, min_trades=30)
    assert [r["strategy"] for r in ranked[:2]] == ["a", "b"] and ranked[2]["rank"] is None and ranked[2]["low_sample"]
