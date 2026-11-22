"""Chart detectors on explicit OHLCV fixtures (test data, never a runtime path): each detector has a case where it fires and a case where it does not, plus the lens maths."""
import numpy as np
import pandas as pd
import pytest

from app.services import stock_lenses as sl

CFG = sl.ChartDetectorConfig()


def frame(close, *, vol=None, spread=0.01, start="2024-01-01"):
    close = np.asarray(close, dtype=float)
    idx = pd.bdate_range(start=start, periods=len(close))
    o = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, close) * (1 + spread), "low": np.minimum(o, close) * (1 - spread), "close": close, "volume": np.full(len(close), 1000.0) if vol is None else np.asarray(vol, dtype=float)}, index=idx)


def trend(n=300, drift=0.004, noise=0.004, seed=1):
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + drift + rng.normal(0, noise, n))


UP, DOWN, FLAT = frame(trend()), frame(trend(drift=-0.004)), frame(100 + np.sin(np.linspace(0, 40, 300)) * 2)
ZIG_UP = frame(100 + np.arange(300) * 0.3 + 4 * np.sin(np.arange(300) / 3))                  # a clean staircase of higher highs and higher lows
ZIG_DOWN = frame(200 - np.arange(300) * 0.3 + 4 * np.sin(np.arange(300) / 3))


def test_trend_stack():
    assert sl.trend_stack(UP, "D", CFG)["value"] is True
    assert sl.trend_stack(DOWN, "D", CFG)["value"] is False
    assert sl.trend_stack(UP.iloc[:100], "D", CFG)["value"] is None                   # not enough candles: unknown, never false


def test_market_structure():
    assert sl.market_structure(ZIG_UP, "D", CFG)["value"] is True
    assert sl.market_structure(ZIG_DOWN, "D", CFG)["value"] is False
    assert sl.market_structure(UP.iloc[:8], "D", CFG)["value"] is None


def test_trend_strength():
    assert sl.trend_strength(UP, "D", CFG)["value"] is True
    assert sl.trend_strength(DOWN, "D", CFG)["value"] is False


def test_relative_strength_needs_a_benchmark_and_beats_it_over_both_windows():
    bench = pd.Series(100 * np.cumprod(np.full(300, 1.001)), index=UP.index)
    assert sl.relative_strength(UP, bench, CFG)["value"] is True
    assert sl.relative_strength(DOWN, bench, CFG)["value"] is False
    assert sl.relative_strength(UP, None, CFG)["value"] is None
    industry = {20: 5.0, 60: 5.0}                                                      # an industry median that is far ahead of the stock
    assert sl.relative_strength(UP, bench, CFG, industry)["value"] is False


def test_volatility_contraction():
    # three pullbacks of 12%, 8%, 4% in a base, with the volatility falling
    seg = []
    p = 100.0
    for depth in (0.12, 0.08, 0.04):
        seg += list(np.linspace(p, p * (1 - depth), 12)) + list(np.linspace(p * (1 - depth), p * 1.02, 12))
        p *= 1.02
    wild = trend(200, drift=0.0, noise=0.03, seed=3)
    f = frame(np.r_[wild, np.array(seg) * wild[-1] / 100, np.full(40, seg[-1] * wild[-1] / 100)], spread=0.002)
    assert sl.volatility_contraction(f, "D", CFG)["value"] is True
    assert sl.volatility_contraction(frame(trend(300, noise=0.03, seed=5)), "D", CFG)["value"] is False
    assert sl.volatility_contraction(UP.iloc[:60], "D", CFG)["value"] is None


def _breakout_frame(retest=False):
    c = np.r_[np.full(120, 100.0) + np.sin(np.arange(120) / 3), [103.0] * 10]
    c = np.r_[c, [108.0]]                                                                  # decisive close above the range
    v = np.r_[np.full(len(c) - 1, 1000.0), 3000.0]
    if retest:
        c = np.r_[c, [106.0, 104.0, 102.5, 102.0, 103.0, 103.2]]
        v = np.r_[v, np.full(6, 900.0)]
    return frame(c, vol=v, spread=0.004)


def test_breakout_and_retest():
    b = _breakout_frame()
    assert sl.breakout(b, CFG)["value"] is True
    assert sl.breakout(UP.iloc[:100], CFG)["value"] is None
    flat = frame(np.full(200, 100.0) + np.sin(np.arange(200) / 3))
    assert sl.breakout(flat, CFG)["value"] is False
    r = sl.breakout_retest(_breakout_frame(retest=True), CFG)
    assert r["value"] is True, r["fact"]
    assert sl.breakout_retest(flat, CFG)["value"] is False


def test_flag():
    pole = np.linspace(100, 125, 12)                                       # +25% in 12 bars
    pull = np.linspace(125, 116, 8)                                        # gives back 36% of the pole
    v = np.r_[np.full(40, 1000.0), np.full(12, 2500.0), np.full(8, 700.0)]
    f = frame(np.r_[np.full(40, 100.0), pole, pull], vol=v, spread=0.003)
    assert sl.flag_pattern(f, CFG)["value"] is True
    assert sl.flag_pattern(frame(np.full(120, 100.0) + np.sin(np.arange(120))), CFG)["value"] is False
    deep = frame(np.r_[np.full(40, 100.0), pole, np.linspace(125, 101, 8)], vol=v, spread=0.003)     # gives back almost everything: not a flag
    assert sl.flag_pattern(deep, CFG)["value"] is False


def test_accumulation_needs_delivery_data_and_unknown_when_it_is_missing():
    c = 100 + np.cumsum(np.tile([1.0, -0.4], 60))
    v = np.where(np.diff(np.r_[c[0], c]) > 0, 2000.0, 800.0)
    f = frame(c, vol=v)
    deliv = pd.Series(np.r_[np.full(100, 40.0), np.full(20, 60.0)], index=f.index)
    assert sl.accumulation(f, deliv, CFG)["value"] is True
    assert sl.accumulation(f, None, CFG)["value"] is None
    assert "unknown" in sl.accumulation(f, None, CFG)["fact"]
    sell = frame(c, vol=np.where(np.diff(np.r_[c[0], c]) > 0, 800.0, 2000.0))
    assert sl.accumulation(sell, deliv, CFG)["value"] is False


def test_distribution_heavy_down_days_and_reversal():
    c = np.full(120, 100.0) + np.arange(120) * 0.1
    v = np.full(120, 1000.0)
    for k in range(5):                                                      # five heavy-volume down days in the last 25 sessions
        c[-20 + k * 3:] -= 1.0
        v[-20 + k * 3] = 3000.0
    assert sl.distribution(frame(c, vol=v), CFG)["value"] is True
    calm = frame(100 + np.arange(120) * 0.1, vol=np.full(120, 1000.0))
    assert sl.distribution(calm, CFG)["value"] is False
    assert sl.distribution(calm.iloc[:30], CFG)["value"] is None


def test_extension_daily_and_long_term():
    c = np.r_[np.full(80, 100.0), np.linspace(100, 160, 20)]
    assert sl.extension(frame(c), "D", "swing", CFG)["value"] is True
    assert sl.extension(frame(np.full(100, 100.0) + np.sin(np.arange(100) / 5)), "D", "swing", CFG)["value"] is False
    w = frame(np.r_[np.full(240, 100.0), np.linspace(100, 160, 60)], start="2022-01-03")
    wk = w.resample("W-FRI").last().dropna()
    assert sl.extension(wk, "W", "long_term", CFG)["value"] is True                   # more than 25% over the 30-week average
    assert sl.extension(frame(np.full(20, 100.0)), "D", "long_term", CFG)["value"] is None


def test_headroom_uses_the_v2_card_and_is_unknown_without_one():
    assert sl.headroom(2.4, CFG)["value"] is True and sl.headroom(0.8, CFG)["value"] is False and sl.headroom(None, CFG)["value"] is None


def test_momentum_regime():
    assert sl.momentum_regime(UP, "D", CFG)["value"] in (True, False)
    assert sl.momentum_regime(DOWN, "D", CFG)["value"] is False
    c = 100 + np.cumsum(np.r_[np.full(60, 0.3), 0.5 * np.sin(np.arange(40))] + 0.25)
    assert sl.momentum_regime(frame(c), "D", CFG)["value"] in (True, False)
    assert sl.momentum_regime(UP.iloc[:30], "D", CFG)["value"] is None


def test_lens_maths_positive_minus_negative_over_applicable_and_unknown_is_not_counted():
    d = lambda v, p: {"id": "x", "value": v, "polarity": p, "fact": "f"}  # noqa: E731
    lens = sl.lens_from_detectors([d(True, "+"), d(True, "+"), d(False, "+"), d(True, "-"), d(None, "+")])
    assert lens["applicable"] == 4 and lens["positive"] == 2 and lens["negative"] == 1 and lens["score"] == pytest.approx((1 / 4 + 1) / 2 * 100) and lens["coverage"] == 0.8
    assert sl.lens_from_detectors([d(None, "+")])["score"] is None and sl.lens_from_detectors([d(None, "+")])["direction"] == "unknown"
    assert sl.lens_from_detectors([d(True, "-"), d(True, "-")])["direction"] == "negative"


def test_chart_lens_picks_the_frame_per_horizon_and_never_invents_a_score():
    bench = pd.Series(100 * np.cumprod(np.full(300, 1.0005)), index=UP.index)
    assert sl.chart_lens(UP, "swing", bench)["frame"] == "D"
    assert sl.chart_lens(UP, "positional", bench)["frame"] == "W"
    assert sl.chart_lens(UP, "long_term", bench)["frame"] == "W"                      # under 24 months of monthly candles: the weekly frame, labelled
    assert sl.chart_lens(None, "swing")["score"] is None
    assert sl.chart_lens(UP.iloc[:20], "swing")["score"] is None


def test_regime_lens_rules():
    up = pd.Series(100 * np.cumprod(np.full(300, 1.002)))
    down = pd.Series(100 * np.cumprod(np.full(300, 0.998)))
    assert sl.market_regime(up, 0.7)["regime"] == "uptrend"
    assert sl.market_regime(up, 0.5)["regime"] == "range"                              # breadth between 35% and 55%
    assert sl.market_regime(up, 0.3)["regime"] == "downtrend"
    assert sl.market_regime(down, 0.6)["regime"] == "downtrend"
    assert sl.market_regime(up, None)["regime"] is None and sl.market_regime(up.iloc[:50], 0.7)["regime"] is None


def test_news_events_lens_is_a_gate_and_context_with_no_score():
    lens = sl.news_events_lens(headlines=["Auditor resigns citing delays", "Company wins award"], events=[{"kind": "results", "date": "2026-11-25", "sessions_ahead": 2}], governance=None, contradictory=False, now=pd.Timestamp("2026-11-22").date())
    assert lens["score"] is None and lens["material_headlines"] == ["Auditor resigns citing delays"] and lens["events_within_3_sessions"] and lens["governance_known"] is False
