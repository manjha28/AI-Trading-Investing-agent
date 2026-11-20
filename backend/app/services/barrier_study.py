"""Phase 0 base rates: triple-barrier outcomes for EVERY bar as a candidate entry, in BOTH directions, with no signal, filter, regime or volume condition.

Pure price paths. The stop/target/horizon grid is used RAW: no cost floor, no theta_feeR, no s_cap, no tradeability filter inside the measurement.
Costs enter only afterwards as a reporting overlay (``cell_report``).

Rules (identical for stop and target, deliberately conservative)
* entry = the OPEN of the entry bar; the entry bar's own high/low already count (no look-ahead: nothing after the open is used to choose entries)
* stop distance s (fraction of entry price); target distance r*s
* first touch wins; within one bar the STOP is checked before the target; touches are STRICT (low < stop / high > target for a long)
* vertical barrier after H minutes (H 1-minute slots including the entry bar), capped by the session's last stored bar; the timeout exit is the close of the last stored bar
* missing minutes are NEVER filled: a missing slot cannot touch a barrier and cannot be an entry; the exit uses the last stored close at or before the horizon
* entries are only taken on bars that carry volume (a zero-volume bar is a carried-forward print, not a tradable price)

Statistics: ``n`` counts overlapping labels. Confidence statements use NON-OVERLAPPING entries (one entry every H slots per session); each such entry is one unit
whose value is the mean of its long and short outcomes, so the two mirror-image directions are never counted as independent samples."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

SLOTS = 375                      # 09:15 .. 15:29 inclusive, one slot per minute
HMAX = 240
S_GRID = (0.0010, 0.0015, 0.0020, 0.0030, 0.0050, 0.0080, 0.0120)   # fractions of entry price: 0.10% .. 1.20%
R_GRID = (1.0, 1.5, 2.0, 2.6, 3.0)
H_GRID = (15, 30, 45, 60, 120, 240)
N_BUCKETS = 13                   # 30-minute entry buckets over 375 slots
INF = 10**9


@dataclass
class Session:
    day: date
    o: np.ndarray                # length SLOTS, NaN where the minute is missing
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray                # volume, NaN where missing


def _first_touch(mask: np.ndarray) -> np.ndarray:
    any_ = mask.any(axis=1)
    return np.where(any_, mask.argmax(axis=1), INF)


class SessionArrays:
    """Per-session first-touch indices for every distinct distance and horizon-dependent exit data, ready to be concatenated across sessions."""

    def __init__(self, sess: Session, distances: list[float], require_volume: bool = True, horizons: tuple[int, ...] = H_GRID):
        L = SLOTS
        o, h, l, c = sess.o, sess.h, sess.l, sess.c
        valid = ~np.isnan(o) & ~np.isnan(h) & ~np.isnan(l) & ~np.isnan(c)
        if require_volume:
            entry_ok = valid & (np.nan_to_num(sess.v, nan=0.0) > 0)
        else:
            entry_ok = valid
        last = int(np.flatnonzero(valid).max()) if valid.any() else -1
        self.entry_ok = entry_ok
        self.o_abs = o                        # raw opens (NaN where missing), used by the gap-through-fill diagnostic
        self.e = np.arange(L)
        self.E = np.where(valid, o, np.nan)
        self.bucket = (self.e // 30).clip(0, N_BUCKETS - 1)
        hi = np.concatenate([np.where(valid, h, -np.inf), np.full(HMAX, -np.inf)])
        lo = np.concatenate([np.where(valid, l, np.inf), np.full(HMAX, np.inf)])
        Wh = sliding_window_view(hi, HMAX)[:L]
        Wl = sliding_window_view(lo, HMAX)[:L]
        E = np.where(np.isnan(self.E), 0.0, self.E)[:, None]
        self.down: dict[float, np.ndarray] = {}
        self.up: dict[float, np.ndarray] = {}
        for x in distances:
            self.down[x] = _first_touch(Wl < E * (1.0 - x))      # first slot (0 = entry bar) whose low is strictly below E(1-x)
            self.up[x] = _first_touch(Wh > E * (1.0 + x))        # first slot whose high is strictly above E(1+x)
        # last stored close at or before each slot (forward fill used ONLY to find the timeout exit price, never to create a bar)
        idx = np.where(valid, np.arange(L), -1)
        lastvalid = np.maximum.accumulate(idx)
        self.vl: dict[int, np.ndarray] = {}
        self.exit_close: dict[int, np.ndarray] = {}
        self.stride: dict[int, np.ndarray] = {}
        self.sigma_ret: dict[int, np.ndarray] = {}
        for H in horizons:
            vl = np.clip(np.minimum(H, last + 1 - self.e), 0, None)
            exit_idx = np.clip(self.e + vl - 1, 0, L - 1)
            li = lastvalid[exit_idx]
            self.vl[H] = vl
            self.exit_close[H] = np.where(li >= 0, c[np.clip(li, 0, L - 1)], np.nan)
            self.stride[H] = (self.e % H) == 0
            self.sigma_ret[H] = np.where(self.E > 0, self.exit_close[H] / np.where(self.E > 0, self.E, 1.0) - 1.0, np.nan)


def concat_sessions(parts: list[SessionArrays], distances: list[float], horizons: tuple[int, ...] = H_GRID) -> dict:
    cat = lambda f: np.concatenate([f(p) for p in parts])  # noqa: E731
    out = {"entry_ok": cat(lambda p: p.entry_ok), "E": cat(lambda p: p.E), "bucket": cat(lambda p: p.bucket),
           "down": {x: cat(lambda p, x=x: p.down[x]) for x in distances}, "up": {x: cat(lambda p, x=x: p.up[x]) for x in distances}}
    for H in horizons:
        out[("vl", H)] = cat(lambda p, H=H: p.vl[H])
        out[("xc", H)] = cat(lambda p, H=H: p.exit_close[H])
        out[("st", H)] = cat(lambda p, H=H: p.stride[H])
        out[("sg", H)] = cat(lambda p, H=H: p.sigma_ret[H])
    return out


def distinct_distances(s_grid: Iterable[float] = S_GRID, r_grid: Iterable[float] = R_GRID) -> list[float]:
    xs = set()
    for s in s_grid:
        xs.add(round(s, 10))
        for r in r_grid:
            xs.add(round(s * r, 10))
    return sorted(xs)


def label_cell(cat: dict, s: float, r: float, H: int, direction: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(win, loss, timeout, r_timeout) per entry for one direction. Stop is checked before target within a bar; touches must occur inside the valid horizon."""
    xs, xt = round(s, 10), round(s * r, 10)
    if direction > 0:
        ts, tt = cat["down"][xs], cat["up"][xt]
    else:
        ts, tt = cat["up"][xs], cat["down"][xt]
    vl = cat[("vl", H)]
    loss = (ts < vl) & (ts <= tt)
    win = (tt < vl) & (tt < ts)
    to = ~(loss | win)
    E, xc = cat["E"], cat[("xc", H)]
    with np.errstate(invalid="ignore", divide="ignore"):
        r_to = direction * (xc - E) / (s * E)
    return win, loss, to, r_to


def study_cells(cat: dict, s_grid=S_GRID, r_grid=R_GRID, h_grid=H_GRID) -> dict:
    """Accumulate, per (s, r, H): overlapping counts (also per 30-minute bucket) and non-overlapping per-entry unit sums."""
    ok = cat["entry_ok"]
    res: dict = {}
    for s in s_grid:
        for r in r_grid:
            for H in h_grid:
                per_dir = [label_cell(cat, s, r, H, d) for d in (1, -1)]
                n = wins = loss = to = 0
                sum_rto = 0.0
                b_n = np.zeros(N_BUCKETS); b_w = np.zeros(N_BUCKETS); b_l = np.zeros(N_BUCKETS); b_t = np.zeros(N_BUCKETS); b_rto = np.zeros(N_BUCKETS)
                unit_R = np.zeros(len(ok)); unit_W = np.zeros(len(ok))
                for (w, l, t, rto) in per_dir:
                    m = ok
                    n += int(m.sum()); wins += int((w & m).sum()); loss += int((l & m).sum()); to += int((t & m).sum())
                    rt = np.where(t & m, np.nan_to_num(rto), 0.0)
                    sum_rto += float(rt.sum())
                    bk = cat["bucket"]
                    b_n += np.bincount(bk[m], minlength=N_BUCKETS); b_w += np.bincount(bk[w & m], minlength=N_BUCKETS)
                    b_l += np.bincount(bk[l & m], minlength=N_BUCKETS); b_t += np.bincount(bk[t & m], minlength=N_BUCKETS)
                    b_rto += np.bincount(bk[t & m], weights=np.nan_to_num(rto)[t & m], minlength=N_BUCKETS)
                    unit_R += 0.5 * (np.where(w, r, 0.0) + np.where(l, -1.0, 0.0) + np.where(t, np.nan_to_num(rto), 0.0))
                    unit_W += 0.5 * w
                st = ok & cat[("st", H)]
                u = unit_R[st]; uw = unit_W[st]
                res[(s, r, H)] = {"n": n, "wins": wins, "loss": loss, "timeout": to, "sum_r_timeout": sum_rto,
                                  "bucket": {"n": b_n, "w": b_w, "l": b_l, "t": b_t, "rto": b_rto},
                                  "no": {"n": int(st.sum()), "sum_R": float(u.sum()), "sum_R2": float((u ** 2).sum()), "sum_W": float(uw.sum()), "sum_W2": float((uw ** 2).sum())}}
    return res


def p_breakeven(r: float, f: float, p_to: float, r_to: float) -> float:
    """Win probability at which E_R_net = 0, holding the timeout share and its mean R fixed and moving probability mass from stop to target:
    E = p(r+1) - (1-p_to)(1+f) + p_to(r_to - f)  =>  p_be = [(1-p_to)(1+f) - p_to(r_to - f)] / (r+1)."""
    return ((1.0 - p_to) * (1.0 + f) - p_to * (r_to - f)) / (r + 1.0)


def cell_report(acc: dict, s: float, r: float, H: int, cost_rt_frac: float) -> dict:
    """Turn accumulated counts into the report row. ``cost_rt_frac`` is the round-trip cost as a FRACTION of notional from the authoritative charge service."""
    n = acc["n"]
    if n == 0:
        return {"s_pct": s * 100, "r": r, "H": H, "n": 0}
    p_win, p_loss, p_to = acc["wins"] / n, acc["loss"] / n, acc["timeout"] / n
    r_to = acc["sum_r_timeout"] / acc["timeout"] if acc["timeout"] else 0.0
    f = cost_rt_frac / s
    e_net = p_win * (r - f) + p_loss * (-1.0 - f) + p_to * (r_to - f)
    e_gross = p_win * r - p_loss + p_to * r_to
    p_be = p_breakeven(r, f, p_to, r_to)
    no = acc["no"]
    k = no["n"]
    row = {"s_pct": round(s * 100, 4), "r": r, "H": H, "n": n, "n_nonoverlap": k, "p_win": p_win, "p_loss": p_loss, "p_timeout": p_to, "r_timeout_mean": r_to, "p_theory": 1.0 / (r + 1.0),
           "cost_rt_pct": cost_rt_frac * 100, "f": f, "E_R_gross": e_gross, "E_R_net": e_net, "p_breakeven": p_be,
           "required_uplift": (p_be / p_win) if p_win > 0 else math.inf}
    if k > 1:
        mR = no["sum_R"] / k
        vR = max(no["sum_R2"] / k - mR * mR, 0.0) * k / (k - 1)
        mW = no["sum_W"] / k
        vW = max(no["sum_W2"] / k - mW * mW, 0.0) * k / (k - 1)
        se_R, se_W = math.sqrt(vR / k), math.sqrt(vW / k)
        row.update({"p_win_nonoverlap": mW, "p_win_se": se_W, "p_win_ci95": [mW - 1.96 * se_W, mW + 1.96 * se_W], "E_R_net_nonoverlap": mR - f, "E_R_net_se": se_R,
                    "E_R_net_t": ((mR - f) / se_R) if se_R > 0 else None,
                    "required_uplift_if_p_win_at_upper95": (p_be / min(1.0, mW + 1.96 * se_W)) if mW + 1.96 * se_W > 0 else math.inf})
    return row


def bucket_report(acc: dict, s: float, r: float, H: int, cost_rt_frac: float) -> list[dict]:
    b = acc["bucket"]
    f = cost_rt_frac / s
    out = []
    for i in range(N_BUCKETS):
        n = b["n"][i]
        if n == 0:
            continue
        pw, pl, pt = b["w"][i] / n, b["l"][i] / n, b["t"][i] / n
        rto = b["rto"][i] / b["t"][i] if b["t"][i] else 0.0
        out.append({"bucket": f"{9 + (15 + 30 * i) // 60:02d}:{(15 + 30 * i) % 60:02d}", "n": int(n), "p_win": pw, "E_R_net": pw * (r - f) + pl * (-1 - f) + pt * (rto - f)})
    return out


def sigma_seasonality(cat: dict, H: int) -> list[dict]:
    """Empirical std of the H-minute return (open of the entry bar -> last stored close within H) by 30-minute entry bucket."""
    ok = cat["entry_ok"] & (cat[("vl", H)] == H)          # full horizons only, so every bucket measures the same H
    sg = cat[("sg", H)]
    out = []
    for i in range(N_BUCKETS):
        m = ok & (cat["bucket"] == i) & ~np.isnan(sg)
        if m.sum() > 30:
            out.append({"bucket": f"{9 + (15 + 30 * i) // 60:02d}:{(15 + 30 * i) % 60:02d}", "n": int(m.sum()), "sigma_pct": float(np.std(sg[m], ddof=1) * 100)})
    return out
