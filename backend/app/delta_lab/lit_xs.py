"""Part 1.2 (cross-sectional momentum / reversal, weekly) and 1.3 (funding carry and contrarian) on Delta perps, daily bars. Hypothetical; decision support, not investment advice.
Definitions: Liu-Tsyvinski-Wu style coin-level momentum ranks (7/14/30-day return, raw and divided by 30-day volatility), long top / short bottom N, long-only top N, and the short-term
reversal version (buy last week's losers); weekly rebalance at the Sunday close (Monday 00:00 UTC), weights held static until the next rebalance. Liquidity floor: 30-day mean turnover
of at least USD 2M per day and at least 31 days of history, point in time; N is capped at half the eligible coins (so 'top/bottom 10' becomes smaller when fewer than 20 coins qualify).
Funding: 7-day funding = sum of the last 21 funding settlements (exact Delta rates). Carry (a): short the coins with the highest 7-day funding (long those with the lowest), with the same
notional hedged by BTC; Delta cannot hold spot, so this is perp-only. Contrarian (b): per coin, funding above its own trailing 180-day percentile and price stalling -> short (mirror long)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import lit_core as K
from . import lit_data as LD

DF = pd.DataFrame


class XP:
    def __init__(self):
        P = LD.panel()
        D = LD.daily(P)
        self.D, self.C, self.F, self.names, self.t = D, D["C"], D["F"], D["names"], D["t"]
        self.ret = np.vstack([np.full((1, self.C.shape[1]), np.nan), self.C[1:] / self.C[:-1] - 1])
        self.vol30 = DF(self.ret).rolling(30, min_periods=25).std().to_numpy()
        self.turn30 = DF(D["TO"]).rolling(30, min_periods=25).mean().to_numpy()
        self.hist = DF(np.isfinite(self.C).astype(float)).rolling(400, min_periods=1).sum().to_numpy()
        self.elig = (self.turn30 >= 2e6) & (self.hist >= 31) & np.isfinite(self.C) & (self.F is not None)
        self.fund7 = DF(np.nan_to_num(self.F)).rolling(7).sum().to_numpy()
        self.dow = ((self.t // 86400) + 3) % 7                      # 0 = Monday
        self.sunday = self.dow == 6
        n_el = self.elig.sum(1)
        first = int(np.flatnonzero((n_el >= 6) & self.sunday)[0])
        self.cuts = K.split_idx(self.t, str(pd.to_datetime(self.t[first], unit="s").date()), None)
        self.btc = list(self.names).index("BTCUSD")
        self.btc_down = K.btc_down_mask(self.C, self.names)


def hold_weekly(x: XP, pick) -> np.ndarray:
    W = np.zeros_like(x.C)
    cur = np.zeros(x.C.shape[1])
    for t in range(len(x.C)):
        if x.sunday[t]:
            el = np.flatnonzero(x.elig[t])
            cur = pick(t, el) if len(el) >= 6 else np.zeros(x.C.shape[1])
        W[t] = cur
    return W


def rank_weights(x: XP, metric: str, k: int, n: int, ls: bool, reverse: bool):
    def pick(t, el):
        r = x.C[t, el] / x.C[t - k, el] - 1
        if metric == "vol-adjusted":
            r = r / np.maximum(x.vol30[t, el], 1e-4)
        order = np.argsort(r)
        m = min(n, len(el) // 2 if ls else len(el))
        lo, hi = el[order[:m]], el[order[-m:]]
        w = np.zeros(x.C.shape[1])
        long_, short_ = (lo, hi) if reverse else (hi, lo)
        if ls:
            w[long_] += 0.5 / m
            w[short_] -= 0.5 / m
        else:
            w[long_] += 1.0 / m
        return w
    return pick


def carry_weights(x: XP, q: float, mode: str, hedge: bool):
    def pick(t, el):
        el = el[el != x.btc]
        if len(el) < 5:
            return np.zeros(x.C.shape[1])
        f = x.fund7[t, el]
        n = max(1, int(round(q * len(el))))
        order = np.argsort(f)
        w = np.zeros(x.C.shape[1])
        if mode in ("short-high", "both"):
            sel = el[order[-n:]]
            sel = sel[x.fund7[t, sel] > 0]
            if len(sel):
                w[sel] -= 0.5 / len(sel)
                if hedge:
                    w[x.btc] += 0.5
        if mode in ("long-low", "both"):
            sel = el[order[:n]]
            sel = sel[x.fund7[t, sel] < 0]
            if len(sel):
                w[sel] += 0.5 / len(sel)
                if hedge:
                    w[x.btc] -= 0.5
        return w
    return pick


def contrarian(x: XP, q: float, hold: int, stall: bool) -> np.ndarray:
    hi = DF(x.fund7).rolling(180, min_periods=90).quantile(q).shift(1).to_numpy()
    lo = DF(x.fund7).rolling(180, min_periods=90).quantile(1 - q).shift(1).to_numpy()
    h5 = DF(x.C).rolling(5, min_periods=5).max().shift(1).to_numpy()
    l5 = DF(x.C).rolling(5, min_periods=5).min().shift(1).to_numpy()
    n = x.C.shape[1]
    cur, held = np.zeros(n), np.zeros(n)
    pos = np.zeros_like(x.C)
    for t in range(len(x.C)):
        held = np.where(cur != 0, held + 1, 0.0)
        ex = held >= hold
        cur, held = np.where(ex, 0.0, cur), np.where(ex, 0.0, held)
        with np.errstate(invalid="ignore"):
            sh = x.elig[t] & (x.fund7[t] >= hi[t]) & (x.fund7[t] > 0) & ((x.C[t] <= h5[t]) if stall else True)
            lg = x.elig[t] & (x.fund7[t] <= lo[t]) & (x.fund7[t] < 0) & ((x.C[t] >= l5[t]) if stall else True)
        ent = cur == 0
        cur = np.where(ent & sh, -1.0, np.where(ent & lg & ~sh, 1.0, cur))
        held = np.where(ent & (sh | lg), 0.0, held)
        pos[t] = cur
    return pos / np.maximum(np.abs(pos).sum(1, keepdims=True), 10)


def run_xs(store: dict, rows: list) -> None:
    x = XP()

    def add(family, name, params, W, bad_btc_hedge=False):
        res = K.run(W, x.C, x.F, 1)
        rows.append({"family": family, "name": name, "params": params, "eval": K.evaluate(res, x.cuts, 1), "turn_per_day": round(float(res["turn"].mean()), 3), "key": len(rows)})
        store[len(rows) - 1] = (res, x)

    for metric in ("raw", "vol-adjusted"):
        for k in (7, 14, 30):
            for ls in (True, False):
                for n in (3, 5, 10):
                    nm = f"{k}d {metric} | {'long top / short bottom' if ls else 'long-only top'} {n}"
                    add("xs_momentum", nm, dict(lookback=k, metric=metric, n=n, long_short=ls), hold_weekly(x, rank_weights(x, metric, k, n, ls, False)))
    for metric in ("raw", "vol-adjusted"):
        for ls in (True, False):
            for n in (3, 5, 10):
                nm = f"reversal 7d {metric} | {'long losers / short winners' if ls else 'long-only 1-week losers'} {n}"
                add("xs_reversal", nm, dict(lookback=7, metric=metric, n=n, long_short=ls), hold_weekly(x, rank_weights(x, metric, 7, n, ls, True)))
    for q in (0.10, 0.20):
        for mode in ("short-high", "long-low", "both"):
            for hedge in (True, False):
                add("funding_carry", f"carry: top/bottom {int(q * 100)}% funding | {mode} | {'BTC hedge' if hedge else 'unhedged'}", dict(q=q, mode=mode, hedge=hedge),
                    hold_weekly(x, carry_weights(x, q, mode, hedge)))
    for q in (0.90, 0.80):
        for hold in (3, 7):
            for stall in (True, False):
                add("funding_contrarian", f"contrarian: funding above own {int(q * 100)}th percentile (mirror below) | hold {hold}d | {'price stalling' if stall else 'no price condition'}",
                    dict(pct=q, hold=hold, stall=stall), contrarian(x, q, hold, stall))
