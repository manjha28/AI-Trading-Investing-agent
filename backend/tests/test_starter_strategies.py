"""Built-in starter strategies: schema validity, parameters-in-definition, install idempotency, customization lineage, no look-ahead."""
import copy

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.strategies import Strategy, StrategyVersion
from app.services import intraday_indicators as ti
from app.services.intraday_strategy import StrategyDefinition, apply_overrides, evaluate, tunable_parameters, validate_definition
from app.services.starter_strategies import STARTERS
from app.services.timeframes import CRYPTO, EQUITY_IN
from tests.test_signals_lab_api import API, add_bars, add_inst, client, engine, h, login, post, nse_sessions  # noqa: F401  (fixtures)

KEYS = ["ema_rsi_vwap", "supertrend_ema", "vwap_reversal", "opening_range_breakout", "ema_pullback", "bollinger_reversal", "donchian_breakout", "adx_macd_momentum", "squeeze_expansion", "prev_day_level_break", "inside_bar_breakout", "choppiness_keltner_trend", "range_edge_rejection"]


def market(n_days=25, seed=7, tz_start="03:45", minutes=375):
    rng = np.random.default_rng(seed)
    frames, price = [], 100.0
    days = [d for d in pd.date_range("2025-01-06", periods=40, freq="D") if d.dayofweek < 5][:n_days]
    for d in days:
        vol = rng.choice([0.08, 0.2, 0.4])
        c = price + np.cumsum(rng.normal(rng.choice([-0.01, 0, 0.01]), vol, minutes))
        price = c[-1]
        o = np.r_[c[0], c[:-1]]
        v = rng.integers(100, 600, minutes).astype(float)
        v[rng.random(minutes) < 0.05] *= 6
        frames.append(pd.DataFrame({"time": pd.date_range(f"{d.date()} {tz_start}", periods=minutes, freq="min", tz="UTC"), "open": o,
                                    "high": np.maximum(o, c) + abs(rng.normal(0, vol / 2, minutes)), "low": np.minimum(o, c) - abs(rng.normal(0, vol / 2, minutes)), "close": c, "volume": v}))
    return pd.concat(frames, ignore_index=True)


def retimed(spec, tf):
    def re(x):
        if isinstance(x, dict):
            return {k: (tf if k == "timeframe" and v == "5m" else re(v)) for k, v in x.items()}
        return [re(i) for i in x] if isinstance(x, list) else x
    out = re(copy.deepcopy(spec))
    out["decision_timeframe"] = tf
    return StrategyDefinition.model_validate(out)


def by_key(key):
    return next(s for s in STARTERS if s["source"]["provenance"]["starter_key"] == key)


def test_catalog_has_exactly_the_thirteen_strategies_and_each_validates():
    assert [s["source"]["provenance"]["starter_key"] for s in STARTERS] == KEYS + ["nifty_options_scalper"]   # 13 equity starters + the options scalper
    for s in STARTERS[:13]:
        d = StrategyDefinition.model_validate(s)
        issues, req = validate_definition(d)
        assert req is not None, issues
        assert d.source.type == "starter" and d.source.provenance["performance"] == "not tested" and d.long_entry and d.short_entry
        assert d.universe == [] and d.constraints.allow_short and d.stop.mode == "atr" and d.target.mode == "rr"
        for decl in d.indicators:   # every parameter is a stored, validated library parameter
            ti.resolve_params(decl.indicator, decl.params)


def test_defaults_match_the_specification():
    p = lambda key: {i["alias"]: i for i in by_key(key)["indicators"]}  # noqa: E731
    a = p("ema_rsi_vwap")
    assert (a["ema_fast"]["params"]["length"], a["ema_slow"]["params"]["length"], a["rsi"]["params"]["length"], a["vol_avg"]["params"]["length"]) == (9, 21, 14, 20)
    b = p("supertrend_ema")
    assert (b["ema_fast"]["params"]["length"], b["ema_slow"]["params"]["length"]) == (20, 50)
    assert p("opening_range_breakout")["orb"]["params"]["minutes"] == 15 and p("opening_range_breakout")["orb"]["timeframe"] == "1m"
    c = p("bollinger_reversal")["bb"]["params"]
    assert (c["length"], c["multiplier"]) == (20, 2.0)
    assert p("adx_macd_momentum")["adx"]["params"]["length"] == 14 and p("adx_macd_momentum")["macd"]["params"] == {"fast": 12, "slow": 26, "signal": 9}
    # previous-channel Donchian: the compared level is shifted one candle so the current candle is excluded
    dc = by_key("donchian_breakout")["long_entry"]["children"][0]["right"]
    assert dc["ref"] == "dc" and dc["offset"] == 1


def test_thresholds_and_tolerances_live_in_the_definition_and_are_tunable():
    d = StrategyDefinition.model_validate(by_key("adx_macd_momentum"))
    keys = {t["key"] for t in tunable_parameters(d)}
    assert {"adx.length", "macd.fast", "macd.slow", "macd.signal", "ema_trend.length"} <= keys
    assert by_key("adx_macd_momentum")["long_entry"]["children"][0]["right"] == {"kind": "const", "value": 20}
    assert by_key("ema_pullback")["long_entry"]["children"][1]["right"]["right"]["right"] == {"kind": "const", "value": 0.5}   # ATR pullback tolerance
    changed = apply_overrides(d, {"adx.length": 10, "macd.fast": 8})
    assert changed.indicators[0].params["length"] == 10 and d.indicators[0].params["length"] == 14    # original untouched


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("tf", ["1m", "5m", "15m"])
def test_long_and_short_rules_fire_and_never_look_ahead_across_timeframes(key, tf):
    f = market()
    d = retimed(by_key(key), tf)
    full = evaluate(d, f, EQUITY_IN)
    if tf != "15m":   # 15m has too few events for the rarer strategies on this sample
        assert full.signals["long_entry"].sum() > 0 and full.signals["short_entry"].sum() > 0, (key, tf)
    for cut in (1500, 3777, 6001):
        part = evaluate(d, f.iloc[:cut].reset_index(drop=True), EQUITY_IN)
        for rule in ("long_entry", "short_entry"):
            assert np.array_equal(full.signals[rule][:cut], part.signals[rule]), (key, tf, rule, cut)


def test_signals_only_on_decision_candle_completion_for_every_starter():
    f = market(8)
    for s in STARTERS[:13]:
        r = evaluate(retimed(s, "15m"), f, EQUITY_IN)
        idx = np.flatnonzero(r.signals["long_entry"] | r.signals["short_entry"])
        # 15m candles complete 15 minutes after the 09:15 session open
        assert all(((i % 375) + 1) % 15 == 0 for i in idx)


def test_opening_range_uses_nse_session_and_a_configurable_crypto_session():
    f = market(3)
    d = StrategyDefinition.model_validate(by_key("opening_range_breakout"))
    r = evaluate(d, f, EQUITY_IN)
    orb = ti.compute("opening_range", f, {"minutes": 15}, ti.Ctx(EQUITY_IN, "1m"))
    assert np.isnan(orb["high"][14]) and orb["high"][15] == f["high"].iloc[:15].max()       # 09:15-09:30 IST, available only after it completes
    fired = np.flatnonzero(r.signals["long_entry"] | r.signals["short_entry"])
    assert (fired % 375 >= 15).all()
    crypto = market(3, tz_start="00:00", minutes=1440)
    default = ti.compute("opening_range", crypto, {"minutes": 60}, ti.Ctx(CRYPTO, "1m"))
    custom = ti.compute("opening_range", crypto, {"minutes": 60, "session_start": "13:30"}, ti.Ctx(CRYPTO, "1m"))
    assert default["high"][60] == crypto["high"].iloc[:60].max()                              # blank = 00:00 UTC, no Indian hours assumed
    i0 = 13 * 60 + 30
    assert np.isnan(custom["high"][i0 + 59]) and custom["high"][i0 + 60] == crypto["high"].iloc[i0:i0 + 60].max() and np.isnan(custom["high"][100])
    with pytest.raises(ti.IndicatorError):
        ti.compute("opening_range", crypto, {"session_start": "zz:zz"}, ti.Ctx(CRYPTO, "1m"))


def test_pullback_and_bollinger_semantics_on_crafted_candles():
    n = 120
    t = pd.date_range("2025-01-06 03:45", periods=n, freq="min", tz="UTC")
    c = np.concatenate([np.linspace(100, 130, 100), [128.5, 129.0, 130.5, 131.5] + [132.0] * 16])
    f = pd.DataFrame({"time": t, "open": np.r_[c[0], c[:-1]], "high": c + 0.2, "low": c - 0.2, "close": c, "volume": 500.0})
    d = retimed(by_key("ema_pullback"), "1m")
    r = evaluate(d, f, EQUITY_IN)
    assert r.signals["long_entry"][:60].sum() == 0               # nothing before EMA50 has warmed up
    bb = retimed(by_key("bollinger_reversal"), "1m")
    spike = np.concatenate([np.full(60, 100.0) + np.sin(np.arange(60)) * 0.5, [95.0, 100.2], np.full(10, 100.0)])
    g = pd.DataFrame({"time": pd.date_range("2025-01-06 03:45", periods=len(spike), freq="min", tz="UTC"), "open": np.r_[spike[0], spike[:-1]], "high": spike + 0.3, "low": spike - 0.3,
                      "close": spike, "volume": 500.0})
    rr = evaluate(bb, g, EQUITY_IN)
    assert rr.signals["long_entry"][61] and rr.signals["long_entry"].sum() == 1   # closed outside at 95, back inside at 100.2 with RSI oversold


# ───────────── install / library / customization (API) ─────────────
def starters_in_db(engine):
    with Session(engine) as db:
        return list(db.scalars(select(StrategyVersion).where(StrategyVersion.source_type == "starter")))


def test_install_is_idempotent_preserves_existing_strategies_and_reports_untested(client, engine):
    login(client, engine)
    iid = add_inst(engine, "AAA")
    mine = post(client, "/strategies", {"definition": {"schema_version": "intraday-strategy/1", "name": "My own", "market": "equity_in", "universe": [iid], "decision_timeframe": "5m",
                "indicators": [], "long_entry": {"type": "compare", "left": {"kind": "price", "field": "close", "timeframe": "5m"}, "op": ">", "right": {"kind": "const", "value": 1}}}}, 201).json()
    first = post(client, "/starters/install", {}, 200).json()
    assert first["installed"] == 14 and first["skipped"] == []
    again = post(client, "/starters/install", {}, 200).json()
    assert again["installed"] == 0
    rows = client.get(f"{API}/strategies").json()
    starter = [r for r in rows if r["origin"] == "starter"]
    assert len(starter) == 14 and len(rows) == 15
    assert all(r["tested"] is False and r["last_backtest"] is None and r["status"] == "suspended" and r["origin_label"] == "Built-in Starter Strategy" for r in starter)
    assert all(r["indicator_summary"] and r["source_type"] == "starter" and r["current_version"] == 1 for r in starter)
    assert next(r for r in rows if r["id"] == mine["id"])["status"] == "suspended" and next(r for r in rows if r["id"] == mine["id"])["current_version"] == 1
    cat = client.get(f"{API}/starters").json()
    assert len(cat["catalog"]) == 14 and all(c["installed_strategy_id"] for c in cat["catalog"]) and "crypto" not in cat["readiness"]  # crypto has its own system (/crypto-delta)


def test_install_never_overwrites_a_same_named_user_strategy(client, engine):
    login(client, engine)
    iid = add_inst(engine, "AAA")
    d = {"schema_version": "intraday-strategy/1", "name": "Starter: VWAP Reversal", "market": "equity_in", "universe": [iid], "decision_timeframe": "5m", "indicators": [],
         "long_entry": {"type": "compare", "left": {"kind": "price", "field": "close", "timeframe": "5m"}, "op": ">", "right": {"kind": "const", "value": 1}}}
    mine = post(client, "/strategies", {"definition": d}, 201).json()
    res = post(client, "/starters/install", {}, 200).json()
    assert res["installed"] == 13 and res["skipped"][0]["name"] == "Starter: VWAP Reversal"
    assert client.get(f"{API}/strategies/{mine['id']}").json()["origin"] is None
    assert post(client, "/starters/install", {}, 200).json()["installed"] == 0


def test_installs_nothing_without_any_instrument_and_does_not_crash(client, engine):
    login(client, engine)
    res = post(client, "/starters/install", {}, 200).json()
    assert res["installed"] == 0 and res["skipped"]


def test_customizing_keeps_the_original_and_records_lineage(client, engine):
    login(client, engine)
    iid = add_inst(engine, "AAA")
    post(client, "/starters/install", {}, 200)
    orig = next(r for r in client.get(f"{API}/strategies").json() if r["starter_key"] == "ema_rsi_vwap")
    before = client.get(f"{API}/strategies/{orig['id']}").json()
    # Open in Builder flow: the UI loads the stored definition as a draft and saves it as a NEW strategy
    d = copy.deepcopy(before["versions"][0]["definition"])
    d["name"] = "My EMA tweak"
    d["universe"] = [iid]
    d["indicators"][0]["params"]["length"] = 12
    d["source"] = {"type": "visual", "artifact": None, "provenance": {"derived_from": {"strategy_id": orig["id"], "version": 1, "starter_key": "ema_rsi_vwap", "name": orig["name"]}}}
    mine = post(client, "/strategies", {"definition": d}, 201).json()
    assert mine["origin"] == "customized" and mine["derived_from"]["strategy_id"] == orig["id"] and mine["tested"] is False
    assert client.get(f"{API}/strategies/{orig['id']}").json() == before                      # original unchanged
    v2 = post(client, f"/strategies/{mine['id']}/versions", {"definition": {**d, "indicators": [{**d["indicators"][0], "params": {"length": 8}}, *d["indicators"][1:]]}}, 201).json()
    assert v2["current_version"] == 2 and v2["origin"] == "customized"
    dup = post(client, f"/strategies/{orig['id']}/duplicate", {"name": "Starter copy"}, 201).json()
    assert dup["origin"] == "customized" and dup["derived_from"]["starter_key"] == "ema_rsi_vwap" and dup["source_type"] == "visual"
    assert client.get(f"{API}/strategies/{orig['id']}").json() == before
    post(client, f"/strategies/{dup['id']}/versions", {"definition": {**dup["versions"][0]["definition"], "universe": [iid]}}, 201)   # independently versionable
    assert post(client, "/validate", {"definition": d}, 200).json()["valid"]


def test_starter_backtests_on_real_stored_data_and_refuses_unavailable_crypto(client, engine):
    login(client, engine)
    iid = add_inst(engine, "BT")
    nse_sessions(engine, iid)
    post(client, "/starters/install", {}, 200)
    s = next(r for r in client.get(f"{API}/strategies").json() if r["starter_key"] == "donchian_breakout")
    r = post(client, "/backtests", {"strategy_id": s["id"], "instruments": [iid], "config": {"start": "2025-01-06T00:00:00Z", "end": "2025-01-11T00:00:00Z"}}, 202).json()
    run = client.get(f"{API}/backtests/{r['id']}").json()
    assert run["status"] == "completed" and run["result"]["dataset"]["instruments"][0]["bars"] == 375 * 5
    assert next(x for x in client.get(f"{API}/strategies").json() if x["id"] == s["id"])["tested"] is True
    crypto = add_inst(engine, "BTC", asset="crypto", country="IN", venue="DELTA", tz="UTC", cur="USD")
    assert post(client, "/backtests", {"strategy_id": s["id"], "instruments": [crypto]}).status_code == 422          # wrong market
    d = copy.deepcopy(client.get(f"{API}/strategies/{s['id']}").json()["versions"][0]["definition"])
    d.update(market="crypto", universe=[crypto], name="Crypto donchian")
    cs = post(client, "/strategies", {"definition": d}, 201).json()
    failed = client.get(f"{API}/backtests/{post(client, '/backtests', {'strategy_id': cs['id'], 'instruments': [crypto]}, 202).json()['id']}").json()
    assert failed["status"] == "failed" and "1-minute" in failed["error"]                                              # no fabricated crypto data
    assert "crypto" not in client.get(f"{API}/starters").json()["readiness"]  # not offered by the Indian system
