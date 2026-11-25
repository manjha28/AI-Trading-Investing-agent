"""Part 1.7: exit variants on the FROZEN R3 entry (hash eaea623c741f), Delta 1-minute data, BTC and ETH, 2024-04-10 to 24 Nov 2026. Only the exit changes; the entry (1h bars, 24-bar range in the lowest
10% of its last 1440 bars, stop-entry both sides valid 60 minutes, stop at the other side of the range) is unchanged. Costs: taker 0.05%+GST and 2 bps slippage for the stop entry, stops, trails, time and
reversal exits; a resting 3R target is a maker fill (0.02%+GST, no slippage); exact Delta funding. Hypothetical; decision support, not investment advice."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import lit_core as K
from . import lit_data as LD
from .lit_misc import FEE, MAKER, SLIP

T_START = int(pd.Timestamp("2024-04-10", tz="UTC").timestamp())
VARIANTS = [("Baseline: 3R target, 48h", dict()), ("2R target, 48h", dict(R=2)), ("4R target, 48h", dict(R=4)), ("Break-even at +1R, then 3R, 48h", dict(be=True)),
            ("ATR trail 2 (1h), no target, 48h", dict(trail=2, R=None)), ("ATR trail 3 (1h), no target, 48h", dict(trail=3, R=None)), ("3R target, time stop 24h", dict(hold=24)),
            ("3R target, time stop 72h", dict(hold=72)), ("Reversal LOSS exit: 1h close back through the breakout level", dict(rev_loss=True)),
            ("Reversal PROFIT exit: after +1.5R, 1h close beyond the prior bar's extreme", dict(rev_profit=True)), ("Both reversal exits", dict(rev_loss=True, rev_profit=True)),
            ("Half off at 1.5R, rest ATR trail 2", dict(partial=True, R=None))]


class Coin:
    def __init__(self, sym: str):
        d = pd.read_pickle(LD.ROOT / "delta_1m" / f"{sym}_1m.pkl").drop_duplicates("time").sort_values("time")
        d = d[(d.time >= T_START - 90 * 86400) & (d.time < LD.T1)]
        self.sym = sym
        self.t, self.o, self.h, self.l, self.c = (d[k].to_numpy(dtype=float if k != "time" else np.int64) for k in ("time", "open", "high", "low", "close"))
        g = (self.t // 3600) * 3600
        a = d.assign(g=g).groupby("g").agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), n=("time", "size"))
        a = a[a.n >= 50]
        self.bt, self.bh, self.bl, self.bc = a.index.to_numpy(), a.h.to_numpy(), a.l.to_numpy(), a.c.to_numpy()
        pc = np.r_[self.bc[0], self.bc[:-1]]
        tr = np.maximum(self.bh - self.bl, np.maximum(abs(self.bh - pc), abs(self.bl - pc)))
        self.atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        P = LD.panel()
        j = list(P["names"]).index(sym)
        f = P["F"][:, j]
        m = np.isfinite(f)
        self.ft, self.fr = P["t"][m], f[m]

    def funding(self, t0: int, t1: int, side: int) -> float:
        m = (self.ft > t0) & (self.ft <= t1)
        return side * float(self.fr[m].sum()) * 1e4


def entries(c: Coin) -> list[tuple]:
    s = pd.Series(c.bh)
    hi24, lo24 = s.rolling(24).max().to_numpy(), pd.Series(c.bl).rolling(24).min().to_numpy()
    rank = pd.Series(hi24 - lo24).rolling(1440).rank(pct=True).to_numpy()
    out, busy = [], 0
    for i in np.flatnonzero(rank <= 0.10):
        te = int(c.bt[i]) + 3600
        if te < T_START or te <= busy or i + 1 >= len(c.bt) or c.bt[i + 1] != te:
            continue
        hi, lo = hi24[i], lo24[i]
        k0 = int(np.searchsorted(c.t, te))
        k1 = min(len(c.t), k0 + 60)
        if k1 - k0 < 30:
            continue
        up, dn = c.h[k0:k1] >= hi, c.l[k0:k1] <= lo
        iu, idn = (int(up.argmax()) if up.any() else 10**9), (int(dn.argmax()) if dn.any() else 10**9)
        if iu == idn:
            continue
        side, kf = (1, k0 + iu) if iu < idn else (-1, k0 + idn)
        entry = max(c.o[kf], hi) if side > 0 else min(c.o[kf], lo)
        stop = lo if side > 0 else hi
        out.append((i, side, kf, entry, stop, hi if side > 0 else lo))
        busy = int(c.t[min(kf + 48 * 60, len(c.t) - 1)])        # entry spacing only; the real busy time depends on each variant's exit, applied below
    return out


def sim(c: Coin, side: int, kf: int, entry: float, stop0: float, level: float, v: dict) -> tuple[float, int]:
    R = abs(entry - stop0)
    if R <= 0:
        return np.nan, kf
    tR = v.get("R", 3)
    target = entry + side * tR * R if tR else None
    hold = v.get("hold", 48)
    end = min(len(c.t), kf + hold * 60)
    stop, armed, best, half_done, half_px = stop0, False, entry, False, None
    seg_start = kf
    t_entry = int(c.t[kf])
    hb = np.searchsorted(c.bt, (c.t[kf] // 3600) * 3600)          # index of the entry hour bar
    px, why, kx = None, "time", end - 1
    while seg_start < end:
        seg_end = min(end, int(np.searchsorted(c.t, (c.t[seg_start] // 3600 + 1) * 3600)))
        if seg_end <= seg_start:
            seg_end = min(end, seg_start + 1)
        H, L = c.h[seg_start:seg_end], c.l[seg_start:seg_end]
        sh = (L <= stop) if side > 0 else (H >= stop)
        if v.get("partial") and not half_done:
            tg = entry + side * 1.5 * R
            th = (H >= tg) if side > 0 else (L <= tg)
        else:
            tg = target
            th = None if tg is None else ((H >= tg) if side > 0 else (L <= tg))
        si = int(sh.argmax()) if sh.any() else 10**9
        ti = int(th.argmax()) if (th is not None and th.any()) else 10**9
        if si < 10**9 or ti < 10**9:
            if si <= ti:
                kx = seg_start + si
                gap = c.o[kx] if kx > kf else stop
                px, why = (min(gap, stop) if side > 0 else max(gap, stop)), "stop"
                break
            kx = seg_start + ti
            if v.get("partial") and not half_done:
                half_done, half_px = True, tg
                stop = max(stop, entry) if side > 0 else min(stop, entry)
            else:
                px, why = tg, "target"
                break
        # end of the hour: the bar is complete, update state
        b = int(np.searchsorted(c.bt, (c.t[seg_end - 1] // 3600) * 3600))
        close_ = c.c[seg_end - 1]
        hi_b, lo_b = float(H.max()), float(L.min())
        best = max(best, hi_b) if side > 0 else min(best, lo_b)
        if v.get("be") and not armed and side * (best - entry) >= R:
            armed, stop = True, (max(stop, entry) if side > 0 else min(stop, entry))
        if v.get("trail") or (v.get("partial") and half_done):
            k_ = v.get("trail", 2)
            cand = best - side * k_ * c.atr[min(b, len(c.atr) - 1)]
            stop = max(stop, cand) if side > 0 else min(stop, cand)
        if v.get("rev_loss") and ((close_ < level) if side > 0 else (close_ > level)) and seg_end < end:
            px, why, kx = close_, "reversal", seg_end - 1
            break
        if v.get("rev_profit") and side * (best - entry) >= 1.5 * R and b > 0 and seg_end < end:
            if (side > 0 and close_ < c.bl[b - 1]) or (side < 0 and close_ > c.bh[b - 1]):
                px, why, kx = close_, "reversal", seg_end - 1
                break
        seg_start = seg_end
    if px is None:
        px, kx = c.c[end - 1], end - 1
    exit_cost = (MAKER if why == "target" else FEE + SLIP)
    ret = side * (px - entry) / entry * 1e4
    if v.get("partial") and half_px is not None:
        ret = 0.5 * side * (half_px - entry) / entry * 1e4 + 0.5 * ret
        exit_cost = 0.5 * MAKER + 0.5 * exit_cost
    net = ret - (FEE + SLIP) - exit_cost - c.funding(t_entry, int(c.t[kx]), side)
    return float(net), kx


def run_r3(rows: list, extra: dict) -> None:
    coins = [Coin("BTCUSD"), Coin("ETHUSD")]
    ent = {c.sym: entries(c) for c in coins}
    cut = K.split_idx(np.arange(T_START, int(LD.T1), 86400), "2024-04-10", None)
    tcuts = [int(T_START + x * 86400) for x in cut[1:3]]
    for name, v in VARIANTS:
        recs = []
        for c in coins:
            busy = 0
            for i, side, kf, entry, stop, level in ent[c.sym]:
                te = int(c.t[kf])
                if te <= busy:
                    continue
                net, kx = sim(c, side, kf, entry, stop, level, v)
                if np.isnan(net):
                    continue
                recs.append((te, net, side, c.sym))
                busy = int(c.t[kx])
        d = pd.DataFrame(recs, columns=["t", "bps", "side", "sym"]).sort_values("t")
        def ps(x):
            b = x.bps.to_numpy()
            if len(b) == 0:
                return {"trades": 0}
            w, l = b[b > 0], b[b <= 0]
            eq = np.cumsum(b)
            return {"trades": len(b), "net_bps": round(float(b.mean()), 1), "win": round(float((b > 0).mean()), 3), "pf": round(float(w.sum() / -l.sum()), 2) if l.sum() < 0 else None,
                    "max_dd_bps": round(float((np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]).max()), 0)}
        ev = {"train": ps(d[d.t < tcuts[0]]), "validation": ps(d[(d.t >= tcuts[0]) & (d.t < tcuts[1])]), "holdout": ps(d[d.t >= tcuts[1]])}
        h = d[d.t >= tcuts[1]]
        ev["holdout_long"], ev["holdout_short"] = ps(h[h.side > 0]), ps(h[h.side < 0])
        rows.append({"family": "r3_exits", "name": name, "params": v, "eval": ev, "key": len(rows)})
        extra[name] = d
