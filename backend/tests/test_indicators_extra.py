"""Indicator library 1.1.0 (indicators_extra). All frames below are TEST DATA generated in this file."""
import math

import numpy as np
import pandas as pd
import pytest

from app.services import intraday_indicators as ti
from app.services.indicators_extra import _percent_rank
from app.services.timeframes import EQUITY_IN, aggregate

CTX = ti.Ctx(EQUITY_IN, "1m")
NEW_IDS = (
    "dema tema tma kama vidya zlema mcginley alma lsma t3 heikin_ashi median ao ac uo elder_ray fisher coppock kst tsi ppo pvo smi rvi "
    "connors_rsi dpo vortex cmo bop squeeze wavetrend laguerre_rsi williams_fractals bb_percent_b bb_width chandelier ulcer chaikin_vol "
    "mass_index envelope natr true_range rvi_volatility chande_kroll"
).split()


def make_frame(n: int = 500, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.4, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.05, n)
    high = np.maximum(open_, close) + rng.uniform(0.02, 0.3, n)
    low = np.minimum(open_, close) - rng.uniform(0.02, 0.3, n)
    return pd.DataFrame({"time": pd.date_range("2025-01-06 03:45", periods=n, freq="min", tz="UTC"), "open": open_, "high": high,
                         "low": low, "close": close, "volume": rng.integers(100, 900, n).astype(float)})


DF = make_frame()


def run(indicator: str, df: pd.DataFrame = DF, **params) -> dict[str, np.ndarray]:
    return ti.compute(indicator, df, params, CTX)


def ema_ref(x, n):
    """EMA seeded with the SMA of the first n values (the library's convention), written independently."""
    out = np.full(len(x), np.nan)
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = out[i - 1] + 2 / (n + 1) * (x[i] - out[i - 1])
    return out


def last_valid(a):
    return a[~np.isnan(a)][-1]


# ───────────────────────── library-wide guarantees ─────────────────────────
def test_the_catalogue_grew_and_every_new_indicator_is_registered_and_documented():
    assert tuple(int(x) for x in ti.INDICATOR_LIBRARY_VERSION.split(".")) >= (1, 1, 0)
    assert len(ti.REGISTRY) >= 89 and set(NEW_IDS) <= set(ti.REGISTRY)
    for indicator_id in NEW_IDS:
        item = ti.REGISTRY[indicator_id]
        assert item.formula and item.category in {"trend", "momentum", "volatility", "volume"} and item.outputs, indicator_id


@pytest.mark.parametrize("indicator_id", NEW_IDS)
def test_no_look_ahead_a_bar_never_changes_when_later_bars_arrive(indicator_id):
    full = run(indicator_id)
    for cut in (60, 150, 333):
        partial = run(indicator_id, DF.iloc[:cut].reset_index(drop=True))
        for name, series in partial.items():
            np.testing.assert_allclose(series, full[name][:cut], rtol=1e-9, atol=1e-9, equal_nan=True,
                                       err_msg=f"{indicator_id}.{name} changed at cut {cut}")


@pytest.mark.parametrize("indicator_id", NEW_IDS)
def test_declared_warmup_is_honest_and_outputs_are_nan_before_it_not_garbage(indicator_id):
    warmup = ti.warmup_bars(indicator_id, {})
    for name, series in run(indicator_id).items():
        valid = np.flatnonzero(~np.isnan(series))
        assert len(valid) > 0, f"{indicator_id}.{name} never produced a value"
        assert valid[0] <= warmup - 1, f"{indicator_id}.{name} first value at {valid[0]} but warm-up claims {warmup}"
        assert np.isfinite(series[valid]).all()


@pytest.mark.parametrize("indicator_id", NEW_IDS)
def test_does_not_mutate_its_input_and_is_deterministic(indicator_id):
    before = DF.copy(deep=True)
    first, second = run(indicator_id), run(indicator_id)
    pd.testing.assert_frame_equal(DF, before)
    for name in first:
        np.testing.assert_array_equal(first[name], second[name])


def test_parameters_are_validated():
    with pytest.raises(ti.IndicatorError):
        run("dema", length=0)
    with pytest.raises(ti.IndicatorError):
        run("dema", bogus=3)
    with pytest.raises(ti.IndicatorError):
        run("dema", source="nope")


def test_intraday_timeframes_are_built_from_one_minute_bars_then_indicators_run_on_the_aggregate():
    def session(day: str, seed: int) -> pd.DataFrame:   # 09:15-15:30 IST = 375 one-minute bars
        frame = make_frame(375, seed=seed)
        frame["time"] = pd.date_range(f"{day} 03:45", periods=375, freq="min", tz="UTC")
        return frame

    one_minute = pd.concat([session("2025-01-06", 3), session("2025-01-07", 4)], ignore_index=True)
    five = aggregate(one_minute, "5m", EQUITY_IN)
    assert len(five) < len(one_minute) and five["volume"].sum() == pytest.approx(one_minute["volume"].sum())
    assert five["high"].max() == pytest.approx(one_minute["high"].max()) and five["low"].min() == pytest.approx(one_minute["low"].min())
    close = five["close"].to_numpy()
    e1 = ema_ref(close, 5)
    expected = 2 * e1 - np.r_[np.full(4, np.nan), ema_ref(e1[4:], 5)]
    np.testing.assert_allclose(ti.compute("dema", five, {"length": 5}, CTX)["value"], expected, equal_nan=True)


# ───────────────────────── values against independent references ─────────────────────────
def test_dema_tema_are_the_documented_combinations_of_chained_emas():
    c = DF["close"].to_numpy()
    e1 = ema_ref(c, 10)
    e2 = np.r_[np.full(9, np.nan), ema_ref(e1[9:], 10)]
    e3 = np.r_[np.full(18, np.nan), ema_ref(e2[18:], 10)]
    np.testing.assert_allclose(run("dema", length=10)["value"], 2 * e1 - e2, equal_nan=True)
    np.testing.assert_allclose(run("tema", length=10)["value"], 3 * e1 - 3 * e2 + e3, equal_nan=True)


def test_tma_is_a_double_sma():
    c = pd.Series(DF["close"])
    # n=8: SMA over ceil(8/2)=4, then SMA over floor(8/2)+1=5
    np.testing.assert_allclose(run("tma", length=8)["value"], c.rolling(4).mean().rolling(5).mean().to_numpy(), equal_nan=True)


def test_kama_matches_a_straight_loop_and_is_flat_on_a_flat_series():
    c = DF["close"].to_numpy()
    n, fast, slow = 10, 2, 30
    out = np.full(len(c), np.nan)
    out[n - 1] = c[n - 1]
    for i in range(n, len(c)):
        er = abs(c[i] - c[i - n]) / np.abs(np.diff(c[i - n:i + 1])).sum()
        sc = (er * (2 / (fast + 1) - 2 / (slow + 1)) + 2 / (slow + 1)) ** 2
        out[i] = out[i - 1] + sc * (c[i] - out[i - 1])
    np.testing.assert_allclose(run("kama")["value"], out, equal_nan=True)
    flat = DF.copy()
    for col in ("open", "high", "low", "close"):
        flat[col] = 50.0
    assert np.nanmax(np.abs(ti.compute("kama", flat, {}, CTX)["value"] - 50.0)) == 0


def test_zlema_alma_lsma_on_known_shapes():
    ramp = DF.copy()
    ramp["close"] = np.arange(len(ramp), dtype=float)
    # linear regression of a perfect line reproduces the line, with slope == step
    lr = ti.compute("lsma", ramp, {"length": 10}, CTX)
    np.testing.assert_allclose(lr["value"][9:], ramp["close"].to_numpy()[9:])
    np.testing.assert_allclose(lr["slope"][9:], 1.0)
    # offset=1 reads the regression line one bar back
    np.testing.assert_allclose(ti.compute("lsma", ramp, {"length": 10, "offset": 1}, CTX)["value"][9:], ramp["close"].to_numpy()[9:] - 1)
    # ALMA weights sum to 1: a constant series is unchanged
    const = ramp.assign(close=7.0)
    np.testing.assert_allclose(ti.compute("alma", const, {}, CTX)["value"][8:], 7.0)
    # ZLEMA of a ramp tracks the ramp with (almost) no lag once settled
    z = ti.compute("zlema", ramp, {"length": 9}, CTX)["value"]   # odd length: floor((n-1)/2) equals the EMA's own lag
    assert abs(z[-1] - ramp["close"].iloc[-1]) < 1e-9


def test_t3_and_mcginley_follow_prices_without_overshooting_a_constant():
    const = DF.assign(close=42.0)
    for indicator_id, params in (("t3", {}), ("mcginley", {"length": 10}), ("vidya", {}), ("tema", {})):
        out = ti.compute(indicator_id, const, params, CTX)["value"]
        np.testing.assert_allclose(out[~np.isnan(out)], 42.0, atol=1e-9)


def test_heikin_ashi_recurrence_on_hand_values():
    df = pd.DataFrame({"time": pd.date_range("2025-01-06 03:45", periods=3, freq="min", tz="UTC"), "open": [10.0, 11.0, 12.0],
                       "high": [12.0, 13.0, 14.0], "low": [9.0, 10.0, 11.0], "close": [11.0, 12.0, 13.0], "volume": [1.0, 1.0, 1.0]})
    out = ti.compute("heikin_ashi", df, {}, CTX)
    assert out["close"].tolist() == [10.5, 11.5, 12.5]                    # (o+h+l+c)/4
    assert out["open"].tolist() == [10.5, 10.5, 11.0]                     # first (o+c)/2, then (prev open + prev close)/2
    assert out["high"].tolist() == [12.0, 13.0, 14.0] and out["low"].tolist() == [9.0, 10.0, 11.0]


def test_awesome_accelerator_elder_cmo_bop_dpo_against_definitions():
    h, l, o, c = (DF[k].to_numpy() for k in ("high", "low", "open", "close"))
    hl2 = pd.Series((h + l) / 2)
    ao = (hl2.rolling(5).mean() - hl2.rolling(34).mean()).to_numpy()
    np.testing.assert_allclose(run("ao")["value"], ao, equal_nan=True)
    np.testing.assert_allclose(run("ac")["value"], ao - pd.Series(ao).rolling(5).mean().to_numpy(), equal_nan=True)
    e = ema_ref(c, 13)
    np.testing.assert_allclose(run("elder_ray")["bull"], h - e, equal_nan=True)
    np.testing.assert_allclose(run("elder_ray")["bear"], l - e, equal_nan=True)
    np.testing.assert_allclose(run("bop")["value"], (c - o) / (h - l))
    d = pd.Series(c).diff()
    su, sd = d.clip(lower=0).rolling(9).sum(), (-d).clip(lower=0).rolling(9).sum()
    np.testing.assert_allclose(run("cmo")["value"], (100 * (su - sd) / (su + sd)).to_numpy(), equal_nan=True)
    np.testing.assert_allclose(run("dpo", length=20)["value"], (pd.Series(c).shift(11) - pd.Series(c).rolling(20).mean()).to_numpy(), equal_nan=True)


def test_ultimate_oscillator_vortex_ppo_pvo_against_definitions():
    h, l, c, v = (pd.Series(DF[k]) for k in ("high", "low", "close", "volume"))
    pc = c.shift(1)
    low_ref = pd.concat([l, pc], axis=1).min(axis=1, skipna=False)   # no previous close on bar 0 -> unknown, not 'ignore'
    bp, tr = c - low_ref, pd.concat([h, pc], axis=1).max(axis=1, skipna=False) - low_ref
    avg = lambda n: bp.rolling(n).sum() / tr.rolling(n).sum()  # noqa: E731
    np.testing.assert_allclose(run("uo")["value"], (100 * (4 * avg(7) + 2 * avg(14) + avg(28)) / 7).to_numpy(), equal_nan=True)
    true_range = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    np.testing.assert_allclose(run("vortex")["plus"], ((h - l.shift(1)).abs().rolling(14).sum() / true_range.rolling(14).sum()).to_numpy(), equal_nan=True)
    slow = ema_ref(c.to_numpy(), 26)
    ppo = 100 * (ema_ref(c.to_numpy(), 12) - slow) / slow
    np.testing.assert_allclose(run("ppo")["ppo"], ppo, equal_nan=True)
    slow_v = ema_ref(v.to_numpy(), 26)
    np.testing.assert_allclose(run("pvo")["pvo"], 100 * (ema_ref(v.to_numpy(), 12) - slow_v) / slow_v, equal_nan=True)


def test_bollinger_percent_b_bandwidth_envelope_natr_chandelier_ulcer():
    c = pd.Series(DF["close"])
    mid, dev = c.rolling(20).mean(), 2 * c.rolling(20).std(ddof=0)
    np.testing.assert_allclose(run("bb_percent_b")["value"], ((c - (mid - dev)) / (2 * dev)).to_numpy(), equal_nan=True)
    np.testing.assert_allclose(run("bb_width")["value"], (2 * dev / mid).to_numpy(), equal_nan=True)
    env = run("envelope", length=20, percent=5.0)
    np.testing.assert_allclose(env["upper"], (mid * 1.05).to_numpy(), equal_nan=True)
    np.testing.assert_allclose(env["lower"], (mid * 0.95).to_numpy(), equal_nan=True)
    h, l = DF["high"].to_numpy(), DF["low"].to_numpy()
    tr = run("true_range")["value"]
    atr = np.full(len(tr), np.nan)
    atr[21] = tr[:22].mean()
    for i in range(22, len(tr)):
        atr[i] = atr[i - 1] + (tr[i] - atr[i - 1]) / 22
    chand = run("chandelier")
    np.testing.assert_allclose(chand["long_stop"], pd.Series(h).rolling(22).max().to_numpy() - 3 * atr, equal_nan=True)
    np.testing.assert_allclose(chand["short_stop"], pd.Series(l).rolling(22).min().to_numpy() + 3 * atr, equal_nan=True)
    assert (run("natr")["value"][30:] > 0).all()
    dd = 100 * (c - c.rolling(14).max()) / c.rolling(14).max()
    np.testing.assert_allclose(run("ulcer")["value"], np.sqrt((dd ** 2).rolling(14).mean()).to_numpy(), equal_nan=True)


def test_true_range_first_bar_is_high_minus_low():
    tr = run("true_range")["value"]
    assert tr[0] == pytest.approx(DF["high"].iloc[0] - DF["low"].iloc[0])
    assert (tr >= (DF["high"] - DF["low"]).to_numpy() - 1e-12).all()


def test_chande_kroll_and_chaikin_and_mass_index_shapes():
    ck = run("chande_kroll")
    assert (ck["stop_long"][~np.isnan(ck["stop_long"])] < DF["high"].max()).all()
    assert np.nanmin(ck["stop_short"] - ck["stop_long"]) is not None
    e = ema_ref(DF["high"].to_numpy() - DF["low"].to_numpy(), 10)
    np.testing.assert_allclose(run("chaikin_vol")["value"], 100 * (e - np.r_[np.full(10, np.nan), e[:-10]]) / np.r_[np.full(10, np.nan), e[:-10]], equal_nan=True)
    assert (run("mass_index")["value"][45:] > 0).all()


# ───────────────────────── bounds, flags and edge cases ─────────────────────────
def test_oscillators_stay_in_their_documented_ranges():
    assert np.nanmin(run("laguerre_rsi")["value"]) >= 0 and np.nanmax(run("laguerre_rsi")["value"]) <= 100
    assert np.nanmin(run("connors_rsi")["value"]) >= 0 and np.nanmax(run("connors_rsi")["value"]) <= 100
    assert np.nanmin(run("uo")["value"]) >= 0 and np.nanmax(run("uo")["value"]) <= 100
    assert np.nanmin(run("cmo")["value"]) >= -100 and np.nanmax(run("cmo")["value"]) <= 100
    assert np.nanmin(run("bop")["value"]) >= -1 and np.nanmax(run("bop")["value"]) <= 1
    assert np.nanmin(run("rvi_volatility")["value"]) >= 0 and np.nanmax(run("rvi_volatility")["value"]) <= 100
    fisher = run("fisher")
    assert np.nanmax(np.abs(fisher["fisher"])) < 10 and np.isnan(fisher["trigger"][8])
    np.testing.assert_allclose(fisher["trigger"][10:], fisher["fisher"][9:-1])   # trigger is the previous bar's value
    smi = run("smi")["smi"]
    assert np.nanmax(np.abs(smi)) <= 100 + 1e-9


def test_percent_rank_excludes_the_current_bar():
    x = np.array([1.0, 2.0, 3.0, 4.0, 2.5])
    out = _percent_rank(x, 4)
    assert np.isnan(out[:4]).all() and out[4] == 50.0       # of [1,2,3,4], two (1 and 2) are <= 2.5


def test_williams_fractals_are_reported_only_after_they_are_complete():
    base = make_frame(30, seed=2)
    h = np.full(30, 10.0)
    h[10] = 15.0                                              # a clear fractal high at bar 10
    base["high"] = h
    base["low"] = h - 1
    base["open"] = h - 0.5
    base["close"] = h - 0.5
    out = ti.compute("williams_fractals", base, {"length": 2}, CTX)
    assert out["up"][10] == 0.0 and out["up"][11] == 0.0      # not knowable at bar 10 or 11
    assert out["up"][12] == 1.0                                # complete (and reported) at bar 12 = 10 + n
    assert np.nansum(out["up"]) == 1.0


def test_squeeze_flags_a_quiet_range_and_momentum_follows_the_trend():
    n = 120
    rng = np.random.default_rng(11)
    close = np.r_[100 + np.cumsum(rng.normal(0, 1.2, 60)), 100 + rng.normal(0, 0.01, 60)]   # volatile, then very quiet
    df = pd.DataFrame({"time": pd.date_range("2025-01-06 03:45", periods=n, freq="min", tz="UTC"), "open": close, "high": close + 0.05,
                       "low": close - 0.05, "close": close, "volume": np.full(n, 100.0)})
    sq = ti.compute("squeeze", df, {}, CTX)
    assert sq["squeeze_on"][-1] == 1.0 and sq["squeeze_off"][-1] == 0.0
    assert set(np.unique(sq["squeeze_on"][~np.isnan(sq["squeeze_on"])])) <= {0.0, 1.0}
    up = DF.assign(close=np.arange(len(DF), dtype=float) + 100)
    assert ti.compute("squeeze", up, {}, CTX)["momentum"][-1] > 0


def test_every_new_indicator_handles_short_and_empty_input_without_raising():
    for indicator_id in NEW_IDS:
        short = ti.compute(indicator_id, DF.iloc[:3].reset_index(drop=True), {}, CTX)
        assert all(len(series) == 3 for series in short.values()), indicator_id
        assert ti.compute(indicator_id, DF.iloc[:0], {}, CTX) is not None
        assert not any(math.isinf(v) for series in short.values() for v in series)


def test_a_zero_range_flat_series_never_produces_infinity():
    flat = DF.assign(open=10.0, high=10.0, low=10.0, close=10.0)
    for indicator_id in NEW_IDS:
        for series in ti.compute(indicator_id, flat, {}, CTX).values():
            assert not np.isinf(series).any(), indicator_id
