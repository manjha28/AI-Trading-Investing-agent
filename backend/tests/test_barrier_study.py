"""Phase 0 labelling, strictness, stop-before-target, timeouts, gaps, non-overlap and filter-value statistics. Expected values worked out by hand.
TEST FIXTURE price paths only; nothing here is market data."""
import math
from datetime import date

import numpy as np
import pytest

from app.services import barrier_study as bs
from app.services import filter_value as fv


def sess(path, vol=1.0):
    """path: list of (o, h, l, c) for slots 0..n-1; remaining slots are MISSING (NaN). One session."""
    n = bs.SLOTS
    a = {k: np.full(n, np.nan) for k in "ohlcv"}
    for i, (o, h, l, c) in enumerate(path):
        a["o"][i], a["h"][i], a["l"][i], a["c"][i], a["v"][i] = o, h, l, c, vol
    return bs.Session(date(2026, 1, 5), a["o"], a["h"], a["l"], a["c"], a["v"])


HZ = (3, 15, 60)


def cat_of(sessions, dists):
    return bs.concat_sessions([bs.SessionArrays(s, dists, horizons=HZ) for s in sessions], dists, HZ)


def label(path, s, r, H, d, entry=0, **kw):
    dists = bs.distinct_distances((s,), (r,))
    c = cat_of([sess(path, **kw)], dists)
    w, l, t, rto = bs.label_cell(c, s, r, H, d)
    return bool(w[entry]), bool(l[entry]), bool(t[entry]), float(rto[entry])


# entry open 100, s = 1% (stop 99 / 101), r = 2 (target 102 / 98)
FLAT = (100, 100.5, 99.5, 100)


def test_target_first_long_and_short_mirror():
    up = [(100, 100.4, 99.6, 100.2), (100.2, 102.5, 100, 102.3)]                 # high 102.5 > 102 on slot 1
    assert label(up, 0.01, 2.0, 60, +1)[:3] == (True, False, False)
    dn = [(100, 100.4, 99.6, 99.8), (99.8, 100.0, 97.5, 97.8)]
    assert label(dn, 0.01, 2.0, 60, -1)[:3] == (True, False, False)


def test_short_stop_is_hit_when_price_rises():
    up = [(100, 100.4, 99.6, 100.2), (100.2, 102.5, 100, 102.3)]
    w, l, t, _ = label(up, 0.01, 2.0, 60, -1)
    assert (w, l, t) == (False, True, False)


def test_stop_is_checked_before_target_inside_one_bar():
    both = [(100, 102.5, 98.5, 101)]                                               # one bar spans stop 99 AND target 102
    assert label(both, 0.01, 2.0, 60, +1)[:3] == (False, True, False)


def test_touches_are_strict():
    exact_target = [(100, 102.0, 99.5, 101.9)]                                     # high == target exactly: NOT a touch
    assert label(exact_target, 0.01, 2.0, 60, +1)[:3] == (False, False, True)
    exact_stop = [(100, 100.5, 99.0, 100)]                                         # low == stop exactly: NOT a touch
    assert label(exact_stop, 0.01, 2.0, 60, +1)[:3] == (False, False, True)


def test_timeout_r_is_measured_not_assumed_zero():
    path = [(100, 100.5, 99.5, 100.2)] * 3 + [(100.2, 100.6, 99.6, 100.5)]        # drifts up, never touches; exit = close of the last bar inside H
    w, l, t, rto = label(path, 0.01, 2.0, 3, +1)                                   # H = 3 slots -> exit close of slot 2 = 100.2
    assert (w, l, t) == (False, False, True) and math.isclose(rto, (100.2 - 100) / (0.01 * 100), rel_tol=1e-9)   # +0.2 R
    assert math.isclose(label(path, 0.01, 2.0, 3, -1)[3], -0.2, rel_tol=1e-9)


def test_horizon_excludes_later_touches_and_session_end_caps_it():
    path = [FLAT, FLAT, FLAT, (100, 102.6, 99.5, 102.5)]                          # target only on slot 3
    assert label(path, 0.01, 2.0, 3, +1)[2] is True                                # H=3 covers slots 0..2 only -> timeout
    assert label(path, 0.01, 2.0, 15, +1)[0] is True                               # H=15 reaches slot 3
    # session ends after slot 3: an entry at slot 3 has only itself in its horizon (vl = 1) and exits at that close
    assert label(path, 0.01, 2.0, 60, +1, entry=3)[0] is True                      # entry bar's own high 102.6 counts
    late = [FLAT] * 4
    w, l, t, rto = label(late, 0.01, 2.0, 60, +1, entry=3)
    assert t and math.isclose(rto, 0.0, abs_tol=1e-12)


def test_missing_minutes_are_never_filled_and_cannot_be_entries():
    path = [FLAT, FLAT, FLAT]
    s = sess(path)
    s.o[1] = s.h[1] = s.l[1] = s.c[1] = np.nan                                     # minute 1 missing
    dists = bs.distinct_distances((0.01,), (2.0,))
    c = cat_of([s], dists)
    assert bool(c["entry_ok"][0]) and not bool(c["entry_ok"][1]) and bool(c["entry_ok"][2])


def test_zero_volume_bars_are_not_entries_but_still_part_of_the_path():
    s = sess([FLAT, FLAT, FLAT])
    s.v[1] = 0.0
    c = cat_of([s], bs.distinct_distances((0.01,), (2.0,)))
    assert list(c["entry_ok"][:3]) == [True, False, True]


def test_p_breakeven_formula_by_hand():
    # r=2, f=0.1, no timeouts: E = p*3 - 1.1 = 0  =>  p_be = 1.1/3
    assert math.isclose(bs.p_breakeven(2.0, 0.1, 0.0, 0.0), 1.1 / 3, rel_tol=1e-12)
    # with 40% timeouts at r_to = 0.2, f = 0.1:  p_be = [0.6*1.1 - 0.4*(0.2-0.1)]/3 = (0.66-0.04)/3
    assert math.isclose(bs.p_breakeven(2.0, 0.1, 0.4, 0.2), 0.62 / 3, rel_tol=1e-12)


def test_cell_report_E_R_net_uses_the_empirical_distribution_including_timeouts():
    acc = {"n": 100, "wins": 20, "loss": 40, "timeout": 40, "sum_r_timeout": 40 * 0.25, "bucket": None,
           "no": {"n": 10, "sum_R": 0.0, "sum_R2": 0.0, "sum_W": 0.0, "sum_W2": 0.0}}
    row = bs.cell_report(acc, 0.01, 2.0, 60, 0.0005)                              # cost 0.05% of notional, s = 1% -> f = 0.05
    f = 0.05
    expect = 0.2 * (2 - f) + 0.4 * (-1 - f) + 0.4 * (0.25 - f)
    assert math.isclose(row["E_R_net"], expect, rel_tol=1e-12) and math.isclose(row["f"], f)
    assert math.isclose(row["required_uplift"], bs.p_breakeven(2.0, f, 0.4, 0.25) / 0.2, rel_tol=1e-12)
    assert bs.cell_report({**acc, "wins": 0}, 0.01, 2.0, 60, 0.0005)["required_uplift"] == math.inf


def test_non_overlap_uses_one_entry_every_H_slots_and_averages_both_directions():
    path = [FLAT] * 10
    dists = bs.distinct_distances((0.01,), (2.0,))
    c = cat_of([sess(path)], dists)
    res = bs.study_cells(c, (0.01,), (2.0,), (15,))
    acc = res[(0.01, 2.0, 15)]
    assert acc["n"] == 2 * 10 and acc["no"]["n"] == 1                              # 10 entries x 2 directions overlapping; stride 15 keeps slot 0 only
    assert acc["timeout"] == 20 and acc["wins"] == 0


def test_filter_value_hand_computed_difference_se_t_and_thinning():
    def rec(i, state, r, inst="X", t=None):
        return {"states": {"f": state}, "instrument": inst, "t_min": (i * 1000.0 if t is None else t), "r_net": r, "win": 1 if r > 0 else 0}
    passed = [rec(i, "pass", r) for i, r in enumerate([1.0, 1.0, -1.0, 1.0])]
    rejected = [rec(i + 10, "fail", r) for i, r in enumerate([-1.0, -1.0, 1.0, -1.0])]
    row = fv.filter_value(passed + rejected, ["f"], min_gap_min=60)[0]
    # passed mean 0.5, var = ((.5)^2*3 + (1.5)^2)/3 = 1.0 ; rejected mean -0.5, var 1.0 ; se = sqrt(1/4 + 1/4) = 0.70711 ; t = 1/0.70711 = 1.4142
    assert row["E_R_net_passed"] == 0.5 and row["E_R_net_rejected"] == -0.5 and math.isclose(row["E_R_net_diff"], 1.0)
    assert math.isclose(row["E_R_net_se"], math.sqrt(0.5)) and math.isclose(row["E_R_net_t"], 1 / math.sqrt(0.5))
    assert row["verdict"] == "no_separation" and row["n_passed"] == 4 and row["p_win_passed"] == 0.75 and row["p_win_rejected"] == 0.25
    # thinning: candidates 10 minutes apart on one instrument collapse to one per 60-minute gap
    clustered = [rec(i, "pass", 1.0, t=float(i * 10)) for i in range(12)]
    assert len(fv.thin(clustered, 60)) == 2
    # other states are excluded from both groups
    mixed = passed + [rec(99, "invalid_data_quality", 5.0)]
    assert fv.filter_value(mixed, ["f"], 60)[0]["n_rejected_raw"] == 0


def test_filter_value_verdicts():
    def many(state, mean):
        return [{"states": {"f": state}, "instrument": f"I{i}", "t_min": 0.0, "r_net": mean + (0.1 if i % 2 else -0.1), "win": 1} for i in range(200)]
    assert fv.filter_value(many("pass", 0.5) + many("fail", 0.0), ["f"], 60)[0]["verdict"] == "earns_its_rejections"
    assert fv.filter_value(many("pass", 0.0) + many("fail", 0.5), ["f"], 60)[0]["verdict"] == "rejected_outperformed"
    assert fv.filter_value(many("pass", 0.0) + many("fail", 0.0), ["f"], 60)[0]["verdict"] == "no_separation"
