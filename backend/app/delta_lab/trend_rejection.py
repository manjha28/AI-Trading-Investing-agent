"""Owner's discretionary rule (25 Nov 2026), written down before any result was seen, then backtested. Hypothetical research only.

1. TREND (1-hour bars): pivots with 2 bars each side (confirmed 2 hours later, so no look-ahead). Down-trend = the last confirmed pivot high is
   lower than the one before AND the last confirmed pivot low is lower than the one before. Up-trend is the mirror. Otherwise: no trade.
2. ENTRY (5m or 15m bars): in a down-trend, a bar that reaches the nearest resistance above the price (the last 1h pivot high, or the UTC
   session VWAP if it is nearer and above) to within 0.1 x ATR(14) of that bar's timeframe, then closes back below the level and below its
   own open = rejection. Short at the next 1-minute open after that close. Longs mirror this in an up-trend (support = last 1h pivot low / VWAP).
3. STOP: the rejection bar's high + 0.1 x ATR. R = stop - entry.
4. EXIT: no fixed target. At +1R the stop moves to break-even; after that it trails to each newly confirmed lower pivot high (2 bars each
   side) on the trail timeframe. Exit when a 1-minute high touches the stop. Hard time stop: 3 days.
5. SKIP when R is under 2 x the round-trip cost (28 bps), or (variant) when the room to the last 1h pivot low is under 2R.
DATA: Binance USD-M 1-minute klines, BTC and ETH, Jan 2023 to Nov 2026 (the SAME data used for today's 5 EMA exit test: the holdout here is
not fresh, which is stated in the report). COSTS: Delta taker 0.05% + 18% GST per fill, 1 bp slippage on entry, 3 bps on stop exits.
SPLITS by entry time, pooled: train 50%, validation 25%, holdout 25%; choose on validation, touch the holdout once."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from functools import lru_cache

from .five_ema_exits import FEE, load as _load, resample
from .round4_d2 import boot_ci
from .run import REPORTS

SYMBOLS = ("BTCUSD", "ETHUSD")
load = lru_cache(maxsize=4)(_load)
K = 2
MAX_MIN = 3 * 1440
COST_RT = 2 * FEE + 4.0


def pivots(h: np.ndarray, lo: np.ndarray, k: int = K) -> tuple[np.ndarray, np.ndarray]:
    """Boolean arrays: bar i is a pivot high / low (needs k bars after it, so it is only KNOWN at bar i+k)."""
    n = len(h)
    ph, pl = np.zeros(n, bool), np.zeros(n, bool)
    for i in range(k, n - k):
        ph[i] = h[i] == h[i - k:i + k + 1].max()
        pl[i] = lo[i] == lo[i - k:i + k + 1].min()
    return ph, pl


def atr(b: pd.DataFrame) -> np.ndarray:
    pc = np.r_[b.close.iloc[0], b.close.to_numpy()[:-1]]
    tr = np.maximum(b.high - b.low, np.maximum(abs(b.high - pc), abs(b.low - pc)))
    return pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()


def run_symbol(sym: str, secs: int, trail_secs: int, room_filter: bool) -> list[dict]:
    m = load(sym)
    t1, o1, h1, l1, c1, v1 = (m[k].to_numpy() for k in ("time", "open", "high", "low", "close", "volume"))
    idx1 = {int(x): i for i, x in enumerate(t1)}
    day = t1 // 86400
    pv = pd.Series(((h1 + l1 + c1) / 3) * v1).groupby(day).cumsum().to_numpy()
    vv = pd.Series(v1).groupby(day).cumsum().to_numpy()
    vwap = pv / np.maximum(vv, 1e-12)
    hb = resample(m, 3600)
    hph, hpl = pivots(hb.high.to_numpy(), hb.low.to_numpy())
    known_at = hb.t.to_numpy() + (K + 1) * 3600                  # a 1h pivot is known once K more hours have closed
    ph_t, ph_v = known_at[hph], hb.high.to_numpy()[hph]
    pl_t, pl_v = known_at[hpl], hb.low.to_numpy()[hpl]
    b = resample(m, secs)
    a = atr(b)
    tb = resample(m, trail_secs)
    tph, tpl = pivots(tb.high.to_numpy(), tb.low.to_numpy())
    tr_known = tb.t.to_numpy() + (K + 1) * trail_secs
    tr_hi_t, tr_hi_v, tr_lo_t, tr_lo_v = tr_known[tph], tb.high.to_numpy()[tph], tr_known[tpl], tb.low.to_numpy()[tpl]
    out, busy = [], -1
    bt, bo, bh, bl, bc = (b[k].to_numpy() for k in ("t", "open", "high", "low", "close"))
    for i in range(30, len(b)):
        close_t = int(bt[i]) + secs
        if close_t <= busy or bt[i] < 1672531200:
            continue
        nh, nl = np.searchsorted(ph_t, close_t, "right"), np.searchsorted(pl_t, close_t, "right")
        if nh < 2 or nl < 2:
            continue
        down = ph_v[nh - 1] < ph_v[nh - 2] and pl_v[nl - 1] < pl_v[nl - 2]
        up = ph_v[nh - 1] > ph_v[nh - 2] and pl_v[nl - 1] > pl_v[nl - 2]
        k = idx1.get(close_t)
        if k is None or k + 1 >= len(t1) or not (down or up):
            continue
        vw = vwap[k - 1]
        tol = 0.1 * a[i]
        side = None
        if down:
            levels = [x for x in (ph_v[nh - 1], vw) if x > bc[i]]
            lvl = min(levels) if levels else None
            if lvl is not None and bh[i] >= lvl - tol and bc[i] < lvl and bc[i] < bo[i]:
                side, stop = "short", bh[i] + tol
        if up and side is None:
            levels = [x for x in (pl_v[nl - 1], vw) if x < bc[i]]
            lvl = max(levels) if levels else None
            if lvl is not None and bl[i] <= lvl + tol and bc[i] > lvl and bc[i] > bo[i]:
                side, stop = "long", bl[i] - tol
        if side is None:
            continue
        sgn = -1 if side == "short" else 1
        entry = o1[k] * (1 - sgn * 1 / 1e4)
        R = (stop - entry) * -sgn
        if R <= 0 or R / entry * 1e4 < 2 * COST_RT:
            continue
        if room_filter:
            room = entry - pl_v[nl - 1] if side == "short" else ph_v[nh - 1] - entry
            if room < 2 * R:
                continue
        # manage on 1-minute bars
        st, be = stop, False
        end = min(len(t1), k + MAX_MIN)
        hi_ptr = np.searchsorted(tr_hi_t if side == "short" else tr_lo_t, close_t, "right")
        piv_t, piv_v = (tr_hi_t, tr_hi_v) if side == "short" else (tr_lo_t, tr_lo_v)
        exit_px, exit_t, reason = c1[end - 1], int(t1[end - 1]), "time"
        for x in range(k, end):
            if (side == "short" and h1[x] >= st) or (side == "long" and l1[x] <= st):
                exit_px, exit_t, reason = (max(o1[x], st) if side == "short" else min(o1[x], st)), int(t1[x]), "stop"
                break
            fav = (entry - l1[x]) if side == "short" else (h1[x] - entry)
            if not be and fav >= R:
                st, be = entry, True
            while hi_ptr < len(piv_t) and piv_t[hi_ptr] <= t1[x]:
                p = piv_v[hi_ptr]
                if be and ((side == "short" and p < st) or (side == "long" and p > st)):
                    st = p
                hi_ptr += 1
        fill = exit_px * (1 - sgn * (3 if reason != "time" else 1) / 1e4)
        bps = (fill - entry) / entry * 1e4 * sgn - 2 * FEE
        out.append({"symbol": sym, "side": side, "t": close_t, "bps": bps, "R_bps": R / entry * 1e4, "r_mult": (fill - entry) * sgn / R, "reason": reason,
                    "hold_min": (exit_t - close_t) / 60})
        busy = exit_t
    return out


def stats(b: np.ndarray) -> dict:
    if len(b) == 0:
        return {"trades": 0}
    w, l = b[b > 0], b[b <= 0]
    eq = np.r_[0, np.cumsum(b)]
    return {"trades": int(len(b)), "net_bps": float(b.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((b > 0).mean()),
            "max_dd_bps": float((np.maximum.accumulate(eq) - eq).max()), "total_bps": float(b.sum())}


def main() -> dict:
    variants = {}
    for tf, secs in (("5m", 300), ("15m", 900)):
        for trail_name, tsecs in (("trail_5m", 300), ("trail_15m", 900)):
            if tsecs < secs:
                continue
            for room in (False, True):
                name = f"{tf}|{trail_name}|room2R={'on' if room else 'off'}"
                rows = []
                for s in SYMBOLS:
                    rows += run_symbol(s, secs, tsecs, room)
                variants[name] = pd.DataFrame(rows).sort_values("t") if rows else pd.DataFrame(columns=["t", "bps", "side", "symbol", "r_mult", "hold_min"])
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in variants.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"variants_added": len(variants), "cuts": [int(c1), int(c2)], "splits": {}, "holdout": {}}
    for name, d in variants.items():
        res["splits"][name] = {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy())}
    chosen = max(variants, key=lambda n: res["splits"][n]["validation"].get("net_bps", -1e9) if res["splits"][n]["validation"].get("trades", 0) >= 30 else -1e9)
    h = variants[chosen][variants[chosen].t >= c2]
    b = h.bps.to_numpy()
    res["chosen"] = chosen
    res["holdout"] = {**stats(b), "boot90": list(boot_ci(b)), "short": stats(h[h.side == "short"].bps.to_numpy()), "long": stats(h[h.side == "long"].bps.to_numpy()),
                      "per_symbol": {s: stats(h[h.symbol == s].bps.to_numpy()) for s in SYMBOLS}, "median_R_bps": float(h.R_bps.median()) if len(h) else None,
                      "median_hold_min": float(h.hold_min.median()) if len(h) else None, "avg_r_multiple": float(h.r_mult.mean()) if len(h) else None}
    (REPORTS / "trend_rejection.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    r = main()
    for n, s in r["splits"].items():
        print(f"{n:32s} train {s['train'].get('net_bps', 0):7.1f} ({s['train']['trades']})  val {s['validation'].get('net_bps', 0):7.1f} ({s['validation']['trades']}) pf {s['validation'].get('pf')}")
    print("CHOSEN", r["chosen"]); print(json.dumps(r["holdout"], indent=1, default=str)[:1500])
