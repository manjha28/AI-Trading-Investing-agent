"""Estimated charges for Dhan fills without broker charges: applied to display copies only, flagged, allocated per order, never zero for unknown products.
Fills below are TEST DATA."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.models import DhanTradeMirror
from app.services.dhan_charge_estimate import estimate_missing_charges

D = Decimal
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)


def fill(order, side, qty, price, *, segment="NSE_FNO", charges=None, symbol="NIFTY-Nov2026-25400-CE", trade_id=None):
    return DhanTradeMirror(id=None, user_id=None, source="dhan", trade_id=trade_id or f"{order}-{side}-{qty}-{price}", order_id=order, symbol=symbol, security_id="1", exchange_segment=segment,
                           side=side, quantity=qty, price=D(price), charges=charges, currency="INR", trade_at=NOW, synced_at=NOW)


is_option = lambda r: "-CE" in (r.symbol or "") or "-PE" in (r.symbol or "")  # noqa: E731


def test_brokerage_is_charged_once_per_order_and_split_across_that_orders_fills():
    rows = [fill("o1", "BUY", 65, "20"), fill("o1", "BUY", 65, "20"), fill("o2", "SELL", 130, "25")]
    out = estimate_missing_charges(rows, is_option)
    assert all(getattr(r, "charges_estimated", False) for r in out)
    buy_total = out[0].charges + out[1].charges
    # one Rs 20 order of Rs 2,600 turnover: Rs 20 brokerage + exchange charge + stamp duty + GST on (brokerage + charge)
    assert D("20") < buy_total < D("30") and out[0].charges == out[1].charges
    assert out[2].charges > buy_total                            # the sell order also pays STT on premium


def test_real_charges_are_never_replaced_and_the_source_rows_are_not_mutated():
    real = fill("o3", "BUY", 65, "20", charges=D("9.99"))
    unknown = fill("o4", "BUY", 65, "20")
    out = estimate_missing_charges([real, unknown], is_option)
    assert out[0] is real and out[0].charges == D("9.99") and not hasattr(out[0], "charges_estimated")
    assert out[1] is not unknown and unknown.charges is None       # a copy: nothing is written back to the stored row
    assert out[1].charges_estimated is True


def test_products_without_a_schedule_stay_unknown_not_zero():
    future = fill("o5", "BUY", 25, "24000", segment="NSE_FNO", symbol="NIFTY-Nov2026-FUT")
    commodity = fill("o6", "BUY", 1, "70000", segment="MCX_COMM", symbol="GOLD")
    bad = fill("o7", "BUY", 0, "20")
    out = estimate_missing_charges([future, commodity, bad], is_option)
    assert [r.charges for r in out] == [None, None, None]


def test_equity_fills_use_the_intraday_schedule():
    rows = [fill("o8", "BUY", 10, "100", segment="NSE_EQ", symbol="TCS")]
    out = estimate_missing_charges(rows, is_option)
    assert out[0].charges_estimated and out[0].charges > 0


def test_dhan_trading_symbols_are_recognised_as_options_so_they_are_priced_and_labelled_correctly():
    from app.api.trades import _classify_segment, _contract_details
    for symbol, kind, strike in (("NIFTY-Nov2026-25350-CE", "call", D("25350")), ("NIFTY-24Nov2026-25400-PE", "put", D("25400"))):
        assert _classify_segment("NSE_FNO", symbol) == "option"
        assert _contract_details(fill("o9", "BUY", 65, "10", symbol=symbol))[1:] == (strike, kind)
    assert _classify_segment("NSE_FNO", "NIFTY-Nov2026-FUT") == "future"
    assert _classify_segment("NSE_EQ", "TCS") == "equity"
