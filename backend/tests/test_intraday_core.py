"""Timeframe aggregation, 1-minute data quality, indicator references and look-ahead safety. Synthetic data here only exercises logic."""
import math

import numpy as np
import pandas as pd
import pytest

from app.services import intraday_indicators as ti
from app.services.bar_quality import clean_and_validate
from app.services.intraday_strategy import StrategyDefinition, evaluate, validate_definition
from app.services.timeframes import CRYPTO, EQUITY_IN, aggregate, align_to_base, decision_mask


def frame(start: str, minutes: int, seed: int = 1, base: float = 100.0) -> pd.DataFrame:
    t = pd.date_range(start, periods=minutes, freq="min", tz="UTC")
    rng = np.random.default_rng(seed)
    c = base + np.cumsum(rng.normal(0, 0.1, minutes))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"time": t, "open": o, "high": np.maximum(o, c) + 0.05, "low": np.minimum(o, c) - 0.05, "close": c,
                         "volume": rng.integers(100, 900, minutes).astype(float)})


def nse_day(date: str, seed: int = 1, base: float = 100.0) -> pd.DataFrame:
    return frame(f"{date} 03:45", 375, seed, base)  # 09:15 IST = 03:45 UTC


# ───────────── timeframes ─────────────
def test_one_minute_is_unchanged():
    f = nse_day("2025-01-06")
    out = aggregate(f, "1m", EQUITY_IN)
    assert len(out) == 375 and np.allclose(out["close"], f["close"]) and (out["bar_end"] == f["time"] + pd.Timedelta(minutes=1)).all()


def test_five_minute_ohlcv_is_exact():
    f = nse_day("2025-01-06")
    out = aggregate(f, "5m", EQUITY_IN)
    first = f.iloc[:5]
    row = out.iloc[0]
    assert row["time"] == f["time"].iloc[0] and row["bar_end"] == f["time"].iloc[0] + pd.Timedelta(minutes=5)
    assert row["open"] == first["open"].iloc[0] and row["close"] == first["close"].iloc[-1]
    assert row["high"] == first["high"].max() and row["low"] == first["low"].min() and row["volume"] == first["volume"].sum()
    assert len(out) == 75 and (out["minutes_present"] == 5).all()


def test_fifteen_minute_anchored_to_session_open_and_sixty_minute_truncated_close():
    f = nse_day("2025-01-06")
    m15 = aggregate(f, "15m", EQUITY_IN)
    assert len(m15) == 25
    ist = m15["time"].dt.tz_convert("Asia/Kolkata")
    assert ist.iloc[0].strftime("%H:%M") == "09:15" and ist.iloc[1].strftime("%H:%M") == "09:30"
    m60 = aggregate(f, "60m", EQUITY_IN)
    starts = m60["time"].dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M").tolist()
    assert starts == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    last = m60.iloc[-1]
    assert last["minutes_expected"] == 15 and bool(last["truncated"]) and last["bar_end"] == f["time"].iloc[-1] + pd.Timedelta(minutes=1)


def test_candles_never_cross_sessions_and_out_of_session_rows_are_dropped():
    f = pd.concat([nse_day("2025-01-06"), nse_day("2025-01-07", seed=2)], ignore_index=True)
    extra = frame("2025-01-06 10:05", 3)  # 15:35 IST: after the close
    out = aggregate(pd.concat([f, extra]).sort_values("time").reset_index(drop=True), "30m", EQUITY_IN)
    assert len(out) == 26 and (out["bar_end"] - out["time"] <= pd.Timedelta(minutes=30)).all()
    assert out["time"].dt.tz_convert("Asia/Kolkata").dt.date.nunique() == 2


def test_incomplete_trailing_candle_is_not_returned():
    f = nse_day("2025-01-06").iloc[:22]  # 09:15..09:36: the 09:30-09:45 candle is still forming
    out = aggregate(f, "15m", EQUITY_IN)
    assert len(out) == 1 and out["bar_end"].iloc[-1] == f["time"].iloc[0] + pd.Timedelta(minutes=15)


def test_missing_minutes_are_flagged_not_invented():
    f = nse_day("2025-01-06").drop(index=[2]).reset_index(drop=True)
    out = aggregate(f, "5m", EQUITY_IN)
    assert out["minutes_present"].iloc[0] == 4 and out["minutes_expected"].iloc[0] == 5 and len(out) == 75


def test_crypto_is_continuous_across_midnight_and_weekend():
    f = frame("2025-01-03 23:30", 60 * 52)  # Fri 23:30 UTC through Sun
    out = aggregate(f, "60m", CRYPTO)
    assert (out["time"].dt.minute == 0).all()
    assert out["minutes_expected"].eq(60).all() and not out["truncated"].any()
    day_sat = out[out["time"].dt.dayofweek == 5]
    assert len(day_sat) == 24  # a full Saturday of 60m candles
    m15 = aggregate(f, "15m", CRYPTO)
    assert m15["time"].iloc[0] == pd.Timestamp("2025-01-03 23:30", tz="UTC")


def test_htf_value_visible_only_after_candle_completes():
    f = nse_day("2025-01-06")
    htf = aggregate(f, "15m", EQUITY_IN)
    htf["v"] = np.arange(len(htf), dtype=float)
    aligned = align_to_base(htf[["bar_end", "v"]], ["v"], f["time"])["v"]
    assert np.isnan(aligned[:14]).all()          # minutes 0..13 close before 09:30
    assert aligned[14] == 0 and aligned[15] == 0  # the 09:15 candle completes at 09:30 = close of minute index 14
    assert aligned[29] == 1                       # 09:30 candle completes with minute index 29


def test_decision_mask_marks_candle_completion_bars():
    f = nse_day("2025-01-06")
    mask = decision_mask(f["time"], aggregate(f, "5m", EQUITY_IN))
    assert mask.sum() == 75 and mask[4] and not mask[3]


# ───────────── data quality ─────────────
NOW = pd.Timestamp("2025-03-01", tz="UTC")


def test_quality_removes_duplicates_and_invalid_rows_and_reports_gaps():
    f = nse_day("2025-01-06")
    f = pd.concat([f, f.iloc[[10]]]).sort_values("time").reset_index(drop=True)
    bad = f.index[20]
    f.loc[bad, "high"] = f.loc[bad, "low"] - 1.0   # high below low
    f = f.drop(index=[30, 31, 32]).reset_index(drop=True)
    clean, rep = clean_and_validate(f, EQUITY_IN, now=NOW)
    assert rep["duplicates"] == 1 and rep["invalid_ohlc_or_volume"] == 1
    assert rep["missing_minutes"] == 4 and rep["status"] == "warn" or rep["status"] == "ok"
    assert clean["time"].is_unique and len(clean) == 375 - 4


def test_quality_refuses_when_too_much_is_missing_and_never_fills():
    f = nse_day("2025-01-06")
    f = f.iloc[::3].reset_index(drop=True)
    clean, rep = clean_and_validate(f, EQUITY_IN, now=NOW)
    assert rep["status"] == "refuse" and rep["missing_pct"] > 60 and len(clean) == len(f)


def test_quality_flags_out_of_session_and_empty_weekdays_and_crypto_gaps():
    f = pd.concat([nse_day("2025-01-06"), nse_day("2025-01-08")], ignore_index=True)  # Jan 7 absent; 2025 has no NSE calendar coverage
    _, rep = clean_and_validate(f, EQUITY_IN, now=NOW)
    assert rep["calendar_unknown_days"] == ["2025-01-07"] and rep["empty_weekdays"] == []   # unknown year: neither missing nor a confirmed holiday
    assert rep["missing_minutes"] == 0 and any("without NSE calendar coverage" in i for i in rep["issues"])
    c = frame("2025-01-01 00:00", 120).drop(index=range(50, 60)).reset_index(drop=True)
    _, repc = clean_and_validate(c, CRYPTO, now=NOW)
    assert repc["missing_minutes"] == 10 and repc["gaps"][0]["missing_minutes"] == 10


def test_quality_empty():
    _, rep = clean_and_validate(pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]), EQUITY_IN, now=NOW)
    assert rep["status"] == "refuse"


# ───────────── indicators vs references ─────────────
# StockCharts RSI worked example (14-period Wilder RSI: 70.53, 66.32, 66.55, 69.41 ...)
CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439]


def ohlcv(closes, spread=0.5):
    c = np.array(closes, dtype=float)
    t = pd.date_range("2025-01-06 03:45", periods=len(c), freq="min", tz="UTC")
    return pd.DataFrame({"time": t, "open": c, "high": c + spread, "low": c - spread, "close": c, "volume": np.full(len(c), 1000.0)})


def run(ind, df, **params):
    return ti.compute(ind, df, params, ti.Ctx(EQUITY_IN, "1m"))


def test_rsi_matches_published_wilder_example():
    out = run("rsi", ohlcv(CLOSES), length=14)["value"]
    assert np.isnan(out[:14]).all()
    assert out[14] == pytest.approx(70.53, abs=0.05) and out[15] == pytest.approx(66.32, abs=0.05) and out[16] == pytest.approx(66.55, abs=0.05) and out[17] == pytest.approx(69.41, abs=0.05)


def test_sma_ema_wma_hand_values():
    df = ohlcv([1, 2, 3, 4, 5, 6])
    assert run("sma", df, length=3)["value"][2:].tolist() == [2, 3, 4, 5]
    ema = run("ema", df, length=3)["value"]       # seed = SMA(1,2,3) = 2 ; alpha = 0.5
    assert ema[2] == 2 and ema[3] == 3 and ema[4] == 4 and np.isnan(ema[:2]).all()
    assert run("wma", df, length=3)["value"][2] == pytest.approx((1 * 1 + 2 * 2 + 3 * 3) / 6)


def test_atr_wilder_hand_calculation():
    h = np.array([10, 11, 12, 11.5, 12.5], dtype=float)
    l = np.array([9, 9.5, 10.5, 10.5, 11], dtype=float)
    c = np.array([9.5, 10.5, 11.5, 11, 12], dtype=float)
    df = ohlcv(c)
    df["high"], df["low"] = h, l
    tr = [1.0, max(1.5, 1.5, 0), max(1.5, 1.5, 0.5), max(1.0, 0.0, 1.0), max(1.5, 1.5, 0)]
    atr = run("atr", df, length=3)["value"]
    seed = np.mean(tr[:3])
    assert atr[2] == pytest.approx(seed)
    assert atr[3] == pytest.approx(seed + (tr[3] - seed) / 3)


def test_bollinger_and_stdev_use_population_deviation():
    df = ohlcv([2, 4, 4, 4, 5, 5, 7, 9])
    out = run("bbands", df, length=8, multiplier=2.0)
    assert out["middle"][7] == 5 and out["upper"][7] == pytest.approx(5 + 2 * 2.0)  # population σ = 2


def test_donchian_obv_vwap_hand_values():
    df = ohlcv([10, 11, 12, 11, 13])
    d = run("donchian", df, length=3)
    assert d["upper"][2] == 12.5 and d["lower"][2] == 9.5
    assert run("obv", df)["value"].tolist() == [0, 1000, 2000, 1000, 2000]
    v = run("vwap", df)["value"]
    assert v[0] == pytest.approx(10.0) and v[1] == pytest.approx(10.5)


def test_stochastic_williams_roc_momentum_cci_reference():
    df = ohlcv([10, 12, 11, 13, 12, 14])
    st = run("stoch", df, k_length=3, k_smooth=1, d_length=1)["k"]
    hh, ll = 13.5, 10.5  # window 1..3: highs 12.5,11.5,13.5 / lows 9.5,... -> recompute explicitly
    win = df.iloc[1:4]
    assert st[3] == pytest.approx(100 * (win["close"].iloc[-1] - win["low"].min()) / (win["high"].max() - win["low"].min()))
    assert run("willr", df, length=3)["value"][3] == pytest.approx(-100 * (win["high"].max() - win["close"].iloc[-1]) / (win["high"].max() - win["low"].min()))
    assert run("roc", df, length=2)["value"][2] == pytest.approx(100 * (11 / 10 - 1))
    assert run("momentum", df, length=2)["value"][3] == 1
    tp = (df["high"] + df["low"] + df["close"]) / 3
    w = tp.iloc[1:4]
    expect = (w.iloc[-1] - w.mean()) / (0.015 * (w - w.mean()).abs().mean())
    assert run("cci", df, length=3)["value"][3] == pytest.approx(expect)


def test_macd_against_independent_scalar_implementation():
    closes = list(np.round(100 + np.cumsum(np.random.default_rng(3).normal(0, 1, 120)), 4))

    def ema_ref(x, n):
        out, a = [None] * len(x), 2 / (n + 1)
        out[n - 1] = sum(x[:n]) / n
        for i in range(n, len(x)):
            out[i] = out[i - 1] + a * (x[i] - out[i - 1])
        return out

    fast, slow = ema_ref(closes, 12), ema_ref(closes, 26)
    macd = [None if f is None or s is None else f - s for f, s in zip(fast, slow)]
    valid = [m for m in macd if m is not None]
    sig = ema_ref(valid, 9)
    out = run("macd", ohlcv(closes))
    k = len(closes) - 1
    assert out["macd"][k] == pytest.approx(macd[k]) and out["signal"][k] == pytest.approx(sig[-1]) and out["hist"][k] == pytest.approx(macd[k] - sig[-1])


def test_adx_supertrend_psar_aroon_keltner_ichimoku_are_well_formed():
    f = nse_day("2025-01-06")
    for name in ("adx", "supertrend", "psar", "aroon", "keltner", "ichimoku", "stochrsi", "mfi", "trix", "choppiness", "cmf", "hma", "vwma", "hist_vol"):
        out = ti.compute(name, f, {}, ti.Ctx(EQUITY_IN, "1m"))
        for key, arr in out.items():
            assert len(arr) == len(f) and np.isfinite(arr[~np.isnan(arr)]).all(), (name, key)
            assert (~np.isnan(arr)).any(), (name, key)
    adx = ti.compute("adx", f, {}, ti.Ctx(EQUITY_IN, "1m"))
    assert 0 <= np.nanmin(adx["adx"]) and np.nanmax(adx["adx"]) <= 100
    ar = ti.compute("aroon", f, {}, ti.Ctx(EQUITY_IN, "1m"))
    assert 0 <= np.nanmin(ar["up"]) and np.nanmax(ar["up"]) <= 100
    st = ti.compute("supertrend", f, {}, ti.Ctx(EQUITY_IN, "1m"))
    assert set(np.unique(st["direction"][~np.isnan(st["direction"])])) <= {-1.0, 1.0}


def test_price_action_indicators():
    df = ohlcv([10, 11, 10.5, 12])
    df["open"] = [10, 10.4, 11, 10.2]
    df["high"] = [11, 11.5, 11.2, 12.5]
    df["low"] = [9.5, 10.2, 10.4, 10.0]
    assert run("inside_bar", df)["value"][2] == 1 and run("outside_bar", df)["value"][3] == 1
    shape = run("candle_shape", df)
    assert shape["body_ratio"][0] == pytest.approx(0.0 / 1.5)
    prev = run("prev_candle", df)
    assert prev["high"][1] == 11 and math.isnan(prev["high"][0])
    eng = run("engulfing", df)
    assert eng["bearish"].shape == (4,)
    br = run("breakout", df, length=2)
    assert br["high"][2] == 11.5 and br["up"][3] == 1


def test_swing_is_confirmed_only_after_right_bars():
    h = [1, 2, 5, 3, 2, 1, 2, 3]
    df = ohlcv([1] * 8)
    df["high"] = [float(x) for x in h]
    df["low"] = [float(x) - 0.5 for x in h]
    sw = run("swing", df, left=2, right=2)["high"]
    assert np.isnan(sw[:4]).all() and sw[4] == 5  # the pivot at index 2 is reported only once 2 later bars exist (index 4)


def test_prev_day_and_opening_range():
    d1, d2 = nse_day("2025-01-06"), nse_day("2025-01-07", seed=2)
    f = pd.concat([d1, d2], ignore_index=True)
    pdh = run("prev_day", f)["high"]
    assert np.isnan(pdh[:375]).all() and pdh[375] == d1["high"].max()
    orr = run("opening_range", f, minutes=15)
    assert np.isnan(orr["high"][14]) and orr["high"][15] == d1["high"].iloc[:15].max() and orr["low"][400] == d2["low"].iloc[:15].min()


def test_insufficient_data_returns_nan_not_estimates():
    df = ohlcv([1, 2, 3])
    assert np.isnan(run("sma", df, length=20)["value"]).all() and np.isnan(run("rsi", df)["value"]).all()
    assert run("sma", df.iloc[:0], length=3)["value"].size == 0


def test_unknown_indicator_and_bad_params_rejected():
    with pytest.raises(ti.IndicatorError):
        ti.resolve_params("sma", {"length": 0})
    with pytest.raises(ti.IndicatorError):
        ti.resolve_params("sma", {"bogus": 1})
    with pytest.raises(ti.IndicatorError):
        ti.resolve_params("macd", {"fast": 30, "slow": 20})
    with pytest.raises(ti.IndicatorError):
        ti.resolve_params("nope", {})


def test_warmup_matches_first_valid_row():
    f = nse_day("2025-01-06")
    for name in ("sma", "ema", "wma", "hma", "rsi", "atr", "adx", "cci", "roc", "momentum", "willr", "mfi", "trix", "bbands", "stdev", "donchian", "keltner",
                 "cmf", "rvol", "vol_roc", "hist_vol", "choppiness", "aroon", "breakout", "supertrend"):
        out = ti.compute(name, f, {}, ti.Ctx(EQUITY_IN, "1m"))
        main = ti.REGISTRY[name].outputs[0]
        first = int(np.argmax(~np.isnan(out[main])))
        assert first == ti.warmup_bars(name) - 1, (name, first, ti.warmup_bars(name))


def test_indicators_are_causal_prefix_invariant():
    """A value at row t must not change when later rows are removed (no future data in any indicator)."""
    f = pd.concat([nse_day("2025-01-06"), nse_day("2025-01-07", seed=2)], ignore_index=True)
    cut = 500
    for name in ti.REGISTRY:
        params = {"anchor": str(f["time"].iloc[3])} if name == "anchored_vwap" else {}
        full = ti.compute(name, f, params, ti.Ctx(EQUITY_IN, "1m"))
        part = ti.compute(name, f.iloc[:cut], params, ti.Ctx(EQUITY_IN, "1m"))
        for key in full:
            a, b = full[key][:cut], part[key]
            assert np.allclose(a, b, equal_nan=True, rtol=1e-9, atol=1e-9), (name, key)


# ───────────── strategy evaluation: multi-timeframe, no look-ahead ─────────────
def mtf_definition(**over):
    d = {"name": "mtf", "decision_timeframe": "5m", "constraints": {"allow_short": True},
         "indicators": [{"alias": "f", "indicator": "ema", "params": {"length": 5}, "timeframe": "5m"},
                        {"alias": "s", "indicator": "ema", "params": {"length": 12}, "timeframe": "5m"},
                        {"alias": "t15", "indicator": "ema", "params": {"length": 6}, "timeframe": "15m"},
                        {"alias": "r1", "indicator": "rsi", "params": {"length": 14}, "timeframe": "1m"}],
         "long_entry": {"type": "group", "op": "and", "children": [
             {"type": "compare", "left": {"kind": "indicator", "ref": "f"}, "op": "crosses_above", "right": {"kind": "indicator", "ref": "s"}},
             {"type": "compare", "left": {"kind": "price", "field": "close", "timeframe": "15m"}, "op": ">", "right": {"kind": "indicator", "ref": "t15"}}]},
         "short_entry": {"type": "compare", "left": {"kind": "indicator", "ref": "f"}, "op": "crosses_below", "right": {"kind": "indicator", "ref": "s"}}}
    d.update(over)
    return StrategyDefinition.model_validate(d)


def test_multi_timeframe_evaluation_has_no_lookahead():
    days = [nse_day(f"2025-01-{d:02d}", seed=d, base=100 + d) for d in (6, 7, 8)]
    f = pd.concat(days, ignore_index=True)
    defn = mtf_definition()
    full = evaluate(defn, f, EQUITY_IN)
    assert full.signals["long_entry"].sum() + full.signals["short_entry"].sum() > 0
    for cut in (260, 411, 700, 905):
        part = evaluate(defn, f.iloc[:cut].reset_index(drop=True), EQUITY_IN)
        for rule in ("long_entry", "short_entry"):
            assert np.array_equal(full.signals[rule][:cut], part.signals[rule]), (rule, cut)


def test_conditions_only_fire_on_decision_candle_completion():
    f = nse_day("2025-01-06")
    r = evaluate(mtf_definition(), f, EQUITY_IN)
    idx = np.flatnonzero(r.signals["long_entry"] | r.signals["short_entry"])
    assert all((i + 1) % 5 == 0 for i in idx)  # 5m candles complete on minute 5, 10, ...


def test_validation_rejects_bad_definitions():
    bad_alias = mtf_definition(long_entry={"type": "compare", "left": {"kind": "indicator", "ref": "nope"}, "op": ">", "right": {"kind": "const", "value": 1}})
    issues, req = validate_definition(bad_alias)
    assert req is None and any("unknown indicator alias" in i["message"] for i in issues)
    no_entry = StrategyDefinition.model_validate({"name": "x"})
    assert validate_definition(no_entry)[1] is None
    bad_tf = mtf_definition(decision_timeframe="7m")
    assert validate_definition(bad_tf)[1] is None
    bad_out = mtf_definition(long_entry={"type": "compare", "left": {"kind": "indicator", "ref": "f", "output": "zzz"}, "op": ">", "right": {"kind": "const", "value": 1}})
    assert validate_definition(bad_out)[1] is None


def test_session_time_window_and_offsets():
    f = nse_day("2025-01-06")
    defn = StrategyDefinition.model_validate({
        "name": "tw", "decision_timeframe": "1m", "indicators": [],
        "long_entry": {"type": "group", "op": "and", "children": [
            {"type": "time_window", "start": "10:00", "end": "10:30"},
            {"type": "compare", "left": {"kind": "price", "field": "close"}, "op": ">", "right": {"kind": "price", "field": "close", "offset": 1}}]}})
    r = evaluate(defn, f, EQUITY_IN)
    ist = (f["time"] + pd.Timedelta(minutes=1)).dt.tz_convert("Asia/Kolkata")
    mod = (ist.dt.hour * 60 + ist.dt.minute).to_numpy()
    fired = np.flatnonzero(r.signals["long_entry"])
    assert len(fired) > 0 and ((mod[fired] >= 600) & (mod[fired] < 630)).all()
    prev = f["close"].shift(1).to_numpy()
    assert (f["close"].to_numpy()[fired] > prev[fired]).all()
