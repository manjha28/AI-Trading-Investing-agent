"""The shared risk gate: liquidation safety, balance-only sizing, paper-only when margin is in use, data and day limits."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.delta_lab import risk
from app.delta_lab.risk import AccountSnapshot, ContractSpec, DayState, MarketState, OrderRequest, RiskConfig, risk_gate

SPEC = ContractSpec("BTCUSD", D("0.001"), D("0.5"), mm_pct=D("0.0025"), im_pct_min=D("0.005"), slope_threshold_notional=D("100000"))
CFG = RiskConfig(leverage=D(10), mode="live")                     # live config so a clean pass is "send"
NOW = datetime(2026, 11, 24, 10, 0, 30, tzinfo=timezone.utc)
FREE = AccountSnapshot(wallet=D(1000), position_margin=D(0), order_margin=D(0), available=D(1000))
MKT = MarketState(now=NOW, last_tick_age_s=D("0.4"), spread=D(1), spread_p95=D(2))


def long_order(entry="60000", stop="59400", target="61200"):
    return OrderRequest("BTCUSD", "long", D(entry), D(stop), D(target), "F1")


def short_order(entry="60000", stop="60600", target="58800"):
    return OrderRequest("BTCUSD", "short", D(entry), D(stop), D(target), "F1")


def gate(order, acct=FREE, market=MKT, day=None, cfg=CFG):
    return risk_gate(SPEC, cfg, acct, market, day or DayState(), order)


def test_clean_long_and_short_pass_with_balance_only_sizing():
    for order in (long_order(), short_order()):
        d = gate(order)
        assert d.action == "send" and d.reasons == ()
        # risk 0.5% of 1000 = 5 USD; stop distance 600; contract value 0.001 -> 5/600/0.001 = 8.33 -> 8 contracts
        assert d.qty == 8
        assert d.required_margin == D("60000") * D("0.001") * 8 / 10
    assert gate(long_order()).liq_price == D("60000") * (1 - D("0.1") + D("0.0025"))


def test_long_stop_beyond_liquidation_is_rejected_and_nothing_is_moved():
    d = gate(long_order(stop="53000"))                              # liq is about 54,150; the stop sits past it
    assert d.action == "reject" and "stop_beyond_liquidation" in d.reasons


def test_short_stop_beyond_liquidation_is_rejected():
    d = gate(short_order(stop="67000"))                             # liq about 65,850
    assert d.action == "reject" and "stop_beyond_liquidation" in d.reasons


def test_stop_too_close_to_liquidation_is_rejected_for_both_sides():
    # entry-liq is about 5,850; half of that is 2,925. A stop 3,500 away is before liquidation but not safe enough.
    assert "stop_too_close_to_liquidation" in gate(long_order(stop="56500")).reasons
    assert "stop_too_close_to_liquidation" in gate(short_order(stop="63500")).reasons
    assert "stop_beyond_liquidation" not in gate(long_order(stop="56500")).reasons


def test_wrong_side_stop_is_rejected():
    assert "stop_on_wrong_side" in gate(long_order(stop="60500")).reasons


def test_insufficient_available_balance_rejects():
    acct = AccountSnapshot(wallet=D(1000), position_margin=D(0), order_margin=D(0), available=D(40))   # needs 48 + fees
    d = gate(long_order(), acct=acct)
    assert d.action == "reject" and "insufficient_available_balance" in d.reasons


def test_the_80_percent_cap_counts_margin_plus_round_trip_fees_and_gst():
    d = gate(long_order())
    assert d.est_round_trip_cost == D("60000") * D("0.001") * 8 * D("0.0005") * 2 * D("1.18")
    need = d.required_margin + d.est_round_trip_cost
    ok = AccountSnapshot(D(1000), D(0), D(0), need / D("0.8") + 1)
    bad = AccountSnapshot(D(1000), D(0), D(0), need / D("0.8") - 1)
    assert gate(long_order(), acct=ok).action == "send" and "insufficient_available_balance" in gate(long_order(), acct=bad).reasons


def test_balance_in_use_goes_to_paper_in_strict_mode_and_per_symbol_mode_only_blocks_that_symbol():
    used = AccountSnapshot(D(1000), position_margin=D(10), order_margin=D(0), available=D(990), symbol_margin={"ETHUSD": D(10)})
    strict = gate(long_order(), acct=used)
    assert strict.action == "paper" and strict.reasons == ("balance_in_use",) and strict.qty == 8
    per = RiskConfig(leverage=D(10), mode="live", in_use_mode="per_symbol")
    assert gate(long_order(), acct=used, cfg=per).action == "send"                       # margin sits on ETHUSD, not BTCUSD
    same = AccountSnapshot(D(1000), D(10), D(0), D(990), symbol_margin={"BTCUSD": D(10)})
    assert gate(long_order(), acct=same, cfg=per).action == "paper"
    order_margin_only = AccountSnapshot(D(1000), D(0), D(5), D(995))
    assert gate(long_order(), acct=order_margin_only).action == "paper"                  # an open order counts as margin in use


def test_default_mode_is_paper_even_when_everything_passes():
    d = gate(long_order(), cfg=RiskConfig(leverage=D(10)))
    assert RiskConfig().mode == "paper" and d.action == "paper" and d.reasons == ("mode_paper",)


def test_stale_or_unknown_ticks_and_wide_spreads_reject():
    assert "stale_data" in gate(long_order(), market=MarketState(NOW, D("2.5"), D(1), D(2))).reasons
    assert "stale_data" in gate(long_order(), market=MarketState(NOW, None, D(1), D(2))).reasons
    assert "spread_above_p95" in gate(long_order(), market=MarketState(NOW, D("0.1"), D(3), D(2))).reasons


def test_within_two_minutes_of_a_funding_timestamp_rejects():
    for stamp in (datetime(2026, 11, 24, 8, 1, 59, tzinfo=timezone.utc), datetime(2026, 11, 24, 7, 58, 30, tzinfo=timezone.utc), datetime(2026, 11, 24, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 24, 23, 59, 30, tzinfo=timezone.utc)):
        assert "funding_window" in gate(long_order(), market=MarketState(stamp, D("0.1"), D(1), D(2))).reasons
    assert "funding_window" not in gate(long_order(), market=MarketState(datetime(2026, 11, 24, 8, 2, 30, tzinfo=timezone.utc), D("0.1"), D(1), D(2))).reasons


def test_daily_loss_limit_and_consecutive_losses_lock_until_the_next_utc_day():
    day = DayState()
    day.roll(NOW)
    day.record_close(D(-20))                                        # exactly -2% of 1000
    assert "daily_loss_limit" in gate(long_order(), day=day).reasons
    day2 = DayState()
    day2.roll(NOW)
    for _ in range(4):
        day2.record_close(D(-1))
    assert "consecutive_losses" in gate(long_order(), day=day2).reasons
    nxt = MarketState(NOW + timedelta(days=1), D("0.1"), D(1), D(2))
    assert gate(long_order(), day=day, market=nxt).action == "send"       # a new UTC day unlocks both
    assert gate(long_order(), day=day2, market=nxt).action == "send"


def test_a_win_resets_the_losing_streak():
    day = DayState(); day.roll(NOW)
    for pnl in (-1, -1, -1, 2, -1):
        day.record_close(D(pnl))
    assert day.losses_in_row == 1 and "consecutive_losses" not in gate(long_order(), day=day).reasons


def test_order_rate_cap():
    cfg = RiskConfig(leverage=D(10), mode="live", max_orders_per_minute=2)
    day = DayState()
    assert gate(long_order(), day=day, cfg=cfg).action == "send" and gate(long_order(), day=day, cfg=cfg).action == "send"
    assert "order_rate_cap" in gate(long_order(), day=day, cfg=cfg).reasons


def test_unreadable_account_and_missing_stop_fail_closed():
    assert risk_gate(SPEC, CFG, None, MKT, DayState(), long_order()).reasons == ("account_unreadable",)
    assert "no_stop" in gate(OrderRequest("BTCUSD", "long", D(60000), D(0), None)).reasons


def test_leverage_is_never_raised_to_fit_and_an_over_limit_leverage_is_refused():
    d = gate(long_order(stop="56500"))
    assert d.leverage == D(10)                                      # reported unchanged
    assert "leverage_not_allowed" in gate(long_order(), cfg=RiskConfig(leverage=D(500), mode="live")).reasons


def test_notional_above_the_slope_threshold_is_refused_because_the_slope_is_not_modelled():
    rich = AccountSnapshot(D(5_000_000), D(0), D(0), D(5_000_000))
    assert "notional_above_margin_slope_threshold" in gate(long_order(), acct=rich).reasons


def test_post_fill_liquidation_check_and_missing_stop_confirmation_flatten():
    assert risk.post_fill_liquidation_ok("long", D(59400), D(54150), D(60000), CFG) is True
    assert risk.post_fill_liquidation_ok("long", D(59400), None, D(60000), CFG) is False             # unreadable fails
    assert risk.post_fill_liquidation_ok("long", D(59400), D(59800), D(60000), CFG) is False         # exchange liq above the stop
    fill = NOW
    assert risk.must_flatten_for_missing_stop(fill, None, fill + timedelta(seconds=2.5)) is True
    assert risk.must_flatten_for_missing_stop(fill, None, fill + timedelta(seconds=1.5)) is False
    assert risk.must_flatten_for_missing_stop(fill, fill + timedelta(seconds=1), fill + timedelta(seconds=10)) is False
    assert risk.must_flatten_for_missing_stop(fill, fill + timedelta(seconds=3), fill + timedelta(seconds=10)) is True   # confirmed too late
