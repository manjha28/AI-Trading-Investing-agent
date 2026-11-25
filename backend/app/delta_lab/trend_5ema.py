"""Part A (trend filter) and Part B (5 EMA entries and exits) of the 2026-11-26 research. Hypothetical; decision support, not investment advice.
Definitions are written in trend_core.py (data, costs, split, bias B1-B4, reversal R1-R3) and in the report. Both sides are traded (a long is the mirror).

SIGNAL (as live): an alert candle whose low is above EMA(5) (short) / whose high is below EMA(5) (long); a later candle that breaks the alert candle's low (high) is the
breakout candle; a later alert replaces an untriggered one. STOP = the alert candle's opposite extreme. One position per coin at a time.
ENTRIES: E1 = at the breakout candle's close (next minute's open, taker); E2 = AlgoTest exactly (stop order at the alert low, shorts only, target alert low - 3 x alert range,
the alert candle must be the one just before, trades closed at 00:00 UTC); E3 = one resting limit at the broken alert extreme / halfway up the alert candle / the EMA(5),
kept for 3 or 6 candles, cancelled if the stop or the live rule's 3R target trades first (maker fill).
EXITS: T2 / T3 = 2R / 3R from the actual fill; T3ref = 3R measured from the breakout close (what live does), so a slipped or pulled-back fill changes the real R:R;
SR = nearest 1h swing support (resistance for a long) at least 2R away, skip if none; TR = break-even at +1R then trail behind the latest confirmed swing high (low).
CHECKS (switchable): the fill check (skip if reward:risk from the real fill is below 1:2) is always on except in the 'asis' row; minstop = skip when the stop is under
3 x the round-trip cost of that trade (about 41 bps for taker both ways)."""
from __future__ import annotations

import json
import sys
import time
from functools import lru_cache

import numpy as np
import pandas as pd

from . import trend_core as C
from .dsr_trades import dsr as dsr_fn
from .round4_d2 import boot_ci
from .run import REPORTS
from .trend_core import FEE, MAKER, MAXC, SLIP, Sym, allow, bps_of, exit_px, first_touch, stats, trail_exit

SYMS = ["BTCUSD", "ETHUSD", "SOLUSD", "LISTAUSD", "EVAAUSD", "AIOTUSD", "BLESSUSD", "AKEUSD", "ARIAUSD", "XRPUSD"]
TFS = {"5m": 300, "15m": 900}
START = 1672531200


@lru_cache(maxsize=12)
def sym(name: str) -> Sym:
    return Sym(name)


def ema5(c: np.ndarray) -> np.ndarray:
    return pd.Series(c).ewm(span=5, adjust=False).mean().to_numpy()


@lru_cache(maxsize=40)
def triggers(name: str, secs: int) -> dict:
    S = sym(name)
    b = S.bars(secs)
    t, h, l, c = b["t"], b["h"], b["l"], b["c"]
    e = ema5(c)
    sell = buy = None
    rows = []
    for i in range(20, len(t)):
        if sell is not None and l[i] < sell[0]:
            rows.append((i, -1, sell[0], sell[1]))
            sell = None
        if buy is not None and h[i] > buy[1]:
            rows.append((i, 1, buy[1], buy[0]))
            buy = None
        if l[i] > e[i]:
            sell = (l[i], h[i])
        if h[i] < e[i]:
            buy = (l[i], h[i])
    a = np.array(rows, dtype=float).reshape(-1, 4)
    i = a[:, 0].astype(int)
    tc = t[i] + secs
    k = np.searchsorted(S.t1, tc)
    good = (tc >= START) & (k < len(S.t1) - 1)
    good[good] &= (S.t1[k[good]] - tc[good]) < 120
    i, k, tc, a = i[good], k[good], tc[good], a[good]
    T = {"i": i, "d": a[:, 1].astype(int), "lvl": a[:, 2], "stop": a[:, 3], "ref": c[i], "ema": e[i], "tc": tc, "k": k,
         "alo": np.where(a[:, 1] < 0, a[:, 2], a[:, 3]), "ahi": np.where(a[:, 1] < 0, a[:, 3], a[:, 2])}
    fl = S.flags(tc.astype(np.int64))
    T["fl"] = fl
    T["allow"] = {f: allow(f, T["d"], fl) for f in ["none"] + C.FILTER_NAMES}
    return T


def run_e13(name: str, secs: int, entry: str, W: int, exit_kind: str, fname: str, minstop: bool, fillcheck: bool = True) -> list[tuple]:
    S, b, T = sym(name), sym(name).bars(secs), triggers(name, secs)
    mask = T["allow"][fname]
    out, busy = [], -1
    for n in np.flatnonzero(mask):
        tc = int(T["tc"][n])
        if tc <= busy:
            continue
        d, stop, ref, k = int(T["d"][n]), float(T["stop"][n]), float(T["ref"][n]), int(T["k"][n])
        Rref = -d * (stop - ref)
        if Rref <= 0:
            continue
        tgt_ref = ref + d * 3 * Rref
        if entry == "close":
            fk, entry_px, fee_in, taker = k, S.o1[k] * (1 + d * SLIP / 1e4), FEE, True
        else:
            lvl = {"broken": T["lvl"][n], "half": (T["alo"][n] + T["ahi"][n]) / 2, "ema": T["ema"][n]}[entry]
            if (d < 0 and not (ref < lvl < stop)) or (d > 0 and not (stop < lvl < ref)):
                continue
            ew = min(len(S.t1) - 1, int(np.searchsorted(S.t1, tc + W * secs)))
            if d < 0:
                reach, sb, tb = S.h1[k:ew] >= lvl, S.h1[k:ew] >= stop, S.l1[k:ew] <= tgt_ref
            else:
                reach, sb, tb = S.l1[k:ew] <= lvl, S.l1[k:ew] <= stop, S.h1[k:ew] >= tgt_ref
            fi = int(reach.argmax()) if reach.any() else 10**9
            ci = min(int(sb.argmax()) if sb.any() else 10**9, int(tb.argmax()) if tb.any() else 10**9)
            if fi == 10**9 or ci <= fi:                              # never filled, or the stop/target traded first: order cancelled
                busy = int(S.t1[min(k + min(ci, ew - k - 1), len(S.t1) - 1)]) if ci < 10**9 else int(S.t1[ew])
                continue
            fk = k + fi
            entry_px, fee_in, taker = (max(lvl, S.o1[fk]) if d < 0 else min(lvl, S.o1[fk])), MAKER, False
        R = -d * (stop - entry_px)
        if R <= 0:
            continue
        rt = fee_in + FEE + (SLIP if taker else 0) + SLIP
        if minstop and R / entry_px * 1e4 < 3 * rt:
            continue
        end = int(np.searchsorted(S.t1, S.t1[fk] + MAXC * secs))
        target = None
        if exit_kind == "T2":
            target = entry_px + d * 2 * R
        elif exit_kind == "T3":
            target = entry_px + d * 3 * R
        elif exit_kind == "T3ref":
            target = tgt_ref
            if fillcheck and d * (target - entry_px) / R < 2:
                continue
        elif exit_kind == "SR":
            lv = S.pivot_levels(int(T["fl"]["jh"][n]), d)
            ok = lv[lv <= entry_px - 2 * R] if d < 0 else lv[lv >= entry_px + 2 * R]
            if len(ok) == 0:
                continue
            target = float(ok.max() if d < 0 else ok.min())
        if exit_kind == "TR":
            j, why, px, _ = trail_exit(S, b, secs, d, fk, entry_px, stop)
            px = px if why != "time" else S.c1[j]
        else:
            j, why = first_touch(S, d, fk, end, stop, target)
            px = exit_px(S, d, fk, j, why, stop, target)
        out.append((int(S.t1[fk]), bps_of(d, entry_px, px, fee_in), d, why, name, R / entry_px * 1e4))
        busy = int(S.t1[j])
    return out


def run_e2(name: str, secs: int, fname: str, minstop: bool) -> list[tuple]:
    """AlgoTest exact, shorts only."""
    S, b = sym(name), sym(name).bars(secs)
    t, h, l, c = b["t"], b["h"], b["l"], b["c"]
    e = ema5(c)
    ii = np.array([i for i in range(20, len(t)) if t[i] >= START and l[i - 1] > e[i - 1] and l[i] < l[i - 1] and t[i - 1] // 86400 == t[i] // 86400])
    if len(ii) == 0:
        return []
    fl = S.flags(t[ii].astype(np.int64))
    ok = allow(fname, -np.ones(len(ii), int), fl)
    out, busy = [], -1
    for n in np.flatnonzero(ok):
        i = int(ii[n])
        if t[i] <= busy:
            continue
        lvl, stop = l[i - 1], h[i - 1]
        s0, e0 = b["s"][i], b["e"][i]
        hit = S.l1[s0:e0] < lvl
        if not hit.any():
            continue
        fk = s0 + int(hit.argmax())
        entry_px = min(S.o1[fk], lvl) * (1 - SLIP / 1e4)
        R = stop - entry_px
        if R <= 0:
            continue
        target = lvl - 3 * (stop - lvl)
        if (entry_px - target) / R < 2:
            continue
        if minstop and R / entry_px * 1e4 < 3 * (2 * FEE + 2 * SLIP):
            continue
        end = min(int(np.searchsorted(S.t1, S.t1[fk] + MAXC * secs)), int(np.searchsorted(S.t1, (t[i] // 86400 + 1) * 86400)))
        if end <= fk:
            continue
        j, why = first_touch(S, -1, fk, end, stop, target)
        px = exit_px(S, -1, fk, j, why, stop, target)
        out.append((int(S.t1[fk]), bps_of(-1, entry_px, px, FEE), -1, why, name, R / entry_px * 1e4))
        busy = int(S.t1[j])
    return out


def frame(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["t", "bps", "d", "why", "symbol", "R_bps"])
    return df.sort_values("t").reset_index(drop=True)


def summarize(df: pd.DataFrame, detail: bool = True) -> dict:
    out = {}
    for sg, lo, hi in (("train", 0, C.SPLIT1), ("validation", C.SPLIT1, C.SPLIT2), ("final", C.SPLIT2, 10**12)):
        x = df[(df.t >= lo) & (df.t < hi)]
        o = stats(x.bps.to_numpy())
        if detail:
            o["long"] = stats(x[x.d > 0].bps.to_numpy())
            o["short"] = stats(x[x.d < 0].bps.to_numpy())
        out[sg] = o
    if detail and len(df):
        out["per_symbol"] = {s: {sg: stats(g[(g.t >= lo) & (g.t < hi)].bps.to_numpy()) for sg, lo, hi in (("validation", C.SPLIT1, C.SPLIT2), ("final", C.SPLIT2, 10**12))} for s, g in df.groupby("symbol")}
        q = pd.to_datetime(df.t, unit="s").dt.to_period("Q").astype(str)
        out["per_quarter"] = {k: stats(g.bps.to_numpy()) for k, g in df.groupby(q)}
    return out


def pooled(fn, **kw) -> pd.DataFrame:
    rows = []
    for s in SYMS:
        try:
            rows += fn(s, **kw)
        except FileNotFoundError:
            pass
    return frame(rows)


def pick_best(versions: dict, calls: dict) -> dict:
    """Best version on VALIDATION only (at least 300 trades); then its final-check trades are re-run for the interval and the deflated-Sharpe input."""
    ok = {k: v for k, v in versions.items() if v["validation"].get("trades", 0) >= 300 and "asis" not in k}
    k = max(ok, key=lambda x: ok[x]["validation"]["net_bps"])
    df = pooled(calls[k])
    f = df[df.t >= C.SPLIT2].bps.to_numpy()
    lo, hi = boot_ci(f)
    return {"key": k, "summary": versions[k], "final_boot90": [lo, hi], "final_bps": [round(float(x), 2) for x in f]}


def main() -> None:
    res: dict = {"filters": {}, "versions": {}, "symbols": SYMS, "split": {"train": "< 2025-07-01", "validation": "2025-07-01..2026-03-31", "final": ">= 2026-04-01"}}
    t0 = time.time()
    # ---- Part A: each filter on the live rule (E1, 3R from the breakout close, no extra checks), both timeframes ----
    for tf, secs in TFS.items():
        for f in ["none"] + C.FILTER_NAMES:
            df = pooled(lambda s, **k: run_e13(s, secs, "close", 0, "T3ref", f, False, fillcheck=False))
            res["filters"][f"{tf}|{f}"] = summarize(df)
        print("part A", tf, round(time.time() - t0), flush=True)
    cand = {}
    for f in C.FILTER_NAMES:
        v = [res["filters"][f"{tf}|{f}"]["validation"] for tf in TFS]
        n = sum(x.get("trades", 0) for x in v)
        if n >= 400:
            cand[f] = sum(x["net_bps"] * x["trades"] for x in v if x.get("trades")) / n
    best_f = max(cand, key=cand.get)
    res["best_filter"] = best_f
    res["filter_validation_score"] = cand
    print("best filter", best_f, flush=True)
    # ---- Part B ----
    calls: dict = {}
    grid = [("E1", "close", 0, x) for x in ("T2", "T3", "T3ref", "SR", "TR")]
    grid += [(f"E3|{lv}|w{W}", lv, W, x) for lv in ("broken", "half", "ema") for W in (3, 6) for x in ("T2", "T3", "T3ref", "SR", "TR")]
    for tf, secs in TFS.items():
        for f in ("none", best_f):
            for ms in (False, True):
                for label, ent, W, ex in grid:
                    key = f"{tf}|{label}|{ex}|filter={'best' if f != 'none' else 'none'}|minstop={int(ms)}"
                    calls[key] = (lambda s, secs=secs, ent=ent, W=W, ex=ex, f=f, ms=ms, **k: run_e13(s, secs, ent, W, ex, f, ms))
                    res["versions"][key] = summarize(pooled(calls[key]))
                key = f"{tf}|E2|algotest|filter={'best' if f != 'none' else 'none'}|minstop={int(ms)}"
                calls[key] = (lambda s, secs=secs, f=f, ms=ms, **k: run_e2(s, secs, f, ms))
                res["versions"][key] = summarize(pooled(calls[key]))
            print("part B", tf, f, round(time.time() - t0), flush=True)
        df = pooled(lambda s, **k: run_e13(s, secs, "close", 0, "T3ref", "none", False, fillcheck=False))
        res["versions"][f"{tf}|E1|asis_live_no_checks|filter=none|minstop=0"] = summarize(df)
    res["best"] = pick_best(res["versions"], calls)
    res["n_variants_this_run_AB"] = len(res["filters"]) - 2 + len(res["versions"])
    (REPORTS / "trend_5ema.json").write_text(json.dumps(res, indent=1, default=str))
    print("done", res["n_variants_this_run_AB"], round(time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
