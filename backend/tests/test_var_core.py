"""VAR core calculations. ALL DATA HERE IS TEST FIXTURE DATA and never flows into runtime paths."""
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from app.services.var import orderflow as of
from app.services.var.config import DeltaCfg, VarConfig
from app.services.var.profile import VolumeProfile, value_area
from app.services.var.structure import atr_series, classify_regime, session_close, vwap_slope
from app.services.var.tpo import initial_balance, tpo_profile
from app.services.var.vtypes import IST, Bar, BarFlow, DataLevel, DeltaQuality, Quote, Side, Trade

T = D("0.05")


def ts(h, m, s=0):
    return datetime(2026, 3, 2, h, 0, 0, tzinfo=IST) + timedelta(minutes=m, seconds=s)


def bar(h, m, o, hi, lo, c, v=1000, flow=None):
    return Bar(ts(h, m), D(str(o)), D(str(hi)), D(str(lo)), D(str(c)), D(v), flow)


# ---------------------------------------------------------------- volume profile
def _prof():
    p = VolumeProfile(T, level=DataLevel.A_TRADE)
    for px, q in [("100.00", 10), ("100.05", 30), ("100.10", 50), ("100.15", 20), ("100.20", 5)]:
        p.add(D(px), D(q))
    return p


def test_poc_val_vah_and_tick_alignment():
    p = _prof()
    s = p.stats(70)
    assert p.price(s.poc) == D("100.10")
    assert (p.price(s.val), p.price(s.vah)) == (D("100.05"), D("100.15"))
    assert s.included >= s.total * D("0.70")


def test_volume_conservation_and_range_spread():
    p = VolumeProfile(T)
    p.add_range(D("100.00"), D("100.30"), D("1000"))
    assert p.total == D("1000") and p.approximate


def test_tie_break_is_deterministic_closest_to_mean_then_lower():
    p = VolumeProfile(T)
    for px in ("100.00", "100.10", "100.20"):
        p.add(D(px), D(10))
    p.add(D("100.05"), D(1))
    assert p.price(p.stats(70).poc) == D("100.10")   # tied levels; 100.10 is closest to the weighted mean
    q = VolumeProfile(T)
    q.add(D("100.00"), D(10)); q.add(D("100.10"), D(10))
    assert q.price(q.stats(70).poc) == D("100.00")   # equal distance -> lower price


def test_value_area_expands_to_greater_adjacent_side():
    w = {0: D(1), 1: D(10), 2: D(50), 3: D(2), 4: D(1)}
    assert value_area(w, 70)[:2] == (2, 2)   # POC alone holds 78% of volume
    assert value_area(w, 90)[:2] == (1, 2)   # POC=2, below (10) beats above (2)


def test_merge_rejects_different_tick():
    with pytest.raises(ValueError):
        VolumeProfile(T).merged(VolumeProfile(D("0.1")))


def test_nodes_find_hvns_and_lvn_between():
    p = VolumeProfile(T)
    shape = [1, 3, 9, 20, 9, 3, 1, 1, 3, 9, 18, 9, 3, 1]
    for k, v in enumerate(shape):
        p.add(T * (2000 + k), D(v))
    kinds = [n.kind for n in p.nodes(smoothing=1, hvn_percentile=70, min_prominence=0.1, lvn_max_ratio=0.3)]
    assert kinds.count("HVN") == 2 and kinds.count("LVN") == 1


# ---------------------------------------------------------------- classification / delta
CFG = DeltaCfg()


def tr(sec, px, q=10, tid=None, side=None):
    return Trade(ts(9, 20, sec), D(px), D(q), trade_id=tid, aggressor=side)


def qt(sec, bid, ask):
    return Quote(ts(9, 20, sec), D(bid), D(ask))


def test_classification_hierarchy():
    assert of.classify_trade(tr(1, "100.05", side=Side.SELL), qt(0, "100.00", "100.05"), None, None, CFG).method == DeltaQuality.EXCHANGE_CONFIRMED
    c = of.classify_trade(tr(1, "100.05"), qt(1, "100.00", "100.05"), None, None, CFG)
    assert (c.direction, c.method) == (Side.BUY, DeltaQuality.QUOTE_CLASSIFIED)
    c = of.classify_trade(tr(1, "100.00"), qt(1, "100.00", "100.05"), None, None, CFG)
    assert c.direction is Side.SELL
    c = of.classify_trade(tr(1, "100.02"), qt(1, "100.00", "100.05"), D("100.00"), None, CFG)  # inside spread -> tick rule
    assert (c.direction, c.method) == (Side.BUY, DeltaQuality.TICK_ESTIMATED)
    c = of.classify_trade(tr(1, "100.02"), qt(1, "100.00", "100.05"), D("100.02"), Side.SELL, CFG)  # zero tick inherits
    assert c.direction is Side.SELL
    assert of.classify_trade(tr(1, "100.02"), None, None, None, CFG).direction is None   # nothing usable -> unclassified


def test_stale_quote_never_classifies():
    old = Quote(ts(9, 19, 0), D("100.00"), D("100.05"))
    c = of.classify_trade(tr(1, "100.05"), old, None, None, CFG)
    assert c.method == DeltaQuality.UNCLASSIFIED and c.direction is None


def test_flow_dedupe_only_on_trade_id_and_out_of_order_unclassified():
    trades = [tr(1, "100.05", 10, "a"), tr(1, "100.05", 10, "a"), tr(1, "100.05", 10, "b"), tr(0, "100.05", 7, "c")]
    rep = of.build_flow(trades, [qt(0, "100.00", "100.05")], T, CFG)
    f = next(iter(rep.bars_flow.values()))
    assert rep.duplicates == 1 and rep.out_of_order == 1
    assert (f.buy, f.unclassified) == (D(20), D(7))      # legitimate identical prints (a, b) both kept
    assert rep.coverage_pct == pytest.approx(20 / 27 * 100)


def test_window_delta_ratio_requires_coverage_and_flow():
    good = [bar(9, 30, 100, 101, 99, 100, flow=BarFlow(D(80), D(10), D(10), DeltaQuality.TICK_ESTIMATED))]
    r, _ = of.window_delta_ratio(good, CFG)
    assert r == pytest.approx(70 / 90)
    bad = [bar(9, 30, 100, 101, 99, 100, flow=BarFlow(D(40), D(10), D(50), DeltaQuality.TICK_ESTIMATED))]
    assert of.window_delta_ratio(bad, CFG)[0] is None
    assert of.window_delta_ratio([bar(9, 30, 100, 101, 99, 100)], CFG)[0] is None   # candle-only: unavailable, not zero


def test_cvd_chain_breaks_on_missing_flow():
    f = BarFlow(D(6), D(4), D(0), DeltaQuality.TICK_ESTIMATED)
    bars = [bar(9, 15, 1, 2, 1, 2, flow=f), bar(9, 20, 1, 2, 1, 2), bar(9, 25, 1, 2, 1, 2, flow=f)]
    assert of.cvd_series(bars) == [2.0, None, None]


def test_feed_validation_flags_unallocated_resets_and_gaps():
    t0 = ts(9, 20)
    obs = [of.FeedObservation(t0, D(100), D(5), t0),
           of.FeedObservation(t0 + timedelta(seconds=1), D(105), D(5), t0 + timedelta(seconds=1)),     # exact
           of.FeedObservation(t0 + timedelta(seconds=2), D(125), D(5), t0 + timedelta(seconds=2)),     # dv 20 > ltq 5 -> 15 unallocated
           of.FeedObservation(t0 + timedelta(seconds=3), D(50), D(5), t0 + timedelta(seconds=3)),      # counter reset
           of.FeedObservation(t0 + timedelta(seconds=200), D(60), D(10), t0 + timedelta(seconds=200))]  # gap
    r = of.validate_feed(obs)
    assert r.exact_increments >= 1 and r.unallocated_volume == D(15) and r.resets == 1 and len(r.gaps) == 1
    assert not r.trade_exact and of.reconcile_volume(D(90), D(0)) is None


def test_pivot_not_visible_before_confirmation_and_divergence():
    cfg = DeltaCfg(pivot_left=1, pivot_right=1, min_swing_separation_bars=2, min_price_excursion_atr=0.0)
    highs = [10, 12, 11, 10, 13, 12, 11]
    cvds = [1.0, 5.0, 4.0, 3.0, 4.5, 4.0, 3.0]
    bars = [bar(9, 15 + 5 * k, h - 1, h, h - 2, h - 1) for k, h in enumerate(highs)]
    assert of.divergence(bars, cvds, 4, 1.0, cfg)["bearish"] is None     # pivot at idx 4 not yet confirmed at bar 4
    d = of.divergence(bars, cvds, 5, 1.0, cfg)["bearish"]
    assert d is not None and d.idx == 4 and d.confirmed_at == 5          # higher price high, lower CVD high


def test_absorption_requires_price_response():
    def mk(n, resp):
        out = []
        for k in range(n):
            d = D(100) if k >= n - 3 else D(5 if k % 2 else -5)
            fl = BarFlow(D(500) + d / 2, D(500) - d / 2, D(0), DeltaQuality.TICK_ESTIMATED)
            c = 100 if k < n - 1 else resp
            out.append(Bar(ts(9, 15) + timedelta(minutes=5 * k), D(100), D("100.5"), D("99.5"), D(str(c)), D(1000), fl))
        return out
    cfg = DeltaCfg()
    kw = dict(percentile=80, window=2, history=60, max_progress_atr=0.5, cfg=cfg)
    assert of.absorption(mk(30, 99), 29, 1.0, **kw)["bearish"]          # heavy +delta, no progress, then rejection
    assert not of.absorption(mk(30, 101), 29, 1.0, **kw)["bearish"]     # no rejection -> high delta alone is not absorption


# ---------------------------------------------------------------- structure
def test_atr_wilder_and_none_before_period():
    bars = [bar(9, 15 + 5 * k, 100, 101, 99, 100) for k in range(20)]
    a = atr_series(bars, 14)
    assert a[12] is None and a[13] == pytest.approx(2.0) and a[19] == pytest.approx(2.0)


def test_vwap_slope_is_atr_normalised():
    assert vwap_slope([100.0, 100.0, 100.0, 101.0], [None, None, None, 2.0], 3, 3) == pytest.approx(0.5)
    assert vwap_slope([100.0] * 4, [None, None, None, None], 3, 3) is None


def test_session_close_is_effective_dated():
    assert session_close(datetime(2026, 12, 4).date()).hour == 15 and session_close(datetime(2026, 12, 4).date()).minute == 30
    assert session_close(datetime(2026, 12, 8).date()).minute == 45
    assert session_close(datetime(2026, 12, 8).date(), is_expiring_index_derivative=True).minute == 30


def test_regime_transition_when_data_short():
    cfg = VarConfig()
    bars = [bar(9, 15 + 5 * k, 100, 101, 99, 100) for k in range(3)]
    r = classify_regime(bars, 2, [100.0] * 3, [1.0] * 3, None, None, [None] * 3, cfg.regime)
    assert r.name == "TRANSITION"


# ---------------------------------------------------------------- TPO / IB
def test_tpo_counts_one_per_row_per_bracket_and_poc():
    bars = [bar(9, 15, 100, 100.10, 100.00, 100.05), bar(9, 20, 100, 100.10, 100.00, 100.05),     # same 30m bracket
            bar(9, 45, 100.05, 100.15, 100.05, 100.10)]                                             # next bracket
    r = tpo_profile(bars, T)
    c = {k: v for k, v in r.counts.items()}
    assert r.brackets == 2 and r.approximate
    assert max(c.values()) == 2 and r.poc is not None and r.poc * T in (D("100.05"), D("100.10"))


def test_initial_balance_not_usable_before_1015():
    bars = [bar(9, 15 + 5 * k, 100, 100 + k, 99, 100) for k in range(12)]   # 09:15..10:10
    early = initial_balance(bars[:6], bars[5].end)
    assert early.complete is False
    done = initial_balance(bars, bars[-1].end)
    assert done.complete is True and done.high == D(111) and done.low == D(99)
