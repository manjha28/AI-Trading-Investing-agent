"""Triple-barrier geometry and labelling.

    s_raw  = k_vol * sigma_H / r
    s      = max(s_raw, cost_rt_pct / theta_feeR, min_tick_mult * tick / price)      SKIP if s > s_cap_mult * s_raw
    target = r * s

Interpretation note (spec §HARD RULES 2 vs §7): a stop triggers when ``low <= stop`` (a touch is enough: the conservative choice for the strategy) and a
target needs ``high > target + penetration`` (strict). Both choices lean against the strategy, so neither side of a trade is flattered.
Within one bar the stop is always checked before the target.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class BarrierCfg:
    k_vol: float = 1.0
    theta_feeR: float = 0.25
    s_cap_mult: float = 2.0
    min_tick_mult: float = 5.0
    r_min: float = 1.5
    waiver_buffer: float = 2.0


def stop_fraction(sigma_h: np.ndarray, r: float, cost_rt_pct: float, tick: float, price: np.ndarray, cfg: BarrierCfg) -> tuple[np.ndarray, np.ndarray]:
    """(s, skip) as fractions of price. ``skip`` is True where the cost/tick floors inflated s beyond ``s_cap_mult`` x the volatility-implied stop."""
    s_raw = cfg.k_vol * sigma_h / r
    s = np.maximum.reduce([s_raw, np.full_like(s_raw, cost_rt_pct / cfg.theta_feeR), cfg.min_tick_mult * tick / price])
    skip = ~np.isfinite(s_raw) | (s > cfg.s_cap_mult * s_raw)
    return s, skip


def h_effective(H: float, minutes_to_flatten: float | None, waiver_window: float | None, cfg: BarrierCfg) -> float:
    """H_effective = min(H, minutes_to_flatten, waiver_window - waiver_buffer)."""
    candidates = [H]
    if minutes_to_flatten is not None:
        candidates.append(minutes_to_flatten)
    if waiver_window is not None:
        candidates.append(waiver_window - cfg.waiver_buffer)
    return float(min(candidates))


def cap_target_with_level(entry: float, direction: int, s: float, r: float, levels: list[float], cfg: BarrierCfg) -> float:
    """Structural levels (poc/vah/val/vwap) may pull the target IN only if the result is still >= r_min * s (as a fraction of entry); else they are ignored."""
    target = r * s
    best = target
    for level in levels:
        if not np.isfinite(level):
            continue
        dist = direction * (level - entry) / entry
        if 0 < dist < best and dist >= cfg.r_min * s:
            best = dist
    return best


def penetration(price: np.ndarray | float, tick: float, slip_bps: float):
    return np.maximum(tick, slip_bps * 1e-4 * price)


def label_triple_barrier(open_: np.ndarray, high: np.ndarray, low: np.ndarray, s: np.ndarray, r: float, H_bars: int, direction: int, tick: float, slip_bps: float,
                         valid: np.ndarray | None = None) -> np.ndarray:
    """First-touch label per bar i, entering at open[i+1]: +1 target, -1 stop, 0 timeout, NaN when unlabellable (no future bars / invalid s).

    Fully vectorised over bars (one pass per horizon step). Stop is checked before target inside a bar; the target needs strict penetration.
    """
    n = len(open_)
    label = np.full(n, np.nan)
    ok = np.zeros(n, dtype=bool)
    ok[: n - H_bars - 1] = True
    ok &= np.isfinite(s)
    if valid is not None:
        ok &= valid
    entry = np.full(n, np.nan)
    entry[:-1] = open_[1:]
    stop = entry * (1 - direction * s)
    target = entry * (1 + direction * r * s)
    pen = penetration(entry, tick, slip_bps)
    undecided = ok.copy()
    label[ok] = 0.0
    for j in range(H_bars):
        k = np.arange(n) + 1 + j
        inside = k < n
        hi = np.where(inside, high[np.minimum(k, n - 1)], np.nan)
        lo = np.where(inside, low[np.minimum(k, n - 1)], np.nan)
        if direction == 1:
            stop_hit, tgt_hit = lo <= stop, hi > target + pen
        else:
            stop_hit, tgt_hit = hi >= stop, lo < target - pen
        stopped = undecided & stop_hit                       # stop first, always
        won = undecided & ~stop_hit & tgt_hit
        label[stopped], label[won] = -1.0, 1.0
        undecided &= ~(stopped | won)
    return label
