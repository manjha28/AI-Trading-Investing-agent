"""5 EMA alert candle (sell side): enter on a PULLBACK instead of at the breakout candle's close (owner's question, 25 Nov 2026).
Hypothetical research; decision support, not investment advice.

SAME signal as five_ema_exits: an alert candle entirely above EMA(5); a later candle breaks below the alert low (the breakout candle).
BASELINE ("close"): short at the breakout candle's close (next 1-minute open), as live does today.
PULLBACK: after the breakout candle closes, rest a SELL LIMIT at a pullback level and wait up to W candles for price to come back up to it:
  LEVEL = "alert_low" (the broken alert low, retested from below) | "half" (halfway between the alert low and the alert high) | "ema" (the EMA(5)
  value at the breakout candle's close). If price trades above the alert high first, or W candles pass, the trade is skipped (no fill).
  A limit fill is a maker fill: Delta maker fee 0.02% + GST, no slippage.
STOP = alert candle high (unchanged). TARGET: "3R" (3 x the distance from the fill to the stop) or "fixed" (the baseline's own target price:
  breakout close - 3 x (stop - breakout close)), so the pullback simply buys a better price for the same trade.
Exits on 1-minute bars; stop before target in the same minute; everything closes after 48 candles. Taker exits: 0.05% + GST, 3 bps slippage on
stops, 1 bp on targets. One position at a time per symbol.
DATA: Binance USD-M 1-minute data, BTC/ETH/SOL/LISTA, 2023-01 to 2026-11, 5m and 15m: the SAME years as the 5 EMA exit study (the holdout is
not fresh). SPLITS by time: train 50%, validation 25%, holdout 25%."""
from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
import pandas as pd

from .five_ema_exits import FEE, SOURCES, START, load as _load, resample
from .round4_d2 import boot_ci
from .run import REPORTS

load = lru_cache(maxsize=4)(_load)
MAKER = 0.0002 * 1.18 * 1e4


def run(sym: str, secs: int, level: str, W: int, target: str) -> list[dict]:
    m = load(sym)
    t1, o1, h1, l1, c1 = (m[k].to_numpy() for k in ("time", "open", "high", "low", "close"))
    idx1 = {int(x): i for i, x in enumerate(t1)}
    b = resample(m, secs)
    bt, bh, bl, bc = (b[k].to_numpy() for k in ("t", "high", "low", "close"))
    ema = pd.Series(bc).ewm(span=5, adjust=False).mean().to_numpy()
    per = secs // 60
    out, alert, busy = [], None, -1
    for i in range(20, len(b)):
        if alert is not None and bl[i] < alert[0] and bt[i] >= START and bt[i] > busy:
            a_low, a_high = alert
            k = idx1.get(int(bt[i]) + secs)
            alert = None
            if k is None or k + 1 >= len(t1):
                continue
            stop, ref = a_high, bc[i]
            if ref >= stop:
                continue
            fixed_target = ref - 3 * (stop - ref)
            if level == "close":
                entry, fill_k, fee_in = o1[k] * (1 - 1e-4), k, FEE
            else:
                lvl = {"alert_low": a_low, "half": (a_low + a_high) / 2, "ema": ema[i]}[level]
                if lvl <= ref or lvl >= stop:
                    continue
                fill_k = None
                for x in range(k, min(len(t1), k + W * per)):
                    if h1[x] >= stop:
                        break
                    if h1[x] >= lvl:
                        fill_k = x
                        break
                if fill_k is None:
                    continue
                entry, fee_in = max(lvl, o1[fill_k]), MAKER
            R = stop - entry
            if R <= 0:
                continue
            tgt = entry - 3 * R if target == "3R" else fixed_target
            if tgt >= entry:
                continue
            end = min(len(t1), fill_k + 48 * per)
            px, why, j = c1[end - 1], "time", end - 1
            for x in range(fill_k, end):
                if h1[x] >= stop:
                    px, why, j = max(o1[x], stop) if x > fill_k else stop, "stop", x
                    break
                if l1[x] <= tgt:
                    px, why, j = min(o1[x], tgt) if x > fill_k else tgt, "target", x
                    break
            slip = 1 if why == "target" else 3
            fill = px * (1 + slip / 1e4)
            bps = (entry - fill) / entry * 1e4 - fee_in - FEE            # FEE = one taker fill incl. GST
            out.append({"symbol": sym, "t": int(bt[i]), "bps": bps, "why": why, "R_bps": R / entry * 1e4})
            busy = int(t1[j])
            continue
        if bl[i] > ema[i]:
            alert = (bl[i], bh[i])
    return out


def stats(b: np.ndarray) -> dict:
    if len(b) == 0:
        return {"trades": 0}
    w, l = b[b > 0], b[b <= 0]
    eq = np.r_[0, np.cumsum(b)]
    return {"trades": int(len(b)), "net_bps": float(b.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((b > 0).mean()),
            "max_dd_bps": float((np.maximum.accumulate(eq) - eq).max())}


def main() -> dict:
    res = {"frames": {}}
    variants = [("close", 0, "3R")] + [(lv, W, tg) for lv in ("alert_low", "half", "ema") for W in (3, 6) for tg in ("3R", "fixed")]
    res["variants_added"] = 2 * (len(variants) - 1)
    for tf, secs in (("5m", 300), ("15m", 900)):
        data = {}
        for lv, W, tg in variants:
            rows = [r for s in SOURCES for r in run(s, secs, lv, W, tg)]
            data[f"{lv}|wait={W}|target={tg}"] = pd.DataFrame(rows).sort_values("t")
        ts = np.sort(data["close|wait=0|target=3R"].t.to_numpy())
        c1, c2 = ts[int(len(ts) * 0.5)], ts[int(len(ts) * 0.75)]
        sp = {n: {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy())} for n, d in data.items()}
        chosen = max(sp, key=lambda n: sp[n]["validation"].get("net_bps", -1e9) if sp[n]["validation"].get("trades", 0) >= 100 else -1e9)
        hold = {}
        for n in dict.fromkeys([chosen, "close|wait=0|target=3R"]):
            h = data[n][data[n].t >= c2]
            hold[n] = {**stats(h.bps.to_numpy()), "boot90": list(boot_ci(h.bps.to_numpy())), "per_symbol": {s: stats(h[h.symbol == s].bps.to_numpy()) for s in SOURCES}}
        res["frames"][tf] = {"splits": sp, "chosen": chosen, "holdout": hold}
    (REPORTS / "five_ema_pullback.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    for tf, x in o["frames"].items():
        print("==", tf, "chosen", x["chosen"])
        for n, s in x["splits"].items():
            print(f"  {n:30s} train {s['train'].get('net_bps', 0):6.1f} ({s['train']['trades']})  val {s['validation'].get('net_bps', 0):6.1f} ({s['validation']['trades']}) pf {s['validation'].get('pf') or 0:.2f}")
        for n, h in x["holdout"].items():
            print("  HOLDOUT", n, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in h.items() if k != "per_symbol"})
