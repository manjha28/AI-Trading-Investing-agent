"""Anatomy of pumps on ILLIQUID coins: what happens after a coin rises 100-200% in 3-4 days. Descriptive only; no strategy, no parameters chosen on outcomes.

DATA: the Binance USDT-M daily candles and funding already downloaded for round 4 D3 (every contract the archive lists, delisted ones included), Jan 2022 to Oct 2026.
EVENT (fixed before looking): close on day 0 is 100% to 200% above the LOWER of the closes 3 and 4 days earlier (the pre-pump base); the coin is illiquid: the median
daily quote volume over the 60 days ending 5 days before day 0 is under 5 million USDT; at least 70 days of history; 30-day refractory per coin. Everything is measured from
the day-0 CLOSE (the first moment the event is known), using completed days only.
Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json
import pickle

import numpy as np
import pandas as pd

from .round4_d3 import D3_DIR, STABLE, _norm_t, make_coin
from .run import REPORTS

RISE_LO, RISE_HI, ILLIQUID_USD, REFRACTORY = 1.0, 2.0, 5_000_000, 30


def events(cn) -> list[tuple[int, float]]:
    out, last = [], -10**9
    vol60 = pd.Series(cn.qv).rolling(60).median().shift(5).to_numpy()
    for i in range(70, len(cn.t)):
        base = min(cn.c[i - 4], cn.c[i - 3])
        if base <= 0 or not np.isfinite(vol60[i]) or vol60[i] >= ILLIQUID_USD or i - last < REFRACTORY:
            continue
        rise = cn.c[i] / base - 1
        if RISE_LO <= rise <= RISE_HI:
            out.append((i, base))
            last = i
    return out


def measure(cn, i: int, base: float) -> dict:
    c0, n = cn.c[i], len(cn.t)
    row = {"symbol": cn.sym, "date": pd.Timestamp(int(cn.t[i]), unit="s").strftime("%Y-%m-%d"), "rise_pct": (c0 / base - 1) * 100, "delisted": cn.delisted,
           "median_qv_60d": float(pd.Series(cn.qv[max(0, i - 65):i - 5]).median()), "day0_qv_x": float(cn.qv[i] / max(1.0, pd.Series(cn.qv[max(0, i - 65):i - 5]).median())),
           "funding_day0_pct": float(cn.funding_by_day[i] * 100), "halted": bool((cn.qv[i + 1:i + 8] <= 0).any())}
    for d in (1, 3, 7, 14, 30):
        row[f"ret_d{d}_pct"] = (cn.c[i + d] / c0 - 1) * 100 if i + d < n else None
    for d in (1, 3, 7):
        row[f"max_up_d{d}_pct"] = (cn.h[i + 1:i + 1 + d].max() / c0 - 1) * 100 if i + d < n else None
    w = slice(i + 1, min(n, i + 31))
    if i + 30 < n:
        hi, lo = cn.h[w], cn.l[w]
        pk = int(np.argmax(cn.c[i:i + 15]))
        row["days_to_peak_close_14d"] = pk
        row["peak_close_vs_day0_pct"] = (cn.c[i + pk] / c0 - 1) * 100
        row["min_low_30d_vs_day0_pct"] = (lo.min() / c0 - 1) * 100
        row["back_to_base_30d"] = bool(lo.min() <= base)
        row["half_retrace_30d"] = bool(lo.min() <= base + 0.5 * (c0 - base))
        first_half = np.flatnonzero(lo <= base + 0.5 * (c0 - base))
        row["days_to_half_retrace"] = int(first_half[0]) + 1 if len(first_half) else None
        row["higher_high_before_half_retrace"] = bool(hi[: (first_half[0] + 1) if len(first_half) else len(hi)].max() > cn.h[i])
        row["qv_d7_vs_day0"] = float(cn.qv[i + 7] / cn.qv[i]) if cn.qv[i] > 0 else None
    return row


def q(vals) -> dict:
    v = np.array([x for x in vals if x is not None], dtype=float)
    if not len(v):
        return {"n": 0}
    return {"n": int(len(v)), "p10": float(np.percentile(v, 10)), "p25": float(np.percentile(v, 25)), "median": float(np.median(v)), "p75": float(np.percentile(v, 75)), "p90": float(np.percentile(v, 90))}


def share(vals) -> dict:
    v = [x for x in vals if x is not None]
    return {"n": len(v), "share": (sum(1 for x in v if x) / len(v)) if v else None}


def run() -> dict:
    raw, last_day = {}, 0
    for p in sorted(D3_DIR.glob("*.pkl")):
        k, f = pickle.load(open(p, "rb"))
        raw[p.stem] = (k, f)
        last_day = max(last_day, int(_norm_t(k.t.to_numpy())[-1]))
    rows = []
    for sym, (k, f) in raw.items():
        if any(sym.startswith(s) for s in STABLE):
            continue
        cn = make_coin(sym, k, f, last_day)
        if cn is None:
            continue
        rows += [measure(cn, i, b) for i, b in events(cn)]
    rows.sort(key=lambda r: r["date"])
    halted = [r for r in rows if r["halted"]]           # zero volume within 7 days: the price is frozen (trading halted or settled), so returns would be fiction
    all_rows, rows = rows, [r for r in rows if not r["halted"]]
    num = ["rise_pct", "day0_qv_x", "funding_day0_pct", "ret_d1_pct", "ret_d3_pct", "ret_d7_pct", "ret_d14_pct", "ret_d30_pct", "max_up_d1_pct", "max_up_d3_pct", "max_up_d7_pct",
           "days_to_peak_close_14d", "peak_close_vs_day0_pct", "min_low_30d_vs_day0_pct", "days_to_half_retrace", "qv_d7_vs_day0"]
    flags = ["back_to_base_30d", "half_retrace_30d", "higher_high_before_half_retrace", "delisted"]
    summary = {k: q([r.get(k) for r in rows]) for k in num} | {k: share([r.get(k) for r in rows]) for k in flags}
    by_year = {}
    for y in sorted({r["date"][:4] for r in rows}):
        rs = [r for r in rows if r["date"].startswith(y)]
        by_year[y] = {"events": len(rs), "ret_d7_median": q([r["ret_d7_pct"] for r in rs]).get("median"), "ret_d30_median": q([r["ret_d30_pct"] for r in rs]).get("median"),
                      "max_up_d3_median": q([r["max_up_d3_pct"] for r in rs]).get("median"), "half_retrace_30d": share([r.get("half_retrace_30d") for r in rs])["share"]}
    out = {"definition": {"rise": "100-200% vs min(close d-4, d-3)", "illiquid_median_qv_60d_usd_below": ILLIQUID_USD, "refractory_days": REFRACTORY},
           "coins_scanned": len(raw), "events": len(rows), "excluded_halted": [(r["symbol"], r["date"]) for r in halted], "coins_with_events": len({r["symbol"] for r in rows}), "summary": summary, "by_year": by_year, "rows": all_rows}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "pump_anatomy.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    r = run()
    print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=1, default=str))
