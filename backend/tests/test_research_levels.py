"""Test data only: seeded synthetic candles exercise pure functions; nothing here reaches a runtime path."""
import numpy as np
import pandas as pd

from app.services import technical_engine as te
from app.services.research_confidence import buy_confidence
from app.services.research_levels import build_levels
from app.services.research_report import classify


def make(n=1500, drift=0.0006, vol=0.013, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    high, low = close * (1 + np.abs(rng.normal(0, 0.006, n))), close * (1 - np.abs(rng.normal(0, 0.006, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": open_, "high": np.maximum.reduce([high, open_, close]), "low": np.minimum.reduce([low, open_, close]), "close": close,
                         "volume": rng.integers(100_000, 500_000, n).astype(float)}, index=idx)


DF = make()


def test_atr_natr_and_wilder_smoothing():
    df = pd.DataFrame({"open": [10] * 20, "high": [12] * 20, "low": [10] * 20, "close": [11] * 20, "volume": [1.0] * 20}, index=pd.bdate_range("2026-01-01", periods=20))
    assert abs(te.atr(df, 14).iloc[-1] - 2.0) < 1e-9
    ind = te.indicators(make(300))
    assert abs(ind["natr"] - ind["atr14"] / ind["close"] * 100) < 1e-6


def test_classic_pivots():
    assert te.classic_pivots(110.0, 90.0, 100.0) == {"S2": 80.0, "S1": 90.0, "P": 100.0, "R1": 110.0, "R2": 120.0}


def test_swing_points_are_confirmed_without_lookahead():
    highs, lows = te.swing_points(DF.iloc[:400], 3)
    assert highs and lows and all(i <= 400 - 4 for i, _ in highs + lows)       # the last k candles can never be a swing


def test_weekly_and_monthly_drop_the_forming_period_and_aggregate_real_candles():
    d = DF.iloc[:203]                                                            # ends mid-week
    w = te.resample(d, "W")
    assert w.index[-1] <= d.index[-1] and w.index[-1].weekday() == 4
    last_week = d[(d.index > w.index[-1] - pd.Timedelta(days=7)) & (d.index <= w.index[-1])]
    assert w.iloc[-1]["high"] == last_week["high"].max() and w.iloc[-1]["close"] == last_week["close"].iloc[-1]
    m = te.resample(d, "M")
    assert m.index[-1] < d.index[-1] or d.index[-1] == m.index[-1]


def test_zones_cluster_by_atr_and_are_capped():
    ind = te.indicators(DF.iloc[:600])
    cfg = te.EngineConfig()
    zones = te.build_zones(DF.iloc[:600], ind, DF.iloc[:600], te.resample(DF.iloc[:600], "W"), te.resample(DF.iloc[:600], "M"), 3, cfg)
    assert zones and all(z["high"] - z["low"] <= 1.5 * ind["atr14"] + 1e-9 for z in zones)
    assert all(z["kind"] in ("support", "resistance") for z in zones) and any(len(z["families"]) >= 2 for z in zones)


def test_horizon_output_is_complete_ordered_and_tick_aligned():
    out = te.analyze(DF, DF["close"] * 0.9)
    assert out["status"] == "available"
    for h, v in out["horizons"].items():
        assert v["status"] == "available" and v["direction"] in ("STRONG_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONG_BEARISH") and v["explanation"]
        if v["entry"]:
            lo, hi, stop = (float(v["entry"]["low"]), float(v["entry"]["high"]), float(v["stop_loss"]["price"]))
            assert stop < lo <= hi
            prices = [float(t["price"]) for t in v["targets"]]
            assert prices == sorted(prices) and all(p > float(v["entry"]["planned_price"]) for p in prices) and all(t["risk_reward"] > 0 for t in v["targets"])
            assert all(round(float(x) / 0.05) * 0.05 - float(x) < 1e-6 for x in [v["entry"]["low"], v["stop_loss"]["price"]])
        assert set(v["components"]) == {"trend", "structure", "momentum"} and v["entry_status"] in ("ENTER_NOW", "WAIT_FOR_ENTRY", "NO_VALID_SETUP")


def test_missing_benchmark_means_not_shown_not_neutral():
    v = te.analyze(DF)["horizons"]["swing"]
    assert v["relative_strength"]["available"] is False and "benchmark" in v["relative_strength"]["note"]
    assert te.analyze(DF, DF["close"] * 0.9)["horizons"]["swing"]["relative_strength"]["available"] is True


def test_thin_history_is_unavailable_with_a_reason():
    out = te.analyze(DF.iloc[:60])
    assert out["horizons"]["long_term"]["status"] == "unavailable" and "completed monthly candles" in out["horizons"]["long_term"]["reason"]
    assert te.analyze(DF.iloc[0:0])["status"] == "unavailable"


def test_breakout_extension_filter_refuses_to_chase():
    ind = te.indicators(DF.iloc[:600]).copy()
    a = ind["atr14"]
    zone = {"low": ind["close"] - 2.4 * a, "high": ind["close"] - 1.8 * a, "mid": ind["close"] - 2.1 * a, "strength": 3, "families": ["swing", "pivot"], "labels": ["swing high"], "kind": "support"}
    ind.update(rvol=2.0, macd_hist=1.0, rsi14=60.0)
    scen = te.entry_scenarios("swing", ind, [zone], "STRONG_UPTREND", te.EngineConfig())
    assert scen["BREAKOUT_ENTRY"]["status"] == "INVALID" and "do not chase" in scen["BREAKOUT_ENTRY"]["reason"].lower()


def test_structural_stop_is_used_when_tight_else_atr_and_rr_uses_the_selected_entry():
    ind = te.indicators(DF.iloc[:600])
    a, c = ind["atr14"], ind["close"]
    sup = {"low": c - 1.5 * a, "high": c - 1.0 * a, "mid": c - 1.25 * a, "strength": 3, "families": ["swing", "ema"], "labels": ["EMA 20"], "kind": "support"}
    res = {"low": c + 2 * a, "high": c + 2.4 * a, "mid": c + 2.2 * a, "strength": 3, "families": ["swing", "pivot"], "labels": ["swing high"], "kind": "resistance"}
    scen = {"zone": [sup["low"], sup["high"] + 0.25 * a], "support_zone": sup}
    plan = te.stop_and_targets("swing", "PULLBACK_ENTRY", scen, "WAIT_FOR_ENTRY", ind, [sup, res], te.EngineConfig(), [c - 2 * a])
    assert plan["method"] == "STRUCTURAL" and plan["stop"] < sup["low"]
    t1 = plan["targets"][0]
    assert abs(t1["risk_reward"] - (float(t1["price"]) - plan["entry"]) / plan["risk"]) < 0.1
    assert "Resistance zone" in t1["basis"]


def test_levels_only_for_buy_and_only_with_current_data():
    eng = te.analyze(DF)
    assess = {"horizons": {"swing": {"recommendation": "BUY"}, "positional": {"recommendation": "HOLD"}, "long_term": {"recommendation": "AVOID"}}}
    out = build_levels({"status": "available"}, assess, eng)["horizons"]
    assert out["positional"]["status"] == "not_applicable" and out["long_term"]["status"] == "not_applicable"
    assert out["swing"]["status"] in ("available", "no_valid_setup")
    assert build_levels({"status": "stale", "message": "old"}, assess, eng)["horizons"]["swing"]["status"] == "stale_or_missing_data"


def test_buy_confidence_is_bounded_ordered_and_explained():
    lv = {"status": "available", "risk_atr": 1.5}
    strong = buy_confidence({"recommendation": "BUY", "score": 90, "confidence": 90}, lv, 3, 65, 4.0)
    weak = buy_confidence({"recommendation": "BUY", "score": 66, "confidence": 50}, lv, 1, 65, 4.0)
    assert 0 <= weak["score"] < strong["score"] <= 100
    assert abs(sum(p["weight"] for p in strong["parts"]) - 1) < 1e-9
    assert buy_confidence({"recommendation": "HOLD", "score": 60, "confidence": 80}, lv, 0, 65, 4.0) is None
    assert buy_confidence({"recommendation": "BUY", "score": 80, "confidence": 80}, {"status": "no_valid_setup"}, 1, 65, 4.0)["parts"][3]["value"] == 0


def test_buckets():
    buy = {"swing": "BUY", "positional": "HOLD", "long_term": "HOLD"}
    avoid = {"swing": "AVOID", "positional": "AVOID", "long_term": "AVOID"}
    none = {"swing": "INSUFFICIENT_EVIDENCE", "positional": "INSUFFICIENT_EVIDENCE", "long_term": "INSUFFICIENT_EVIDENCE"}
    assert classify(buy, False, True)[0] == "buy" and classify(buy, True, True)[0] == "buy"
    assert classify(avoid, False, True)[0] == "no_new_entry"
    assert classify(avoid, True, True)[0] == "hold" and "no new purchase" in classify(avoid, True, True)[1]
    assert classify(none, True, True)[0] == "not_enough_data" and classify(buy, False, False)[0] == "not_enough_data"


def test_backtest_replays_without_lookahead_and_reports_split():
    from app.services.technical_backtest import backtest
    df = make(1300, drift=0.0008)
    res = backtest(df, "swing", step=10, warmup=260)
    assert res["label"].startswith("Hypothetical") and set(res) >= {"all", "in_sample", "out_of_sample", "notes"}
    n_all = res["all"]["trades"]
    assert n_all == res["in_sample"]["trades"] + res["out_of_sample"]["trades"]
    # truncating the future must not change any earlier trade (no look-ahead)
    cut = backtest(df.iloc[:900], "swing", step=10, warmup=260)
    early = [t for t in res["trades"] if t["date"] < df.index[880].date().isoformat()]
    assert all(t in res["trades"] for t in cut["trades"] if t["date"] < df.index[880].date().isoformat()) or not early


def test_baseline_comparison_runs_both_modes_on_identical_terms():
    from app.services.technical_backtest import compare
    res = compare(make(1300, drift=0.0008), "swing", step=10, warmup=260)
    assert res["engine"]["mode"] == "engine" and res["baseline"]["mode"] == "baseline"
    assert set(res["out_of_sample_summary"]) >= {"trades", "win_rate", "expectancy_r"} and "same engine direction gate" in res["note"]


def test_nearest_pivots_picks_the_closest_support_below_and_resistance_above():  # test data only
    from app.services.research_levels import nearest_pivots
    pivots = {"daily": {"S1": "95.00", "P": "100.00", "R1": "108.00"}, "weekly": {"S1": "98.50", "R1": "104.00"}}
    support, resistance = nearest_pivots(pivots, "101.00")
    assert support == {"name": "daily P", "price": "100.00"} and resistance == {"name": "weekly R1", "price": "104.00"}
    assert nearest_pivots(pivots, "90.00")[0] is None and nearest_pivots(None, "90.00") == (None, None)
