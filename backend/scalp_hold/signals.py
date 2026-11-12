"""Boolean signals T (trend pullback), R (band reversion), B (value-area breakout). No composite score."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SignalCfg:
    min_bars_since_anchor: int = 20
    rvol_min: float = 1.0
    # T
    band: float = 0.25
    t_lookback: int = 6
    t_min_pullback_bars: int = 2
    t_max_depth: float = 1.0
    # R
    z_ext: float = 2.0
    r_lookback: int = 3
    strong_opposition_mult: float = 2.0
    # B
    b_break: float = 0.15
    b_rvol: float = 1.3
    b_node_max: float = 0.8
    regime_thr: float = 0.35


def _consecutive(cond: pd.Series, n: int) -> pd.Series:
    return cond.astype(float).rolling(n, min_periods=n).min().fillna(0.0).astype(bool)


def _any_recent(cond: pd.Series, n: int) -> pd.Series:
    return cond.astype(float).rolling(n, min_periods=1).max().fillna(0.0).astype(bool)


def compute_signals(df: pd.DataFrame, f: pd.DataFrame, cfg: SignalCfg = SignalCfg()) -> pd.DataFrame:
    """Boolean columns T_long, T_short, R_long, R_short, B_long, B_short (index-aligned to ``df``). All use only bars up to and including i."""
    close, sigma, vwap = df["close"], f["sigma"], f["vwap"]
    base = (f["bsa"] >= cfg.min_bars_since_anchor) & (f["rvol"] >= cfg.rvol_min) & sigma.gt(0)
    dclose = close.diff()
    out: dict[str, pd.Series] = {}
    for name, d in (("long", 1), ("short", -1)):
        # T: trend pullback
        in_band = ((close - vwap).abs() <= cfg.band * sigma)
        recent = _any_recent(in_band, cfg.t_lookback)
        lasted = _any_recent(in_band & in_band.shift(1, fill_value=False), cfg.t_lookback)          # >= 2 consecutive in-band bars, not an instant wick
        depth = (d * (vwap - close) / sigma)                                                         # distance AGAINST the trend, in sigma
        shallow = depth.rolling(cfg.t_lookback, min_periods=1).max() <= cfg.t_max_depth
        confirm = _consecutive(d * dclose > 0, 2)
        out[f"T_{name}"] = base & (f["regime"] == d) & (f["htf_bias"] == d) & recent & lasted & shallow & confirm
        # R: band reversion (long = fade a low extreme)
        z = f["z"]
        extreme = _any_recent((d * z) <= -cfg.z_ext, cfg.r_lookback)
        back_inside = (d * z) > -cfg.z_ext
        not_strongly_opposed = ~((d * f["slope_n"]) <= -cfg.strong_opposition_mult * cfg.regime_thr)
        out[f"R_{name}"] = base & extreme & back_inside & confirm & not_strongly_opposed.fillna(True)
        # B: value-area breakout
        level = f["vah"] if d == 1 else f["val"]
        node = f["node_fwd_up"] if d == 1 else f["node_fwd_dn"]
        beyond = (d * (close - level)) >= cfg.b_break * sigma
        out[f"B_{name}"] = base & _consecutive(beyond, 2) & (f["rvol"] >= cfg.b_rvol) & (node <= cfg.b_node_max) & (f["regime"] != -d)
    return pd.DataFrame({k: v.fillna(False).astype(bool) for k, v in out.items()}, index=df.index)


def thesis_dead(stype: str, d: int, k: int, arrays: dict[str, np.ndarray], entry: dict, cfg: SignalCfg = SignalCfg()) -> bool:
    """Early exit on invalidation at bar k (evaluated on the CLOSE of bar k)."""
    sigma = arrays["sigma"][k]
    if stype == "T":
        opposite = arrays["regime"][k] == -d
        wrong_side = np.isfinite(sigma) and sigma > 0 and (d * (arrays["vwap"][k] - arrays["close"][k]) / sigma) > cfg.band
        return bool(opposite or wrong_side)
    if stype == "R":
        extreme = entry.get("extreme")
        if extreme is None or not np.isfinite(extreme):
            return False
        broke = arrays["low"][k] < extreme if d == 1 else arrays["high"][k] > extreme
        return bool(broke and arrays["rvol"][k] > 1.0)
    if stype == "B":
        level = entry.get("level")
        if level is None or not np.isfinite(level) or not np.isfinite(sigma):
            return False
        return bool(d * (arrays["close"][k] - level) < -cfg.b_break * sigma)
    return False


def regime_flip(d: int, k: int, arrays: dict[str, np.ndarray]) -> bool:
    """T only: the trend regime no longer agrees with the trade direction."""
    return bool(arrays["regime"][k] != d)
