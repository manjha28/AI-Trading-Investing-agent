from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.services.options_signal_engine import (
    ActiveOptionPosition,
    OHLCVBar,
    OptionQuote,
    OptionsSignalInput,
    evaluate_options_signal,
)


def _fixture(*, bearish: bool = False, now: datetime | None = None) -> OptionsSignalInput:
    now = now or datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    base = D("100")
    for index in range(30):
        direction = D("-0.20") if bearish else D("0.20")
        close = base + direction * index
        high = close + D("0.10")
        low = close - D("0.10")
        volume = D("100")
        if index == 29:
            close = (
                max(b.high for b in bars[-10:]) + D("1.0")
                if not bearish
                else min(b.low for b in bars[-10:]) - D("1.0")
            )
            high = close + D("0.10")
            low = close - D("0.10")
            volume = D("300")
        bars.append(
            OHLCVBar(
                timestamp=now - timedelta(minutes=5 * (29 - index)),
                open=close - D("0.05"),
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    spot = bars[-1].close
    expiry = now + timedelta(hours=6)
    call = OptionQuote(
        strike=spot,
        option_type="call",
        expiry_at=expiry,
        bid=D("9.90"),
        ask=D("10.00"),
        bid_qty=D("100"),
        ask_qty=D("100"),
        ltp=D("9.95"),
        volume=D("1000"),
        open_interest=D("10000"),
        delta=D("0.55"),
        gamma=D("0.02"),
        theta=D("-0.50"),
        vega=D("0.30"),
        implied_volatility=D("0.25"),
        lot_size=25,
    )
    put = replace(call, option_type="put", delta=D("-0.55"))
    return OptionsSignalInput(
        underlying_symbol="TEST",
        chain_underlying_symbol="TEST",
        as_of=now,
        spot=spot,
        spot_timestamp=now - timedelta(seconds=1),
        spot_source="broker_live",
        chain_timestamp=now - timedelta(seconds=1),
        chain_source="broker_live",
        chain_spot=spot,
        expiry_at=expiry,
        bars=tuple(bars),
        chain=(call, put),
        capital=D("100000"),
        risk_fraction=D("0.005"),
    )


def test_bullish_breakout_emits_buy_call_with_evidence() -> None:
    result = evaluate_options_signal(_fixture())
    assert result.action == "BUY_CALL"
    assert result.strike is not None
    assert result.reference_entry_premium == D("10.00")
    assert result.estimated_premium_risk_per_lot == D("250.00")
    assert result.suggested_lots_by_risk_budget == 2
    assert result.evidence["bullish_breakout"] == 1
    assert "No order was placed" in result.disclaimer


def test_bearish_breakdown_emits_buy_put() -> None:
    result = evaluate_options_signal(_fixture(bearish=True))
    assert result.action == "BUY_PUT"
    assert result.selected_option_type == "put"


def test_stale_chain_suppresses_action() -> None:
    data = _fixture()
    stale = replace(data, chain_timestamp=data.as_of - timedelta(minutes=1))
    result = evaluate_options_signal(stale)
    assert result.action == "NO_TRADE"
    assert "stale_option_chain" in result.blockers


def test_underlying_mismatch_suppresses_action() -> None:
    result = evaluate_options_signal(replace(_fixture(), chain_underlying_symbol="OTHER"))
    assert result.action == "NO_TRADE"
    assert "underlying_symbol_mismatch" in result.blockers


def test_missing_greeks_reject_contract() -> None:
    data = _fixture()
    chain = tuple(replace(q, gamma=D("0")) if q.option_type == "call" else q for q in data.chain)
    result = evaluate_options_signal(replace(data, chain=chain))
    assert result.action == "NO_TRADE"
    assert "missing_or_invalid_greeks" in result.blockers


def test_excessive_theta_to_premium_rejects_contract() -> None:
    data = _fixture()
    chain = tuple(replace(q, theta=D("-3")) if q.option_type == "call" else q for q in data.chain)
    result = evaluate_options_signal(replace(data, chain=chain))
    assert result.action == "NO_TRADE"
    assert "theta_decay_too_high_for_premium" in result.blockers


def test_very_high_iv_rank_suppresses_long_premium_signal() -> None:
    result = evaluate_options_signal(replace(_fixture(), iv_rank_percentile=D("92")))
    assert result.action == "NO_TRADE"
    assert "implied_volatility_rank_too_high_for_buying" in result.blockers


def test_position_stop_uses_bid_not_ltp() -> None:
    data = _fixture()
    call = data.chain[0]
    chain = (replace(call, bid=D("7.40"), ask=D("7.50")), data.chain[1])
    active = ActiveOptionPosition(
        option_type="call",
        strike=call.strike,
        expiry_at=call.expiry_at,
        entry_premium=D("10.00"),
    )
    result = evaluate_options_signal(replace(data, chain=chain, active_position=active))
    assert result.action == "EXIT_CALL"
    assert result.option_return_pct_at_bid == D("-26.00")
    assert result.reasons == ("premium_stop_loss_reached_at_bid",)


def test_invalid_timezone_aware_requirement_blocks_signal() -> None:
    data = _fixture()
    result = evaluate_options_signal(replace(data, chain_timestamp=data.as_of.replace(tzinfo=None)))
    assert result.action == "NO_TRADE"
    assert "timestamps_must_be_timezone_aware" in result.blockers


def test_active_position_without_exit_does_not_emit_second_entry_signal() -> None:
    data = _fixture()
    active = ActiveOptionPosition(
        option_type="call",
        strike=data.chain[0].strike,
        expiry_at=data.chain[0].expiry_at,
        entry_premium=D("9.95"),
    )
    result = evaluate_options_signal(replace(data, active_position=active))
    assert result.action == "NO_TRADE"
    assert "active_position_no_exit_triggered" in result.blockers


def test_call_with_negative_delta_is_rejected() -> None:
    data = _fixture()
    chain = (replace(data.chain[0], delta=D("-0.55")), data.chain[1])
    result = evaluate_options_signal(replace(data, chain=chain))
    assert result.action == "NO_TRADE"
    assert "delta_sign_inconsistent_with_option_type" in result.blockers


def test_nearby_call_oi_wall_blocks_long_call() -> None:
    data = _fixture()
    base_call = data.chain[0]
    wall = replace(
        base_call,
        strike=data.spot + D("0.05"),
        open_interest=D("100000"),
        volume=D("5000"),
    )
    result = evaluate_options_signal(replace(data, chain=data.chain + (wall,)))
    assert result.action == "NO_TRADE"
    assert "nearby_call_oi_wall" in result.blockers
    assert result.evidence["call_oi_wall_strike"] == str(wall.strike)
