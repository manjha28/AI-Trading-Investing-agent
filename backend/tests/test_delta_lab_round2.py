"""Round-2 helpers: quarter windows, the median train score, the deflated-Sharpe luck bound, and the Binance 1-second reduction. TEST data only."""
import io
import zipfile

import numpy as np

from app.delta_lab.binance_data import aggtrades_day_to_1s
from app.delta_lab.round2_run import by_quarter, deflated, quarter, quarters_between, score

T = 1_700_000_000  # 2023-11-14


def test_quarters_cover_the_range_in_order():
    q = quarters_between(1_577_836_800, 1_640_995_199)         # 2020-01-01 .. 2021-12-31 23:59:59
    assert q[0] == "2020-Q1" and q[-1] == "2021-Q4" and len(q) == 8 and quarter(1_640_995_199) == "2021-Q4"


def test_the_choice_score_is_the_median_quarter_not_the_best_one():
    qs = ["2020-Q1", "2020-Q2", "2020-Q3", "2020-Q4"]
    def trades(vals):
        return [{"entry_time": 1_580_000_000 + i * 8_000_000, "net": v} for i, v in enumerate(vals)]
    lucky = trades([100.0, -1.0, -1.0, -1.0])                    # one great quarter
    steady = trades([2.0, 2.0, 2.0, 2.0])
    assert score(lucky, qs) < score(steady, qs) and by_quarter(lucky)["2020-Q1"] == 100.0


def test_deflation_makes_a_best_of_many_result_less_convincing():
    rng = np.random.default_rng(1)
    pnl = rng.normal(0.05, 1.0, 250)                              # a modest daily edge over a year (test data)
    few, many = deflated(pnl, 2, 1e-4), deflated(pnl, 500, 1e-4)
    assert many["p_luck_best_of_n"] > few["p_luck_best_of_n"] and many["deflated_sharpe_prob"] < few["deflated_sharpe_prob"]
    assert deflated(np.zeros(100), 10, 1e-4)["p_luck_best_of_n"] is None


def test_aggtrades_reduce_to_one_second_bars_with_signed_volume():
    rows = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n" + "\n".join([
        f"1,100.0,2.0,1,1,{T * 1000 + 100},false", f"2,101.0,1.0,2,2,{T * 1000 + 900},true", f"3,99.0,4.0,3,3,{(T + 1) * 1000 + 5},false"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", rows)
    out = aggtrades_day_to_1s(buf.getvalue())
    first, second = out.iloc[0], out.iloc[1]
    assert first["open"] == 100.0 and first["high"] == 101.0 and first["close"] == 101.0 and first["volume"] == 3.0 and first["buy"] == 2.0 and first["sell"] == 1.0
    assert second["time"] == T + 1 and second["buy"] == 4.0 and second["sell"] == 0.0
