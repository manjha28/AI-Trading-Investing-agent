"""Rule mechanics of the two index option studies. All prices are TEST DATA."""
from datetime import date

import numpy as np

from app.equity_lab import index_options_common as c
from app.equity_lab.short_straddle import short_leg


def arrays(*opens, highs=None, lows=None):
    o = np.array(opens, dtype=float)
    return o, np.array(highs if highs is not None else opens, dtype=float), np.array(lows if lows is not None else opens, dtype=float)


def test_a_short_leg_is_stopped_at_the_stop_or_at_a_worse_open():
    o, h, low = arrays(100, 105, 130, 90, 80)
    assert short_leg(o, h, low, 0.25, "none") == [(100.0, 130.0, 0, 2)]                  # opened beyond 125: filled at the open
    o, h, low = arrays(100, 105, 120, 90, 80, highs=[100, 105, 127, 90, 80])
    assert short_leg(o, h, low, 0.25, "none") == [(100.0, 125.0, 0, 2)]
    assert short_leg(*arrays(100, 90, 80, 70, 60), 0.25, "none") == [(100.0, 60.0, 0, 4)]      # never stopped: bought back at the exit minute


def test_re_entry_sells_again_and_trailing_uses_only_earlier_lows():
    o, h, low = arrays(100, 130, 100, 90, 80)
    assert [t[2:] for t in short_leg(o, h, low, 0.25, "re-enter once")] == [(0, 1), (2, 4)]
    o, h, low = arrays(100, 60, 75, 50, 50)
    assert short_leg(o, h, low, 0.25, "trail after 30% decay") == [(100.0, 75.0, 0, 2)]      # low 60 reached 30% decay; stop at 72, opened at 75


def test_costs_charge_stt_on_sells_only_and_slippage_is_always_against_the_trader():
    sell = c.charges("NIFTY", date(2026, 5, 4), [(-1, 100.0, 65)])
    buy = c.charges("NIFTY", date(2026, 5, 4), [(1, 100.0, 65)])
    assert sell - buy > 100 * 65 * 0.0015 - 1                                                 # STT 0.15% since April 2026
    assert c.slipped(100, 1, ("pct", 0.005)) == 100.5 and c.slipped(100, -1, ("rs", 1.0)) == 99.0
    assert c.expiry_days("NIFTY", [date(2026, 11, d) for d in (23, 24, 25, 26)]) == {date(2026, 11, 24)}
