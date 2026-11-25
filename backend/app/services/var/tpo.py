"""TPO (time-price opportunity) and Initial Balance.

One TPO per price row per time bracket. Rows are instrument ticks. When built from candle ranges the profile is
``approximate`` (every row in [low, high] is treated as visited). TPO POC (time) and volume POC are separate fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from .profile import ZERO, choose_poc, tick_index, value_area
from .vtypes import IST, Bar


@dataclass(frozen=True)
class TPOResult:
    counts: dict[int, int]
    poc: int | None
    val: int | None
    vah: int | None
    brackets: int
    approximate: bool
    single_prints: list[int]


@dataclass(frozen=True)
class InitialBalance:
    high: Decimal | None
    low: Decimal | None
    complete: bool          # only True once the IB window has fully closed at the evaluation time

    @property
    def mid(self) -> Decimal | None:
        return None if self.high is None or self.low is None else (self.high + self.low) / 2

    @property
    def range(self) -> Decimal | None:
        return None if self.high is None or self.low is None else self.high - self.low


def session_open(ts: datetime) -> datetime:
    local = ts.astimezone(IST)
    return datetime.combine(local.date(), time(9, 15), tzinfo=IST)


def tpo_profile(bars: list[Bar], tick: Decimal, *, bracket_minutes: int = 30, pct: float = 70.0) -> TPOResult:
    if not bars:
        return TPOResult({}, None, None, None, 0, True, [])
    open_ts = session_open(bars[0].ts)
    visited: dict[int, set[int]] = {}
    for b in bars:
        k = int((b.ts.astimezone(IST) - open_ts) // timedelta(minutes=bracket_minutes))
        lo, hi = tick_index(b.low, tick), tick_index(b.high, tick)
        for r in range(lo, hi + 1):
            visited.setdefault(r, set()).add(k)
    counts = {r: len(s) for r, s in visited.items()}
    w = {r: Decimal(c) for r, c in counts.items()}
    va = value_area(w, pct)
    poc = choose_poc(w) if w else None
    brackets = len({k for s in visited.values() for k in s})
    return TPOResult(counts, poc, va[0] if va else None, va[1] if va else None, brackets, True,
                     sorted(r for r, c in counts.items() if c == 1))


def initial_balance(bars: list[Bar], now: datetime, *, minutes: int = 60) -> InitialBalance:
    """IB from bars that opened inside the window. ``complete`` only when ``now`` >= window end (no early use)."""
    if not bars:
        return InitialBalance(None, None, False)
    start = session_open(bars[0].ts)
    end = start + timedelta(minutes=minutes)
    inside = [b for b in bars if b.ts >= start and b.end <= end and b.end <= now]
    if not inside:
        return InitialBalance(None, None, False)
    complete = now >= end
    return InitialBalance(max(b.high for b in inside), min(b.low for b in inside), complete)
