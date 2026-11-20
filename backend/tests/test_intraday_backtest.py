"""Deterministic fixtures for the event-driven backtester. Signals are injected directly so engine behaviour is tested in isolation.
Synthetic prices exist only to exercise execution logic; they are never market evidence."""
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.services.bar_quality import clean_and_validate
from app.services.intraday_backtest import BacktestConfig, InstrumentRun, run_backtest
from app.services.transaction_costs import get_schedule, trade_charges
from app.services.intraday_strategy import EvalResult, StrategyDefinition
from app.services.intraday_validation import in_out_of_sample, sensitivity, walk_forward
from app.services.timeframes import CRYPTO, EQUITY_IN

D = Decimal
ZERO_COSTS = {"brokerage_flat": 0, "brokerage_pct": 0, "stt_sell_pct": 0, "exchange_txn_pct": 0, "sebi_pct": 0, "stamp_buy_pct": 0, "ipft_pct": 0, "gst_pct": 0}
ZERO_CRYPTO = {"taker_pct": 0, "maker_pct": 0, "gst_pct": 0}


def bars(start: str, n: int, price: float = 100.0) -> pd.DataFrame:
    t = pd.date_range(start, periods=n, freq="min", tz="UTC")
    return pd.DataFrame({"time": t, "open": price, "high": price, "low": price, "close": price, "volume": 1000.0})


def setbar(f: pd.DataFrame, i: int, **kw) -> None:
    for k, v in kw.items():
        f.loc[i, k] = v


def result(f: pd.DataFrame, **rules) -> EvalResult:
    sig = {k: np.zeros(len(f), bool) for k in ("long_entry", "short_entry", "long_exit", "short_exit")}
    for k, idx in rules.items():
        for i in (idx if isinstance(idx, (list, tuple)) else [idx]):
            sig[k][i] = True
    return EvalResult(f["time"], np.arange(len(f)), sig, {}, {}, {}, [])


def defn(market="equity_in", **over) -> StrategyDefinition:
    d = {"name": "t", "market": market, "universe": [], "constraints": {"allow_short": True},
         "indicators": [], "long_entry": {"type": "compare", "left": {"kind": "price"}, "op": ">", "right": {"kind": "const", "value": 0}},
         "sizing": {"method": "fixed_quantity", "value": 10}}
    d.update(over)
    return StrategyDefinition.model_validate(d)


def run(f, d, res, market="equity_in", slippage=0, zero_costs=True, **cfg_over):
    profile = EQUITY_IN if market == "equity_in" else CRYPTO
    cfg = BacktestConfig(slippage_bps=slippage, cost_overrides=(ZERO_COSTS if market == "equity_in" else ZERO_CRYPTO) if zero_costs else {}, **cfg_over)
    quality = {"status": "ok", "issues": []}
    return run_backtest(d, [InstrumentRun("i1", "TEST", profile, f, res, quality)], cfg)


NSE = "2025-01-06 03:45"  # 09:15 IST


def test_entry_at_next_bar_open_not_signal_bar_close():
    f = bars(NSE, 60)
    setbar(f, 5, close=101.0, high=101.0)       # signal bar closes at 101
    setbar(f, 6, open=103.0, close=103.0, high=103.0, low=103.0)
    for i in range(7, 60):
        setbar(f, i, open=103.0, close=103.0, high=103.0, low=103.0)
    res = run(f, defn(), result(f, long_entry=5, long_exit=20))
    t = res["trades"][0]
    assert t["entry_raw"] == "103" and t["entry_fill"] == "103.00" and t["signal_time"] == t["entry_time"]
    assert pd.Timestamp(t["entry_time"]) == f["time"].iloc[6]


def test_signal_exit_fills_at_next_open_and_pnl_is_exact():
    f = bars(NSE, 60)
    for i in range(6, 60):
        setbar(f, i, open=110.0, close=110.0, high=110.0, low=110.0)
    setbar(f, 6, open=105.0, close=108.0, high=108.0, low=105.0)
    setbar(f, 21, open=112.0, close=112.0, high=112.0, low=112.0)
    res = run(f, defn(), result(f, long_entry=5, long_exit=20))
    t = res["trades"][0]
    assert pd.Timestamp(t["exit_time"]) == f["time"].iloc[21] and t["exit_reason"] == "signal_exit"
    assert t["entry_fill"] == "105.00" and t["exit_fill"] == "112.00" and t["net_pnl"] == "70.00" and t["gross_pnl"] == "70.00"


def test_stop_loss_target_and_trailing():
    f = bars(NSE, 60)
    d_stop = defn(stop={"mode": "percent", "value": 1})
    setbar(f, 10, low=98.5)                      # stop at 99 hit inside bar 10
    res = run(f, d_stop, result(f, long_entry=5))
    t = res["trades"][0]
    assert t["exit_reason"] == "stop_loss" and t["exit_fill"] == "99.00" and pd.Timestamp(t["exit_time"]) == f["time"].iloc[10]
    f = bars(NSE, 60)
    setbar(f, 12, high=102.5)                    # target 2% = 102 hit
    t = run(f, defn(stop={"mode": "percent", "value": 1}, target={"mode": "percent", "value": 2}), result(f, long_entry=5))["trades"][0]
    assert t["exit_reason"] == "target" and t["exit_fill"] == "102.00"
    f = bars(NSE, 60)
    for i in range(8, 60):
        v = 103.0 if i == 8 else 104.0 if i == 9 else 103.9
        setbar(f, i, open=v, close=v, high=v, low=v)
    setbar(f, 11, low=102.5)                     # fixed 1.00 trail below the high 104 -> 103.00, touched by low 102.5
    t = run(f, defn(trail={"mode": "percent", "value": 1}), result(f, long_entry=5))["trades"][0]
    assert t["exit_reason"] == "trailing_stop" and t["exit_fill"] == "103.00" and pd.Timestamp(t["exit_time"]) == f["time"].iloc[11]


def test_trailing_stop_does_not_use_the_current_bars_own_high():
    f = bars(NSE, 40)
    setbar(f, 8, high=110.0, low=95.0)           # one wild bar: high then crash. trail must be computed from bars BEFORE it
    t = run(f, defn(trail={"mode": "percent", "value": 1}), result(f, long_entry=5))["trades"][0]
    assert t["exit_reason"] == "trailing_stop" and t["exit_fill"] == "99.00"   # trail was 99 (from bars before it), not 109 from its own high


def test_gap_through_stop_fills_at_open_not_at_stop():
    f = bars(NSE, 40)
    for i in range(10, 40):
        setbar(f, i, open=95.0, close=95.0, high=95.0, low=95.0)
    t = run(f, defn(stop={"mode": "percent", "value": 1}), result(f, long_entry=5))["trades"][0]
    assert t["exit_reason"] == "stop_gap" and t["exit_fill"] == "95.00"


def test_stop_and_target_touched_in_same_bar_assumes_stop_and_flags_it():
    f = bars(NSE, 40)
    setbar(f, 10, high=103.0, low=98.0)
    t = run(f, defn(stop={"mode": "percent", "value": 1}, target={"mode": "percent", "value": 2}), result(f, long_entry=5))["trades"][0]
    assert t["exit_reason"] == "stop_loss" and t["ambiguous_stop_and_target_same_bar"] is True
    assert "stop assumed first" in run(f, defn(stop={"mode": "percent", "value": 1}), result(f, long_entry=5))["assumptions"]["same_bar_stop_and_target"]


def test_short_trade_and_gap_through_target():
    f = bars(NSE, 40)
    for i in range(12, 40):
        setbar(f, i, open=96.0, close=96.0, high=96.0, low=96.0)
    t = run(f, defn(target={"mode": "percent", "value": 2}), result(f, short_entry=5))["trades"][0]
    assert t["side"] == "short" and t["exit_reason"] == "target_gap" and t["exit_fill"] == "96.00" and t["net_pnl"] == "40.00"


def test_slippage_is_a_continuous_bps_move_and_charges_come_from_the_central_engine():
    f = bars(NSE, 40)
    for i in range(20, 40):
        setbar(f, i, open=110.0, close=110.0, high=110.0, low=110.0)
    res = run(f, defn(sizing={"method": "fixed_quantity", "value": 100}), result(f, long_entry=5, long_exit=19), slippage=10, zero_costs=False)
    t = res["trades"][0]
    assert t["entry_fill"] == "100.10" and t["exit_fill"] == "109.89"        # 10 bps of 100 and of 110, not rounded to the 0.05 tick
    ch = trade_charges("long", D(100), D("100.10"), D("109.89"), get_schedule("dhan_nse_intraday_2026_11"))
    assert D(t["charges"]) == ch.total and t["cost_breakdown"]["total"] == str(ch.total) and t["cost_breakdown"]["brokerage"] == "6.30"   # 0.03% of 10,010 = 3.003 and of 10,989 = 3.2967: percentage, below the cap
    assert D(t["slippage_cost"]) == D("10.00") + D("11.00")                   # 0.10 x 100 + 0.11 x 100
    assert D(t["gross_pnl"]) == D("1000.00")                                  # raw prices, unchanged by fees or slippage
    assert D(t["net_pnl"]) == (D("109.89") - D("100.10")) * 100 - ch.total


def test_low_priced_share_slippage_is_not_inflated_by_tick_rounding():
    f = bars(NSE, 40, 13.0)
    for i in range(20, 40):
        setbar(f, i, open=13.0, close=13.0, high=13.0, low=13.0)
    t = run(f, defn(sizing={"method": "fixed_quantity", "value": 7000}), result(f, long_entry=5, long_exit=19), slippage=2, zero_costs=False)["trades"][0]
    # 2 bps of 13.00 = 0.0026 per side; the old model rounded each side up to a whole 0.05 tick (about 0.38% of price)
    assert t["entry_fill"] == "13.0026" and t["exit_fill"] == "12.9974"
    assert D(t["slippage_cost"]) == D("36.40")                                # 0.0026 x 7,000 x 2
    ticks = run(f, defn(sizing={"method": "fixed_quantity", "value": 7000}), result(f, long_entry=5, long_exit=19), slippage=2, zero_costs=False, slippage_model="ticks", slippage_ticks=1)["trades"][0]
    assert ticks["entry_fill"] == "13.05" and ticks["exit_fill"] == "12.95"   # an explicit, labelled tick model is still available


def test_every_exit_reason_gets_the_same_central_fee_treatment():
    prof = get_schedule("dhan_nse_intraday_2026_11")
    cases = []
    f = bars(NSE, 40); setbar(f, 10, low=98.5)
    cases.append(run(f, defn(sizing={"method": "fixed_quantity", "value": 1000}, stop={"mode": "percent", "value": 1}), result(f, long_entry=5), zero_costs=False))
    f = bars(NSE, 40)
    for i in range(10, 40):
        setbar(f, i, open=95.0, close=95.0, high=95.0, low=95.0)
    cases.append(run(f, defn(sizing={"method": "fixed_quantity", "value": 1000}, stop={"mode": "percent", "value": 1}), result(f, long_entry=5), zero_costs=False))
    f = bars(NSE, 40); setbar(f, 12, high=102.5)
    cases.append(run(f, defn(sizing={"method": "fixed_quantity", "value": 1000}, stop={"mode": "percent", "value": 1}, target={"mode": "percent", "value": 2}), result(f, long_entry=5), zero_costs=False))
    reasons = set()
    for res in cases:
        t = res["trades"][0]
        reasons.add(t["exit_reason"])
        expected = trade_charges(t["side"], D(t["qty"]), D(t["entry_fill"]), D(t["exit_fill"]), prof)
        assert D(t["charges"]) == expected.total, t["exit_reason"]
        assert t["cost_breakdown"]["brokerage"] == str(expected.brokerage) and t["cost_breakdown"]["brokerage"] == "40.00"   # one order each way
    assert reasons == {"stop_loss", "stop_gap", "target"}


def test_short_trade_charges_use_sell_then_buy_and_totals_reconcile():
    f = bars(NSE, 40)
    for i in range(12, 40):
        setbar(f, i, open=96.0, close=96.0, high=96.0, low=96.0)
    res = run(f, defn(sizing={"method": "fixed_quantity", "value": 1000}), result(f, short_entry=5, short_exit=11), zero_costs=False)
    t = res["trades"][0]
    b = t["cost_breakdown"]
    assert t["side"] == "short" and D(b["sell_turnover"]) == D("100000") and D(b["buy_turnover"]) == D("96000")     # entry sell 1,000 x 100 ; exit buy 1,000 x 96
    assert b["stt"] == "25" and b["stamp_duty"] == "3"                                                              # STT on the sell leg, stamp on the buy leg (2.88 -> 3)
    assert D(t["net_pnl"]) == D(t["gross_pnl"]) - D(t["slippage_cost"]) - D(t["charges"])
    m = res["metrics"]
    assert m["total_turnover"] == "196000.00" and m["total_buy_turnover"] == "96000.00" and m["cost_components"]["stt"] == "25.00"
    assert abs(m["charges_pct_of_turnover"] - float(D(t["charges"]) / D(196000) * 100)) < 0.0001
    assert m["slippage_is_separate_from_charges"] is True and D(m["net_profit"]) == D(t["net_pnl"])


def test_equity_square_off_and_no_overnight_positions():
    f = bars(NSE, 375)
    res = run(f, defn(), result(f, long_entry=5))
    t = res["trades"][0]
    assert t["exit_reason"] == "square_off"
    assert pd.Timestamp(t["exit_time"]).tz_convert("Asia/Kolkata").strftime("%H:%M") == "15:15"
    two = pd.concat([bars(NSE, 375), bars("2025-01-07 03:45", 375)], ignore_index=True)
    res = run(two, defn(session={"square_off": "15:25"}), result(two, long_entry=370))  # signal after the day's last entry window
    assert res["skipped_signals"].get("after_no_entry_time", 0) == 1 and res["metrics"]["total_trades"] == 0


def test_no_entries_after_cutoff_and_missing_squareoff_bar_forces_session_end():
    f = bars(NSE, 375)
    f = f.drop(index=range(355, 375)).reset_index(drop=True)   # data stops 15:10: the 15:15 bar does not exist
    nxt = pd.concat([f, bars("2025-01-07 03:45", 30)], ignore_index=True)
    res = run(nxt, defn(), result(nxt, long_entry=300))
    t = res["trades"][0]
    assert t["exit_reason"] == "session_end" and pd.Timestamp(t["exit_time"]).date() == pd.Timestamp("2025-01-06").date()


def test_crypto_holds_overnight_and_over_weekend_without_session_rules():
    f = bars("2025-01-03 20:00", 60 * 60)    # Fri 20:00 -> Sun 08:00 UTC
    res = run(f, defn(market="crypto", sizing={"method": "fixed_quantity", "value": 1}), result(f, long_entry=5), market="crypto")
    t = res["trades"][0]
    assert t["exit_reason"] == "end_of_data" and t["holding_minutes"] > 2000
    assert res["assumptions"]["square_off_minute_local"] is None


def test_missing_candle_blocks_fill_instead_of_using_stale_price():
    f = bars(NSE, 60).drop(index=[6, 7, 8, 9, 10, 11, 12]).reset_index(drop=True)  # 7-minute hole after the signal bar
    res = run(f, defn(), result(f, long_entry=5))
    assert res["metrics"]["total_trades"] == 0 and res["skipped_signals"] == {"no_contiguous_next_bar": 1}


def test_duplicate_and_invalid_candles_are_removed_before_simulation():
    f = bars(NSE, 40)
    dup = pd.concat([f, f.iloc[[3]]]).sort_values("time").reset_index(drop=True)
    clean, rep = clean_and_validate(dup, EQUITY_IN, now=pd.Timestamp("2025-03-01", tz="UTC"))
    assert rep["duplicates"] == 1 and len(clean) == 40


def test_insufficient_history_is_refused_by_quality_check():
    f = bars(NSE, 40).iloc[::2].reset_index(drop=True)
    _, rep = clean_and_validate(f, EQUITY_IN, now=pd.Timestamp("2025-03-01", tz="UTC"))
    assert rep["status"] == "refuse"


def test_long_and_short_both_directions_and_direction_filter():
    f = bars(NSE, 80)
    for i in range(30, 80):
        setbar(f, i, open=99.0, close=99.0, high=99.0, low=99.0)
    res = run(f, defn(), result(f, long_entry=5, long_exit=20, short_entry=40, short_exit=60))
    assert [t["side"] for t in res["trades"]] == ["long", "short"]
    only_long = run(f, defn(), result(f, long_entry=5, long_exit=20, short_entry=40, short_exit=60), direction="long")
    assert [t["side"] for t in only_long["trades"]] == ["long"]


def test_conflicting_signals_are_skipped_and_reported():
    f = bars(NSE, 40)
    res = run(f, defn(), result(f, long_entry=5, short_entry=5))
    assert res["metrics"]["total_trades"] == 0 and res["skipped_signals"] == {"conflicting_long_and_short_signal": 1}


def test_multiple_instruments_and_concurrency_cap_with_shared_capital():
    f = bars(NSE, 60)
    runs = [InstrumentRun("a", "AAA", EQUITY_IN, f, result(f, long_entry=5, long_exit=30), {"status": "ok", "issues": []}),
            InstrumentRun("b", "BBB", EQUITY_IN, f, result(f, long_entry=6, long_exit=30), {"status": "ok", "issues": []})]
    cfg = BacktestConfig(slippage_bps=0, cost_overrides=ZERO_COSTS, max_concurrent=1)
    res = run_backtest(defn(), runs, cfg)
    assert res["metrics"]["total_trades"] == 1 and res["skipped_signals"] == {"max_concurrent_positions": 1}
    both = run_backtest(defn(), runs, BacktestConfig(slippage_bps=0, cost_overrides=ZERO_COSTS, max_concurrent=2))
    assert both["metrics"]["total_trades"] == 2 and set(both["by_instrument"]) == {"AAA", "BBB"}


def test_capital_limit_blocks_oversized_positions():
    f = bars(NSE, 60)
    d = defn(sizing={"method": "fixed_notional", "value": 5_000_000})
    res = run(f, d, result(f, long_entry=5, long_exit=30), initial_capital=D("100000"))
    assert res["metrics"]["total_trades"] == 0 and res["skipped_signals"] == {"insufficient_capital": 1}


def test_risk_percent_sizing_uses_stop_distance():
    f = bars(NSE, 60)
    d = defn(sizing={"method": "risk_percent", "value": 1}, stop={"mode": "percent", "value": 1})
    res = run(f, d, result(f, long_entry=5, long_exit=30), initial_capital=D("100000"))
    t = res["trades"][0]
    assert t["qty"] == "1000"   # risk 1% of 100,000 = 1,000 over a 1.00 stop distance


def test_no_lookahead_changing_future_prices_cannot_change_earlier_trades():
    f = bars(NSE, 120)
    for i in range(6, 120):
        setbar(f, i, open=100 + i * 0.1, close=100 + i * 0.1, high=100 + i * 0.1 + 0.05, low=100 + i * 0.1 - 0.05)
    r = result(f, long_entry=5, long_exit=40)
    base = run(f, defn(), r)["trades"][0]
    g = f.copy()
    for i in range(60, 120):
        setbar(g, i, open=1.0, close=1.0, high=1.0, low=1.0)
    changed = run(g, defn(), result(g, long_entry=5, long_exit=40))["trades"][0]
    assert base == changed


def test_run_is_reproducible_bit_for_bit():
    f = bars(NSE, 200)
    for i in range(6, 200):
        setbar(f, i, open=100 + (i % 7) * 0.2, close=100 + (i % 5) * 0.3, high=101.5, low=98.5)
    d = defn(stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2})
    r1 = run(f, d, result(f, long_entry=[5, 50, 90], short_entry=[30, 130]), slippage=3, zero_costs=False)
    r2 = run(f, d, result(f, long_entry=[5, 50, 90], short_entry=[30, 130]), slippage=3, zero_costs=False)
    assert r1 == r2 and r1["dataset"]["sha256"] == r2["dataset"]["sha256"] and r1["versions"]["engine"].startswith("intraday-engine/")


def test_metrics_and_reporting_contract():
    f = bars(NSE, 200)
    for i in range(6, 200):
        setbar(f, i, open=100 + (i % 7) * 0.2, close=100 + (i % 5) * 0.3, high=101.5, low=98.5)
    res = run(f, defn(stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2}), result(f, long_entry=[5, 50, 90], short_entry=[30, 130]), slippage=3, zero_costs=False)
    m = res["metrics"]
    for key in ("total_trades", "win_rate", "net_profit", "gross_pnl_before_costs", "total_charges", "total_slippage", "max_drawdown_pct", "profit_factor_net",
                "expectancy_per_trade", "max_consecutive_wins", "max_consecutive_losses", "avg_holding_minutes", "return_pct"):
        assert key in m
    assert D(m["net_profit"]) == sum(D(t["net_pnl"]) for t in res["trades"])
    assert D(m["gross_pnl_before_costs"]) - D(m["total_slippage"]) - D(m["total_charges"]) == pytest.approx(D(m["net_profit"]), abs=D("0.05"))
    assert m["sharpe_ratio"] is None and "needs" in m["ratios_note"]          # too little data: no misleading ratios
    assert any("too small a sample" in w for w in res["warnings"])
    assert res["assumptions"]["cost_profile"]["assumed"] is True and res["equity_curve"] and res["benchmark"]["per_instrument"]
    assert res["sample"]["reliable"] is False


def test_in_out_of_sample_walk_forward_and_sensitivity_structure():
    days = [bars(f"2025-01-{d:02d} 03:45", 375, 100 + d) for d in range(6, 31) if pd.Timestamp(f"2025-01-{d:02d}").dayofweek < 5]
    for k, fr in enumerate(days):
        fr["close"] = fr["open"] + np.sin(np.arange(len(fr)) / 9.0) * 0.5 + k * 0.01
        fr["high"] = np.maximum(fr["open"], fr["close"]) + 0.05
        fr["low"] = np.minimum(fr["open"], fr["close"]) - 0.05
    f = pd.concat(days, ignore_index=True)
    d = StrategyDefinition.model_validate({
        "name": "wf", "decision_timeframe": "5m", "constraints": {"allow_short": False}, "sizing": {"method": "fixed_quantity", "value": 10},
        "indicators": [{"alias": "f", "indicator": "ema", "params": {"length": 5}, "timeframe": "5m"}, {"alias": "s", "indicator": "ema", "params": {"length": 12}, "timeframe": "5m"}],
        "long_entry": {"type": "compare", "left": {"kind": "indicator", "ref": "f"}, "op": "crosses_above", "right": {"kind": "indicator", "ref": "s"}},
        "long_exit": {"type": "compare", "left": {"kind": "indicator", "ref": "f"}, "op": "crosses_below", "right": {"kind": "indicator", "ref": "s"}}})
    from app.services.intraday_runner import make_backtester
    bt = make_backtester(None)
    runs = [InstrumentRun("a", "AAA", EQUITY_IN, f, None, {"status": "ok", "issues": []})]  # type: ignore[arg-type]
    cfg = BacktestConfig(slippage_bps=1, cost_overrides=ZERO_COSTS)
    io = in_out_of_sample(d, runs, cfg, bt)
    assert io["in_sample"]["total_trades"] > 0 and pd.Timestamp(io["split_at"]) > f["time"].iloc[0]
    sens = sensitivity(d, runs, cfg, bt, ["f.length", "s.length"])
    assert sens["window"] == "in-sample only" and len(sens["grid"]) >= 4
    wf = walk_forward(d, runs, cfg, bt, train_days=8, test_days=4, keys=["f.length"])
    assert wf["folds"] and all(pd.Timestamp(x["test"][0]) >= pd.Timestamp(x["train"][1]) for x in wf["folds"])
    assert all("chosen_params" in x and "test_result" in x for x in wf["folds"]) and "train windows only" in wf["optimised_on"]


# ───────────── stop at an indicator level (e.g. the 5-EMA alert candle's high) ─────────────
def level_result(f: pd.DataFrame, *, short_at=None, long_at=None, short_level=None, long_level=None) -> EvalResult:
    r = result(f, **{k: v for k, v in (("short_entry", short_at), ("long_entry", long_at)) if v is not None})
    n = len(f)
    r.stop_levels = {side: np.full(n, lvl) for side, lvl in (("short", short_level), ("long", long_level)) if lvl is not None}
    return r


def level_defn(**over):
    d = dict(indicators=[{"alias": "al", "indicator": "ema_alert_candle", "params": {"length": 5}, "timeframe": "5m"}],
             short_entry={"type": "compare", "left": {"kind": "price"}, "op": ">", "right": {"kind": "const", "value": 0}},
             stop={"mode": "level", "short_level": {"kind": "indicator", "ref": "al", "output": "short_stop"}, "long_level": {"kind": "indicator", "ref": "al", "output": "long_stop"}},
             target={"mode": "rr", "value": 3})
    d.update(over)
    return defn(**d)


def test_level_stop_uses_the_signal_candles_level_not_an_atr_and_sizes_the_rr_target_from_it():
    f = bars(NSE, 60)
    setbar(f, 9, high=104.0)                                   # price trades up to 104 after the short entry at 100 (level 103): stopped at 103? no - through it
    t = run(f, level_defn(), level_result(f, short_at=5, short_level=103.0))["trades"][0]
    assert t["side"] == "short" and t["exit_reason"] == "stop_loss" and t["exit_fill"] == "103.00"
    g = bars(NSE, 60)
    setbar(g, 12, low=90.0)                                    # target = 100 - 3 x (103 - 100) = 91
    t2 = run(g, level_defn(), level_result(g, short_at=5, short_level=103.0))["trades"][0]
    assert t2["exit_reason"] == "target" and t2["exit_fill"] == "91.00"


def test_long_level_stop_mirrors_it_and_a_buffer_moves_the_stop_beyond_the_level():
    f = bars(NSE, 60)
    setbar(f, 9, low=96.0)
    d = level_defn(long_entry={"type": "compare", "left": {"kind": "price"}, "op": ">", "right": {"kind": "const", "value": 0}}, short_entry=None,
                   stop={"mode": "level", "value": 1.0, "long_level": {"kind": "indicator", "ref": "al", "output": "long_stop"}})
    t = run(f, d, level_result(f, long_at=5, long_level=98.0))["trades"][0]
    assert t["side"] == "long" and t["exit_reason"] == "stop_loss" and t["exit_fill"] == "97.00"     # 98 less a 1% buffer = 97.02, rounded to the 0.05 tick


def test_a_level_the_price_has_already_passed_or_a_missing_level_opens_no_trade_and_says_why():
    f = bars(NSE, 40)
    through = run(f, level_defn(), level_result(f, short_at=5, short_level=99.0))               # short stop BELOW the entry price: invalid
    assert through["trades"] == [] and through["skipped_signals"].get("stop_level_beyond_entry") == 1
    unknown = level_result(f, short_at=5, short_level=103.0)
    unknown.stop_levels["short"][:] = np.nan
    res = run(f, level_defn(), unknown)
    assert res["trades"] == [] and res["skipped_signals"].get("stop_level_unavailable") == 1


def test_the_level_is_read_on_the_signal_candle_so_a_later_level_cannot_leak_in():
    f = bars(NSE, 40)
    r = level_result(f, short_at=5, short_level=103.0)
    r.stop_levels["short"][6:] = 150.0                          # changes only AFTER the signal candle (bar 5)
    setbar(f, 9, high=104.0)
    t = run(f, level_defn(), r)["trades"][0]
    assert t["exit_fill"] == "103.00"                           # still the level known at bar 5


def test_validation_requires_a_declared_level_operand_for_each_enabled_direction():
    from app.services.intraday_strategy import validate_definition
    good = level_defn()
    assert not [i for i in validate_definition(good)[0] if i["level"] == "error"]
    missing = level_defn(stop={"mode": "level", "long_level": None, "short_level": None})
    assert any("short_level" in i["message"] for i in validate_definition(missing)[0] if i["level"] == "error")
    undeclared = level_defn(stop={"mode": "level", "short_level": {"kind": "indicator", "ref": "nope", "output": "short_stop"}})
    assert any("not declared" in i["message"] for i in validate_definition(undeclared)[0] if i["level"] == "error")
    shifted = level_defn(stop={"mode": "level", "short_level": {"kind": "indicator", "ref": "al", "output": "short_stop", "offset": 1}})
    assert any("offset 0" in i["message"] for i in validate_definition(shifted)[0] if i["level"] == "error")
