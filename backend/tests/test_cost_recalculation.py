"""Historical recalculation touches ONLY charges and derived net figures."""
from decimal import Decimal as D

from app.services.cost_recalculation import recalc_trade, recalculate_result
from app.services.transaction_costs import get_schedule, trade_charges
from tests.test_intraday_backtest import NSE, bars, defn, result, run, setbar

FROZEN = ("symbol", "side", "qty", "signal_time", "entry_time", "exit_time", "entry_raw", "entry_fill", "exit_raw", "exit_fill", "exit_reason", "gross_pnl", "slippage_cost",
          "stop_initial", "target", "holding_minutes")


def make_result():
    f = bars(NSE, 80)
    for i in range(30, 80):
        setbar(f, i, open=99.0, close=99.0, high=99.0, low=99.0)
    res = run(f, defn(sizing={"method": "fixed_quantity", "value": 500}, stop={"mode": "percent", "value": 2}), result(f, long_entry=5, long_exit=20, short_entry=40, short_exit=60), slippage=2, zero_costs=False)
    assert len(res["trades"]) == 2
    return res


def test_recalculation_changes_only_charges_and_net_and_keeps_originals():
    res = make_result()
    for t in res["trades"]:                    # simulate the old, wrong figures
        t["charges"], t["net_pnl"] = "999.00", str(D(t["net_pnl"]) + D(t["charges"]) - D("999.00"))
    old_metrics = dict(res["metrics"])
    new = recalculate_result(res, {"market": "equity_in"}, {"initial_capital": "1000000"})
    for before, after in zip(res["trades"], new["trades"]):
        assert all(before[k] == after[k] for k in FROZEN)                                    # nothing but fees moved
        expected = trade_charges(after["side"], D(after["qty"]), D(after["entry_fill"]), D(after["exit_fill"]), get_schedule("dhan_nse_intraday_2026_11"))
        assert D(after["charges"]) == expected.total and after["charges_before_recalculation"] == "999.00"
        sign = 1 if after["side"] == "long" else -1
        assert D(after["net_pnl"]) == ((D(after["exit_fill"]) - D(after["entry_fill"])) * D(after["qty"]) * sign - expected.total).quantize(D("0.01"))
    assert new["metrics"]["total_trades"] == old_metrics["total_trades"]
    assert new["cost_recalculation"]["old"]["total_charges"] == old_metrics["total_charges"] and new["cost_recalculation"]["new"]["total_charges"] == old_metrics["total_charges"]   # the engine already used the central service: recalculating is a no-op
    assert D(new["metrics"]["net_profit"]) == sum((D(t["net_pnl"]) for t in new["trades"]), D(0))
    assert new["metrics"]["slippage_is_separate_from_charges"] and new["trades"][0]["cost_breakdown"]["brokerage"]
    assert res["trades"][0]["charges"] == "999.00"                                           # input was not mutated


def test_recalculating_twice_is_stable():
    res = make_result()
    once = recalc_trade(res["trades"][0], get_schedule("dhan_nse_intraday_2026_11"))
    twice = recalc_trade(once, get_schedule("dhan_nse_intraday_2026_11"))
    assert once["charges"] == twice["charges"] and once["net_pnl"] == twice["net_pnl"]
