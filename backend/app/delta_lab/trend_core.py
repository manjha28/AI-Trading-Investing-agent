"""Shared engine for the 2026-11-26 research (trend filter, 5 EMA entries, VWAP bands). Hypothetical; decision support, not investment advice. No order path.

DATA: Delta India 1-minute last-price candles (public API, data/delta_1m), BTC/ETH/SOL/LISTA + Delta's top-10 crypto perps by 24h turnover. Each coin's own history
starts when Delta listed it (BTC/ETH Dec 2023). No Binance data. A bar is built from the minutes that exist; the newest unfinished bar is dropped.
COSTS: taker 0.05% + 18% GST per fill, maker 0.02% + 18% GST, 1 bp slippage on every taker fill (entry and exit; a resting limit that fills is a maker fill, no slippage).
SPLIT (fixed before looking): train < 2025-07-01, validation 2025-07-01..2026-03-31, final check >= 2026-04-01 (by entry-fill time).
Everything uses completed bars only: a 1h/4h value is used only once that bar has closed; a pivot is used only after its confirming bars have closed."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3] / "data" / "delta_1m"
FEE = 0.0005 * 1.18 * 1e4          # 5.9 bps per taker fill (fee + GST)
MAKER = 0.0002 * 1.18 * 1e4        # 2.36 bps per maker fill
SLIP = 1.0                          # bps per taker fill
SPLIT1 = int(pd.Timestamp("2025-07-01", tz="UTC").timestamp())
SPLIT2 = int(pd.Timestamp("2026-04-01", tz="UTC").timestamp())
MAXC = 48                           # candles a trade may live
FILTER_NAMES: list[str] = []


def seg(t: np.ndarray | int) -> np.ndarray | str:
    t = np.asarray(t)
    return np.where(t < SPLIT1, "train", np.where(t < SPLIT2, "validation", "final"))


class Sym:
    """One coin: 1-minute arrays plus every higher-timeframe series needed for the bias filters."""

    def __init__(self, name: str):
        d = pd.read_pickle(ROOT / f"{name}_1m.pkl").drop_duplicates("time").sort_values("time")
        d = d[d.time >= 1672531200].reset_index(drop=True)
        self.name = name
        self.t1, self.o1, self.h1, self.l1, self.c1, self.v1 = (d[k].to_numpy(dtype=float if k != "time" else np.int64) for k in ("time", "open", "high", "low", "close", "volume"))
        self._bars: dict[int, dict] = {}
        self._tables()

    # ---- bars -------------------------------------------------------------------------------------------------
    def bars(self, secs: int) -> dict:
        if secs in self._bars:
            return self._bars[secs]
        g = (self.t1 // secs) * secs
        df = pd.DataFrame({"g": g, "o": self.o1, "h": self.h1, "l": self.l1, "c": self.c1, "v": self.v1})
        a = df.groupby("g").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"))
        t = a.index.to_numpy()
        keep = t + secs <= self.t1[-1] + 60                     # drop the newest unfinished bar
        b = {"t": t[keep], **{k: a[k].to_numpy()[keep] for k in "ohlcv"}}
        b["s"] = np.searchsorted(self.t1, b["t"])              # first 1m index of each bar
        b["e"] = np.searchsorted(self.t1, b["t"] + secs)        # 1m index after the bar
        self._bars[secs] = b
        return b

    # ---- higher-timeframe tables (1h structure, 1h EMAs, 4h EMA, rolling 24h VWAP) --------------------------------
    def _tables(self) -> None:
        h = self.bars(3600)
        H, L, C = h["h"], h["l"], h["c"]
        n = len(C)
        self.h_end = h["t"] + 3600
        ema = lambda x, sp: pd.Series(x).ewm(span=sp, adjust=False).mean().to_numpy()
        e50, e20 = ema(C, 50), ema(C, 20)
        # pivots (2 bars each side, strict): pivot at j is known once bar j+2 has closed
        ph = np.zeros(n, bool)
        pl = np.zeros(n, bool)
        for j in range(2, n - 2):
            ph[j] = H[j] > H[j - 1] and H[j] > H[j - 2] and H[j] > H[j + 1] and H[j] > H[j + 2]
            pl[j] = L[j] < L[j - 1] and L[j] < L[j - 2] and L[j] < L[j + 1] and L[j] < L[j + 2]
        self.ph_idx, self.pl_idx = np.flatnonzero(ph), np.flatnonzero(pl)
        self.ph_px, self.pl_px = H[self.ph_idx], L[self.pl_idx]
        b1, r1l, r1s, r2l, r2s = (np.zeros(n, dtype=np.int8) for _ in range(5))
        for jc in range(5, n):                                   # jc = last COMPLETED 1h bar
            hi = self.ph_idx[(self.ph_idx <= jc - 2) & (self.ph_idx >= jc - 23)]
            lo = self.pl_idx[(self.pl_idx <= jc - 2) & (self.pl_idx >= jc - 23)]
            allhi = self.ph_idx[self.ph_idx <= jc - 2]
            alllo = self.pl_idx[self.pl_idx <= jc - 2]
            if len(hi) >= 2 and len(lo) >= 2:
                a, b_, c_, d_ = H[hi[-1]], H[hi[-2]], L[lo[-1]], L[lo[-2]]
                b1[jc] = -1 if (a < b_ and c_ < d_) else (1 if (a > b_ and c_ > d_) else 0)
            if len(allhi):
                r1l[jc] = C[jc] > H[allhi[-1]]                    # 1h close above the last swing high (break of structure up)
            if len(alllo):
                r1s[jc] = C[jc] < L[alllo[-1]]
            if len(alllo) >= 2:
                r2l[jc] = (L[alllo[-1]] > L[alllo[-2]]) and C[jc] > e20[jc]     # higher low, then a close above EMA20
            if len(allhi) >= 2:
                r2s[jc] = (H[allhi[-1]] < H[allhi[-2]]) and C[jc] < e20[jc]     # lower high, then a close below EMA20
        rising = np.r_[False, e50[1:] > e50[:-1]]
        falling = np.r_[False, e50[1:] < e50[:-1]]
        b2 = np.where((C < e50) & falling, -1, np.where((C > e50) & rising, 1, 0)).astype(np.int8)
        self.B1, self.B2, self.R1l, self.R1s, self.R2l, self.R2s = b1, b2, r1l, r1s, r2l, r2s
        f = self.bars(14400)
        e4 = ema(f["c"], 20)
        self.f_end = f["t"] + 14400
        self.B3 = np.where(f["c"] < e4, -1, 1).astype(np.int8)
        # rolling 24h VWAP at every minute (typical price)
        tp = (self.h1 + self.l1 + self.c1) / 3
        cpv, cv = np.r_[0, np.cumsum(tp * self.v1)], np.r_[0, np.cumsum(self.v1)]
        lo_i = np.searchsorted(self.t1, self.t1 - 86400, side="right")
        idx = np.arange(len(self.t1))
        vv = cv[idx + 1] - cv[lo_i]
        self.vwap24 = np.where(vv > 0, (cpv[idx + 1] - cpv[lo_i]) / np.maximum(vv, 1e-12), np.nan)

    def flags(self, tref: np.ndarray) -> dict[str, np.ndarray]:
        """Bias and reversal state as known at time tref (only bars that had closed by then)."""
        jh = np.searchsorted(self.h_end, tref, side="right") - 1
        j4 = np.searchsorted(self.f_end, tref, side="right") - 1
        k = np.searchsorted(self.t1, tref) - 1                   # last completed minute
        ok = (jh >= 5) & (j4 >= 0) & (k >= 0)
        jh, j4, k = np.clip(jh, 0, None), np.clip(j4, 0, None), np.clip(k, 0, None)
        v = self.vwap24[k]
        b4 = np.where(np.isnan(v), 0, np.where(self.c1[k] < v, -1, 1)).astype(np.int8)
        return {"ok": ok, "B1": self.B1[jh], "B2": self.B2[jh], "B3": self.B3[j4], "B4": b4,
                "R1l": self.R1l[jh].astype(bool), "R1s": self.R1s[jh].astype(bool), "R2l": self.R2l[jh].astype(bool), "R2s": self.R2s[jh].astype(bool), "jh": jh}

    def pivot_levels(self, jh: int, side: int) -> np.ndarray:
        """1h swing lows (side<0, support for a short) or highs (side>0) confirmed by 1h bar jh, last 7 days."""
        idx, px = (self.pl_idx, self.pl_px) if side < 0 else (self.ph_idx, self.ph_px)
        m = (idx <= jh - 2) & (idx >= jh - 168)
        return px[m]


for _b in ("B1", "B2", "B3", "B4"):
    for _r in ("R1", "R2", "R3"):
        for _n in ("both", "none"):
            if _b in ("B3", "B4") and _n == "none":
                continue                                          # these two have no neutral state
            FILTER_NAMES.append(f"{_b}|{_r}|neutral={_n}")


def allow(fname: str, d: np.ndarray, fl: dict[str, np.ndarray]) -> np.ndarray:
    """d = +1 long / -1 short. With the trend: allowed. Against it: only after the reversal rule. Neutral: both or none."""
    if fname == "none":
        return fl["ok"].copy()
    b, r, n = fname.split("|")
    bias = fl[b]
    rev = np.zeros(len(d), bool) if r == "R3" else np.where(d > 0, fl[f"{r}l"], fl[f"{r}s"])
    return fl["ok"] & ((bias == d) | ((bias == -d) & rev) | ((bias == 0) & (n == "neutral=both")))


# ---- trade simulation helpers (1-minute path, stop before target inside the same minute) ---------------------------------
def first_touch(S: Sym, d: int, k0: int, end: int, stop: float, target: float | None) -> tuple[int, str]:
    if d < 0:
        sh = S.h1[k0:end] >= stop
        th = (S.l1[k0:end] <= target) if target is not None else None
    else:
        sh = S.l1[k0:end] <= stop
        th = (S.h1[k0:end] >= target) if target is not None else None
    si = int(sh.argmax()) if sh.any() else 10**9
    ti = int(th.argmax()) if (th is not None and th.any()) else 10**9
    if si == 10**9 and ti == 10**9:
        return end - 1, "time"
    return (k0 + si, "stop") if si <= ti else (k0 + ti, "target")


def exit_px(S: Sym, d: int, k0: int, j: int, why: str, stop: float, target: float | None) -> float:
    if why == "time":
        return S.c1[j]
    lvl = stop if why == "stop" else target
    if j == k0:
        return lvl
    o = S.o1[j]
    if why == "stop":
        return max(o, lvl) if d < 0 else min(o, lvl)
    return min(o, lvl) if d < 0 else max(o, lvl)


def bps_of(d: int, entry: float, px: float, fee_in: float) -> float:
    fill = px * (1 - d * SLIP / 1e4)                              # selling out of a long lowers the price; buying back a short raises it
    return d * (fill - entry) / entry * 1e4 - fee_in - FEE


def trail_exit(S: Sym, b: dict, secs: int, d: int, fk: int, entry: float, stop0: float) -> tuple[int, str, float, float]:
    """Break-even at +1R, then the stop follows the latest confirmed swing high (short) / low (long) of the trading timeframe, only ever tightening.
    A candle's information is used from the NEXT candle on. Returns (1m index of exit, why, exit price raw, final stop)."""
    R = abs(stop0 - entry)
    stop, armed = stop0, False
    ib = int(np.searchsorted(b["t"], S.t1[fk], side="right") - 1)
    tgt1 = entry - d * R
    end_t = S.t1[fk] + MAXC * secs
    cur = fk
    for p in range(ib, min(len(b["t"]), ib + MAXC + 2)):
        e = b["e"][p]
        lo_i = cur if p == ib else b["s"][p]
        if lo_i >= e:
            continue
        end = min(e, int(np.searchsorted(S.t1, end_t)))
        if lo_i >= end:
            break
        j, why = first_touch(S, d, lo_i, end, stop, None)
        if why == "stop":
            return j, "stop", exit_px(S, d, lo_i, j, "stop", stop, None), stop
        if end < e or p + 1 >= len(b["t"]):
            return end - 1, "time", S.c1[end - 1], stop
        # candle p is complete
        if not armed and ((d < 0 and S.l1[lo_i:end].min() <= tgt1) or (d > 0 and S.h1[lo_i:end].max() >= tgt1)):
            armed, stop = True, (min(stop, entry) if d < 0 else max(stop, entry))
        if armed and p >= ib + 2:
            c = p - 1                                              # candidate pivot: bar p-1 vs its neighbours p-2 and p
            if d < 0 and b["h"][c] > b["h"][c - 1] and b["h"][c] > b["h"][p] and b["h"][c] < stop and b["h"][c] > b["c"][p]:
                stop = b["h"][c]
            if d > 0 and b["l"][c] < b["l"][c - 1] and b["l"][c] < b["l"][p] and b["l"][c] > stop and b["l"][c] < b["c"][p]:
                stop = b["l"][c]
    last = min(len(S.t1), int(np.searchsorted(S.t1, end_t))) - 1
    return last, "time", S.c1[last], stop


def stats(b: np.ndarray) -> dict:
    if len(b) == 0:
        return {"trades": 0}
    w, l = b[b > 0], b[b <= 0]
    eq = np.r_[0, np.cumsum(b)]
    return {"trades": int(len(b)), "net_bps": round(float(b.mean()), 2), "win_rate": round(float((b > 0).mean()), 3),
            "pf": round(float(w.sum() / -l.sum()), 3) if l.sum() < 0 else None, "max_dd_bps": round(float((np.maximum.accumulate(eq) - eq).max()), 1),
            "sr": round(float(b.mean() / b.std(ddof=1)), 5) if len(b) > 2 and b.std() > 0 else None}
