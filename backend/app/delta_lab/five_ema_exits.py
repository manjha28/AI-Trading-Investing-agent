"""5 EMA alert-candle (sell side, as the live test and scanner run it): which EXIT works? Same entries for every variant; only the exit changes.

ENTRY (as live): an alert candle closes entirely above EMA(5) (its low > EMA); a later candle breaks below the alert low; the order goes in at the
CLOSE of that breakout candle (live acts on completed candles), filled at the next 1-minute open plus slippage. Stop = alert candle high.
R = stop - entry. Trades where the entry is already at or above the stop are skipped. One position per symbol at a time.
EXITS are managed on 1-minute bars inside the trade (stop checked before target in the same minute). Everything also closes after 48 bars.
DATA: Binance USD-M 1-minute klines (checksum-verified downloads, already on disk), Jan 2023 to Nov 2026, resampled to 5m and 15m. Delta trades
the same contracts in USD; Binance is used for its long, gap-free history.
COSTS (Delta): taker 0.05% + 18% GST on entry and on every exit (the live bracket's take-profit and stop are market orders), slippage 1 bp on
entry and target exits, 3 bps on stop, trailing and time exits. Funding not included (holds are hours).
SPLITS by entry time, pooled per timeframe: train 50%, validation 25%, holdout 25%. The variant is chosen on validation only; the holdout is
touched once, for the chosen variant and the current 3R rule. Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .run import REPORTS
from .round4_d2 import boot_ci

ROOT = Path(__file__).resolve().parents[3] / "data"
SOURCES = {"BTCUSD": ROOT / "binance" / "BTCUSDT_last_1m.pkl", "ETHUSD": ROOT / "binance" / "ETHUSDT_last_1m.pkl",
           "SOLUSD": ROOT / "binance_r5" / "SOLUSDT_last_1m.pkl.gz", "LISTAUSD": ROOT / "binance_r5" / "LISTAUSDT_last_1m.pkl.gz"}
START = 1672531200                       # 2023-01-01 UTC
FEE = 0.0005 * 1.18 * 1e4               # bps per fill
SLIP_GOOD, SLIP_BAD = 1.0, 3.0
MAX_BARS = 48
VARIANTS = ["fixed_1R", "fixed_1.5R", "fixed_2R", "fixed_3R", "level_swing", "level_prev_day", "level_vwap", "level_round", "level_nearest",
            "partial_1R_trail", "partial_level_trail", "trail_atr_1.5", "trail_prior_candle", "time_6", "time_12"]


def load(sym: str) -> pd.DataFrame:
    d = pd.read_pickle(SOURCES[sym])
    d = d[d["time"] >= START - 86400 * 3].drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return d[["time", "open", "high", "low", "close", "volume"]].astype({"time": "int64"})


def resample(m: pd.DataFrame, secs: int) -> pd.DataFrame:
    g = (m["time"] // secs) * secs
    b = m.groupby(g).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), n=("time", "size")).reset_index().rename(columns={"time": "t"})
    return b[b["n"] == secs // 60].reset_index(drop=True)           # complete bars only


def round_step(price: float) -> float:
    return 5 * 10 ** (math.floor(math.log10(price)) - 2)


def trades_for(sym: str, secs: int) -> list[dict]:
    m = load(sym)
    t1, o1, h1, l1, c1, v1 = (m[k].to_numpy() for k in ("time", "open", "high", "low", "close", "volume"))
    bars = resample(m, secs)
    bt, bo, bh, bl, bc = (bars[k].to_numpy() for k in ("t", "open", "high", "low", "close"))
    ema = pd.Series(bc).ewm(span=5, adjust=False).mean().to_numpy()
    tr = np.maximum(bh - bl, np.maximum(abs(bh - np.r_[bc[0], bc[:-1]]), abs(bl - np.r_[bc[0], bc[:-1]])))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    day = t1 // 86400
    pv = pd.Series(((h1 + l1 + c1) / 3) * v1).groupby(day).cumsum().to_numpy()
    vv = pd.Series(v1).groupby(day).cumsum().to_numpy()
    vwap = np.where(vv > 0, pv / np.maximum(vv, 1e-12), np.nan)
    daily_low = pd.Series(l1).groupby(day).min()
    idx1 = {int(x): i for i, x in enumerate(t1)}
    out, alert, busy_until = [], None, -1
    for i in range(20, len(bars)):
        if alert is not None and bl[i] < alert[0] and bt[i] >= START and bt[i] > busy_until:
            a_low, a_high = alert
            close_t = int(bt[i]) + secs                              # entry decided at the breakout bar's close
            k = idx1.get(close_t)
            if k is not None and k + 1 < len(t1):
                entry = o1[k] * (1 - SLIP_GOOD / 1e4)
                stop = a_high
                if entry < stop:
                    pdl = daily_low.get(int(close_t // 86400) - 1, np.nan)
                    levels = {"swing": float(np.min(bl[i - 20:i])), "prev_day": float(pdl), "vwap": float(vwap[k - 1]), "round": math.floor(entry / round_step(entry)) * round_step(entry)}
                    end = min(len(t1), k + MAX_BARS * secs // 60)
                    tr_ = simulate(entry, stop, levels, k, end, o1, h1, l1, c1, t1, bt, bh, bc, atr, i, secs)
                    for name, res in tr_.items():
                        res.update(symbol=sym, variant=name, t=close_t)
                        out.append(res)
                    busy_until = max(r["exit_t"] for r in tr_.values() if r["variant_base"])  # one position at a time, by the 3R rule's own holding
            alert = None
        if bl[i] > ema[i]:
            alert = (bl[i], bh[i])
    return out


def _exit(entry: float, px: float, kind: str) -> float:
    slip = SLIP_GOOD if kind == "target" else SLIP_BAD
    fill = px * (1 + slip / 1e4)                                   # a short buys back higher
    return (entry - fill) / entry * 1e4 - 2 * FEE


def simulate(entry, stop, levels, k, end, o1, h1, l1, c1, t1, bt, bh, bc, atr, i, secs) -> dict[str, dict]:
    R = stop - entry
    H, L = h1[k:end], l1[k:end]
    res: dict[str, dict] = {}

    def fixed(target):
        hit_s = np.flatnonzero(H >= stop)
        hit_t = np.flatnonzero(L <= target) if target is not None else np.array([], dtype=int)
        s = hit_s[0] if len(hit_s) else 10**9
        tt = hit_t[0] if len(hit_t) else 10**9
        if s == 10**9 and tt == 10**9:
            return _exit(entry, c1[end - 1], "time"), end - 1, "time"
        if s <= tt:
            return _exit(entry, max(o1[k + s], stop), "stop"), k + s, "stop"
        return _exit(entry, min(o1[k + tt], target), "target"), k + tt, "target"

    for name, mult in (("fixed_1R", 1), ("fixed_1.5R", 1.5), ("fixed_2R", 2), ("fixed_3R", 3)):
        bps, j, why = fixed(entry - mult * R)
        res[name] = {"bps": bps, "exit_t": int(t1[j]), "why": why, "R_bps": R / entry * 1e4, "variant_base": name == "fixed_3R"}
    cands = {n: v for n, v in levels.items() if np.isfinite(v) and entry - v >= R}
    for n in ("swing", "prev_day", "vwap", "round"):
        tgt = cands.get(n, entry - 3 * R)                              # no usable level at least 1R away: fall back to 3R
        bps, j, why = fixed(tgt)
        res[f"level_{n}"] = {"bps": bps, "exit_t": int(t1[j]), "why": why, "variant_base": False}
    nearest = max(cands.values()) if cands else entry - 3 * R
    bps, j, why = fixed(nearest)
    res["level_nearest"] = {"bps": bps, "exit_t": int(t1[j]), "why": why, "variant_base": False}
    # bar-by-bar managed variants (stop updated only after a completed bar of the strategy's timeframe)
    per = secs // 60

    def managed(first_target, trail):
        st, rem, pnl = stop, 1.0, 0.0
        for b in range(0, (end - k) // per + 1):
            lo_i, hi_i = k + b * per, min(end, k + (b + 1) * per)
            for x in range(lo_i, hi_i):
                if h1[x] >= st:
                    return pnl + rem * _exit(entry, max(o1[x], st), "stop"), int(t1[x])
                if rem == 1.0 and first_target is not None and l1[x] <= first_target:
                    pnl += 0.5 * _exit(entry, min(o1[x], first_target), "target")
                    rem = 0.5
            if hi_i >= end:
                break
            bar_close, bar_high = c1[hi_i - 1], float(np.max(h1[lo_i:hi_i]))
            st = min(st, bar_close + 1.5 * atr[min(i + 1 + b, len(atr) - 1)]) if trail == "atr" else min(st, bar_high)
        return pnl + rem * _exit(entry, c1[end - 1], "time"), int(t1[end - 1])

    for name, ft, trail in (("partial_1R_trail", entry - R, "prior"), ("partial_level_trail", nearest, "prior"), ("trail_atr_1.5", None, "atr"), ("trail_prior_candle", None, "prior")):
        bps, et = managed(ft, trail)
        res[name] = {"bps": bps, "exit_t": et, "why": "managed", "variant_base": False}
    for name, nb in (("time_6", 6), ("time_12", 12)):
        e2 = min(end, k + nb * per)
        hs = np.flatnonzero(h1[k:e2] >= stop)
        if len(hs):
            res[name] = {"bps": _exit(entry, max(o1[k + hs[0]], stop), "stop"), "exit_t": int(t1[k + hs[0]]), "why": "stop", "variant_base": False}
        else:
            res[name] = {"bps": _exit(entry, c1[e2 - 1], "time"), "exit_t": int(t1[e2 - 1]), "why": "time", "variant_base": False}
    return res


def stats(b: np.ndarray) -> dict:
    if len(b) == 0:
        return {"trades": 0}
    w, l = b[b > 0], b[b <= 0]
    eq = np.cumsum(b)
    return {"trades": int(len(b)), "net_bps": float(b.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((b > 0).mean()),
            "max_dd_bps": float((np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]).max())}


def main() -> dict:
    result = {"variants": VARIANTS, "variants_added": len(VARIANTS) * 2, "frames": {}}
    for secs, tf in ((300, "5m"), (900, "15m")):
        rows = []
        for sym in SOURCES:
            rows += trades_for(sym, secs)
        df = pd.DataFrame(rows)
        base = df[df.variant == "fixed_3R"].sort_values("t")
        ts = base["t"].to_numpy()
        c1, c2 = ts[int(len(ts) * 0.5)], ts[int(len(ts) * 0.75)]
        split = lambda d, lo, hi: d[(d.t >= lo) & (d.t < hi)]          # noqa: E731
        f = {"train": {}, "validation": {}}
        for v in VARIANTS:
            d = df[df.variant == v]
            f["train"][v] = stats(split(d, 0, c1).bps.to_numpy())
            f["validation"][v] = stats(split(d, c1, c2).bps.to_numpy())
        chosen = max(VARIANTS, key=lambda v: f["validation"][v].get("net_bps", -1e9))
        hold = {}
        for v in dict.fromkeys([chosen, "fixed_3R"]):
            b = split(df[df.variant == v], c2, 10**12).bps.to_numpy()
            lo, hi = boot_ci(b) if len(b) > 1 else (None, None)
            hold[v] = {**stats(b), "boot90": [lo, hi], "per_symbol": {s: stats(split(df[(df.variant == v) & (df.symbol == s)], c2, 10**12).bps.to_numpy()) for s in SOURCES}}
        r_bps = base["R_bps"].describe(percentiles=[0.1, 0.5, 0.9]).to_dict()
        result["frames"][tf] = {"cuts": [int(c1), int(c2)], "chosen_on_validation": chosen, "splits": f, "holdout": hold, "R_bps": r_bps, "entries": int(len(base))}
    (REPORTS / "five_ema_exits.json").write_text(json.dumps(result, indent=1, default=str))
    return result


if __name__ == "__main__":
    r = main()
    for tf, x in r["frames"].items():
        print(tf, "entries", x["entries"], "chosen", x["chosen_on_validation"])
        for v in r["variants"]:
            print(f"  {v:22s} train {x['splits']['train'][v].get('net_bps', 0):7.1f} ({x['splits']['train'][v]['trades']})  val {x['splits']['validation'][v].get('net_bps', 0):7.1f} pf {x['splits']['validation'][v].get('pf')}")
        for v, h in x["holdout"].items():
            print("  HOLDOUT", v, {k: h[k] for k in ("trades", "net_bps", "pf", "win_rate", "max_dd_bps", "boot90")})
