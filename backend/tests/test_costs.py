"""Cost vectors from the VWAPX-TB spec. Exact to +/- 0.01."""
from datetime import datetime, timezone
from decimal import Decimal as D

from scalp_hold.venues import CostCtx, crypto_delta, make_fill, nse_equity_mis, nse_index_fut, warn_unverified

TS = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)


def approx(value, expected, tol="0.01"):
    assert abs(value - D(expected)) <= D(tol), f"{value} != {expected}"


def f(side, notional, maker, seq=0):
    return make_fill(side, float(notional), 1, maker, TS, seq)


# ───────────── CRYPTO_DELTA, $10,000 per leg ─────────────
def test_crypto_maker_in_taker_out():
    approx(crypto_delta().costs([f("buy", 10000, True), f("sell", 10000, False, 1)], CostCtx("BTCUSD", 40)), "8.26")


def test_crypto_maker_maker():
    approx(crypto_delta().costs([f("buy", 10000, True), f("sell", 10000, True, 1)], CostCtx("BTCUSD", 40)), "4.72")


def test_crypto_taker_taker():
    approx(crypto_delta().costs([f("buy", 10000, False), f("sell", 10000, False, 1)], CostCtx("BTCUSD", 40)), "11.80")


def test_crypto_waiver_zeroes_closing_fills_only_when_enabled_and_under_window():
    fills = [f("buy", 10000, True), f("sell", 10000, True, 1)]
    approx(crypto_delta(waiver_enabled=True).costs(fills, CostCtx("BTCUSD", 20)), "2.36")      # BTCUSD window 30 min
    approx(crypto_delta(waiver_enabled=True).costs(fills, CostCtx("BTCUSD", 31)), "4.72")      # held too long
    approx(crypto_delta(waiver_enabled=True).costs(fills, CostCtx("ETHUSD", 16)), "4.72")      # other symbols: 15 min window
    approx(crypto_delta(waiver_enabled=False).costs(fills, CostCtx("BTCUSD", 20)), "4.72")     # default off


def test_crypto_partials_are_fee_neutral():
    split = [f("buy", 10000, True), f("sell", 6000, True, 1), f("sell", 4000, True, 2)]
    approx(crypto_delta().costs(split, CostCtx("BTCUSD", 40)), "4.72")


def test_tds_is_config_and_default_zero():
    fills = [f("buy", 10000, True), f("sell", 10000, True, 1)]
    approx(crypto_delta().costs(fills, CostCtx("BTCUSD", 40, tds_pct=D("0.1"))), "24.72")     # +0.1% x 10,000 x 2 legs


# ───────────── NSE_INDEX_FUT: 1 lot = 65, buy 26000, sell 26100 ─────────────
def index_fills(split=False):
    qty = 65
    if not split:
        return [make_fill("buy", 26000, qty, False, TS, 0), make_fill("sell", 26100, qty, False, TS, 1)]
    return [make_fill("buy", 26000, qty, False, TS, 0), make_fill("sell", 26100, 30, False, TS, 1), make_fill("sell", 26100, 35, False, TS, 2)]


def test_index_future_components_and_total():
    v = nse_index_fut()
    b = v.breakdown_fn(index_fills(), CostCtx())
    for key, expected in {"stt": "848.25", "exchange": "61.97", "stamp": "33.80", "sebi": "3.39", "brokerage": "40.00", "gst": "18.96"}.items():
        approx(b[key], expected)
    total = v.costs(index_fills(), CostCtx())
    approx(total, "1006.37")
    approx(total / D(26000 * 65) * 100, "0.0595", "0.0001")


def test_index_future_split_exit_costs_exactly_23_60_more():
    v = nse_index_fut()
    extra = v.costs(index_fills(split=True), CostCtx()) - v.costs(index_fills(), CostCtx())
    approx(extra, "23.60")                       # extra 20 brokerage + 3.60 GST: partials are NOT free in India


# ───────────── NSE_EQUITY_MIS: 1000 sh, buy 500, sell 505 ─────────────
def test_equity_mis_components_and_total():
    v = nse_equity_mis()
    fills = [make_fill("buy", 500, 1000, False, TS, 0), make_fill("sell", 505, 1000, False, TS, 1)]
    b = v.breakdown_fn(fills, CostCtx())
    for key, expected in {"stt": "126.25", "exchange": "30.85", "stamp": "15.00", "sebi": "1.01", "brokerage": "40.00", "gst": "12.93"}.items():
        approx(b[key], expected)
    approx(v.costs(fills, CostCtx()), "226.04")
    approx(v.costs(fills, CostCtx()) / D(500000) * 100, "0.0452", "0.0001")


def test_equity_brokerage_is_capped_by_percentage_for_small_orders():
    v = nse_equity_mis()
    b = v.breakdown_fn([make_fill("buy", 100, 10, False, TS, 0), make_fill("sell", 100, 10, False, TS, 1)], CostCtx())
    approx(b["brokerage"], "0.60")               # 0.03% x 1000 = 0.30 per order, below the 20 flat cap


def test_unverified_assumptions_warn_on_stderr(capsys):
    msgs = warn_unverified(scalper_offer_enabled=True, tds_pct=D("1"), slip_bps={"BTCUSD": 1.0})
    err = capsys.readouterr().err
    assert len(msgs) == 3 and err.count("WARNING") == 3
    assert warn_unverified(scalper_offer_enabled=False, tds_pct=D("0"), slip_bps={}) == []
