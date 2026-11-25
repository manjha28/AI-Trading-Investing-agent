"""(1) The AlgoTest "5Ema Indicator" Pine script, replicated exactly, and (2) the owner's two-order idea. Hypothetical research only.

(1) ALGOTEST rules, read from the script the owner shared (25 Nov 2026):
  - signal on bar i: low[i-1] > EMA5[i-1] (the PREVIOUS candle is entirely above the EMA) AND low[i] < low[i-1] (this candle breaks its low),
    and no trade is open. The alert candle is only ever the immediately previous candle.
  - sell at the alert low (sellat = low[i-1]): an intrabar stop entry, filled the first minute price trades below it (at that minute's open if
    it gapped below), plus 1 bp slippage, taker fee.
  - stop = high[i-1]; target = low[i-1] - RR x (high[i-1] - low[i-1]), RR = 3 (the script's default).
  - the script forgets everything at each new day; here a trade is simply managed to its stop or target (48-candle cap), one at a time.
  OURS differs in three ways: our alert candle stays valid until a later candle breaks it (not only the next candle); the LIVE app enters at
  the breakout candle's CLOSE (the script enters intrabar at the alert low); our target is 3 x (stop - entry) from the fill.
(2) TWO ORDERS, same stop (the alert high), same target (the live rule's target from the breakout close): order A sells at the breakout close
  (taker, as live does now); order B rests a sell limit halfway up the alert candle for up to 6 candles (maker). Result per signal = A + B
  (B is 0 when it never fills), in bps of ONE contract's notional, so a two-fill signal counts double the exposure.
DATA/COSTS/SPLITS as five_ema_pullback (Binance 1-minute data for BTC/ETH/SOL/LISTA, 2023 to Nov 2026, same years as today's other 5 EMA tests)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .five_ema_exits import FEE, SOURCES, START, resample
from .five_ema_pullback import load, run as pb_run, stats
from .round4_d2 import boot_ci
from .run import REPORTS


def algotest(sym: str, secs: int, rr: float = 3.0) -> list[dict]:
    m = load(sym)
    t1, o1, h1, l1, c1 = (m[k].to_numpy() for k in ("time", "open", "high", "low", "close"))
    idx1 = {int(x): i for i, x in enumerate(t1)}
    b = resample(m, secs)
    bt, bh, bl, bc = (b[k].to_numpy() for k in ("t", "high", "low", "close"))
    ema = pd.Series(bc).ewm(span=5, adjust=False).mean().to_numpy()
    per = secs // 60
    out, busy = [], -1
    for i in range(20, len(b)):
        if bt[i] < START or bt[i] <= busy or not (bl[i - 1] > ema[i - 1] and bl[i] < bl[i - 1]):
            continue
        k0 = idx1.get(int(bt[i]))
        if k0 is None:
            continue
        lvl, stop = bl[i - 1], bh[i - 1]
        target = lvl - rr * (stop - lvl)
        fk = next((x for x in range(k0, min(len(t1), k0 + per)) if l1[x] < lvl), None)
        if fk is None:
            continue
        entry = min(o1[fk], lvl) * (1 - 1e-4)
        if entry >= stop:
            continue
        end = min(len(t1), fk + 48 * per)
        px, why, j = c1[end - 1], "time", end - 1
        for x in range(fk, end):
            if h1[x] >= stop:
                px, why, j = (max(o1[x], stop) if x > fk else stop), "stop", x
                break
            if l1[x] <= target:
                px, why, j = (min(o1[x], target) if x > fk else target), "target", x
                break
        fill = px * (1 + (1 if why == "target" else 3) / 1e4)
        out.append({"symbol": sym, "t": int(bt[i]), "bps": (entry - fill) / entry * 1e4 - 2 * FEE, "why": why})
        busy = int(t1[j])
    return out


def split_stats(d: pd.DataFrame, c1: int, c2: int) -> dict:
    h = d[d.t >= c2].bps.to_numpy()
    lo, hi = boot_ci(h)
    return {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy()), "holdout": {**stats(h), "boot90": [lo, hi]},
            "holdout_per_symbol": {s: stats(d[(d.t >= c2) & (d.symbol == s)].bps.to_numpy()) for s in SOURCES}}


def main() -> dict:
    res = {"frames": {}, "variants_added": 4, "note": "fixed rules, nothing chosen: train/validation/holdout are reported side by side"}
    for tf, secs in (("5m", 300), ("15m", 900)):
        algo = pd.DataFrame([r for s in SOURCES for r in algotest(s, secs)]).sort_values("t")
        a = pd.DataFrame([r for s in SOURCES for r in pb_run(s, secs, "close", 0, "fixed")])
        bdf = pd.DataFrame([r for s in SOURCES for r in pb_run(s, secs, "half", 6, "fixed")])
        two = a[["symbol", "t", "bps"]].merge(bdf[["symbol", "t", "bps"]], on=["symbol", "t"], how="left", suffixes=("_close", "_pullback"))
        two["filled_both"] = two.bps_pullback.notna()
        two["bps"] = two.bps_close + two.bps_pullback.fillna(0)
        two = two.sort_values("t")
        ts = np.sort(a.t.to_numpy())
        c1, c2 = int(ts[int(len(ts) * 0.5)]), int(ts[int(len(ts) * 0.75)])
        res["frames"][tf] = {"algotest_exact": split_stats(algo, c1, c2), "close_only_live_rule": split_stats(a.sort_values("t"), c1, c2),
                             "two_orders_close_plus_pullback": {**split_stats(two, c1, c2), "share_both_filled": float(two.filled_both.mean()),
                                                                "pullback_leg_alone_holdout_bps": stats(two[(two.t >= c2) & two.filled_both].bps_pullback.to_numpy())}}
    (REPORTS / "five_ema_algotest.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    for tf, x in o["frames"].items():
        for n, s in x.items():
            print(tf, f"{n:32s}", {k: (s[k].get("trades"), round(s[k].get("net_bps", 0), 1), round(s[k].get("pf") or 0, 2), round(s[k].get("win_rate", 0), 2)) for k in ("train", "validation", "holdout")},
                  "both" if "share_both_filled" in s else "", round(s.get("share_both_filled", 0), 2))
