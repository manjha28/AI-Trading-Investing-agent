"""Part C of the 2026-11-26 research: VWAP deviation bands (the video's "[Smith] VWAP Deviation" idea). Hypothetical; decision support, not investment advice.
The script's source was not supplied, so this is the generic version the brief names: session VWAP of the typical price (high+low+close)/3 weighted by volume, reset 00:00 UTC,
bands at 1, 2, 3 volume-weighted standard deviations around it (sigma = sqrt(sum(v*tp^2)/sum(v) - vwap^2)). Nothing is traded in the first 2 hours of a session (bands too narrow).
FADE (k = 2 or 3): a bar closes outside the k-sigma band; the next bar closes back inside -> enter at that close (next minute open, taker) toward VWAP.
  Stop: 'next' = the (k+1)-sigma band, or 'half' = k-sigma band + 0.5 sigma. Target: VWAP or the 1-sigma band on the same side (skipped if the entry is already past it).
TREND PULLBACK: a bar closes on the trend side of VWAP (above for a long); a limit rests AT that bar's VWAP for the next bar (maker fill if touched). Stop = 1 sigma on the other
  side of VWAP; target = the +1 sigma or +2 sigma band (mirror for shorts). Without the Part A filter the trend side is simply the side of VWAP price is on.
Timeframes 1m / 5m / 15m, entries all hours or 08:00-20:00 UTC only. Fees are usually bigger than a 1m move: see the report. Exits are managed on 1-minute bars, 48 candles max."""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

from . import trend_core as C
from .round4_d2 import boot_ci
from .run import REPORTS
from .trend_5ema import SYMS, START, frame, summarize, sym
from .trend_core import FEE, MAKER, MAXC, SLIP, bps_of, exit_px, first_touch, allow

TFS = {"1m": 60, "5m": 300, "15m": 900}


def session(name: str, secs: int) -> dict:
    S = sym(name)
    b = S.bars(secs)
    tp = (b["h"] + b["l"] + b["c"]) / 3
    day = b["t"] // 86400
    df = pd.DataFrame({"d": day, "pv": tp * b["v"], "v": b["v"], "p2": tp * tp * b["v"]})
    g = df.groupby("d")
    cv, cpv, cp2 = g.v.cumsum().to_numpy(), g.pv.cumsum().to_numpy(), g.p2.cumsum().to_numpy()
    vw = np.where(cv > 0, cpv / np.maximum(cv, 1e-12), np.nan)
    sd = np.sqrt(np.maximum(np.where(cv > 0, cp2 / np.maximum(cv, 1e-12) - vw ** 2, np.nan), 0))
    ok = (b["t"] - day * 86400 >= 7200) & (sd > 0) & np.isfinite(vw)
    return {"b": b, "vw": vw, "sd": sd, "ok": ok, "day": day}


def finish(S, secs, d, fk, entry, stop, target, fee_in, name, tag, out) -> int | None:
    R = -d * (stop - entry)
    if R <= 0 or d * (target - entry) <= 0:
        return None
    end = int(np.searchsorted(S.t1, S.t1[fk] + MAXC * secs))
    j, why = first_touch(S, d, fk, end, stop, target)
    px = exit_px(S, d, fk, j, why, stop, target)
    out.append((int(S.t1[fk]), bps_of(d, entry, px, fee_in), d, why, name, R / entry * 1e4))
    return int(S.t1[j])


def run_fade(name: str, secs: int, k: int, stop_kind: str, tgt_kind: str, fname: str, hours: bool) -> list[tuple]:
    S, X = sym(name), session(name, secs)
    b, vw, sd, ok, day = X["b"], X["vw"], X["sd"], X["ok"], X["day"]
    c = b["c"]
    up, dn = vw + k * sd, vw - k * sd
    outside_up, outside_dn = c > up, c < dn
    trig_s = np.r_[False, outside_up[:-1]] & (c < up) & ok & np.r_[False, day[1:] == day[:-1]]
    trig_l = np.r_[False, outside_dn[:-1]] & (c > dn) & ok & np.r_[False, day[1:] == day[:-1]]
    idx = np.flatnonzero(trig_s | trig_l)
    if len(idx) == 0:
        return []
    d = np.where(trig_s[idx], -1, 1)
    tc = b["t"][idx] + secs
    kk = np.searchsorted(S.t1, tc)
    good = (kk < len(S.t1) - 1) & (tc >= START)
    fl = S.flags(tc.astype(np.int64))
    good &= allow(fname, d, fl)
    if hours:
        hr = (tc % 86400) // 3600
        good &= (hr >= 8) & (hr < 20)
    out, busy = [], -1
    for n in np.flatnonzero(good):
        if tc[n] <= busy:
            continue
        i, dd, fk = idx[n], int(d[n]), int(kk[n])
        if S.t1[fk] - tc[n] >= 120:
            continue
        s = sd[i]
        entry = S.o1[fk] * (1 + dd * SLIP / 1e4)
        sgn = -dd                                              # the band side the price was on: +1 above VWAP
        stop = vw[i] + sgn * ((k + 1) * s if stop_kind == "next" else (k + 0.5) * s)
        target = vw[i] if tgt_kind == "vwap" else vw[i] + sgn * s
        r = finish(S, secs, dd, fk, entry, stop, target, FEE, name, "fade", out)
        if r is not None:
            busy = r
    return out


def run_pull(name: str, secs: int, tgt_sigma: int, fname: str, hours: bool) -> list[tuple]:
    S, X = sym(name), session(name, secs)
    b, vw, sd, ok, day = X["b"], X["vw"], X["sd"], X["ok"], X["day"]
    c, l, h = b["c"], b["l"], b["h"]
    n1 = len(c) - 1
    nxt_same = day[1:] == day[:-1]
    long_t = np.zeros(len(c), bool)
    short_t = np.zeros(len(c), bool)
    long_t[:-1] = (c[:-1] > vw[:-1]) & (l[1:] <= vw[:-1]) & nxt_same & ok[:-1]
    short_t[:-1] = (c[:-1] < vw[:-1]) & (h[1:] >= vw[:-1]) & nxt_same & ok[:-1]
    idx = np.flatnonzero(long_t | short_t)
    if len(idx) == 0:
        return []
    d = np.where(long_t[idx], 1, -1)
    tc = b["t"][idx] + secs                                        # the limit goes in at this bar's close
    fl = S.flags(tc.astype(np.int64))
    good = allow(fname, d, fl) & (tc >= START)
    if hours:
        hr = (tc % 86400) // 3600
        good &= (hr >= 8) & (hr < 20)
    out, busy = [], -1
    for n in np.flatnonzero(good):
        if tc[n] <= busy:
            continue
        i, dd = int(idx[n]), int(d[n])
        s0, e0 = int(b["s"][i + 1]), int(b["e"][i + 1])
        lim = vw[i]
        hit = (S.l1[s0:e0] <= lim) if dd > 0 else (S.h1[s0:e0] >= lim)
        if not hit.any():
            continue
        fk = s0 + int(hit.argmax())
        entry = min(lim, S.o1[fk]) if dd > 0 else max(lim, S.o1[fk])
        stop = vw[i] - dd * sd[i]
        target = vw[i] + dd * tgt_sigma * sd[i]
        r = finish(S, secs, dd, fk, entry, stop, target, MAKER, name, "pull", out)
        if r is not None:
            busy = r
    return out


def pooled(fn, *a) -> pd.DataFrame:
    rows = []
    for s in SYMS:
        rows += fn(s, *a)
    return frame(rows)


def main(best_filter: str) -> None:
    res: dict = {"versions": {}, "best_filter": best_filter}
    calls: dict = {}
    t0 = time.time()
    for tf, secs in TFS.items():
        for f in ("none", best_filter):
            for hrs in (False, True):
                tag = f"{tf}|filter={'best' if f != 'none' else 'none'}|hours={'08-20' if hrs else 'all'}"
                for k in (2, 3):
                    for sk in ("next", "half"):
                        for tk in ("vwap", "1s"):
                            key = f"{tag}|fade k={k} stop={sk} target={tk}"
                            calls[key] = (run_fade, (secs, k, sk, tk, f, hrs))
                            res["versions"][key] = summarize(pooled(run_fade, *calls[key][1]))
                for ts in (1, 2):
                    key = f"{tag}|pullback target={ts}sigma"
                    calls[key] = (run_pull, (secs, ts, f, hrs))
                    res["versions"][key] = summarize(pooled(run_pull, *calls[key][1]))
        print("part C", tf, round(time.time() - t0), flush=True)
    for part in ("fade", "pullback"):
        ok = {k: v for k, v in res["versions"].items() if part in k and v["validation"].get("trades", 0) >= 300}
        k = max(ok, key=lambda x: ok[x]["validation"]["net_bps"])
        df = pooled(calls[k][0], *calls[k][1])
        fin = df[df.t >= C.SPLIT2].bps.to_numpy()
        lo, hi = boot_ci(fin)
        res.setdefault("best", {})[part] = {"key": k, "summary": res["versions"][k], "final_boot90": [lo, hi], "final_bps": [round(float(x), 2) for x in fin]}
    res["n_variants_this_run_C"] = len(res["versions"])
    (REPORTS / "trend_vwap.json").write_text(json.dumps(res, indent=1, default=str))
    print("done", len(res["versions"]), flush=True)


if __name__ == "__main__":
    import sys
    main(sys.argv[1])
