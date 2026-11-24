"""Engine arithmetic on a tiny hand-made TEST fixture (never used at runtime): fees, GST, funding sign, maker-through rule, stop-before-target, mark-based stops."""
from decimal import Decimal as D

import numpy as np

from app.delta_lab.engine import CostModel, Leg, Market, Signal, simulate
from app.delta_lab.risk import ContractSpec, RiskConfig

SPEC = ContractSpec("BTCUSD", D("0.001"), D("0.5"), mm_pct=D("0.0025"), im_pct_min=D("0.005"), slope_threshold_notional=D("100000"))
CFG = RiskConfig(leverage=D(10))
T0 = 1_700_000_000 - 1_700_000_000 % 86400 + 86400 + 10 * 3600          # 10:00 UTC, a day with funding at 16:00 later


def market(closes, highs=None, lows=None, mark_low=None, funding=None):
    n = len(closes)
    c = np.array(closes, float)
    h = np.array(highs if highs is not None else c + 1, float)
    low = np.array(lows if lows is not None else c - 1, float)
    t = np.arange(n, dtype=np.int64) * 60 + T0
    ml = np.array(mark_low if mark_low is not None else low, float)
    m = Market("BTCUSD", SPEC, 0.5, 0.001, t, c.copy(), h, low, c, np.ones(n), c.copy(), h.copy(), ml, c.copy(), funding or {})
    m.atr_1h = np.full(n, 100.0)
    return m


NOFRICTION = CostModel(taker_fee=0.0, maker_fee=0.0, gst=0.0, slippage_bps=0.0, slippage_ticks_min=0.0)


def test_taker_round_trip_charges_fees_and_gst_on_both_legs_and_slippage_separately():
    m = market([60000.0] * 40)
    sig = Signal(0, "T", "t", [Leg("long", 60000, 59400, None)], "market", max_hold=5)
    trades, _, _ = simulate(m, [sig], CostModel(slippage_bps=0.0, slippage_ticks_min=1.0), CFG)
    t = trades[0]
    notional = 60000 * 0.001 * t["qty"]
    assert abs(t["fees"] - 2 * notional * 0.0005) < 1e-6 and abs(t["gst"] - t["fees"] * 0.18) < 1e-9
    assert abs(t["slippage"] - 2 * 0.5 * 0.001 * t["qty"]) < 1e-9                    # one tick on each leg
    assert abs(t["net"] - (t["gross"] - t["slippage"] - t["fees"] - t["gst"] - t["funding"])) < 1e-9


def test_a_maker_limit_fills_only_when_price_trades_through_not_when_it_touches():
    touch = market([60000.0] * 10, lows=[60000.0] * 10)                             # low == limit: a touch
    through = market([60000.0] * 10, lows=[60000.0, 59999.5] + [60000.0] * 8)       # low < limit: through
    sig = Signal(0, "T", "t", [Leg("long", 60000.0, 59400, None)], "limit", valid_bars=5, max_hold=3)
    assert simulate(touch, [sig], NOFRICTION, CFG)[0] == []
    assert len(simulate(through, [sig], NOFRICTION, CFG)[0]) == 1


def test_stop_is_checked_on_the_mark_price_and_wins_when_the_target_is_in_the_same_bar():
    highs = [60000.0] * 4 + [61500.0] + [60000.0] * 10
    lows = [60000.0] * 4 + [59900.0] + [60000.0] * 10
    m = market([60000.0] * 15, highs=highs, lows=lows, mark_low=[60000.0] * 4 + [59300.0] + [60000.0] * 10)   # mark trades through the stop while last also reaches the target
    sig = Signal(0, "T", "t", [Leg("long", 60000.0, 59400.0, 60600.0)], "market", max_hold=10)
    t = simulate(m, [sig], NOFRICTION, CFG)[0][0]
    assert t["exit_reason"] == "stop" and t["net"] < 0
    m2 = market([60000.0] * 15, highs=highs, lows=lows)                              # mark does not reach the stop: the target is taken
    assert simulate(m2, [sig], NOFRICTION, CFG)[0][0]["exit_reason"] == "target"


def test_funding_is_paid_by_longs_and_received_by_shorts_when_the_rate_is_positive():
    n = 8 * 60                                                                       # 10:00 to 18:00 UTC crosses the 16:00 funding time
    m = market([60000.0] * n, funding={T0 + 6 * 3600: 0.0001})
    long_t = simulate(m, [Signal(0, "T", "t", [Leg("long", 60000, 58500, None)], "market", max_hold=n - 5)], NOFRICTION, CFG)[0][0]
    short_t = simulate(m, [Signal(0, "T", "t", [Leg("short", 60000, 61500, None)], "market", max_hold=n - 5)], NOFRICTION, CFG)[0][0]
    assert long_t["funding"] > 0 > short_t["funding"] and abs(long_t["funding"] + short_t["funding"]) < 1e-9
    assert abs(long_t["funding"] - 0.0001 * 60000 * 0.001 * long_t["qty"]) < 1e-9
    assert long_t["net"] == long_t["gross"] - long_t["funding"] or abs(long_t["net"] - (long_t["gross"] - long_t["funding"])) < 1e-9


def test_a_signal_while_a_position_is_open_is_skipped_and_the_funding_window_is_rejected_with_a_reason():
    m = market([60000.0] * 40)
    a = Signal(0, "T", "t", [Leg("long", 60000, 59400, None)], "market", max_hold=20)
    b = Signal(5, "T", "t", [Leg("long", 60000, 59400, None)], "market", max_hold=3)
    trades, _, counts = simulate(m, [a, b], NOFRICTION, CFG)
    assert len(trades) == 1 and counts == {"skipped_position_open": 1}
    t16 = 16 * 3600
    m2 = market([60000.0] * 300)
    m2.t = m2.t - m2.t[0] + (T0 - 10 * 3600 + t16 - 3 * 60)                           # the signal bar closes at 16:00:00 sharp
    _, _, counts2 = simulate(m2, [Signal(2, "T", "t", [Leg("long", 60000, 59400, None)], "market")], NOFRICTION, CFG)
    assert counts2 == {"funding_window": 1}


def test_converting_maker_to_taker_adds_the_taker_fee():
    m = market([60000.0] * 12, lows=[60000.0, 59990.0] + [60000.0] * 10, highs=[60000.0] * 4 + [60700.0] + [60000.0] * 7)
    sig = Signal(0, "T", "t", [Leg("long", 60000.0, 59400.0, 60600.0)], "limit", valid_bars=3, max_hold=8)
    base = simulate(m, [sig], CostModel(slippage_bps=0, slippage_ticks_min=0), CFG)[0][0]
    allt = simulate(m, [sig], CostModel(slippage_bps=0, slippage_ticks_min=0, maker_to_taker_share=1.0), CFG)[0][0]
    assert allt["fees"] > base["fees"] and abs(base["fees"] - (60000 + 60600) * 0.001 * base["qty"] * 0.0002) < 1e-6
