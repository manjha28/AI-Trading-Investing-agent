"""The owner's BTWUSD example (25 Nov 2026), written as a rule and tested on Delta's own coins. Hypothetical research; decision support, not advice.

What BTWUSD did (Delta 1h candles, UTC): a slide from about 1.10 to a 0.334 low on 17 Nov 03:00; a +50% bounce that failed on a 7x-volume red
candle (17 Nov 09:00); then on 18 Nov 01:00-05:00 FIVE hours in a row at 10-23x normal volume while price held flat at 0.44-0.47, right on the
heaviest-traded level (about 0.455). Volume dried up, price broke above that block's high (0.498) at 18 Nov 15:00, and ran to about 0.90 by 25 Nov.
Heavy volume that can't push price down = buyers absorbing the sellers ("accumulation"), then the markup.

RULE (1-hour bars, all known at a bar's close):
1. A real decline first: the lowest low of the last 3 days is >= D% below the highest high of the 14 days before it (D = 30 or 40).
2. Absorption block: within the last 12 hours, at least 3 bars with volume >= A x the median of the prior 3 days (A = 5 or 10), and the block's
   closes stay within a 10% band (heavy volume, price going nowhere), with the block's low above the 3-day low.
3. Entry: the first 1h close ABOVE the block's highest high, within 48 h after the block. Buy at the next hour's open.
4. Stop: the block's lowest low. Trail: TRAIL = "3atr_4h" (best close - 3 x ATR(14) of 4-hour bars) or "1d_low" (the lowest low of the last
   24 hours). Time stop H days (7 or 14). One position per coin; 2 days off after an exit.
COSTS: Delta taker 0.05% + 18% GST per fill, 20 bps slippage per fill, Delta's FUNDING series while long.
DATA: Delta's 15m candles (resampled to 1h) and hourly funding for the 205 crypto USD perpetuals listed today, up to 2 years back.
SPLITS by signal time: train 50%, validation 25%, holdout 25%; choose on validation (>= 20 trades: these are rarer), holdout once."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .delta_pump_intraday import FEE_BPS, OUT, SLIP_BPS, funding_series, stats
from .round4_d2 import boot_ci
from .run import REPORTS


def hourly(sym: str) -> pd.DataFrame | None:
    f = OUT / f"{sym}_15m.pkl"
    if not f.exists():
        return None
    d = pd.read_pickle(f)
    if len(d) < 96 * 20:
        return None
    d = d.astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
    g = d.time // 3600 * 3600
    h = d.groupby(g).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")).reset_index()
    return h


def run_symbol(sym: str, D: float, A: float, trail: str, H: int) -> list[dict]:
    b = hourly(sym)
    if b is None:
        return []
    t, o, h, lo, c, v = (b[k].to_numpy() for k in ("time", "open", "high", "low", "close", "volume"))
    n = len(b)
    med = pd.Series(v).rolling(72, min_periods=24).median().shift(1).to_numpy()
    low3 = pd.Series(lo).rolling(72, min_periods=24).min().to_numpy()
    high14 = pd.Series(h).rolling(336, min_periods=72).max().shift(72).to_numpy()
    b4 = b.assign(g=b.time // 14400 * 14400).groupby("g").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    pc = b4.close.shift(1).fillna(b4.close)
    atr4 = pd.Series(np.maximum(b4.high - b4.low, np.maximum(abs(b4.high - pc), abs(b4.low - pc)))).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    atr_t = b4.index.to_numpy() + 14400
    fund = funding_series(sym)
    f_t, f_v = fund.index.to_numpy(), fund.to_numpy()
    out, free_from, pending = [], -1, None          # pending = (block high, block low, block end index)
    for i in range(96, n - 1):
        if t[i] < free_from:
            continue
        if not (np.isfinite(low3[i]) and np.isfinite(high14[i]) and high14[i] > 0):
            continue
        # 1-2: look for a fresh absorption block ending at i
        if np.isfinite(med[i]) and med[i] > 0 and low3[i] <= high14[i] * (1 - D / 100):
            w = slice(i - 11, i + 1)
            heavy = v[w] >= A * med[i]
            if heavy.sum() >= 3:
                idx = np.flatnonzero(heavy) + i - 11
                closes = c[idx[0]:i + 1]
                blk_hi, blk_lo = float(np.max(h[idx[0]:i + 1])), float(np.min(lo[idx[0]:i + 1]))
                if closes.max() / closes.min() - 1 <= 0.10 and blk_lo > low3[i] * 1.0001:
                    pending = (blk_hi, blk_lo, i)
        # 3: breakout close above the block within 48 h
        if pending is None:
            continue
        blk_hi, blk_lo, end = pending
        if i - end > 48:
            pending = None
            continue
        if i == end or c[i] <= blk_hi:
            continue
        e = i + 1
        entry, stop = o[e], blk_lo
        pending = None
        if entry <= stop:
            continue
        best, exit_px, exit_i, why = entry, None, None, "time"
        last = min(n - 1, e + H * 24)
        for k in range(e, last + 1):
            if lo[k] <= stop:
                exit_px, exit_i, why = (min(o[k], stop) if k > e else stop), k, "stop"
                break
            best = max(best, c[k])
            if trail == "1d_low":
                stop = max(stop, float(np.min(lo[max(0, k - 23):k + 1])))     # the last 24 hours, entry hours included or not (fixed 25 Nov: it was a 1-hour stop)
            else:
                j = int(np.searchsorted(atr_t, t[k] + 3600, "right")) - 1
                if j >= 0:
                    stop = max(stop, best - 3 * atr4[j])
            exit_px, exit_i = c[k], k
        a, z = int(t[e]), int(t[exit_i]) + 3600
        m = (f_t >= a) & (f_t < z)
        funding = float(f_v[m].sum() / 8) if m.any() else 0.0
        gross = (exit_px / entry - 1) * 1e4
        out.append({"symbol": sym, "t": int(t[i]), "net_bps": gross - 2 * (FEE_BPS + SLIP_BPS) - funding * 1e4, "gross_bps": gross, "funding_bps": funding * 1e4,
                    "days": (z - a) / 86400, "why": why, "entry": entry, "max_run_pct": (float(np.max(h[e:exit_i + 1])) / entry - 1) * 100})
        free_from = z + 2 * 86400
    return out


def main() -> dict:
    uni = json.loads((OUT / "universe.json").read_text())
    grid = [(D, A, tr, H) for D in (30, 40) for A in (5, 10) for tr in ("3atr_4h", "1d_low") for H in (7, 14)]
    data = {}
    for D, A, tr, H in grid:
        rows = [r for s in uni for r in run_symbol(s, D, A, tr, H)]
        data[f"decline>={D}%|vol>={A}x|trail={tr}|max={H}d"] = pd.DataFrame(rows).sort_values("t") if rows else pd.DataFrame(columns=["t", "net_bps", "symbol"])
    btw = {n: d[d.symbol == "BTWUSD"][["t", "entry", "net_bps", "days", "why", "max_run_pct"]].to_dict("records") for n, d in data.items() if len(d)}
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in data.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"variants_added": len(grid), "cuts": [pd.Timestamp(int(c1), unit="s").isoformat(), pd.Timestamp(int(c2), unit="s").isoformat()], "splits": {}, "btw_example": btw}
    for n, d in data.items():
        res["splits"][n] = {"train": stats(d[d.t < c1].net_bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].net_bps.to_numpy())}
    ok = [n for n in data if res["splits"][n]["validation"].get("trades", 0) >= 20]
    chosen = max(ok, key=lambda n: res["splits"][n]["validation"]["mean_net_pct"]) if ok else None
    if chosen:
        hd = data[chosen][data[chosen].t >= c2]
        r = np.sort(hd.net_bps.to_numpy())
        lo_, hi_ = boot_ci(hd.net_bps.to_numpy())
        res["chosen"] = chosen
        res["holdout"] = {**stats(hd.net_bps.to_numpy()), "boot90_pct": [lo_ / 100, hi_ / 100], "mean_without_best_pct": float(r[:-1].mean() / 100) if len(r) > 1 else None,
                          "mean_without_top3_pct": float(r[:-3].mean() / 100) if len(r) > 3 else None, "exits": hd.why.value_counts().to_dict(), "median_days": float(hd.days.median()),
                          "median_funding_bps": float(hd.funding_bps.median()), "top": hd.sort_values("net_bps").tail(5)[["symbol", "net_bps", "days", "max_run_pct"]].to_dict("records")}
    (REPORTS / "delta_absorption.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    for nme, s in o["splits"].items():
        print(f"{nme:45s} train {s['train'].get('mean_net_pct', 0):6.1f}% ({s['train']['trades']})  val {s['validation'].get('mean_net_pct', 0):6.1f}% ({s['validation']['trades']}) pf {s['validation'].get('pf') or 0:.2f}")
    print("CHOSEN", o.get("chosen")); print(json.dumps(o.get("holdout"), default=str)); print("BTW", json.dumps(o["btw_example"], default=str)[:1500])
