"""Ride volume pumps on DELTA's own perpetuals, entering on 15m or 30m bars. Hypothetical research; decision support, not investment advice.

Owner's request (25 Nov 2026): look for volume pumps and price jumps on Delta coins, enter on the 15-30 minute timeframe, ride the move.
DATA: Delta India's own public candles (15m, up to 2 years back, as far as each listing goes) and its hourly FUNDING series, for every live
USD perpetual that is a crypto coin (stock, ETF and metal tokens excluded). Only coins listed TODAY (survivorship bias, stated in the report).
Turnover = volume x contract value x close (USD).
UNIVERSE at signal time: "illiquid" = median daily turnover over the prior 14 days below 2 M USD.
ENTRY on bar b (15m, or 30m built from two 15m bars), all known at b's close:
  - volume ignition: b's turnover >= V x the median bar turnover of the prior 3 days (V = 10 or 20)
  - price jump: b closes >= +J% above its open (J = 5 or 10) and in the top third of its range
  - not already extended: b's close < +50% above the lowest low of the prior 3 days
  - buy at the next bar's open. One position per coin; no new entry on that coin for 1 day after an exit.
EXIT: initial stop = b's low. TRAIL = "4h_low" (lowest low of the last 4 hours of bars) or "3atr_1h" (highest close since entry - 3 x the
  1-hour ATR(14)). Time stop H days (3 or 7). Stop exits fill at the stop (or the bar open if it gapped through).
COSTS: Delta taker 0.05% + 18% GST per fill, 20 bps slippage per fill (thin books on a spike), funding from Delta's FUNDING series (longs pay
  a positive rate), counted every hour held (the series is the 8-hour rate quoted hourly, so each hour is 1/8 of it).
SPLITS by signal time, pooled: train 50%, validation 25%, holdout 25%; choose on validation (>= 30 trades), holdout once."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from .round4_d2 import boot_ci
from .run import REPORTS

BASE = "https://api.india.delta.exchange"
OUT = Path(__file__).resolve().parents[3] / "data" / "delta_15m"
NOT_CRYPTO = {"XAUT", "PAXG", "SLVON", "XAU", "XAG"}
FEE_BPS, SLIP_BPS = 5.0 * 1.18, 20.0
YEARS = 2


def universe() -> dict[str, float]:
    with httpx.Client(timeout=30) as c:
        ps = c.get(f"{BASE}/v2/products", params={"contract_types": "perpetual_futures"}).json()["result"]
    out = {}
    for p in ps:
        s = p["symbol"]
        if not s.endswith("USD") or p.get("state") != "live" or s.endswith("BUSD") or s[:-3] in NOT_CRYPTO:
            continue
        if (p.get("quoting_asset") or {}).get("symbol") != "USD":
            continue
        out[s] = float(p["contract_value"])
    return out


def fetch(client: httpx.Client, symbol: str, resolution: str, secs: int) -> pd.DataFrame:
    end = int(time.time())
    start_all = end - YEARS * 365 * 86400
    frames, cur_end = [], end
    while cur_end > start_all:
        cur_start = max(start_all, cur_end - 3900 * secs)
        for attempt in range(4):
            try:
                r = client.get(f"{BASE}/v2/history/candles", params={"symbol": symbol, "resolution": resolution, "start": cur_start, "end": cur_end})
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1)); continue
                rows = r.json().get("result") or []
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(2 * (attempt + 1)); rows = []
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        cur_end = cur_start
        time.sleep(0.15)
    if not frames:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.concat(frames).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


def download() -> dict[str, float]:
    OUT.mkdir(parents=True, exist_ok=True)
    uni = universe()
    with httpx.Client(timeout=30, headers={"User-Agent": "FinByteNexus/1.0 (research)"}) as c:
        for s in uni:
            f, g = OUT / f"{s}_15m.pkl", OUT / f"{s}_funding_1h.pkl"
            if not f.exists():
                fetch(c, s, "15m", 900).to_pickle(f)
            if not g.exists():
                fetch(c, f"FUNDING:{s}", "1h", 3600).to_pickle(g)
    (OUT / "universe.json").write_text(json.dumps(uni))
    return uni


def bars(sym: str, cv: float, tf: int) -> pd.DataFrame | None:
    d = pd.read_pickle(OUT / f"{sym}_15m.pkl")
    if len(d) < 96 * 20:
        return None
    d = d.astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
    if tf == 1800:
        g = d["time"] // 1800 * 1800
        d = d.groupby(g).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum"), n=("time", "size")).reset_index()
        d = d[d.n == 2].drop(columns="n")
    d["turnover"] = d.volume * cv * d.close
    return d.reset_index(drop=True)


def funding_series(sym: str) -> pd.Series:
    f = OUT / f"{sym}_funding_1h.pkl"
    d = pd.read_pickle(f) if f.exists() else pd.DataFrame()
    if d.empty:
        return pd.Series(dtype=float)
    return pd.Series(d["close"].astype(float).to_numpy() / 100.0, index=d["time"].astype("int64").to_numpy())     # percent -> fraction, 8-hour rate


def run_symbol(sym: str, cv: float, tf: int, V: float, J: float, trail: str, H: int) -> list[dict]:
    d = bars(sym, cv, tf)
    if d is None:
        return []
    t, o, h, lo, c, tv = (d[k].to_numpy() for k in ("time", "open", "high", "low", "close", "turnover"))
    per_day = 86400 // tf
    med_bar = pd.Series(tv).rolling(3 * per_day, min_periods=per_day).median().shift(1).to_numpy()
    daily = pd.Series(tv).rolling(14 * per_day, min_periods=7 * per_day).sum().shift(1).to_numpy() / 14
    low3 = pd.Series(lo).rolling(3 * per_day, min_periods=per_day).min().shift(1).to_numpy()
    h1 = d.assign(g=d.time // 3600 * 3600).groupby("g").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    pc = h1.close.shift(1).fillna(h1.close)
    atr1h = pd.Series(np.maximum(h1.high - h1.low, np.maximum(abs(h1.high - pc), abs(h1.low - pc)))).ewm(alpha=1 / 14, adjust=False).mean()
    atr_t, atr_v = h1.index.to_numpy() + 3600, atr1h.to_numpy()             # an hour's ATR is known once that hour has closed
    fund = funding_series(sym)
    f_t, f_v = fund.index.to_numpy(), fund.to_numpy()
    look = 4 * 3600 // tf
    out, free_from = [], -1
    for b in range(per_day, len(d) - 1):
        if t[b] < free_from or not np.isfinite(med_bar[b]) or med_bar[b] <= 0 or not np.isfinite(daily[b]) or daily[b] >= 2_000_000:
            continue
        rng = h[b] - lo[b]
        if tv[b] < V * med_bar[b] or o[b] <= 0 or c[b] / o[b] - 1 < J / 100 or rng <= 0 or (c[b] - lo[b]) / rng < 2 / 3:
            continue
        if np.isfinite(low3[b]) and c[b] / low3[b] - 1 >= 0.5:
            continue
        e = b + 1
        entry, stop = o[e], lo[b]
        if entry <= stop:
            continue
        best, exit_px, exit_i, why = entry, None, None, "time"
        last = min(len(d) - 1, e + H * per_day)
        for i in range(e, last + 1):
            if lo[i] <= stop:
                exit_px, exit_i, why = (min(o[i], stop) if i > e else stop), i, "stop"
                break
            best = max(best, c[i])
            if trail == "4h_low":
                stop = max(stop, float(np.min(lo[max(e, i - look + 1):i + 1])))
            else:
                k = int(np.searchsorted(atr_t, t[i] + tf, "right")) - 1
                if k >= 0:
                    stop = max(stop, best - 3 * atr_v[k])
            exit_px, exit_i = c[i], i
        a, z = int(t[e]), int(t[exit_i]) + tf
        m = (f_t >= a) & (f_t < z)
        funding = float(f_v[m].sum() / 8) if m.any() else 0.0          # hourly samples of the 8-hour rate
        gross = (exit_px / entry - 1) * 1e4
        out.append({"symbol": sym, "t": int(t[b]), "net_bps": gross - 2 * (FEE_BPS + SLIP_BPS) - funding * 1e4, "gross_bps": gross, "funding_bps": funding * 1e4,
                    "hours": (z - a) / 3600, "why": why, "max_run_pct": (float(np.max(h[e:exit_i + 1])) / entry - 1) * 100, "funding_known": bool(m.any())})
        free_from = z + 86400
    return out


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    eq = np.r_[0, np.cumsum(r)]
    return {"trades": int(len(r)), "mean_net_pct": float(r.mean() / 100), "median_net_pct": float(np.median(r) / 100), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None,
            "win_rate": float((r > 0).mean()), "worst_pct": float(r.min() / 100), "best_pct": float(r.max() / 100), "max_dd_pct_summed": float((np.maximum.accumulate(eq) - eq).max() / 100)}


def main() -> dict:
    uni = download()
    grid = [(tf, V, J, tr, H) for tf in (900, 1800) for V in (10, 20) for J in (5, 10) for tr in ("4h_low", "3atr_1h") for H in (3, 7)]
    data = {}
    for tf, V, J, tr, H in grid:
        rows = [r for s, cv in uni.items() for r in run_symbol(s, cv, tf, V, J, tr, H)]
        data[f"{tf // 60}m|vol>={V}x|bar>=+{J}%|trail={tr}|max={H}d"] = pd.DataFrame(rows).sort_values("t") if rows else pd.DataFrame(columns=["t", "net_bps"])
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in data.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"coins": len(uni), "coins_with_history": int(sum(1 for s in uni if (OUT / f"{s}_15m.pkl").exists() and len(pd.read_pickle(OUT / f"{s}_15m.pkl")) >= 96 * 20)),
           "variants_added": len(grid), "cuts": [pd.Timestamp(int(c1), unit="s").isoformat(), pd.Timestamp(int(c2), unit="s").isoformat()], "splits": {}}
    for n, d in data.items():
        res["splits"][n] = {"train": stats(d[d.t < c1].net_bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].net_bps.to_numpy())}
    ok = [n for n in data if res["splits"][n]["validation"].get("trades", 0) >= 30]
    chosen = max(ok, key=lambda n: res["splits"][n]["validation"]["mean_net_pct"]) if ok else None
    if chosen:
        hd = data[chosen][data[chosen].t >= c2]
        r = np.sort(hd.net_bps.to_numpy())
        lo_, hi_ = boot_ci(hd.net_bps.to_numpy())
        res["chosen"] = chosen
        res["holdout"] = {**stats(hd.net_bps.to_numpy()), "boot90_pct": [lo_ / 100, hi_ / 100], "mean_without_best_pct": float(r[:-1].mean() / 100) if len(r) > 1 else None,
                          "mean_without_top3_pct": float(r[:-3].mean() / 100) if len(r) > 3 else None, "exits": hd.why.value_counts().to_dict(), "median_hours": float(hd.hours.median()),
                          "median_funding_bps": float(hd.funding_bps.median()), "funding_known_share": float(hd.funding_known.mean()),
                          "top": hd.sort_values("net_bps").tail(5)[["symbol", "net_bps", "hours", "max_run_pct"]].to_dict("records")}
    (REPORTS / "delta_pump_intraday.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    for n, s in o["splits"].items():
        print(f"{n:50s} train {s['train'].get('mean_net_pct', 0):6.1f}% ({s['train']['trades']})  val {s['validation'].get('mean_net_pct', 0):6.1f}% ({s['validation']['trades']}) pf {s['validation'].get('pf') or 0:.2f}")
    print("CHOSEN", o.get("chosen")); print(json.dumps(o.get("holdout"), default=str))
