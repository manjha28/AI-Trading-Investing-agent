"""Transaction-cost engine. Every expected figure below was worked out by hand from the Dhan schedule, NOT by running the code under test.

Schedule (percent): brokerage MIN(20, 0.03%) per ORDER; NSE txn 0.0030699 / BSE 0.00375 on total turnover; STT 0.025 on sell turnover; SEBI 0.0001;
stamp 0.003 on buy turnover; IPFT 0.0000001; GST 18% on brokerage + exchange txn + SEBI. STT and stamp round to the rupee, the rest to 2 dp (half-up).
"""
from decimal import Decimal as D

import pytest

from app.services.transaction_costs import (Execution, InvalidExecution, UnsupportedProduct, compute_charges, get_schedule, gross_pnl, per_unit_round_trip, schedule_for_market,
                                            trade_charges, trade_executions)

NSE = get_schedule("dhan_nse_intraday_2026_11")
BSE = get_schedule("dhan_bse_intraday_2026_11")
DELIVERY = get_schedule("dhan_nse_delivery_2026_11")


def parts(b):
    return {k: getattr(b, k) for k in ("brokerage", "stt", "exchange_txn", "sebi", "ipft", "stamp_duty", "gst")}


def test_long_trade_hand_calculated():
    # buy 100 @ 1000 = 100,000 ; sell 100 @ 1010 = 101,000 ; total 201,000
    b = trade_charges("long", D(100), D(1000), D(1010), NSE)
    assert (b.buy_turnover, b.sell_turnover, b.turnover) == (D(100000), D(101000), D(201000))
    assert parts(b) == {"brokerage": D("40.00"),      # 20 + 20 (0.03% of 100,000 = 30 -> capped at 20; of 101,000 = 30.3 -> 20)
                        "stt": D(25),                 # 0.00025 x 101,000 = 25.25 -> 25
                        "exchange_txn": D("6.17"),    # 0.000030699 x 201,000 = 6.1705
                        "sebi": D("0.20"),            # 0.000001 x 201,000 = 0.201
                        "ipft": D("0.00"),            # 1e-9 x 201,000 = 0.000201
                        "stamp_duty": D(3),           # 0.00003 x 100,000
                        "gst": D("8.35")}             # 0.18 x (40 + 6.17 + 0.20) = 8.3466
    assert b.total == D("82.72") and gross_pnl("long", D(100), D(1000), D(1010)) == D(1000)
    assert b.pct_of_turnover.quantize(D("0.0001")) == D("0.0412")


def test_short_trade_uses_actual_sides_sell_first_buy_later():
    # sell 200 @ 500 = 100,000 (entry) ; buy 200 @ 495 = 99,000 (exit)
    ex = trade_executions("short", D(200), D(500), D(495))
    assert [(e.order_id, e.side) for e in ex] == [("entry", "sell"), ("exit", "buy")]
    b = compute_charges(ex, NSE)
    assert (b.buy_turnover, b.sell_turnover) == (D(99000), D(100000))
    assert parts(b) == {"brokerage": D("40.00"), "stt": D(25), "exchange_txn": D("6.11"), "sebi": D("0.20"), "ipft": D("0.00"), "stamp_duty": D(3),
                        "gst": D("8.34")}                       # 0.18 x 46.31 = 8.3358 ; stamp 0.00003 x 99,000 = 2.97 -> 3 ; txn 0.000030699 x 199,000 = 6.1091
    assert b.total == D("82.65") and gross_pnl("short", D(200), D(500), D(495)) == D(1000)


def test_stt_is_on_the_sell_leg_for_both_long_and_short_and_stamp_on_the_buy_leg():
    long_b = trade_charges("long", D(100), D(100), D(100), NSE)
    short_b = trade_charges("short", D(100), D(100), D(100), NSE)
    assert long_b.stt == short_b.stt == D(3) and long_b.stamp_duty == short_b.stamp_duty == D(0)   # 0.00025 x 10,000 = 2.5 -> 3 ; 0.00003 x 10,000 = 0.3 -> 0
    only_buy = compute_charges([Execution("o", "buy", D(100), D(1000))], NSE)
    only_sell = compute_charges([Execution("o", "sell", D(100), D(1000))], NSE)
    assert (only_buy.stt, only_buy.stamp_duty) == (D(0), D(3)) and (only_sell.stt, only_sell.stamp_duty) == (D(25), D(0))


def test_small_turnover_brokerage_is_percentage_not_the_cap():
    # buy 10 @ 100 = 1,000 ; sell 10 @ 101 = 1,010
    b = trade_charges("long", D(10), D(100), D(101), NSE)
    assert b.brokerage == D("0.60")        # 0.0003 x 1,000 = 0.30 and 0.0003 x 1,010 = 0.303 -> 0.30
    assert (b.stt, b.stamp_duty) == (D(0), D(0))     # 0.2525 and 0.03 round to 0 rupees
    assert (b.exchange_txn, b.sebi, b.gst) == (D("0.06"), D("0.00"), D("0.12"))   # 0.0617 ; 0.002 ; 0.18 x 0.66 = 0.1188
    assert b.total == D("0.78")


def test_brokerage_cap_is_20_per_order_never_per_share_or_candle():
    b = trade_charges("long", D(100000), D(1000), D(1001), NSE)       # turnover 200 million
    assert b.brokerage == D("40.00")
    tiny = trade_charges("long", D(1), D(100), D(101), NSE)
    assert tiny.brokerage == D("0.06")                                 # 0.03 + 0.03 : percentage, not 20 x quantity


def test_multiple_fills_of_one_order_pay_brokerage_once():
    fills = [Execution("buy1", "buy", D(40), D(100)), Execution("buy1", "buy", D(30), D("100.5")), Execution("buy1", "buy", D(30), D(101)),
             Execution("sell1", "sell", D(100), D(102))]
    b = compute_charges(fills, NSE)
    assert b.buy_turnover == D("10045.00")                              # 4,000 + 3,015 + 3,030
    assert [o["brokerage"] for o in b.orders] == ["3.01", "3.06"]       # 0.0003 x 10,045 = 3.0135 ; 0.0003 x 10,200 = 3.06 : one charge each
    assert b.brokerage == D("6.07")
    as_separate = compute_charges([Execution(f"b{i}", s, q, p) for i, (s, q, p) in enumerate([("buy", D(40), D(100)), ("buy", D(30), D("100.5")), ("buy", D(30), D(101)), ("sell", D(100), D(102))])], NSE)
    assert as_separate.brokerage == D("1.20") + D("0.90") + D("0.91") + D("3.06")    # 4,000 -> 1.20 ; 3,015 -> 0.9045 -> 0.90 ; 3,030 -> 0.909 -> 0.91 ; 10,200 -> 3.06


def test_partial_exits_are_separate_orders_with_their_own_brokerage():
    ex = [Execution("entry", "buy", D(100), D(1000)), Execution("exit1", "sell", D(60), D(1010)), Execution("exit2", "sell", D(40), D(1005))]
    b = compute_charges(ex, NSE)
    assert (b.buy_turnover, b.sell_turnover) == (D(100000), D(100800))
    assert [o["brokerage"] for o in b.orders] == ["20.00", "18.18", "12.06"]     # 100,000 -> cap ; 60,600 x 0.0003 ; 40,200 x 0.0003
    assert parts(b) == {"brokerage": D("50.24"), "stt": D(25), "exchange_txn": D("6.16"), "sebi": D("0.20"), "ipft": D("0.00"), "stamp_duty": D(3), "gst": D("10.19")}
    assert b.total == D("94.79")                                                # gst: 0.18 x (50.24 + 6.16 + 0.20) = 10.188


@pytest.mark.parametrize("label,side,qty,entry,exit_,total", [
    ("stop-loss exit", "long", D(100), D(1000), D(990), D("82.65")),      # 40 + STT 25 + 6.11 + 0.20 + stamp 3 + GST 8.34
    ("stop-gap exit (gap down)", "long", D(100), D(1000), D(950), D("81.50")),   # 40 + STT 24 (23.75) + 5.99 + 0.20 + 3 + GST 8.31
    ("target exit", "short", D(100), D(1000), D(980), D("82.61")),        # 40 + STT 25 + 6.08 + 0.20 + stamp 3 (2.94) + GST 8.33
])
def test_stop_target_and_gap_exits_are_charged_exactly_like_any_other_exit(label, side, qty, entry, exit_, total):
    # the exit REASON never changes the fee formula: only the executed prices do
    b = trade_charges(side, qty, entry, exit_, NSE)
    assert b.total == total, label
    pnl = gross_pnl(side, qty, entry, exit_)
    assert pnl - b.total == pnl - total


def test_bse_uses_the_bse_transaction_rate_and_nothing_else_changes():
    b = trade_charges("long", D(100), D(1000), D(1010), BSE)
    assert b.exchange_txn == D("7.54")        # 0.0000375 x 201,000 = 7.5375
    assert b.gst == D("8.59")                 # 0.18 x (40 + 7.54 + 0.20) = 8.5932
    assert b.total == D("84.33") and b.brokerage == D("40.00") and b.stt == D(25)


def test_loss_making_trades_still_have_positive_charges():
    b = trade_charges("long", D(100), D(1000), D(900), NSE)
    pnl = gross_pnl("long", D(100), D(1000), D(900))
    assert pnl == D(-10000) and b.total > 0 and (pnl - b.total) < pnl


def test_delivery_is_a_different_schedule_not_the_intraday_one():
    d = trade_charges("long", D(100), D(1000), D(1010), DELIVERY)
    assert d.brokerage == D(0) and d.dp_charges == D(0)
    assert d.stt == D(201)                   # 0.001 x (100,000 + 101,000): STT on BOTH legs for delivery
    assert d.stamp_duty == D(15)             # 0.00015 x 100,000
    assert (d.exchange_txn, d.sebi, d.gst) == (D("6.17"), D("0.20"), D("1.15"))   # 0.18 x 6.37 = 1.1466
    assert d.total == D("223.52")
    assert d.total != trade_charges("long", D(100), D(1000), D(1010), NSE).total


def test_intraday_never_carries_delivery_dp_or_service_fees_unless_they_happened():
    b = trade_charges("long", D(100), D(1000), D(1010), NSE)
    assert b.dp_charges == 0 and b.other == 0
    custom = get_schedule("dhan_nse_intraday_2026_11")
    ov = {"auto_square_off_flat": "50", "call_and_trade_flat": "30"}
    ex = trade_executions("long", D(100), D(1000), D(1010))
    assert compute_charges(ex, custom, overrides=ov).other == 0
    assert compute_charges(ex, custom, overrides=ov, auto_square_off=True).other == D("50.00")
    assert compute_charges(ex, custom, overrides=ov, auto_square_off=True, call_and_trade_orders=1).other == D("80.00")


def test_overrides_use_the_same_percent_units():
    b = compute_charges(trade_executions("long", D(100), D(1000), D(1010)), NSE, overrides={"brokerage_flat": "10"})
    assert b.brokerage == D("20.00")


@pytest.mark.parametrize("bad", [
    [], [Execution("o", "buy", D(0), D(100))], [Execution("o", "buy", D(-5), D(100))], [Execution("o", "buy", D(5), D(0))], [Execution("o", "buy", D(5), D("NaN"))],
    [Execution("o", "hold", D(5), D(100))], [Execution("", "buy", D(5), D(100))], [Execution("o", "buy", 5, D(100))],
])
def test_zero_quantity_and_invalid_executions_are_rejected(bad):
    with pytest.raises(InvalidExecution):
        compute_charges(bad, NSE)


def test_unknown_or_fno_schedules_and_wrong_market_are_rejected():
    with pytest.raises(UnsupportedProduct):
        get_schedule(product="fno")
    with pytest.raises(ValueError):
        get_schedule("nope")
    with pytest.raises(ValueError):
        schedule_for_market("crypto", "dhan_nse_intraday_2026_11")
    with pytest.raises(ValueError):
        schedule_for_market("equity_in", "crypto_delta_india_2024")


def test_per_unit_scaling_costs_a_realistic_reference_position():
    per_unit = per_unit_round_trip("long", D(500), D(510), DELIVERY)
    assert per_unit == trade_charges("long", D(200), D(500), D(510), DELIVERY).total / 200   # 100,000 / 500 = 200 shares


def test_crypto_schedule_unchanged_semantics():
    sch = schedule_for_market("crypto")
    b = compute_charges([Execution("e", "buy", D(1), D(1000)), Execution("x", "sell", D(1), D(1010), "maker")], sch)
    assert b.other == D("0.70") and b.gst == D("0.13")       # 0.0005 x 1000 + 0.0002 x 1010 = 0.7020 ; 0.18 x 0.70 = 0.126
