"""Simulator and barrier rules on hand-built paths (TEST DATA: arithmetic fixtures, never results)."""
import re
from pathlib import Path

import numpy as np
import pandas as pd

from scalp_hold.barriers import BarrierCfg, h_effective, label_triple_barrier, stop_fraction
from scalp_hold.features import compute
from scalp_hold.sim import SimCfg, simulate
from scalp_hold.venues import CRYPTO_DELTA, NSE_INDEX_FUT, crypto_delta

N = 40


def path(overrides: dict[int, dict]) -> pd.DataFrame:
    idx = pd.date_range("2026-10-05 00:00", periods=N, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 10.0}, index=idx)
    for i, vals in overrides.items():
        for k, v in vals.items():
            df.iloc[i, df.columns.get_loc(k)] = v
    return df


def run(df, *, d=1, s=0.01, target=0.02, H_bars=6, venue=CRYPTO_DELTA, cfg=None, slip_bps=0.0):
    f = compute(df, venue, with_profile=False)
    sig = pd.DataFrame(False, index=df.index, columns=["T_long", "T_short", "R_long", "R_short", "B_long", "B_short"])
    forced = [{"i": 5, "d": d, "s": s, "target_pct": target, "H_bars": H_bars, "stype": "X"}]
    return simulate(df, f, sig, venue, "BTCUSD", cfg or SimCfg(equity=100_000, early_exits=False), slip_bps=slip_bps, forced=forced)


def test_target_requires_strict_penetration_not_a_touch():
    # entry open[6]=100; target = 102; bar 7 high touches exactly 102 -> no fill; tick is 0.5 so penetration needs high > 102.5
    touch = path({7: {"high": 102.0}})
    assert run(touch).trades.iloc[0]["exit_reason"] == "time"
    through = path({7: {"high": 102.6}})
    t = run(through).trades.iloc[0]
    assert t["exit_reason"] == "target" and abs(t["exit_px"] - 102.0) < 1e-9          # fills AT the target, not at the high


def test_stop_is_checked_before_target_inside_one_bar():
    both = path({7: {"high": 103.0, "low": 98.0}})
    assert run(both).trades.iloc[0]["exit_reason"] == "stop"


def test_gap_through_the_stop_fills_at_the_worse_open():
    gap = path({7: {"open": 97.0, "high": 97.5, "low": 96.5, "close": 97.0}})
    t = run(gap).trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_px"] == 97.0                         # min(stop 99, open 97)


def test_time_exit_and_short_side_symmetry():
    flat = path({})
    assert run(flat).trades.iloc[0]["exit_reason"] == "time"
    t = run(path({7: {"low": 97.4}}), d=-1).trades.iloc[0]
    assert t["side"] == "short" and t["exit_reason"] == "target" and abs(t["exit_px"] - 98.0) < 1e-9


def test_costs_and_pnl_come_from_decimal_fills_and_breakdown_is_recorded():
    t = run(path({7: {"high": 102.6}})).trades.iloc[0]
    assert t["gross_pnl"] > 0 and t["net_pnl"] < t["gross_pnl"]
    assert set(__import__("json").loads(t["cost_breakdown_json"])) == {"fees", "gst", "tds"}
    assert abs(t["R"] - t["net_pnl"] / (t["notional"] * t["s"])) < 1e-6


def test_below_min_lot_is_skipped_and_reported_on_nifty_with_a_small_account():
    df = path({}).assign(open=26000.0, high=26010.0, low=25990.0, close=26000.0)
    res = run(df, venue=NSE_INDEX_FUT, cfg=SimCfg(equity=100_000, early_exits=False))        # 1 lot = 65 x 26000 = 16.9L notional
    assert res.trades.empty and res.skips["below_min_lot"] == 1 and res.below_min_lot_rate == 1.0
    assert res.filtered.iloc[0]["reason"] == "below_min_lot" and "MFE_pct" in res.filtered.columns            # filtered signals still get MFE/MAE


def test_mfe_mae_are_recorded_on_every_trade():
    t = run(path({7: {"high": 101.5, "low": 99.5}})).trades.iloc[0]
    assert t["MFE_pct"] >= 1.5 - 1e-9 and t["MAE_pct"] > 0 and t["MFE_bar"] >= 0


def test_waiver_shortens_the_horizon_and_zeroes_closing_fees_when_enabled():
    venue = crypto_delta(waiver_enabled=True)
    assert h_effective(120, None, venue.waiver.window_for("BTCUSD"), BarrierCfg()) == 28.0       # min(120, 30 - 2)
    t = run(path({7: {"high": 102.6}}), venue=venue, H_bars=4).trades.iloc[0]
    off = run(path({7: {"high": 102.6}}), H_bars=4).trades.iloc[0]
    assert t["net_pnl"] > off["net_pnl"]


def test_barrier_floors_and_cap():
    cfg = BarrierCfg()
    s, skip = stop_fraction(np.array([0.004, 0.0005]), 2.0, 0.0004, 0.5, np.array([68000.0, 68000.0]), cfg)
    assert np.isclose(s[0], 0.004 / 2) and not skip[0]                           # volatility-implied stop wins
    assert np.isclose(s[1], 0.0004 / 0.25) and skip[1]                            # cost floor 0.0016 > 2 x s_raw 0.00025 -> skipped


def test_label_first_touch_stop_before_target_and_timeout():
    o = np.array([100.0, 100, 100, 100, 100, 100, 100, 100]); h = o + 0.1; l = o - 0.1
    h[2] = 105.0; l[2] = 95.0                       # bar 2 spans both barriers
    lab = label_triple_barrier(o, h, l, np.full(8, 0.01), 2.0, 3, 1, 0.01, 0.0)
    assert lab[0] == -1.0                           # stop-before-target within the bar
    assert np.isnan(lab[-1]) and lab[3] == 0.0      # no future bars -> NaN; quiet path -> timeout


def test_decimal_is_confined_to_venues_sim_and_cli():
    root = Path(__file__).resolve().parents[1] / "scalp_hold"
    offenders = [p.name for p in root.glob("*.py") if p.name not in {"venues.py", "sim.py", "cli.py", "app_adapter.py"} and re.search(r"\bDecimal\b|import decimal", p.read_text())]
    assert offenders == []


def test_no_entry_hours_filter_and_must_work_timing_are_parameters_with_spec_defaults():
    from datetime import time as dtime
    assert abs(SimCfg().must_work_at_frac - 1 / 3) < 1e-9 and SimCfg().no_entry_hours_ist == frozenset()        # spec defaults unchanged
    # forced entry at bar 5 -> entry bar 6 = 00:30 UTC = 06:00 IST; excluding hour 6 drops it with a recorded reason
    res = run(path({}), cfg=SimCfg(equity=100_000, early_exits=False, no_entry_hours_ist=frozenset({6})))
    assert res.trades.empty and res.skips["entry_hour_excluded"] == 1
    assert run(path({}), cfg=SimCfg(equity=100_000, early_exits=False, no_entry_hours_ist=frozenset({7}))).trades.iloc[0]["exit_reason"] == "time"
    del dtime


def test_must_work_fires_at_the_configured_fraction_of_h():
    from scalp_hold.features import compute
    df = path({})                                                   # flat path: MFE never reaches 0.3 x target
    f = compute(df, CRYPTO_DELTA, with_profile=False)
    sig = pd.DataFrame(False, index=df.index, columns=["T_long", "T_short", "R_long", "R_short", "B_long", "B_short"])
    forced = [{"i": 5, "d": 1, "s": 0.01, "target_pct": 0.02, "H_bars": 12, "stype": "X"}]       # X: no thesis/regime exits, isolating must_work
    early = simulate(df, f, sig, CRYPTO_DELTA, "BTCUSD", SimCfg(equity=100_000, H=60.0, must_work_enabled=True, must_work_at_frac=1 / 3), slip_bps=0.0, forced=forced).trades.iloc[0]
    late = simulate(df, f, sig, CRYPTO_DELTA, "BTCUSD", SimCfg(equity=100_000, H=60.0, must_work_enabled=True, must_work_at_frac=2 / 3), slip_bps=0.0, forced=forced).trades.iloc[0]
    assert early["exit_reason"] == "must_work" and late["exit_reason"] == "must_work"
    assert early["hold_minutes"] == 20 and late["hold_minutes"] == 40                     # H/3 vs 2H/3 of a 60-minute horizon
    default = simulate(df, f, sig, CRYPTO_DELTA, "BTCUSD", SimCfg(equity=100_000, H=60.0), slip_bps=0.0, forced=forced).trades.iloc[0]
    assert SimCfg().must_work_enabled is False and default["exit_reason"] != "must_work"   # OFF by default: the barrier geometry is allowed to resolve


def test_funnel_reconciles_and_counts_position_open_skips():
    from scalp_hold.features import compute
    from scalp_hold.funnel import scalp_funnel
    df = path({})
    f = compute(df, CRYPTO_DELTA, with_profile=False)
    sig = pd.DataFrame(False, index=df.index, columns=["T_long", "T_short", "R_long", "R_short", "B_long", "B_short"])
    forced = [{"i": 5, "d": 1, "s": 0.01, "target_pct": 0.02, "H_bars": 12, "stype": "X"}, {"i": 7, "d": 1, "s": 0.01, "target_pct": 0.02, "H_bars": 12, "stype": "X"}]
    res = simulate(df, f, sig, CRYPTO_DELTA, "BTCUSD", SimCfg(equity=100_000, H=60.0), slip_bps=0.0, forced=forced)
    fn = scalp_funnel(res, "BTCUSD")
    assert fn["candidates_generated"] == 2 and fn["trades_executed"] == 1 and fn["reconciles"]
    assert {r["reason"]: r["n"] for r in fn["reasons"]} == {"position_open": 1}        # previously this skip was uncounted and the funnel could not add up


def test_entry_cutoff_is_configurable():
    assert SimCfg().entry_cutoff_minutes is None
    from scalp_hold.app_adapter import DEFAULT_PARAMS, _hhmm, coerce_params
    assert _hhmm("15:10") == 15 * 60 + 10 and _hhmm("") is None and DEFAULT_PARAMS["must_work_enabled"] is False
    assert coerce_params({"entry_cutoff": "15:10", "must_work_enabled": "true"}) == {"entry_cutoff": "15:10", "must_work_enabled": True}
