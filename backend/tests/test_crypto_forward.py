"""Delta 5 EMA paper forward test: alert-candle rule, causal fills, costs, and the owner-scoped API. Candles here are TEST DATA."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.crypto_delta import forward as f
from app.crypto_delta.candles import Candle

D = Decimal
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
SPEC = f.Spec("BTCUSD", D("0.001"), D("0.0005"), D("0.5"))
START = T0


def bar(i, o, h, lo, c):
    return Candle(T0 + timedelta(minutes=15 * i), D(o), D(h), D(lo), D(c), D(10))


def base(n=8, price=100):
    """Flat candles that straddle the EMA so no alert forms."""
    return [bar(i, price, price + 1, price - 1, price) for i in range(n)]


def sell_setup(after):
    # candle 8 sits entirely above the 5 EMA (low 103 > ema ~100): sell alert, low 103 high 106; `after` are the later candles
    return base() + [bar(8, 104, 106, 103, 105)] + after


def run(candles, direction=-1, rr=3, slip=0):
    return f.replay(candles, "BTCUSD", f.FiveEmaParams(5, D(rr), direction), SPEC, 2, D(slip), START)


def test_sell_alert_breaks_low_and_hits_the_target():
    out = run(sell_setup([bar(9, 104, 104.5, 102, 102.5), bar(10, 102, 102.5, 90, 91)]))
    t = out["trades"][0]
    assert t["side"] == "sell" and t["exit_reason"] == "target"
    assert D(t["entry_fill"]) == D(103) and D(t["stop_initial"]) == D(106)      # stop at the alert candle's high
    assert D(t["target"]) == D(94)                                              # 3 x the 3-point stop distance below the entry
    assert D(t["gross_pnl"]) == D(9) * D(2) * D("0.001") and D(t["fees"]) > 0
    assert D(t["net_pnl"]) < D(t["gross_pnl"])                                  # fees always reduce it


def test_stop_wins_a_bar_that_touches_both():
    out = run(sell_setup([bar(9, 104, 104.5, 102, 102.5), bar(10, 102, 107, 90, 95)]))
    assert out["trades"][0]["exit_reason"] == "stop" and D(out["trades"][0]["gross_pnl"]) < 0


def test_gap_below_the_alert_low_fills_at_the_open():
    out = run(sell_setup([bar(9, 101, 102, 100, 101)]))
    assert D(out["open_positions"][0]["entry_fill"]) == D(101)


def test_direction_filter_and_no_trade_before_start():
    assert run(sell_setup([bar(9, 104, 104.5, 102, 102.5)]), direction=1)["trades"] == []
    late = f.replay(sell_setup([bar(9, 104, 104.5, 102, 102.5), bar(10, 102, 102.5, 90, 91)]), "BTCUSD", f.FiveEmaParams(), SPEC, 1, D(0), T0 + timedelta(days=1))
    assert late["trades"] == [] and late["open_positions"] == []


def test_slippage_is_adverse_and_summary_reconciles():
    out = run(sell_setup([bar(9, 104, 104.5, 102, 102.5), bar(10, 102, 102.5, 90, 91)]), slip=10)
    t = out["trades"][0]
    assert D(t["entry_fill"]) < D(103) and D(t["slippage_cost"]) > 0
    s = f.summarise(out["trades"])
    assert D(s["net_pnl"]) == D(t["gross_pnl"]) - D(t["slippage_cost"]) - D(t["fees"])


def test_no_data_is_not_zero():
    s = f.summarise([])
    assert s["closed_trades"] == 0 and s["win_rate"] is None and s["avg_r"] is None
