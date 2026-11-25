"""FRESH out-of-sample test of the frozen dump_study short rule ("mid|base") on Binance USD-M coins that Delta India does NOT list.
None of this data was used to design or choose the rule. Hypothetical research; decision support, not investment advice.

FROZEN RULE (exactly as dump_study chose it on Delta data): pump event = a 1h close >= +15% above the close 3 hours earlier on >= 10x the prior
3-day median hourly volume; short at the next hour's open after the first 1h close below the pump bar's midpoint within 6 hours; stop 0.5%
above the post-pump peak; target = the close 3 hours before the pump; 3-day time stop.
DATA: Binance 1h klines + 8-hour funding from data.binance.vision (checksum-verified), only for the months around days the daily file flags as
possible pumps (daily close >= +15% on >= 5x volume), Jan 2024 to Oct 2026, coins whose base asset is not on Delta India today. Delisted
Binance contracts are included (they are in the daily archive), so this side has no survivorship bias.
COSTS: taker 0.05% + 18% GST and 20 bps slippage per fill; Binance funding (shorts receive a positive rate)."""
from __future__ import annotations

import json
import pickle
from datetime import date

import httpx
import numpy as np
import pandas as pd

from . import binance_data as bd
from .delta_pump_intraday import FEE_BPS, OUT, SLIP_BPS, stats
from .round4_d2 import boot_ci
from .round4_d3 import D3_DIR, STABLE, _norm_t
from .run import REPORTS

CACHE = OUT.parent / "binance_1h_fresh"


def candidates() -> dict[str, set[tuple[int, int]]]:
    delta_bases = {s[:-3] for s in json.loads((OUT / "universe.json").read_text())}
    out: dict[str, set[tuple[int, int]]] = {}
    for p in sorted(D3_DIR.glob("*.pkl")):
        sym = p.stem
        if not sym.endswith("USDT") or any(sym.startswith(s) for s in STABLE):
            continue
        base = sym[:-4]
        if base in delta_bases or base.removeprefix("1000") in delta_bases:
            continue
        k, _f = pickle.load(open(p, "rb"))
        t = _norm_t(k.t.to_numpy())
        c, qv = k.c.to_numpy(float), k.qv.to_numpy(float)
        med = pd.Series(qv).rolling(60, min_periods=20).median().shift(1).to_numpy()
        for i in range(1, len(c)):
            d = pd.Timestamp(int(t[i]), unit="s")
            if d.year < 2024 or not np.isfinite(med[i]) or med[i] <= 0 or c[i - 1] <= 0:
                continue
            if c[i] / c[i - 1] - 1 >= 0.15 and qv[i] >= 5 * med[i]:
                prev = (d - pd.offsets.MonthBegin(1))
                out.setdefault(sym, set()).update({(d.year, d.month), (prev.year, prev.month)})
    return out


def load(sym: str, months: set[tuple[int, int]]) -> tuple[pd.DataFrame, pd.Series]:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{sym}.pkl"
    if f.exists():
        return pickle.load(open(f, "rb"))
    frames, fund = [], []
    with httpx.Client(timeout=60, follow_redirects=True) as cl:
        for y, m in sorted(months):
            blob = bd.fetch_verified(cl, f"{bd.ROOT}/monthly/klines/{sym}/1h/{sym}-1h-{y:04d}-{m:02d}.zip")
            if blob is not None:
                frames.append(bd._csv(blob, ["open_time", "open", "high", "low", "close", "volume"]))
            fb = bd.fetch_verified(cl, f"{bd.ROOT}/monthly/fundingRate/{sym}/{sym}-fundingRate-{y:04d}-{m:02d}.zip")
            if fb is not None:
                fund.append(bd._csv(fb, ["calc_time", "funding_interval_hours", "last_funding_rate"]))
    if not frames:
        res = (pd.DataFrame(), pd.Series(dtype=float))
    else:
        k = pd.concat(frames, ignore_index=True)
        k["open_time"] = pd.to_numeric(k["open_time"], errors="coerce")
        k = k.dropna(subset=["open_time"])
        k["time"] = (k.open_time.astype("int64") // (1000 if k.open_time.iloc[0] < 10**14 else 10**6)).astype("int64")
        k = k.drop(columns="open_time").astype({"open": float, "high": float, "low": float, "close": float, "volume": float}).drop_duplicates("time").sort_values("time").reset_index(drop=True)
        fs = pd.Series(dtype=float)
        if fund:
            fd = pd.concat(fund, ignore_index=True)
            fd["t"] = pd.to_numeric(fd.calc_time, errors="coerce") // 1000
            fd = fd.dropna(subset=["t"])
            fs = pd.Series(pd.to_numeric(fd.last_funding_rate, errors="coerce").to_numpy(), index=fd.t.astype("int64").to_numpy()).dropna().sort_index()
        res = (k, fs)
    pickle.dump(res, open(f, "wb"))
    return res


def run_symbol(k: pd.DataFrame, fs: pd.Series) -> list[dict]:
    if k.empty or len(k) < 100:
        return []
    o, h, lo, c, v, t = (k[x].to_numpy() for x in ("open", "high", "low", "close", "volume", "time"))
    n = len(k)
    gap = np.r_[3600, np.diff(t)]
    med = pd.Series(v).rolling(72, min_periods=24).median().shift(1).to_numpy()
    out, last = [], -10**9
    for e in range(80, n - 2):
        if e - last < 72 or not np.isfinite(med[e]) or med[e] <= 0 or c[e - 3] <= 0 or gap[e - 2:e + 1].max() != 3600:
            continue
        if not (c[e] / c[e - 3] - 1 >= 0.15 and v[e] >= 10 * med[e]):
            continue
        last = e
        mid, peak, s = (h[e] + lo[e]) / 2, h[e], None
        for j in range(e + 1, min(n - 1, e + 7)):
            peak = max(peak, h[j])
            if c[j] < mid:
                s = j
                break
        if s is None:
            continue
        entry, stop, base = o[s + 1], peak * 1.005, c[e - 3]
        if entry >= stop:
            continue
        px, xi, why = None, None, "time"
        for j in range(s + 1, min(n, s + 73)):
            if h[j] >= stop:
                px, xi, why = (max(o[j], stop) if j > s + 1 else stop), j, "stop"
                break
            if lo[j] <= base:
                px, xi, why = (min(o[j], base) if j > s + 1 else base), j, "target"
                break
            px, xi = c[j], j
        if px is None:
            continue
        a, z = int(t[s + 1]), int(t[xi]) + 3600
        fz = fs[(fs.index >= a) & (fs.index < z)]
        gross = (entry / px - 1) * 1e4
        out.append({"t": int(t[e]), "net_bps": gross - 2 * (FEE_BPS + SLIP_BPS) + float(fz.sum()) * 1e4, "why": why, "hours": (z - a) / 3600})
    return out


def main() -> dict:
    cand = candidates()
    rows = []
    for sym, months in cand.items():
        k, fs = load(sym, months)
        for r in run_symbol(k, fs):
            rows.append({"symbol": sym, **r})
    d = pd.DataFrame(rows).sort_values("t")
    r = d.net_bps.to_numpy()
    lo_, hi_ = boot_ci(r)
    srt = np.sort(r)
    res = {"coins_scanned": len(cand), "coins_with_trades": int(d.symbol.nunique()), **stats(r), "boot90_pct": [lo_ / 100, hi_ / 100],
           "mean_without_best_pct": float(srt[:-1].mean() / 100), "exits": d.why.value_counts().to_dict(), "median_hours": float(d.hours.median()),
           "by_year": {str(y): stats(g.net_bps.to_numpy()) for y, g in d.groupby(pd.to_datetime(d.t, unit="s").dt.year)}, "variants_added": 0}
    (REPORTS / "dump_fresh.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    print(json.dumps(main(), indent=1, default=str))
