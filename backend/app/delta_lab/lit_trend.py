"""Part 1.1 (time-series trend following) and 1.4 (volatility breakout / squeeze) on Delta perps, daily and 4h bars. Hypothetical; decision support, not investment advice.
Sources used for definitions: Donchian/turtle breakouts, SMA crossovers, EMA(100) filter and time-series momentum as in 'Catching Crypto Trends' and 'Systematic trend-following with
adaptive portfolio construction' (titles and abstracts; the papers' full text was not fetched), Moskowitz-Ooi-Pedersen style TSMOM with volatility targeting (position = signal x target vol / 30-day vol,
sized at entry and held), Bollinger squeeze (Bollinger/Carter), NR7 and inside bar (Crabel). Exits are checked on completed bars (closes); a signal on bar t is held over bar t+1.
Universes: U4 = BTC, ETH, SOL, XRP; ALL = every Delta crypto perp with at least 200 bars of its own history at that time (point in time)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import lit_core as K
from . import lit_data as LD

DF = pd.DataFrame
START, END = "2024-04-10", None


def atr_of(C, H, L, n=14):
    pc = np.vstack([np.full((1, C.shape[1]), np.nan), C[:-1]])
    tr = np.fmax(H - L, np.fmax(np.abs(H - pc), np.abs(L - pc)))
    return DF(tr).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().to_numpy()


class Panel:
    def __init__(self, tf: str, universe: str):
        P = LD.panel()
        X = P if tf == "4h" else LD.daily(P)
        self.bpd = 6 if tf == "4h" else 1
        self.tf, self.univ = tf, universe
        cols = [list(X["names"]).index(c) for c in LD.MAJORS] if universe == "U4" else list(range(len(X["names"])))
        g = lambda k: X[k][:, cols]
        self.C, self.H, self.L, self.O, self.F = g("C"), g("H"), g("L"), g("O"), g("F")
        self.names = X["names"][cols]
        self.t = X["t"]
        self.TO = g("TO")
        self.cuts = K.split_idx(self.t, START, END)
        b = self.bpd
        self.ret = np.vstack([np.full((1, self.C.shape[1]), np.nan), self.C[1:] / self.C[:-1] - 1])
        self.vol = DF(self.ret).rolling(30 * b, min_periods=20 * b).std().to_numpy() * np.sqrt(365 * b)
        self.elig = (DF(np.isfinite(self.C).astype(float)).rolling(200 * b, min_periods=1).sum().to_numpy() >= 200 * b) & np.isfinite(self.vol)
        self.atr = atr_of(self.C, self.H, self.L, 14 * b)
        Dd = LD.daily(P)
        self.btc_down = K.btc_down_mask(Dd["C"], Dd["names"])
        jb = list(X["names"]).index("BTCUSD")
        self.btc_up = (X["C"][:, jb] > DF(X["C"][:, jb]).ewm(span=100 * b, adjust=False, min_periods=100 * b).mean()[0].to_numpy())

    def sma(self, n):
        return DF(self.C).rolling(n * self.bpd, min_periods=n * self.bpd).mean().to_numpy()

    def ema(self, n):
        return DF(self.C).ewm(span=n * self.bpd, adjust=False, min_periods=n * self.bpd).mean().to_numpy()


def donchian(p: Panel, n: int, m: int):
    b = p.bpd
    hi_n = DF(p.H).rolling(n * b, min_periods=n * b).max().shift(1).to_numpy()
    lo_n = DF(p.L).rolling(n * b, min_periods=n * b).min().shift(1).to_numpy()
    hi_m = DF(p.H).rolling(m * b, min_periods=m * b).max().shift(1).to_numpy()
    lo_m = DF(p.L).rolling(m * b, min_periods=m * b).min().shift(1).to_numpy()
    cur = np.zeros(p.C.shape[1])
    pos = np.zeros_like(p.C)
    for t in range(len(p.C)):
        c = p.C[t]
        with np.errstate(invalid="ignore"):
            le, se = c > hi_n[t], c < lo_n[t]
            xl, xs = (cur > 0) & (c < lo_m[t]), (cur < 0) & (c > hi_m[t])
        cur = np.where(le, 1.0, np.where(se, -1.0, np.where(xl | xs, 0.0, cur)))
        pos[t] = cur
    return pos


def signals(p: Panel) -> dict[str, np.ndarray]:
    out = {f"Donchian {n}/{n // 2}": donchian(p, n, n // 2) for n in (20, 50, 100)}
    for a, b_ in ((10, 50), (20, 100), (50, 200)):
        s, l = p.sma(a), p.sma(b_)
        out[f"SMA {a}/{b_}"] = np.where(np.isfinite(s) & np.isfinite(l), np.sign(s - l), 0.0)
    e = p.ema(100)
    out["Price vs EMA100"] = np.where(np.isfinite(e), np.sign(p.C - e), 0.0)
    for k in (14, 30, 90):
        r = p.C / np.vstack([np.full((k * p.bpd, p.C.shape[1]), np.nan), p.C[:-k * p.bpd]]) - 1
        out[f"TSMOM {k}d"] = np.where(np.isfinite(r), np.sign(r), 0.0)
    return out


def with_stop(p: Panel, sig: np.ndarray, k: float, chandelier: bool) -> np.ndarray:
    """ATR trailing stop (k x ATR below the highest close since entry) or a chandelier (22-day extreme -/+ 3 ATR). After a stop-out the coin stays flat until the signal changes."""
    b = p.bpd
    hh = DF(p.H).rolling(22 * b, min_periods=1).max().to_numpy()
    ll = DF(p.L).rolling(22 * b, min_periods=1).min().to_numpy()
    n = p.C.shape[1]
    cur, stop, blocked = np.zeros(n), np.full(n, np.nan), np.zeros(n)
    pos = np.zeros_like(p.C)
    for t in range(len(p.C)):
        c, s, a = p.C[t], sig[t], p.atr[t]
        blocked = np.where(s != blocked, 0.0, blocked)
        flip = (cur != 0) & (s != cur)
        cur, stop = np.where(flip, 0.0, cur), np.where(flip, np.nan, stop)
        with np.errstate(invalid="ignore"):
            hit = ((cur > 0) & (c < stop)) | ((cur < 0) & (c > stop))
            blocked = np.where(hit, cur, blocked)
            cur = np.where(hit, 0.0, cur)
            if chandelier:
                cand = np.where(cur > 0, hh[t] - k * a, ll[t] + k * a)
            else:
                cand = c - cur * k * a
            stop = np.where(cur > 0, np.fmax(stop, cand), np.where(cur < 0, np.fmin(stop, cand), stop))
            enter = (cur == 0) & (s != 0) & (blocked != s) & np.isfinite(a)
            init = (hh[t] - k * a) if chandelier else (c - s * k * a)
            init = np.where(s > 0, init, (ll[t] + k * a) if chandelier else init)
        cur = np.where(enter, s, cur)
        stop = np.where(enter, init, stop)
        pos[t] = cur
    return pos


def weights(p: Panel, pos: np.ndarray, mode: str, target: float) -> np.ndarray:
    pos = np.where(p.elig, pos, 0.0)
    if mode in ("LO", "LO+BTC regime"):
        pos = np.clip(pos, 0, None)
    if mode == "LO+BTC regime":
        pos = pos * p.btc_up[:, None]
    prev = np.vstack([np.zeros((1, pos.shape[1])), pos[:-1]])
    entry = (pos != 0) & (pos != prev)
    size = np.minimum(target / np.maximum(p.vol, 0.05), 3.0) / np.maximum(p.elig.sum(1, keepdims=True), 1)
    sz = DF(np.where(entry, size, np.nan)).ffill().to_numpy()
    return np.where(pos != 0, pos * np.nan_to_num(sz), 0.0)


def events(p: Panel, tl: np.ndarray, ts: np.ndarray, exit_kind: str, param: float, maxhold: int, mid=None, sl_long=None, sl_short=None, K_slots: int = 10) -> np.ndarray:
    """Event strategy: enter at the close of the trigger bar, exit by 'time' (param bars), 'mid' (close back through the middle band), 'atr' (trailing param x ATR), or a stop level from the setup."""
    n = p.C.shape[1]
    cur, held, stop = np.zeros(n), np.zeros(n), np.full(n, np.nan)
    pos = np.zeros_like(p.C)
    for t in range(len(p.C)):
        c, a = p.C[t], p.atr[t]
        with np.errstate(invalid="ignore"):
            held = np.where(cur != 0, held + 1, 0.0)
            ex = (held >= maxhold)
            if exit_kind == "time":
                ex |= held >= param
            elif exit_kind == "mid":
                ex |= ((cur > 0) & (c < mid[t])) | ((cur < 0) & (c > mid[t]))
            elif exit_kind == "atr":
                ex |= ((cur > 0) & (c < stop)) | ((cur < 0) & (c > stop))
                cand = c - cur * param * a
                stop = np.where(cur > 0, np.fmax(stop, cand), np.where(cur < 0, np.fmin(stop, cand), stop))
            if sl_long is not None:
                ex |= ((cur > 0) & (c < stop)) | ((cur < 0) & (c > stop))
        cur = np.where(ex, 0.0, cur)
        held = np.where(ex, 0.0, held)
        ent = (cur == 0) & p.elig[t]
        nl, ns = ent & tl[t], ent & ts[t] & ~tl[t]
        cur = np.where(nl, 1.0, np.where(ns, -1.0, cur))
        if exit_kind == "atr":
            stop = np.where(nl, c - param * a, np.where(ns, c + param * a, stop))
        if sl_long is not None:
            stop = np.where(nl, sl_long[t], np.where(ns, sl_short[t], stop))
        held = np.where(nl | ns, 0.0, held)
        pos[t] = cur
    cnt = np.maximum(np.abs(pos).sum(1, keepdims=True), K_slots)
    return pos / cnt


def run_trend(tf: str, univ: str, store: dict, rows: list) -> None:
    p = Panel(tf, univ)
    sigs = signals(p)
    for nm, pos in sigs.items():
        for mode in ("LS", "LO", "LO+BTC regime"):
            for tgt in (0.10, 0.20):
                add(p, store, rows, "trend", f"{nm} | {mode} | vol {int(tgt * 100)}%", dict(signal=nm, mode=mode, vol_target=tgt, tf=tf, universe=univ), weights(p, pos, mode, tgt))
    for nm in ("Donchian 20/10", "Donchian 50/25", "Donchian 100/50", "SMA 20/100", "Price vs EMA100"):
        for kind, k in (("ATR trail", 2), ("ATR trail", 3), ("ATR trail", 4), ("Chandelier", 3)):
            for mode in ("LS", "LO"):
                pos = with_stop(p, sigs[nm], k, kind == "Chandelier")
                add(p, store, rows, "trend_stops", f"{nm} + {kind} {k}xATR | {mode} | vol 15%", dict(signal=nm, stop=f"{kind} {k}", mode=mode, vol_target=0.15, tf=tf, universe=univ), weights(p, pos, mode, 0.15))


def run_vol(tf: str, store: dict, rows: list) -> None:
    p = Panel(tf, "ALL")
    b = p.bpd
    ma = DF(p.C).rolling(20 * b, min_periods=20 * b).mean().to_numpy()
    sd = DF(p.C).rolling(20 * b, min_periods=20 * b).std().to_numpy()
    up, dn = ma + 2 * sd, ma - 2 * sd
    width = 4 * sd / ma
    for q in (0.10, 0.20):
        thr = DF(width).rolling(180 * b, min_periods=90 * b).quantile(q).shift(1).to_numpy()
        with np.errstate(invalid="ignore"):
            sq = width <= thr
        armed = DF(sq.astype(float)).rolling(5, min_periods=1).max().to_numpy() > 0
        tl, ts = armed & (p.C > up), armed & (p.C < dn)
        for ek, par, nm in (("mid", 0, "exit at the middle band"), ("atr", 3, "trail 3 ATR")):
            w = events(p, tl, ts, ek, par, 20 * b, mid=ma)
            add(p, store, rows, "squeeze", f"BB squeeze bottom {int(q * 100)}% then break | {nm}", dict(width_pct=q, exit=nm, tf=tf, universe="ALL"), w)
    pc = np.vstack([np.full((1, p.C.shape[1]), np.nan), p.C[:-1]])
    pa = np.vstack([np.full((1, p.C.shape[1]), np.nan), p.atr[:-1]])
    for k in (1, 2):
        with np.errstate(invalid="ignore"):
            tl, ts = p.C > pc + k * pa, p.C < pc - k * pa
        for hold in (5, 10):
            add(p, store, rows, "atr_expansion", f"ATR expansion {k}x | hold {hold}d", dict(k_atr=k, hold_days=hold, tf=tf, universe="ALL"), events(p, tl, ts, "time", hold * b, hold * b))
    rng = p.H - p.L
    rmin = DF(rng).rolling(7, min_periods=7).min().to_numpy()
    with np.errstate(invalid="ignore"):
        nr7 = rng <= rmin
        inside = (p.H < np.vstack([np.full((1, p.C.shape[1]), np.nan), p.H[:-1]])) & (p.L > np.vstack([np.full((1, p.C.shape[1]), np.nan), p.L[:-1]]))
    for nm, setup in (("NR7", nr7), ("Inside bar", inside)):
        sH = np.vstack([np.full((1, p.C.shape[1]), np.nan), np.where(setup, p.H, np.nan)[:-1]])
        sL = np.vstack([np.full((1, p.C.shape[1]), np.nan), np.where(setup, p.L, np.nan)[:-1]])
        with np.errstate(invalid="ignore"):
            tl, ts = p.C > sH, p.C < sL
        for hold in (5, 10):
            add(p, store, rows, "nr7_inside", f"{nm} then break next bar | hold {hold}d, stop at setup bar's other side", dict(setup=nm, hold_days=hold, tf=tf, universe="ALL"),
                events(p, tl, ts, "time", hold * b, hold * b, sl_long=sL, sl_short=sH))


def add(p: Panel, store: dict, rows: list, family: str, name: str, params: dict, W: np.ndarray) -> None:
    res = K.run(W, p.C, p.F, p.bpd)
    ev = K.evaluate(res, p.cuts, p.bpd)
    rows.append({"family": family, "name": name, "params": params, "eval": ev, "turn_per_day": round(float(res["turn"].mean()), 3), "key": len(rows)})
    store[len(rows) - 1] = (res, p)
