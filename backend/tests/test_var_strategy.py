"""VAR strategy rules, risk, execution and replay. ALL DATA IS TEST FIXTURE DATA (see var_fixtures.py)."""
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from var_fixtures import (CONTRACT, TICK, breakout_long_bars, day_open, flow, make_ctx, mkbar, prior_sessions, ref_levels,  # noqa: E402
                          reversal_short_bars, run_all)

from app.services.var import execution as ex  # noqa: E402
from app.services.var.backtest import InstrumentData, chronological_splits, run_backtest  # noqa: E402
from app.services.var.config import VarConfig  # noqa: E402
from app.services.var.risk import DayState, can_enter, size_position  # noqa: E402
from app.services.var.strategy import EngineState, evaluate  # noqa: E402
from app.services.var.vtypes import Cond, Signal  # noqa: E402

D0 = date(2026, 2, 2)


def cfgA(**kw):
    return VarConfig(variant="A", engines=("breakout",), **kw)


# ---------------------------------------------------------------- Engine A
def test_valid_breakout_has_derivation_and_all_conditions_pass():
    sigs, rejs, _ = run_all(make_ctx(breakout_long_bars(D0), cfgA()))
    assert len(sigs) == 1 and not rejs
    i, s = sigs[0]
    assert s.setup == "ACCEPTANCE_BREAKOUT" and s.direction == "LONG" and s.regime == "BULLISH_TREND"
    assert s.entry > s.reference_level > s.stop and s.targets[0] > s.entry
    assert s.entry % TICK == 0 and s.stop % TICK == 0 and s.targets[0] % TICK == 0
    assert all(c.state is Cond.PASS for c in s.conditions)
    assert {"reference", "stop_rule", "entry_rule", "atr_5m"} <= set(s.derivation)
    assert s.risk_reward == pytest.approx(2.5, abs=0.2)


def test_single_close_is_not_acceptance():
    sigs, rejs, _ = run_all(make_ctx(breakout_long_bars(D0, acceptance2=False), cfgA()))
    assert not [s for _, s in sigs if s.setup == "ACCEPTANCE_BREAKOUT"]


def test_failed_retest_closing_back_through_level_kills_setup():
    sigs, rejs, _ = run_all(make_ctx(breakout_long_bars(D0, retest_ok=False), cfgA()))
    assert not sigs


def test_low_relative_volume_is_rejected_with_reason():
    sigs, rejs, _ = run_all(make_ctx(breakout_long_bars(D0), cfgA(), hist_vol=2000))
    assert not sigs and any("relative_volume" in r for _, rj in rejs for r in rj.reasons)


def test_missing_time_slot_history_invalidates_not_defaults():
    from var_fixtures import prior_sessions as ps, prev_session  # noqa
    ctx = make_ctx(breakout_long_bars(D0), cfgA())
    ctx.rvol_hist = {}
    st = EngineState()
    out = [evaluate(ctx, i, st) for i in range(len(ctx.bars))]
    rj = [r for _s, rs, _g in out for r in rs]
    assert rj and any(c.name == "relative_volume" and c.state is Cond.INVALID_DATA for r in rj for c in r.conditions)


def test_duplicate_signal_suppressed():
    ctx = make_ctx(breakout_long_bars(D0), cfgA())
    st = EngineState()
    first = evaluate(ctx, 9, st)
    again = evaluate(ctx, 9, st)
    assert len(first[0]) == 1 and not again[0] and any("not_duplicate" in x for r in again[1] for x in r.reasons)


def test_no_lookahead_signal_identical_with_future_bars_removed():
    bars = breakout_long_bars(D0)
    full = make_ctx(bars + [mkbar(D0, 10, 24100, 24300, 23500, 23600)], cfgA())
    cut = make_ctx(bars, cfgA())
    a = evaluate(full, 9, EngineState())[0]
    b = evaluate(cut, 9, EngineState())[0]
    assert [(x.entry, x.stop, x.targets) for x in a] == [(x.entry, x.stop, x.targets) for x in b] and a


def test_variant_b_without_order_flow_is_invalid_not_fabricated():
    cfg = VarConfig(variant="B", engines=("breakout",))
    sigs, rejs, _ = run_all(make_ctx(breakout_long_bars(D0, flows=False), cfg))
    assert not sigs
    names = {(c.name, c.state) for _, r in rejs for c in r.conditions}
    assert ("delta_ratio", Cond.INVALID_DATA) in names


def test_variant_b_with_good_flow_signals_and_reports_quality():
    cfg = VarConfig(variant="B", engines=("breakout",))
    sigs, _r, _ = run_all(make_ctx(breakout_long_bars(D0, flows=True), cfg))
    assert len(sigs) == 1 and sigs[0][1].delta_quality == "tick_estimated"


def test_variant_b_low_coverage_rejected():
    bars = breakout_long_bars(D0, flows=True)
    bars = [b.__class__(**{**b.__dict__, "flow": flow(300, 100, 600)}) for b in bars]   # 40% classified
    sigs, rejs, _ = run_all(make_ctx(bars, VarConfig(variant="B", engines=("breakout",))))
    assert not sigs and any("coverage" in x for _, r in rejs for x in r.reasons)


def test_non_contiguous_bars_rejected_as_stale():
    bars = breakout_long_bars(D0)
    del bars[7]
    bars = [b.__class__(**{**b.__dict__, "ts": day_open(D0) + timedelta(minutes=5 * (k if k < 7 else k + 1))}) for k, b in enumerate(bars)]
    sigs, rejs, _ = run_all(make_ctx(bars, cfgA()))
    assert not sigs


def test_feed_failure_blocks_via_transition_regime():
    sigs, rejs, _ = run_all(make_ctx(breakout_long_bars(D0), cfgA(), feed_ok=False))
    assert not sigs and any("feed_quality" in x or "regime" in x for _, r in rejs for x in r.reasons)


def test_excluded_instrument_day_rejected():
    sigs, rejs, _ = run_all(make_ctx(breakout_long_bars(D0), cfgA(), excluded_reason="scheduled results day"))
    assert not sigs and any("scheduled results" in x for _, r in rejs for x in r.reasons)


# ---------------------------------------------------------------- Engine B
def cfgR(v="A", ratio=0.1):
    """ratio relaxes only the economic filter so that the structural rules are what these fixtures exercise."""
    return VarConfig(variant=v, engines=("reversal",), risk={"min_net_reward_to_cost": ratio})


def test_economic_filter_uses_the_schedule_for_the_session_date():
    """Same trade, two dates. Pre-2026-04-01 STT is 0.02% (C ~ Rs 520, net/C 1.69 -> passes k=1); from 2026-04-01 it is 0.05% (C ~ Rs 1,050, net/C < 1 -> rejected)."""
    nov = date(2026, 11, 2)
    sigs_old, _r, _ = run_all(make_ctx(reversal_short_bars(D0), cfgR("A", ratio=1.0)))
    assert [1 for i, s in sigs_old if i == 17]
    sigs, rejs, _ = run_all(make_ctx(reversal_short_bars(nov), cfgR("A", ratio=1.0)))
    assert not [1 for i, s in sigs if i == 17]
    msg = [x for i, r in rejs if i == 17 for x in r.reasons if x.startswith("net_reward_after_costs")]
    assert msg and "net/C" in msg[0] and "need >= 1.0" in msg[0]


@pytest.mark.parametrize("v,fl", [("A", False), ("B", True), ("D", True)])
def test_valid_failed_auction_short(v, fl):
    sigs, rejs, _ = run_all(make_ctx(reversal_short_bars(D0, flows=fl), cfgR(v)))
    s = [x for i, x in sigs if i == 17]
    assert len(s) == 1
    s = s[0]
    VAH, _val, POC = ref_levels()
    assert s.direction == "SHORT" and s.regime == "BALANCED" and s.reference_level == VAH
    assert s.stop > s.reference_level > s.entry > s.targets[0] and s.risk_reward >= 1.5
    assert s.targets[0] == POC


def test_reversal_requires_order_flow_failure_in_variant_b():
    bars = reversal_short_bars(D0, flows=True)
    bars = [b.__class__(**{**b.__dict__, "flow": flow(520, 480) if k != 17 else flow(300, 700)}) for k, b in enumerate(bars)]
    sigs, rejs, _ = run_all(make_ctx(bars, cfgR("B")))
    assert not [1 for i, _s in sigs if i == 17]
    assert any("order_flow_failure" in x for i, r in rejs if i == 17 for x in r.reasons)


def test_trend_day_probe_is_not_faded():
    sigs, rejs, _ = run_all(make_ctx(reversal_short_bars(D0, trend_acceptance=True), cfgR()))
    assert not [1 for i, s in sigs if s.direction == "SHORT"]


def test_oversized_sweep_is_not_eligible():
    sigs, rejs, _ = run_all(make_ctx(reversal_short_bars(D0, sweep=40), cfgR()))
    assert not [1 for i, s in sigs if s.direction == "SHORT"]


def test_target_too_close_is_rejected():
    from app.services.var.profile import tick_index
    ctx = make_ctx(reversal_short_bars(D0), cfgR())
    near = tick_index(ctx.bars[17].low, TICK) - 3          # a POC a few ticks below the planned entry
    ctx.prev_stats = type(ctx.prev_stats)(**{**ctx.prev_stats.__dict__, "poc": near})
    ctx.dev_stats = [s and type(s)(**{**s.__dict__, "poc": near}) for s in ctx.dev_stats]
    sigs, rejs, _ = run_all(ctx)
    assert not [1 for i, s in sigs if i == 17]
    assert any("first_target_rr" in x for i, r in rejs if i == 17 for x in r.reasons)


# ---------------------------------------------------------------- risk
def _sig(direction="LONG", entry="24000", stop="23970", target="24075", **kw):
    return Signal("VAR_V1", "ACCEPTANCE_BREAKOUT", "NIFTY-FUT-TEST", direction, day_open(D0), "BULLISH_TREND", D("23990"), "test",
                  D(entry), D(stop), [D(target)], 2.5, "tick_estimated", "C_candle_approximate", warnings=["expiring"] * 0)


def test_position_size_floors_to_whole_lots_and_rejects_oversized_lot():
    cfg = VarConfig()
    sz = size_position(D("2000000"), D("24000"), D("23970"), D("24075"), "LONG", CONTRACT, cfg, D0)
    assert sz.lots >= 1 and sz.lots * (D(30) * 75 + sz.one_lot_costs) <= sz.risk_budget
    assert (sz.lots + 1) * (D(30) * 75 + sz.one_lot_costs) > sz.risk_budget          # never rounded up
    small = size_position(D("100000"), D("24000"), D("23970"), D("24075"), "LONG", CONTRACT, cfg, D0)
    assert small.lots == 0 and "exceeds" in small.reason


def test_risk_gates():
    cfg = VarConfig()
    day = DayState(start_equity=D("2000000"))
    sz = size_position(day.start_equity, D("24000"), D("23970"), D("24075"), "LONG", CONTRACT, cfg, D0)
    now = day_open(D0) + timedelta(hours=1)
    assert can_enter(day, sz, CONTRACT, now, cfg) == []
    day.open_index_positions = 1
    assert any("index" in r for r in can_enter(day, sz, CONTRACT, now, cfg))
    day.open_index_positions = 0
    day.realized_net = D("-10000")                       # = 0.50% of 2,000,000
    assert any("daily loss" in r for r in can_enter(day, sz, CONTRACT, now, cfg))
    day.realized_net = D(0); day.entries = 3
    assert any("maximum entries" in r for r in can_enter(day, sz, CONTRACT, now, cfg))
    day.entries = 0; day.consecutive_losses = 2
    assert any("consecutive" in r for r in can_enter(day, sz, CONTRACT, now, cfg))
    day.consecutive_losses = 0; day.open_risk = D("9000")
    assert any("aggregate" in r for r in can_enter(day, sz, CONTRACT, now, cfg))
    day.open_risk = D(0); day.last_stop_out[CONTRACT.instrument_id] = now - timedelta(minutes=5)
    assert any("cooldown" in r for r in can_enter(day, sz, CONTRACT, now, cfg))


def test_charges_come_from_the_single_authoritative_service():
    from app.services import indian_charges as ic
    from app.services.var.charges import order_charges
    c = order_charges(CONTRACT, VarConfig().charges, D0, "entry", "buy", D("26000"), D(65))
    ref = ic.calculate_trade_charges(exchange="NSE", segment="futures", instrument_type="index_future", broker_profile="dhan", trading_date=D0, order_id="entry",
                                     side="buy", execution_price=D("26000"), executed_quantity=D(65))
    assert c.raw == ref.raw and c.total > 0 and isinstance(c.total, D)
    import app.services.var as v, pkgutil
    assert "costs" not in {m.name for m in pkgutil.iter_modules(v.__path__)}          # the duplicate calculator is gone


def test_config_validation():
    with pytest.raises(Exception):
        VarConfig(risk={"risk_per_trade_pct": 1.0, "max_daily_loss_pct": 0.5})
    with pytest.raises(Exception):
        VarConfig(reversal={"minimum_sweep_atr": 0.8, "maximum_sweep_atr": 0.5})
    with pytest.raises(Exception):
        VarConfig(unknown_key=1)


# ---------------------------------------------------------------- execution
def _pos(cfg=None, direction="LONG", lots=2):
    cfg = cfg or VarConfig()
    s = _sig() if direction == "LONG" else _sig("SHORT", "24000", "24030", "23925")
    nxt = mkbar(D0, 20, "24000", "24005", "23995", "24002")
    p = ex.simulate_entry(s, CONTRACT, nxt, lots, cfg)
    assert p is not None
    return p, cfg


def b(k, o, h, l, c):
    return mkbar(D0, 21 + k, o, h, l, c)


def test_entry_requires_trigger_touch_and_applies_slippage():
    cfg = VarConfig()
    assert ex.simulate_entry(_sig(), CONTRACT, mkbar(D0, 20, "23990", "23999", "23980", "23995"), 1, cfg) is None
    p = ex.simulate_entry(_sig(), CONTRACT, mkbar(D0, 20, "23990", "24010", "23985", "24005"), 1, cfg)
    assert p.entry == D("24000") + TICK * cfg.execution.entry_slippage_ticks


def test_gap_that_breaks_rr_cancels_entry():
    cfg = VarConfig()
    assert ex.simulate_entry(_sig(), CONTRACT, mkbar(D0, 20, "24060", "24070", "24055", "24065"), 1, cfg) is None


def test_stop_first_when_stop_and_target_in_same_bar():
    p, cfg = _pos()
    bars = [b(0, "24002", "24200", "23900", "24100")]
    ex.manage(p, bars, 0, cfg)
    assert p.closed and p.exit_reason == "stop"
    r = ex.finalize(p, cfg)
    assert r["net_pnl"] < 0 and r["r_multiple"] < 0


def test_partial_at_1r_then_stop_moves_to_cost_adjusted_breakeven_next_bar():
    p, cfg = _pos()
    r1 = p.entry + p.risk_per_unit
    bars = [b(0, "24002", str(r1 + 1), "24000", str(r1)), b(1, str(r1), str(r1 + 2), str(r1 - 1), str(r1)),
            b(2, str(r1), str(r1 + 1), str(p.entry - 5), str(p.entry - 2))]
    ex.manage(p, bars, 0, cfg)
    assert p.partial_done and p.remaining_lots == 1 and p.stop == p.initial_stop     # not moved until next bar
    ex.manage(p, bars, 1, cfg)
    assert p.stop > p.entry                                                          # cost-adjusted: above raw entry
    ex.manage(p, bars, 2, cfg)
    assert p.closed and p.exit_reason == "stop_after_partial_or_trail"
    assert ex.finalize(p, cfg)["gross_pnl"] > 0


def test_stop_is_never_widened_and_hits_at_gap_open():
    p, cfg = _pos()
    before = p.stop
    ex.manage(p, [b(0, "23950", "23960", "23940", "23955")], 0, cfg)    # gaps through the stop
    assert p.closed and p.stop == before
    fill = p.fills[0][1]
    assert fill <= D("23950") - TICK * cfg.execution.stop_slippage_ticks + D("0.0001")


def test_time_stop_exits_without_progress_after_six_bars():
    p, cfg = _pos()
    bars = [b(k, "24001", "24006", "23996", "24001") for k in range(8)]
    for k in range(8):
        ex.manage(p, bars, k, cfg)
        if p.closed:
            break
    assert p.closed and p.exit_reason == "time_stop"


def test_structural_exit_fills_at_next_open():
    p, cfg = _pos()
    ex.manage(p, [b(0, "24001", "24006", "23996", "24001")], 0, cfg)
    ex.manage(p, [b(0, "24001", "24006", "23996", "24001"), b(1, "23999", "24000", "23990", "23995")], 1, cfg, struct_exit=True)
    assert p.exit_reason == "structural_invalidation"


def test_target_exit_and_short_direction_symmetry():
    p, cfg = _pos(direction="SHORT")
    assert p.direction == -1 and p.stop > p.entry > p.target
    ex.manage(p, [b(0, "23999", "24002", "23900", "23920")], 0, cfg)
    assert p.closed and p.exit_reason == "target"
    assert ex.finalize(p, cfg)["gross_pnl"] > 0


def test_session_square_off_before_close():
    p, cfg = _pos()
    late = mkbar(D0, 20, "24001", "24002", "24000", "24001")
    late = late.__class__(**{**late.__dict__, "ts": datetime(2026, 2, 2, 15, 20, tzinfo=late.ts.tzinfo)})
    p.bars_held = 0
    ex.manage(p, [late], 0, cfg)
    assert p.closed and p.exit_reason == "session_square_off"


def test_trailing_variant_only_trails_after_activation():
    cfg = VarConfig(exits={"exit_model": "structural_trail"})
    p, _ = _pos(cfg)
    r = p.risk_per_unit
    bars = [b(0, "24002", str(p.entry + r + 1), "24000", str(p.entry + r)),
            b(1, str(p.entry + r), str(p.entry + r + 2), str(p.entry + r - 1), str(p.entry + r + 1)),
            b(2, str(p.entry + r + 1), str(p.entry + D("1.6") * r), str(p.entry + r), str(p.entry + D("1.5") * r)),
            b(3, str(p.entry + D("1.5") * r), str(p.entry + D("1.7") * r), str(p.entry + D("1.4") * r), str(p.entry + D("1.6") * r))]
    for k in range(4):
        ex.manage(p, bars, k, cfg)
    assert p.partial_done and p.trail_active and p.stop > p.entry


# ---------------------------------------------------------------- backtest replay
def _data(flows=False):
    sess = prior_sessions(22)
    d = date(2026, 2, 2)
    bars = breakout_long_bars(d, flows=flows)
    # continue the day so the trade can resolve: drift up through target
    ref = bars[-1].close
    for k in range(10, 40):
        c = ref + (k - 9) * D("3")
        bars.append(mkbar(d, k, c - 1, c + 2, c - 2, c, 1500))
    sessions = {sd: sb for sd, sb in sess}
    sessions[d] = bars
    return InstrumentData(CONTRACT, sessions)


def test_backtest_deterministic_labelled_hypothetical_and_costed():
    cfg = VarConfig(variant="A", engines=("breakout",))
    r1 = run_backtest([_data()], cfg, D("2000000"), start=date(2026, 2, 2))
    r2 = run_backtest([_data()], cfg, D("2000000"), start=date(2026, 2, 2))
    assert r1.summary == r2.summary and [t["net_pnl"] for t in r1.trades] == [t["net_pnl"] for t in r2.trades]
    assert r1.hypothetical and r1.summary["hypothetical"] and "HYPOTHETICAL" in r1.assumptions["label"]
    assert r1.summary["trades"] == 1
    t = r1.trades[0]
    assert t["charges"] > 0 and t["net_pnl"] == t["gross_pnl"] - t["charges"]                      # reconciles exactly
    comp = sum((t[k] for k in ("brokerage", "exchange_charge", "ipft", "sebi", "stt", "stamp_duty", "gst", "other_charges")), D(0))
    assert comp == t["charges"] and t["charge_orders"][0]["order_id"] == "entry" and t["fee_schedule_version"]
    assert t["costs_verified"] is False and "stamp" in t["unverified_components"]                 # owner-stated futures stamp duty is flagged
    assert r1.assumptions["cost_rates_verified"] is False and r1.window["bars"] > 0
    assert t["conditions"] and t["derivation"] and t["strategy_version"] == "1.0.0"


def test_backtest_skips_sessions_without_prior_profile():
    d = date(2026, 2, 2)
    inst = InstrumentData(CONTRACT, {d: breakout_long_bars(d)})
    r = run_backtest([inst], VarConfig(variant="A", engines=("breakout",)), D("2000000"))
    assert r.summary["trades"] == 0 and r.skipped_sessions and "no prior session" in r.skipped_sessions[0]["reason"]


def test_chronological_splits_do_not_overlap_and_keep_order():
    days = [date(2026, 1, 1) + timedelta(days=k) for k in range(40)]
    s = chronological_splits(days)
    assert s["train"][1] < s["validation"][0] <= s["validation"][1] < s["out_of_sample"][0]
    with pytest.raises(ValueError):
        chronological_splits(days[:2])


def test_per_order_accounting_partial_exit_charges_three_brokerages_and_reconciles():
    from app.services import indian_charges as ic
    p, cfg = _pos(lots=2)
    r1 = p.entry + p.risk_per_unit
    bars = [b(0, "24002", str(r1 + 1), "24000", str(r1)), b(1, str(r1), str(r1 + 2), str(r1 - 1), str(r1)),
            b(2, str(r1), str(r1 + 1), str(p.entry - 5), str(p.entry - 2))]
    for k in range(3):
        ex.manage(p, bars, k, cfg)
    assert p.closed and len(p.fills) == 2                                # partial + final exit = two exit orders
    res = ex.finalize(p, cfg)
    assert [o["order_id"] for o in res["charge_orders"]] == ["entry", "exit1", "exit2"]      # three orders
    assert res["charges_raw"]["brokerage"] == "60"                                           # three brokerages, not one, not per lot
    assert res["net_pnl"] == res["gross_pnl"] - res["charges"]                              # reconciles exactly
    comp = sum((res[k] for k in ("brokerage", "exchange_charge", "ipft", "sebi", "stt", "stamp_duty", "gst", "other_charges")), D(0))
    assert comp == res["charges"] and res["fee_schedule_version"] == ic.SCHEDULE_VERSION
    assert res["turnover"] == sum((D(o["turnover"]) for o in res["charge_orders"]), D(0))
    assert res["lots"] == 2 and res["lot_size"] == 75                                       # lots are converted to units once: turnover uses lots*lot_size
    assert D(res["charge_orders"][0]["units"]) == D(2 * 75)


def test_var_funnel_reconciles_and_attributes_first_failed():
    from app.services.var.funnel import build_funnel
    r = run_backtest([_data()], VarConfig(variant="A", engines=("breakout",)), D("2000000"), start=date(2026, 2, 2))
    f = build_funnel(r.rejections, r.signals, r.trades, r.stage_counts)
    assert f["reconciles"] and f["trades_executed"] == len(r.trades)
    strat = f["stages"][0]
    assert strat["entered"] == f["candidates_generated"] == strat["rejected"] + strat["passed"]
    assert sum(x["n"] for x in strat["first_failed"]) == strat["rejected"]                  # exclusive attribution sums to the stage total
    assert f["pattern_stages"].get("BO:acceptance:long_or_up", 0) >= 1
