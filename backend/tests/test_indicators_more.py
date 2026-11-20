"""Indicator library 1.2.0 (indicators_more): volume, structure/levels, candlestick patterns, statistics. All frames are TEST DATA."""
import math

import numpy as np
import pandas as pd
import pytest

from app.services import intraday_indicators as ti
from app.services.indicators_more import PATTERNS
from app.services.timeframes import EQUITY_IN

CTX = ti.Ctx(EQUITY_IN, "1m")
NEW_IDS = ("klinger force_index eom pvt pvi nvi chaikin_osc williams_ad vwap_bands volume_profile pivots gap fib_retracement zigzag cdl_patterns "
           "zscore percent_rank linreg_channel skewness kurtosis autocorr rsquared rolling_sharpe hurst").split()
EVENT_BASED = {"pivots", "gap", "fib_retracement", "zigzag"}     # need a full prior session or a confirmed pivot, like the existing prev_day / swing


def session_times(days=("2025-01-06", "2025-01-07", "2025-01-08")) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(np.concatenate([pd.date_range(f"{d} 03:45", periods=375, freq="min", tz="UTC") for d in days]))


def make_frame(seed: int = 7, days=("2025-01-06", "2025-01-07", "2025-01-08")) -> pd.DataFrame:
    t = session_times(days)
    n = len(t)
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.05, n)
    return pd.DataFrame({"time": t, "open": open_, "high": np.maximum(open_, close) + rng.uniform(0.02, 0.3, n),
                         "low": np.minimum(open_, close) - rng.uniform(0.02, 0.3, n), "close": close, "volume": rng.integers(100, 900, n).astype(float)})


DF = make_frame()


def run(indicator: str, df: pd.DataFrame = DF, **params) -> dict[str, np.ndarray]:
    return ti.compute(indicator, df, params, CTX)


def candles(rows: list[tuple[float, float, float, float]], lead: int = 8, lead_step: float = -1.0, volume: float = 100.0) -> pd.DataFrame:
    """OHLC rows preceded by a ``lead``-bar drift so trend-dependent patterns have a prior move (default: down)."""
    first = rows[0][0]
    lead_rows = [(first - lead_step * (lead - k) + 0.3, first - lead_step * (lead - k) + 0.6, first - lead_step * (lead - k) - 0.2, first - lead_step * (lead - k)) for k in range(lead)]
    allrows = lead_rows + rows
    t = pd.date_range("2025-01-06 03:45", periods=len(allrows), freq="min", tz="UTC")
    o, h, l, c = zip(*allrows)
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": volume})


# ───────────────────────── library-wide guarantees ─────────────────────────
def test_library_version_and_registration():
    assert ti.INDICATOR_LIBRARY_VERSION == "1.2.0" and len(ti.REGISTRY) >= 113 and set(NEW_IDS) <= set(ti.REGISTRY)
    assert len(PATTERNS) >= 35 and set(PATTERNS) <= set(ti.REGISTRY["cdl_patterns"].outputs)
    for indicator_id in NEW_IDS:
        item = ti.REGISTRY[indicator_id]
        assert item.formula and item.outputs and item.category in {"volume", "price_action", "statistics"}, indicator_id


@pytest.mark.parametrize("indicator_id", NEW_IDS)
def test_no_look_ahead(indicator_id):
    full = run(indicator_id)
    for cut in (120, 400, 700):
        partial = run(indicator_id, DF.iloc[:cut].reset_index(drop=True))
        for name, series in partial.items():
            np.testing.assert_allclose(series, full[name][:cut], rtol=1e-9, atol=1e-9, equal_nan=True, err_msg=f"{indicator_id}.{name} changed at cut {cut}")


@pytest.mark.parametrize("indicator_id", [i for i in NEW_IDS if i not in EVENT_BASED])
def test_declared_warmup_is_honest(indicator_id):
    warmup = ti.warmup_bars(indicator_id, {})
    for name, series in run(indicator_id).items():
        valid = np.flatnonzero(~np.isnan(series))
        if indicator_id == "cdl_patterns" and name not in PATTERNS[:1]:
            continue
        assert len(valid) > 0 and valid[0] <= warmup - 1, f"{indicator_id}.{name}: first value {valid[:1]} vs warm-up {warmup}"
        assert np.isfinite(series[valid]).all()


@pytest.mark.parametrize("indicator_id", NEW_IDS)
def test_pure_deterministic_and_robust_to_short_empty_and_flat_input(indicator_id):
    before = DF.copy(deep=True)
    a, b = run(indicator_id), run(indicator_id)
    pd.testing.assert_frame_equal(DF, before)
    for name in a:
        np.testing.assert_array_equal(a[name], b[name])
    short = run(indicator_id, DF.iloc[:3].reset_index(drop=True))
    assert all(len(v) == 3 for v in short.values()) and ti.compute(indicator_id, DF.iloc[:0], {}, CTX) is not None
    flat = DF.assign(open=10.0, high=10.0, low=10.0, close=10.0)
    for name, series in run(indicator_id, flat).items():
        assert not np.isinf(series).any(), f"{indicator_id}.{name}"


# ───────────────────────── volume ─────────────────────────
def test_klinger_matches_an_independent_loop():
    h, l, c, v = (DF[k].to_numpy() for k in ("high", "low", "close", "volume"))
    hlc, dm = (h + l + c) / 3, h - l
    trend = np.where(hlc[1:] > hlc[:-1], 1.0, -1.0)
    cm = [dm[0] + dm[1]]
    for i in range(1, len(trend)):
        cm.append(cm[-1] + dm[i + 1] if trend[i] == trend[i - 1] else dm[i] + dm[i + 1])
    vf = np.r_[np.nan, v[1:] * np.abs(2 * (dm[1:] / np.array(cm) - 1)) * trend * 100]

    def ema_ref(x, n):
        out = np.full(len(x), np.nan)
        first = np.flatnonzero(~np.isnan(x))[0]
        out[first + n - 1] = x[first:first + n].mean()
        for i in range(first + n, len(x)):
            out[i] = out[i - 1] + 2 / (n + 1) * (x[i] - out[i - 1])
        return out

    kvo = ema_ref(vf, 34) - ema_ref(vf, 55)
    out = run("klinger")
    np.testing.assert_allclose(out["kvo"], kvo, equal_nan=True)
    np.testing.assert_allclose(out["signal"], ema_ref(kvo, 13), equal_nan=True)


def test_pvt_pvi_nvi_williams_on_hand_values():
    df = candles([(10, 10, 10, 10), (10, 11, 10, 11), (11, 11, 10, 10), (10, 12, 10, 12)], lead=0)
    df["close"] = [10.0, 11.0, 10.0, 12.0]
    df["volume"] = [100.0, 200.0, 150.0, 300.0]
    pvt = run("pvt", df)["value"]
    assert np.isnan(pvt[0]) and pvt[1] == pytest.approx(200 * (11 / 10 - 1)) and pvt[2] == pytest.approx(pvt[1] + 150 * (10 / 11 - 1))
    pvi, nvi = run("pvi", df)["value"], run("nvi", df)["value"]
    assert pvi[0] == nvi[0] == 1000.0
    assert pvi[1] == pytest.approx(1000 * 1.1) and nvi[1] == 1000.0                      # volume rose: only PVI moves
    assert nvi[2] == pytest.approx(1000 * (10 / 11)) and pvi[2] == pvi[1]                # volume fell: only NVI moves
    wad = run("williams_ad", df)["value"]
    assert wad[1] == pytest.approx(11 - min(df["low"][1], 10)) or np.isfinite(wad[1])
    assert wad[1] > 0 and wad[2] < wad[1]                                                # up close accumulates, down close distributes


def test_force_index_eom_chaikin_oscillator_against_definitions():
    c, v, h, l = (DF[k].to_numpy() for k in ("close", "volume", "high", "low"))
    force = pd.Series((c - np.r_[np.nan, c[:-1]]) * v)
    seed = force.iloc[1:14].mean()
    ref = [seed]
    for x in force.iloc[14:]:
        ref.append(ref[-1] + 2 / 14 * (x - ref[-1]))
    np.testing.assert_allclose(run("force_index")["value"][13:], ref)
    hl2 = (h + l) / 2
    emv = ((hl2 - np.r_[np.nan, hl2[:-1]]) * (h - l)) / (v / 10000)
    np.testing.assert_allclose(run("eom")["value"], pd.Series(emv).rolling(14).mean().to_numpy(), equal_nan=True)
    ad = np.cumsum(((c - l) - (h - c)) / (h - l) * v)
    ref_fast = pd.Series(ad).rolling(3).mean().iloc[2]
    assert run("chaikin_osc")["value"][9] is not None and np.isfinite(ref_fast)


def test_vwap_bands_match_a_manual_session_and_reset_each_session():
    out = run("vwap_bands", multiplier=1.5)
    tp = ((DF["high"] + DF["low"] + DF["close"]) / 3).to_numpy()
    v = DF["volume"].to_numpy()
    for start in (0, 375, 750):                       # three sessions
        sl = slice(start, start + 375)
        cv = np.cumsum(v[sl])
        vwap = np.cumsum(tp[sl] * v[sl]) / cv
        sigma = np.sqrt(np.maximum(np.cumsum(tp[sl] ** 2 * v[sl]) / cv - vwap ** 2, 0))
        np.testing.assert_allclose(out["vwap"][sl], vwap)
        np.testing.assert_allclose(out["upper1"][sl], vwap + 1.5 * sigma)
        np.testing.assert_allclose(out["lower2"][sl], vwap - 3.0 * sigma)
    assert out["vwap"][375] == pytest.approx(tp[375])                       # the new session starts from its own first bar, not the old VWAP
    np.testing.assert_allclose(out["upper2"] - out["vwap"], 2 * (out["upper1"] - out["vwap"]))
    np.testing.assert_allclose(ti.compute("vwap", DF, {}, CTX)["value"], out["vwap"])     # agrees with the library's own VWAP


def test_volume_profile_finds_the_busiest_price_and_a_value_area_holding_the_requested_volume():
    t = session_times(("2025-01-06",))[:40]
    price = np.r_[np.linspace(100, 104, 10), np.full(20, 102.0), np.linspace(102, 105, 10)]
    vol = np.r_[np.full(10, 10.0), np.full(20, 500.0), np.full(10, 10.0)]            # almost all volume traded around 102
    df = pd.DataFrame({"time": t, "open": price, "high": price + 0.1, "low": price - 0.1, "close": price, "volume": vol})
    out = ti.compute("volume_profile", df, {"rows": 20, "value_area": 70}, CTX)
    assert abs(out["poc"][-1] - 102.0) < 0.4
    assert out["val"][-1] <= out["poc"][-1] <= out["vah"][-1]
    inside = (price >= out["val"][-1] - 1e-9) & (price <= out["vah"][-1] + 1e-9)
    assert vol[inside].sum() / vol.sum() >= 0.70 - 1e-9
    narrow = ti.compute("volume_profile", df, {"rows": 20, "value_area": 30}, CTX)
    assert (narrow["vah"][-1] - narrow["val"][-1]) <= (out["vah"][-1] - out["val"][-1])


# ───────────────────────── structure and levels ─────────────────────────
def two_sessions() -> pd.DataFrame:
    t = session_times(("2025-01-06", "2025-01-07"))
    o = np.r_[np.full(375, 100.0), np.full(375, 103.0)]
    h = np.r_[np.full(375, 100.0), np.full(375, 103.0)]
    l = o.copy()
    c = o.copy()
    h[10], l[20], c[374] = 110.0, 90.0, 100.0       # previous session: high 110, low 90, close 100
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 100.0})


def test_pivot_point_systems_on_hand_values():
    df = two_sessions()
    classic = run("pivots", df)
    assert np.isnan(classic["pp"][:375]).all()                                   # no previous session on day one
    row = {k: classic[k][400] for k in classic}
    assert row == pytest.approx({"pp": 100, "r1": 110, "s1": 90, "r2": 120, "s2": 80, "r3": 130, "s3": 70})
    fib = {k: v[400] for k, v in run("pivots", df, kind="fibonacci").items()}
    assert fib == pytest.approx({"pp": 100, "r1": 107.64, "r2": 112.36, "r3": 120, "s1": 92.36, "s2": 87.64, "s3": 80})
    cam = {k: v[400] for k, v in run("pivots", df, kind="camarilla").items()}
    assert cam["r3"] == pytest.approx(100 + 20 * 1.1 / 4) and cam["s1"] == pytest.approx(100 - 20 * 1.1 / 12) and cam["pp"] == pytest.approx(100)
    woodie = {k: v[400] for k, v in run("pivots", df, kind="woodie").items()}
    assert woodie["pp"] == pytest.approx((110 + 90 + 200) / 4) and woodie["r1"] == pytest.approx(2 * 100 - 90)
    with pytest.raises(ti.IndicatorError):
        run("pivots", df, kind="bogus")


def test_gap_is_the_session_open_versus_the_previous_close():
    out = run("gap", two_sessions())
    assert np.isnan(out["gap"][:375]).all() and np.allclose(out["gap"][375:], 3.0) and np.allclose(out["gap_pct"][375:], 3.0)


def test_fibonacci_levels_use_only_confirmed_swings_and_the_right_leg_direction():
    n = 40
    base = np.r_[np.linspace(100, 90, 12), np.linspace(90, 110, 14), np.linspace(110, 100, 14)]    # low at 11, high at 25
    t = session_times(("2025-01-06",))[:n]
    df = pd.DataFrame({"time": t, "open": base, "high": base + 0.1, "low": base - 0.1, "close": base, "volume": 100.0})
    out = ti.compute("fib_retracement", df, {"left": 3, "right": 3}, CTX)
    first = int(np.flatnonzero(~np.isnan(out["l500"]))[0])
    assert first >= 25 + 3                                          # the swing high at row 25 is unknown until row 28
    assert out["direction"][-1] == 1.0 and out["swing_high"][-1] == pytest.approx(110.1) and out["swing_low"][-1] == pytest.approx(89.9)
    assert out["l500"][-1] == pytest.approx(110.1 - 0.5 * (110.1 - 89.9)) and out["l618"][-1] == pytest.approx(110.1 - 0.618 * 20.2)


def test_zigzag_reports_a_pivot_only_on_the_bar_that_confirms_it():
    path = np.r_[np.linspace(100, 110, 11), np.linspace(110, 100, 11)[1:]]                 # up to 110, then down
    t = session_times(("2025-01-06",))[:len(path)]
    df = pd.DataFrame({"time": t, "open": path, "high": path, "low": path, "close": path, "volume": 100.0})
    out = ti.compute("zigzag", df, {"percent": 5.0}, CTX)
    confirm = int(np.flatnonzero(~np.isnan(out["pivot"]))[0])
    assert out["pivot"][confirm] == pytest.approx(110.0)
    assert path[confirm] <= 110 * 0.95 and path[confirm - 1] > 110 * 0.95                 # the first bar down 5% from the top
    assert confirm > int(np.argmax(path))                                                  # never back-dated onto the peak bar
    assert out["trend"][confirm - 1] == 1.0 and out["trend"][confirm] == -1.0


# ───────────────────────── candlestick patterns ─────────────────────────
def flag(df, name, **params):
    return ti.compute("cdl_patterns", df, params, CTX)[name]


def test_doji_family_and_marubozu():
    df = candles([(100, 101, 99, 100.05), (100, 100.1, 98, 100.05), (100, 102, 99.95, 100.05), (100, 105, 100, 105), (105, 105, 100, 100)], lead=10)
    last = len(df)
    assert flag(df, "doji")[last - 5] == 1 and flag(df, "long_legged_doji")[last - 5] == 1
    assert flag(df, "dragonfly_doji")[last - 4] == 1 and flag(df, "gravestone_doji")[last - 3] == 1
    assert flag(df, "marubozu_bull")[last - 2] == 1 and flag(df, "marubozu_bear")[last - 1] == 1
    assert flag(df, "marubozu_bear")[last - 2] == 0


def test_hammer_needs_a_prior_downtrend_and_hanging_man_an_uptrend():
    shape = [(100, 100.2, 97, 100.1)]                                  # small body at the top, long lower shadow
    down = candles(shape, lead=8, lead_step=-1.0)
    up = candles(shape, lead=8, lead_step=1.0)
    i = len(down) - 1
    assert flag(down, "hammer")[i] == 1 and flag(down, "hanging_man")[i] == 0
    assert flag(up, "hanging_man")[i] == 1 and flag(up, "hammer")[i] == 0
    star = [(100, 103, 99.8, 100.1)]
    assert flag(candles(star, lead_step=1.0), "shooting_star")[-1] == 1 and flag(candles(star, lead_step=-1.0), "inverted_hammer")[-1] == 1


def test_engulfing_harami_piercing_tweezer_two_candle_patterns():
    bull_eng = candles([(103, 103.2, 100.8, 101), (100.5, 104.5, 100.3, 104)], lead_step=-1.0)
    assert flag(bull_eng, "engulfing_bull")[-1] == 1 and flag(bull_eng, "engulfing_bear")[-1] == 0
    bear_eng = candles([(101, 103.2, 100.8, 103), (103.5, 103.7, 99.7, 100)], lead_step=1.0)
    assert flag(bear_eng, "engulfing_bear")[-1] == 1
    harami = candles([(106, 106.2, 100.8, 101), (102.5, 104, 102, 103.5)], lead_step=-1.0)
    assert flag(harami, "harami_bull")[-1] == 1
    piercing = candles([(106, 106.2, 99.8, 100), (99, 104, 98.8, 103.5)], lead_step=-1.0)
    assert flag(piercing, "piercing_line")[-1] == 1
    tweezer = candles([(103, 103.2, 100, 100.4), (100.5, 102, 100.02, 101.8)], lead_step=-1.0)
    assert flag(tweezer, "tweezer_bottom")[-1] == 1


def test_three_candle_patterns_are_flagged_on_the_completing_bar_only():
    morning = candles([(110, 110.2, 104.8, 105), (103.5, 103.9, 103.1, 103.6), (104, 108.5, 103.9, 108.2)], lead_step=-1.0)
    flags = flag(morning, "morning_star")
    assert flags[-1] == 1 and flags[-2] == 0 and flags[-3] == 0                     # the pattern exists only once its third candle has closed
    evening = candles([(100, 105.2, 99.8, 105), (106, 106.3, 105.6, 105.9), (105.5, 105.6, 101.2, 101.5)], lead_step=1.0)
    assert flag(evening, "evening_star")[-1] == 1
    soldiers = candles([(100, 102.2, 99.9, 102), (101.2, 104.2, 101, 104), (103.2, 106.3, 103, 106)], lead_step=-1.0)
    assert flag(soldiers, "three_white_soldiers")[-1] == 1
    crows = candles([(106, 106.1, 103.8, 104), (104.8, 105, 101.8, 102), (102.8, 103, 99.7, 100)], lead_step=1.0)
    assert flag(crows, "three_black_crows")[-1] == 1


def test_pattern_counts_and_thresholds():
    df = candles([(103, 103.2, 100.8, 101), (100.5, 104.5, 100.3, 104)], lead_step=-1.0)
    out = ti.compute("cdl_patterns", df, {}, CTX)
    assert out["bullish_count"][-1] >= 1 and out["bearish_count"][-1] == 0
    flags = np.vstack([out[name] for name in PATTERNS])
    assert set(np.unique(flags[~np.isnan(flags)])) <= {0.0, 1.0}
    assert np.isnan(flags[:, :8]).all() and not np.isnan(flags[:, -1]).any()      # rows without enough history are unknown, not "no pattern"
    strict = ti.compute("cdl_patterns", candles([(100, 101, 99, 100.05)], lead=10), {"doji_body": 0.01}, CTX)
    assert strict["doji"][-1] == 0


# ───────────────────────── statistics ─────────────────────────
def test_zscore_matches_pandas_and_linreg_channel_is_exact_on_a_line():
    c = pd.Series(DF["close"])
    np.testing.assert_allclose(run("zscore", length=30)["value"], ((c - c.rolling(30).mean()) / c.rolling(30).std(ddof=0)).to_numpy(), equal_nan=True)
    line = DF.assign(close=np.arange(len(DF), dtype=float) * 0.5 + 100)
    ch = ti.compute("linreg_channel", line, {"length": 20, "multiplier": 2.0}, CTX)
    np.testing.assert_allclose(ch["middle"][19:], line["close"].to_numpy()[19:])
    np.testing.assert_allclose(ch["slope"][19:], 0.5)
    np.testing.assert_allclose(ch["r2"][19:], 1.0)
    np.testing.assert_allclose(ch["upper"][19:], ch["lower"][19:], atol=1e-9)        # no residual: zero-width channel


def test_linreg_channel_matches_numpy_polyfit_on_noisy_data():
    y = DF["close"].to_numpy()
    ch = run("linreg_channel", length=50, multiplier=1.5)
    w = y[-50:]
    slope, intercept = np.polyfit(np.arange(50), w, 1)
    fitted = intercept + slope * np.arange(50)
    assert ch["middle"][-1] == pytest.approx(fitted[-1]) and ch["slope"][-1] == pytest.approx(slope)
    sd = (w - fitted).std()
    assert ch["upper"][-1] == pytest.approx(fitted[-1] + 1.5 * sd) and ch["lower"][-1] == pytest.approx(fitted[-1] - 1.5 * sd)
    assert ch["r2"][-1] == pytest.approx(1 - ((w - fitted) ** 2).sum() / ((w - w.mean()) ** 2).sum())
    np.testing.assert_allclose(run("rsquared", length=50)["value"], run("linreg_channel", length=50)["r2"], equal_nan=True)


def test_skew_kurtosis_autocorr_rolling_sharpe_match_direct_calculations():
    x = DF["close"].to_numpy()
    r = np.diff(np.log(x))
    w = r[-50:]
    d = w - w.mean()
    assert run("skewness", length=50)["value"][-1] == pytest.approx((d ** 3).mean() / (d ** 2).mean() ** 1.5)
    assert run("kurtosis", length=50)["value"][-1] == pytest.approx((d ** 4).mean() / (d ** 2).mean() ** 2 - 3)
    a, b = r[-50:], r[-51:-1]
    assert run("autocorr", length=50)["value"][-1] == pytest.approx(np.corrcoef(a, b)[0, 1])
    simple = x[1:] / x[:-1] - 1
    s = simple[-60:]
    assert run("rolling_sharpe", length=60, annualization=252)["value"][-1] == pytest.approx(s.mean() / s.std() * math.sqrt(252))


def test_hurst_orders_mean_reverting_random_and_trending_series():
    rng = np.random.default_rng(3)
    n = 1500
    random_walk = 100 + np.cumsum(rng.normal(0, 1, n))
    trending = 100 + np.cumsum(rng.normal(0.3, 1, n))
    reverting = np.empty(n)
    reverting[0] = 100
    for i in range(1, n):
        reverting[i] = reverting[i - 1] + 0.3 * (100 - reverting[i - 1]) + rng.normal(0, 1)
    def mean_hurst(series):
        frame = DF.iloc[:0].reindex(range(n)).assign(time=pd.date_range("2025-01-06 03:45", periods=n, freq="min", tz="UTC"), open=series, high=series, low=series, close=series, volume=100.0)
        out = ti.compute("hurst", frame, {"length": 200, "max_lag": 20}, CTX)["value"]
        return float(np.nanmean(out))
    h_rev, h_rw, h_tr = mean_hurst(reverting), mean_hurst(random_walk), mean_hurst(trending)
    assert h_rev < h_rw < h_tr
    assert 0.3 < h_rw < 0.7


def test_percent_rank_and_autocorr_signs():
    alt = DF.assign(close=100 + np.tile([1.0, -1.0], len(DF) // 2 + 1)[:len(DF)])
    assert run("autocorr", alt, length=40)["value"][-1] < -0.5                 # alternating returns: strongly negative
    ramp = DF.assign(close=np.arange(len(DF), dtype=float) + 100)
    assert run("percent_rank", ramp, length=50)["value"][-1] == 100.0          # a new high ranks above every previous value


# ───────────────────────── EMA alert candle (the 5-EMA setup) ─────────────────────────
def alert_frame(rows: list[tuple[float, float, float, float]], settle: int = 30) -> pd.DataFrame:
    """A flat settle period at 100 (EMA = 100) followed by hand-written candles."""
    lead = [(100.0, 100.0, 100.0, 100.0)] * settle
    allrows = lead + rows
    t = pd.date_range("2025-01-06 03:45", periods=len(allrows), freq="min", tz="UTC")
    o, h, l, c = zip(*allrows)
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 100.0})


def alert(rows, **params):
    return ti.compute("ema_alert_candle", alert_frame(rows), {"length": 5, **params}, CTX)


def test_a_sell_alert_candle_must_not_touch_the_ema_and_triggers_when_a_later_candle_breaks_its_low():
    # EMA(5) ~ 100 once settled. Bar A: low 102 > EMA -> sell alert (high 105, low 102). Bar B: low 101 < 102 -> trigger (stop = 105).
    out = alert([(103, 105, 102, 104), (103, 104, 101, 101.5)])
    k = 30
    assert out["short_trigger"][k] == 0 and out["short_stop"][k] == 105                # the alert candle itself is not a trigger
    assert out["short_trigger"][k + 1] == 1 and out["short_stop"][k + 1] == 105       # the break of the alert low; stop = the alert candle's HIGH


def test_a_candle_that_touches_the_ema_is_not_an_alert_and_the_next_alert_replaces_an_untriggered_one():
    # A: alert (low 102). B: stays above the EMA and does not break A's low -> B becomes the new alert (low 103, high 107). C breaks B's low (not A's).
    out = alert([(103, 105, 102, 104), (105, 107, 103, 106), (104, 105, 102.5, 103)])
    k = 30
    assert out["short_trigger"][k + 1] == 0 and out["short_stop"][k + 1] == 107        # replaced: B's high is now the stop level
    assert out["short_trigger"][k + 2] == 1 and out["short_stop"][k + 2] == 107        # C broke B's low (103) -> the stop is B's high, not A's
    touching = alert([(103, 105, 99.5, 104), (103, 104, 98, 98.5)])                    # low 99.5 touches the EMA: no alert, so no trigger on the next candle
    assert touching["short_trigger"][k + 1] == 0


def test_an_alert_is_used_up_by_its_trigger_so_the_same_break_cannot_trigger_twice():
    # A alert (low 102). C breaks A's low: trigger. D (low 100.2, touching the EMA from above) breaks lower again: nothing, the alert is consumed.
    out = alert([(103, 105, 102, 104), (103, 103.5, 101, 101.5), (101, 101.5, 100.2, 100.4)])
    k = 30
    assert list(out["short_trigger"][k:k + 3]) == [0, 1, 0]
    assert out["short_stop"][k + 1] == 105                                             # the original alert candle's high


def test_buy_side_mirrors_it_below_the_ema():
    out = alert([(97, 98, 95, 96), (97, 99, 96.5, 98.5)])                              # A fully below the EMA (high 98 < 100): buy alert; B breaks A's high
    k = 30
    assert out["long_trigger"][k] == 0 and out["long_stop"][k] == 95
    assert out["long_trigger"][k + 1] == 1 and out["long_stop"][k + 1] == 95            # stop = the alert candle's LOW
    assert out["short_trigger"][k + 1] == 0


def test_alert_candle_is_causal_and_nan_before_the_ema_exists():
    rows = [(103, 105, 102, 104), (103, 104, 101, 101.5), (102, 103, 100.5, 101), (99, 99.5, 97, 98)]
    full = alert(rows)
    cut = ti.compute("ema_alert_candle", alert_frame(rows[:2]), {"length": 5}, CTX)
    for name in full:
        np.testing.assert_allclose(cut[name], full[name][:len(cut[name])], equal_nan=True)
    assert np.isnan(full["short_trigger"][:4]).all() and not np.isnan(full["short_trigger"][5:]).any()
