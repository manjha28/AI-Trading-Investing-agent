"""Paper trend candidates for the scanner. The price paths below are TEST DATA."""
import numpy as np

from app.delta_scanner.trend_strategies import positions


def _bars(path):
    c = np.array(path, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return o, c + 0.5, c - 0.5, c, np.full(len(c), 1.0)


def test_donchian_goes_long_on_a_breakout_and_exits_on_the_trail():
    path = [100.0] * 80 + [100 + i for i in range(1, 30)] + [128 - 4 * i for i in range(1, 15)]
    trades = positions("trend_donchian_4h", *_bars(path))
    assert trades and trades[0][1] == 1 and trades[0][2] is not None      # one long, closed by the trailing stop on the drop


def test_momentum_holds_the_sign_of_the_56_day_return():
    path = [100 + i for i in range(120)] + [220 - 2 * i for i in range(120)]
    sides = [t[1] for t in positions("momentum_56d", *_bars(path))]
    assert sides[0] == 1 and -1 in sides


def test_rsi2_needs_the_200_day_filter_and_exits_at_the_5_day_average():
    rng = np.random.default_rng(3)
    path = list(100 + np.cumsum(rng.normal(0.3, 1.0, 400)))                 # rising market: only long dips qualify
    trades = positions("rsi2_daily", *_bars(path))
    assert trades and all(t[1] == 1 for t in trades)
