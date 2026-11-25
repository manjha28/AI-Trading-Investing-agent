"""Evaluation engine for the literature round (Delta). A strategy is a weight matrix W[t, coin] decided at the CLOSE of bar t and held over bar t+1.
Per bar: gross = W*return, cost = (5.9 taker fee+GST + 2 slippage) bps x |change of W| (maker fills are costed by the caller through W_cost_scale), funding = W*rate at bar start
(a long pays a positive rate; exact Delta rate at 00/08/16 UTC). Episodes (a coin's contiguous same-side position) give 'trades'; their net is in bps of the notional held.
Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .round4_d2 import boot_ci

COST = (0.0005 * 1.18 * 1e4 + 2.0) / 1e4          # per unit notional per side
RUNNING_TOTAL_BEFORE = 1100                          # the owner's running count at the start of this round


def split_idx(t: np.ndarray, start: str, end: str | None = None) -> tuple[int, int, int, int]:
    """Bar indices of (start, train/validation cut, validation/holdout cut, end): 50/25/25 of the calendar span."""
    a = int(np.searchsorted(t, pd.Timestamp(start, tz="UTC").timestamp()))
    b = len(t) if end is None else int(np.searchsorted(t, pd.Timestamp(end, tz="UTC").timestamp()))
    span = b - a
    return a, a + span // 2, a + span * 3 // 4, b


def run(W: np.ndarray, C: np.ndarray, F: np.ndarray, bpd: int, cost: float = COST, entry_cost: np.ndarray | None = None) -> dict:
    """W, C, F are (T, N). Returns per-bar series and the episode list. entry_cost (optional, per-coin-bar) replaces COST on that bar's turnover (maker entries)."""
    W = np.nan_to_num(W)
    R = np.zeros_like(C)
    R[1:] = C[1:] / C[:-1] - 1
    R = np.nan_to_num(R, nan=0.0, posinf=0.0, neginf=0.0)
    Fn = np.nan_to_num(F)
    Wp = np.vstack([np.zeros((1, W.shape[1])), W[:-1]])               # position held over bar t = decided at t-1
    Wpp = np.vstack([np.zeros((2, W.shape[1])), W[:-2]]) if len(W) > 1 else Wp
    gross = Wp * R
    c = cost if entry_cost is None else entry_cost
    costs = np.abs(Wp - Wpp) * c
    fund = Wp * Fn
    net = gross - costs - fund
    T = len(W)
    eps = []
    sgn = np.sign(W)
    for j in np.flatnonzero((sgn != 0).any(axis=0)):
        s = sgn[:, j]
        edges = np.flatnonzero(np.diff(np.r_[0, s, 0]) != 0)
        for a, b in zip(edges[:-1], edges[1:]):
            if s[a] == 0:
                continue
            # decision bars a..b-1; held over bars a+1..b (exit cost lands on bar b+1)
            hi = min(b + 1, T - 1)
            pnl = net[a + 1: hi + 1, j].sum()
            size = np.abs(W[a:b, j]).mean()
            if size > 0:
                eps.append((j, a, int(s[a]), pnl / size * 1e4, gross[a + 1: hi + 1, j].sum() / size * 1e4, fund[a + 1: hi + 1, j].sum() / size * 1e4, b - a))
    ep = pd.DataFrame(eps, columns=["coin", "t", "side", "bps", "gross_bps", "fund_bps", "bars"])
    day = lambda x: x.reshape(-1, bpd).sum(1) if len(x) % bpd == 0 else np.add.reduceat(x, np.arange(0, len(x), bpd))
    return {"net": day(net.sum(1)), "gross": day(gross.sum(1)), "cost": day(costs.sum(1)), "fund": day(fund.sum(1)), "ep": ep,
            "turn": day(np.abs(Wp - Wpp).sum(1)), "gross_exposure": float(np.abs(Wp).sum(1).mean())}


def pstats(ep: pd.DataFrame, net_daily: np.ndarray, lo: int, hi: int, bpd: int, ann: int = 365) -> dict:
    """Metrics for one period [lo, hi) given in BARS; episodes by entry bar; daily series cut in days."""
    e = ep[(ep.t >= lo) & (ep.t < hi)]
    b = e.bps.to_numpy()
    d = net_daily[lo // bpd: hi // bpd]
    out = {"trades": int(len(b))}
    if len(b):
        w, l = b[b > 0], b[b <= 0]
        out |= {"net_bps": round(float(b.mean()), 1), "win": round(float((b > 0).mean()), 3), "pf": round(float(w.sum() / -l.sum()), 2) if l.sum() < 0 else None}
    if len(d) > 5:
        eq = np.cumsum(d)
        out |= {"max_dd_pct": round(float((np.maximum.accumulate(eq) - eq).max() * 100), 1), "sharpe": round(float(d.mean() / d.std() * np.sqrt(ann)), 2) if d.std() > 0 else 0.0,
                "ann_ret_pct": round(float(d.mean() * ann * 100), 1), "trades_per_month": round(len(b) / (len(d) / (ann / 12)), 1)}
    return out


def evaluate(res: dict, cuts: tuple[int, int, int, int], bpd: int, ann: int = 365) -> dict:
    a, b, c, d = cuts
    ep, nd = res["ep"], res["net"]
    o = {"train": pstats(ep, nd, a, b, bpd, ann), "validation": pstats(ep, nd, b, c, bpd, ann), "holdout": pstats(ep, nd, c, d, bpd, ann)}
    h = ep[(ep.t >= c) & (ep.t < d)]
    o["holdout_long"] = {"trades": int((h.side > 0).sum()), "net_bps": round(float(h[h.side > 0].bps.mean()), 1) if (h.side > 0).any() else None}
    o["holdout_short"] = {"trades": int((h.side < 0).sum()), "net_bps": round(float(h[h.side < 0].bps.mean()), 1) if (h.side < 0).any() else None}
    return o


def holdout_extras(res: dict, cuts, bpd: int, names: np.ndarray, btc_down: np.ndarray) -> dict:
    a, b, c, d = cuts
    ep = res["ep"]
    h = ep[(ep.t >= c) & (ep.t < d)]
    lo, hi = boot_ci(h.bps.to_numpy())
    pc = {str(names[j]): {"trades": int(len(g)), "net_bps": round(float(g.bps.mean()), 1)} for j, g in h.groupby("coin")}
    top = dict(sorted(pc.items(), key=lambda kv: -kv[1]["trades"])[:8])
    nd = res["net"]
    dd = btc_down[a // bpd: d // bpd]
    seg = nd[a // bpd: d // bpd]
    reg = {"days": int(dd.sum()), "sum_pct": round(float(seg[dd].sum() * 100), 1) if dd.any() else None}
    full = {"gross_bps_per_trade": round(float(h.gross_bps.mean()), 1) if len(h) else None, "funding_bps_per_trade": round(float(h.fund_bps.mean()), 1) if len(h) else None}
    return {"boot90": [round(lo, 1), round(hi, 1)], "per_coin": top, "btc_down20": reg, "decomp": full}


def btc_down_mask(D_C: np.ndarray, names: np.ndarray) -> np.ndarray:
    """Days (daily bars) on which BTC's trailing 30-day return is below -20%."""
    j = list(names).index("BTCUSD")
    s = pd.Series(D_C[:, j])
    return (s / s.shift(30) - 1 < -0.20).fillna(False).to_numpy()


def dsr_prob(daily: np.ndarray, n_trials: int, var_sr: float) -> float | None:
    from statistics import NormalDist
    import math
    x = daily[np.isfinite(daily)]
    if len(x) < 30 or x.std() == 0:
        return None
    sr = x.mean() / x.std(ddof=1)
    Z = NormalDist()
    g = 0.5772156649
    sr0 = math.sqrt(var_sr) * ((1 - g) * Z.inv_cdf(1 - 1 / n_trials) + g * Z.inv_cdf(1 - 1 / (n_trials * math.e)))
    cc = x - x.mean()
    skew, kurt = float((cc ** 3).mean() / x.std() ** 3), float((cc ** 4).mean() / x.std() ** 4)
    den = math.sqrt(max(1e-9, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    return float(Z.cdf((sr - sr0) * math.sqrt(len(x) - 1) / den))
