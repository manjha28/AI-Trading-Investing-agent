"""Authoritative charge calculator. Every expected number below was computed BY HAND from the verified rates (see the
comments), never taken from the calculator's own output. Dates: futures STT 0.02% before 2026-04-01 / 0.05% after;
NSE transaction charge changed 2026-03-01."""
from datetime import date
from decimal import Decimal as D

import pytest

from app.services import indian_charges as ic

FUT = dict(exchange="NSE", segment="futures", instrument_type="index_future", broker_profile="dhan")
CASH = dict(exchange="NSE", segment="equity_cash", instrument_type="cash", broker_profile="dhan")
NOV = date(2026, 11, 2)


def fut(side, price, qty, day=NOV, oid="o", **kw):
    return ic.calculate_trade_charges(**FUT, trading_date=day, order_id=oid, side=side, execution_price=D(price), executed_quantity=D(qty), **kw)


def cash(side, price, qty, day=NOV, oid="o"):
    return ic.calculate_trade_charges(**CASH, trading_date=day, order_id=oid, side=side, execution_price=D(price), executed_quantity=D(qty))


# A: long futures round trip. 1 lot = 65 units. buy 26000 -> turnover 1,690,000; sell 26100 -> turnover 1,696,500.
def test_A_long_futures_round_trip_each_charge():
    b = fut("buy", 26000, 65)
    assert b.turnover == D("1690000")
    assert b.rounded == {"brokerage": D("20.00"), "stt": D("0"), "exchange": D("30.93"), "ipft": D("0.00"), "sebi": D("1.69"), "stamp": D("34"), "gst": D("9.47"), "dp": D("0.00")}
    assert b.raw["stamp"] == D("33.80000") and b.raw["exchange"] == D("30.925310000")        # 1.69e6 * 0.0018299% ; 1.69e6 * 0.002%
    assert b.total == D("96.09")                                                              # 20+30.93+0+1.69+34+9.47
    s = fut("sell", 26100, 65)
    assert s.rounded == {"brokerage": D("20.00"), "stt": D("848"), "exchange": D("31.04"), "ipft": D("0.00"), "sebi": D("1.70"), "stamp": D("0"), "gst": D("9.49"), "dp": D("0.00")}
    assert s.total == D("910.23")                                                             # 20+848+31.04+1.70+9.49
    assert b.total + s.total == D("1006.32")


# B: short futures: STT on the OPENING sell, stamp on the CLOSING buy. Same prices, opposite order.
def test_B_short_futures_stt_on_open_stamp_on_close():
    open_sell = fut("sell", 26100, 65, oid="open")
    close_buy = fut("buy", 26000, 65, oid="close")
    assert open_sell.rounded["stt"] == D("848") and open_sell.rounded["stamp"] == D("0")
    assert close_buy.rounded["stamp"] == D("34") and close_buy.rounded["stt"] == D("0")
    assert open_sell.total + close_buy.total == D("1006.32")


# C: partial exits = three orders = three brokerages (not one, not per lot).
def test_C_partial_exits_three_brokerages():
    entry = fut("buy", 26000, 130, oid="e")
    p1 = fut("sell", 26100, 65, oid="p1")
    p2 = fut("sell", 26200, 65, oid="p2")
    agg = ic.sum_charges([entry, p1, p2])
    assert agg["orders"] == 3 and agg["raw"]["brokerage"] == D("60")
    assert p1.raw["brokerage"] == D("20") and p2.raw["brokerage"] == D("20")
    assert p1.rounded["stt"] == D("848") and p2.rounded["stt"] == D("852")                    # 1,696,500*0.05% = 848.25 -> 848 ; 1,703,000*0.05% = 851.50 -> half up 852


# D: one order, several fills -> brokerage once; turnover aggregated.
def test_D_one_order_multiple_fills_single_brokerage():
    b = ic.calculate_trade_charges(**FUT, trading_date=NOV, order_id="x", side="sell", fills=[(D("26100"), D("30")), (D("26100"), D("35"))])
    assert b.raw["brokerage"] == D("20") and b.turnover == D("1696500") and b.rounded["stt"] == D("848")


# E: STT history. sell turnover 1,696,500: 0.02% = 339.30 -> 339 before 2026-04-01; 0.05% = 848.25 -> 848 on/after.
def test_E_historical_stt():
    assert fut("sell", 26100, 65, day=date(2026, 3, 31)).rounded["stt"] == D("339")
    assert fut("sell", 26100, 65, day=date(2026, 4, 1)).rounded["stt"] == D("848")


# F: NSE transaction charge across 2026-03-01. futures buy turnover 1,690,000: 0.00173% = 29.237 ; 0.0018299% = 30.92531.
#    cash turnover 1,000,000: 0.00297% = 29.70 ; 0.0030699% = 30.699.
def test_F_exchange_charge_across_2026_03_01():
    assert fut("buy", 26000, 65, day=date(2026, 2, 27)).rounded["exchange"] == D("29.24")
    assert fut("buy", 26000, 65, day=date(2026, 3, 1)).rounded["exchange"] == D("30.93")
    assert cash("buy", 1000, 1000, day=date(2026, 2, 27)).rounded["exchange"] == D("29.70")
    assert cash("buy", 1000, 1000, day=date(2026, 3, 1)).rounded["exchange"] == D("30.70")


# G: GST base = brokerage + exchange + IPFT + SEBI. NOT STT, NOT stamp. sell leg base = 20 + 31.0442535 + 0.0016965 + 1.6965 = 52.74245 -> *18% = 9.493641
def test_G_gst_base_excludes_stt_and_stamp():
    s = fut("sell", 26100, 65)
    assert s.raw["gst"] == (D("20") + s.raw["exchange"] + s.raw["ipft"] + s.raw["sebi"]) * D("0.18")
    assert s.rounded["gst"] == D("9.49")
    assert s.rounded["gst"] < D("20")                                                          # would be ~162.20 if STT were in the base
    assert fut("buy", 26000, 65).rounded["gst"] == D("9.47")                                   # stamp (34) not in the base either


# H: no IPFT double count. 1 crore turnover: exchange 182.99 + IPFT 0.01 = 183.00 total, not 183.00 + 0.01.
def test_H_ipft_not_double_counted():
    b = fut("buy", 1000, 10000)
    assert b.turnover == D("10000000")
    assert b.raw["exchange"] == D("182.9900000") and b.raw["ipft"] == D("0.0100000")
    assert b.raw["exchange"] + b.raw["ipft"] == D("183.0000000")


# I: lots -> units conversion exactly once (historical lot size supplied by the caller for that date).
def test_I_lot_conversion_once():
    by_lots = ic.calculate_trade_charges(**FUT, trading_date=NOV, order_id="l", side="buy", execution_price=D("26000"), executed_quantity=D("1"), lot_size=65, quantity_in_lots=True)
    by_units = fut("buy", 26000, 65)
    assert by_lots.units == D("65") and by_lots.turnover == D("1690000") and by_lots.raw == by_units.raw
    two = ic.calculate_trade_charges(**FUT, trading_date=NOV, order_id="l2", side="buy", execution_price=D("26000"), executed_quantity=D("2"), lot_size=65, quantity_in_lots=True)
    assert two.turnover == D("3380000") and two.raw["brokerage"] == D("20")                    # not per lot
    with pytest.raises(ic.InvalidExecution):
        ic.calculate_trade_charges(**FUT, trading_date=NOV, order_id="l3", side="buy", execution_price=D("26000"), executed_quantity=D("1"), quantity_in_lots=True)


# J: invalid / missing schedules raise; nothing silently returns zero.
def test_J_invalid_or_missing_schedule_raises():
    with pytest.raises(ic.ScheduleUnavailable):
        fut("buy", 26000, 65, day=date(2024, 9, 30))                                           # before the first declared rate
    with pytest.raises(ic.ScheduleUnavailable):
        fut("buy", 26000, 65, fee_schedule_version="IN-NSE-DHAN-1999")
    with pytest.raises(ic.ScheduleUnavailable):
        ic.calculate_trade_charges(**{**FUT, "broker_profile": "zerodha"}, trading_date=NOV, order_id="o", side="buy", execution_price=D(1), executed_quantity=D(1))
    with pytest.raises(ic.ScheduleUnavailable):
        ic.calculate_trade_charges(**CASH, trading_date=NOV, order_id="o", side="sell", execution_price=D(1), executed_quantity=D(1), transaction_type="delivery")
    with pytest.raises(ic.InvalidExecution):
        ic.calculate_trade_charges(**FUT, trading_date=NOV, order_id="o", side="buy")
    with pytest.raises(ic.InvalidExecution):
        fut("buy", 0, 65)
    assert fut("buy", 26000, 65, order_status="rejected").total == D(0)                        # an order that did not execute costs nothing, explicitly


# K: equity cash intraday round trip: 1000 sh, buy 500 / sell 505. DP none. STT 0.025% sell, stamp 0.003% buy.
#    buy 500,000: brokerage min(20, 150)=20; exch 15.3495->15.35; ipft 0.0005->0.00; sebi 0.50; stamp 15; gst (20+15.3495+0.0005+0.5)*.18=6.453->6.45 => 57.30
#    sell 505,000: 20; exch 15.502995->15.50; sebi 0.505->0.51; stt 126.25->126; gst (20+15.502995+0.000505+0.505)*.18=6.48153->6.48 => 168.49
def test_K_equity_cash_intraday_round_trip_long_and_short():
    b, s = cash("buy", 500, 1000), cash("sell", 505, 1000)
    assert b.rounded == {"brokerage": D("20.00"), "stt": D("0"), "exchange": D("15.35"), "ipft": D("0.00"), "sebi": D("0.50"), "stamp": D("15"), "gst": D("6.45"), "dp": D("0.00")}
    assert s.rounded == {"brokerage": D("20.00"), "stt": D("126"), "exchange": D("15.50"), "ipft": D("0.00"), "sebi": D("0.51"), "stamp": D("0"), "gst": D("6.48"), "dp": D("0.00")}
    assert b.total == D("57.30") and s.total == D("168.49") and b.total + s.total == D("225.79")
    short_open, short_close = cash("sell", 505, 1000), cash("buy", 500, 1000)                  # short: STT on the open, stamp on the close
    assert short_open.rounded["stt"] == D("126") and short_close.rounded["stamp"] == D("15")
    assert b.rounded["dp"] == D(0) and s.rounded["dp"] == D(0)


def test_K_cash_brokerage_is_lower_of_flat_and_percent():
    assert cash("buy", 100, 10).raw["brokerage"] == D("0.30")                                  # 0.03% * 1000 < Rs 20
    assert cash("buy", 100, 10000).raw["brokerage"] == D("20")                                 # 0.03% * 1,000,000 = 300 > Rs 20


# L: segment isolation.
def test_L_segments_never_cross():
    with pytest.raises(ic.SegmentMismatch):
        ic.calculate_trade_charges(**{**CASH, "instrument_type": "index_future"}, trading_date=NOV, order_id="o", side="buy", execution_price=D(1), executed_quantity=D(1))
    with pytest.raises(ic.SegmentMismatch):
        ic.calculate_trade_charges(**{**FUT, "instrument_type": "cash"}, trading_date=NOV, order_id="o", side="buy", execution_price=D(1), executed_quantity=D(1))
    for t in ("index_future", "stock_future"):
        assert fut("buy", 100, 10, **{}) is not None
        ic.calculate_trade_charges(**{**FUT, "instrument_type": t}, trading_date=NOV, order_id="o", side="buy", execution_price=D(100), executed_quantity=D(10))


def test_unverified_components_are_surfaced_and_report_lists_every_line():
    assert fut("buy", 26000, 65).unverified_components == ("stamp",)                           # owner-stated futures stamp duty
    assert set(fut("sell", 26100, 65, day=date(2026, 3, 10)).unverified_components) >= {"stt"}  # pre-April STT is owner-stated
    assert "ipft" in fut("buy", 26000, 65, day=date(2026, 2, 27)).unverified_components       # pre-revision IPFT split unknown
    rep = ic.schedule_report("futures", "index_future", NOV)
    assert set(rep["components"]) == {"brokerage", "stt", "exchange", "ipft", "sebi", "stamp", "gst", "dp"} and rep["components"]["stt"]["rate"] == "0.05"
    assert rep["schedule_version"] == ic.SCHEDULE_VERSION and rep["last_verified"] and rep["components"]["exchange"]["effective_from"] == "2026-03-01"
    with pytest.raises(ic.ScheduleUnavailable):
        ic.schedule_report("futures", "index_future", date(2024, 1, 1))


def test_net_reconciles_exactly():
    orders = [fut("buy", 26000, 65, oid="e"), fut("sell", 26100, 65, oid="x")]
    agg = ic.sum_charges(orders)
    gross = (D("26100") - D("26000")) * D(65)
    assert gross - agg["total"] == D("6500") - D("1006.32")
    assert agg["total"] == sum(agg["rounded"].values(), D(0))
