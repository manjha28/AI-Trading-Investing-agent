"""Delivery (CNC) equity charges, Phase 2b-f. Expected values are computed by hand from the rates on Dhan's pricing page (read 2026-11-22), not by calling the calculator:
buy 100 x Rs 1,000 = Rs 1,00,000 on 2026-06-15; sell 100 x Rs 1,010 = Rs 1,01,000. Test data only."""
from datetime import date
from decimal import Decimal as D

import pytest

from app.services import indian_charges as ic

BUY_DAY = date(2026, 6, 15)


def _order(side, price, qty=100, day=BUY_DAY, segment="equity_delivery", ttype="delivery"):
    return ic.calculate_trade_charges(exchange="NSE", segment=segment, instrument_type="cash", broker_profile="dhan", trading_date=day, order_id=f"t-{side}", side=side,
                                      execution_price=D(price), executed_quantity=D(qty), transaction_type=ttype)


def test_delivery_buy_by_hand():
    b = _order("buy", "1000")
    r = b.rounded
    assert b.turnover == D("100000")
    assert r["brokerage"] == D("0.00") and r["stt"] == D("100") and r["stamp"] == D("15")          # STT 0.1% on buy; stamp 0.015% on buy
    assert r["exchange"] == D("3.07") and r["ipft"] == D("0.00") and r["sebi"] == D("0.10")        # 0.0030699% -> 3.0699; 0.0001% -> 0.10
    assert r["gst"] == D("0.57") and r["dp"] == D("0.00")                                          # 18% x (3.0699 + 0.0001 + 0.1) = 0.5706; no DP charge on a buy
    assert b.total == D("118.74") and b.verified


def test_delivery_sell_by_hand_has_stt_and_a_dp_charge_with_gst_and_no_stamp():
    s = _order("sell", "1010")
    r = s.rounded
    assert r["stt"] == D("101") and r["stamp"] == D("0") and r["brokerage"] == D("0.00")
    assert r["exchange"] == D("3.10") and r["sebi"] == D("0.10") and r["dp"] == D("12.50")
    assert r["gst"] == D("2.83")                                                                   # 18% x (3.100599 + 0.000101 + 0.101 + 12.50 DP) = 2.8263
    assert s.total == D("119.53")


def test_round_trip_helper_matches_the_two_orders():
    rt = ic.delivery_round_trip(buy_price=D("1000"), sell_price=D("1010"), quantity=D("100"), buy_date=BUY_DAY, sell_date=BUY_DAY)
    assert rt["total"] == D("238.27") and rt["buy"] == D("118.74") and rt["sell"] == D("119.53")
    assert rt["pct_of_buy_turnover"] == pytest.approx(D("0.23827"), abs=D("0.00001"))
    assert rt["unverified_components"] == []


def test_a_small_position_pays_proportionally_more_because_the_dp_charge_is_flat():
    big = ic.delivery_round_trip(buy_price=D("1000"), sell_price=D("1000"), quantity=D("100"), buy_date=BUY_DAY, sell_date=BUY_DAY)
    small = ic.delivery_round_trip(buy_price=D("1000"), sell_price=D("1000"), quantity=D("10"), buy_date=BUY_DAY, sell_date=BUY_DAY)
    assert small["pct_of_buy_turnover"] > big["pct_of_buy_turnover"] * D("1.3")


def test_exchange_rate_before_and_after_the_2026_03_01_revision():
    before = _order("buy", "1000", day=date(2026, 2, 27))
    after = _order("buy", "1000", day=date(2026, 3, 2))
    assert before.raw["exchange"] == D("100000") * D("0.00297") / 100
    assert after.raw["exchange"] == D("100000") * D("0.0030699") / 100


def test_segments_never_cross_and_old_dates_are_refused():
    with pytest.raises(ic.ScheduleUnavailable):
        _order("buy", "1000", segment="equity_delivery", ttype="intraday")          # a delivery schedule cannot price an intraday order
    with pytest.raises(ic.ScheduleUnavailable):
        _order("buy", "1000", segment="equity_cash", ttype="delivery")              # and the intraday schedule still refuses delivery
    with pytest.raises(ic.ScheduleUnavailable):
        _order("buy", "1000", day=date(2024, 9, 30))                                # before the first declared rate


def test_intraday_equity_charges_are_unchanged_by_the_delivery_addition():
    o = ic.calculate_trade_charges(exchange="NSE", segment="equity_cash", instrument_type="cash", broker_profile="dhan", trading_date=BUY_DAY, order_id="i1", side="sell",
                                   execution_price=D("1000"), executed_quantity=D("100"))
    assert o.rounded["stt"] == D("25") and o.rounded["dp"] == D("0.00") and o.rounded["brokerage"] == D("20.00") and o.rounded["stamp"] == D("0")
    assert o.raw["gst"] == (D("20") + o.raw["exchange"] + o.raw["ipft"] + o.raw["sebi"]) * D("0.18")      # GST base still excludes STT and stamp, and no DP in intraday
