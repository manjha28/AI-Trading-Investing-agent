"""Can a pump be seen coming? Delta coins, 1h. Hypothetical research; decision support, not investment advice.

PUMP (the thing to predict) = the dump_study event: a 1h close >= +15% above the close 3 hours earlier on >= 10x the prior 3-day median volume.
For EVERY hour on every coin, features from the PAST only (known at that hour's close):
  vol6   = volume of the last 6 hours / (6 x median hourly volume of the prior 3 days)
  ret24  = return over the last 24 hours (%)
  squeeze = high-low range of the last 24h / the average 24h range of the prior 14 days (low = coiled)
  funding = Delta's funding rate at that hour (negative = shorts paying longs: crowded short)
  updays = number of the last 6 hours that closed up
LABEL: a pump starts within the next 24 hours. BASE RATE = share of all hours followed by a pump.
LIFT = P(pump within 24h | feature bucket) / base rate, measured on the TRAIN half (by time), checked on the VALIDATION quarter.
RULE candidates (5, fixed before any result was read: common pre-pump ideas): buy at the next hour's open when the
signal fires; stop = the lowest low of the last 24h; exit on a trailing stop (highest close - 3 x ATR14 of 1h) or after 3 days;
1 trade per coin per 3 days. Costs: taker 0.05% + 18% GST and 20 bps slippage per fill, funding paid while long.
SPLITS by time: train 50%, validation 25%, holdout 25%."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .delta_absorption import hourly
from .delta_pump_intraday import FEE_BPS, OUT, SLIP_BPS, funding_series, stats
from .round4_d2 import boot_ci
from .run import REPORTS


def frame(sym: str) -> pd.DataFrame | None:
    b = hourly(sym)
    if b is None or len(b) < 24 * 30:
        return None
    c, v, h, lo = b.close, b.volume, b.high, b.low
    med = v.rolling(72, min_periods=24).median().shift(1)
    pump = (c / c.shift(3) - 1 >= 0.15) & (v >= 10 * med)
    f = pd.DataFrame({"t": b.time, "open": b.open, "high": h, "low": lo, "close": c, "pump": pump})
    f["vol6"] = v.rolling(6).sum() / (6 * med)
    f["ret24"] = (c / c.shift(24) - 1) * 100
    r24 = h.rolling(24).max() - lo.rolling(24).min()
    f["squeeze"] = r24 / r24.rolling(336, min_periods=168).mean().shift(24)
    f["updays"] = (c > c.shift(1)).astype(int).rolling(6).sum()
    fund = funding_series(sym)
    f["funding"] = pd.Series(fund.reindex(b.time.to_numpy(), method="ffill").to_numpy() * 100 if len(fund) else np.nan, index=b.index)
    fut = pump[::-1].rolling(24, min_periods=1).max()[::-1].shift(-1)          # a pump in hours t+1..t+24
    f["label"] = fut.fillna(0).astype(bool)
    pc = c.shift(1).fillna(c)
    f["atr"] = pd.Series(np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))).ewm(alpha=1 / 14, adjust=False).mean()
    f["low24"] = lo.rolling(24).min()
    f["symbol"] = sym
    return f.iloc[24 * 15:].reset_index(drop=True)


BUCKETS = {"vol6": [0, 0.5, 1, 2, 4, 1e9], "ret24": [-100, -10, -3, 3, 10, 1e9], "squeeze": [0, 0.5, 0.8, 1.2, 2, 1e9], "funding": [-100, -0.05, -0.01, 0.01, 0.05, 100], "updays": [-1, 1, 3, 4, 5, 6]}


def lifts(d: pd.DataFrame, base: float) -> dict:
    out = {}
    for col, cuts in BUCKETS.items():
        g = d.groupby(pd.cut(d[col], cuts), observed=True)["label"]
        out[col] = {str(k): {"hours": int(len(x)), "p_pump_24h": float(x.mean()), "lift": float(x.mean() / base) if base else None} for k, x in g}
    return out


def trade(f: pd.DataFrame, i: int, fund: pd.Series) -> dict | None:
    if i + 1 >= len(f):
        return None
    o, h, lo, c, t, atr = (f[k].to_numpy() for k in ("open", "high", "low", "close", "t", "atr"))
    e = i + 1
    entry, stop = o[e], f.low24.iloc[i]
    if not np.isfinite(stop) or entry <= stop:
        return None
    best, px, j, why = entry, None, None, "time"
    for k in range(e, min(len(f), e + 72)):
        if lo[k] <= stop:
            px, j, why = (min(o[k], stop) if k > e else stop), k, "stop"
            break
        best = max(best, c[k])
        stop = max(stop, best - 3 * atr[k])
        px, j = c[k], k
    if px is None:
        return None
    a, z = int(t[e]), int(t[j]) + 3600
    fz = fund[(fund.index >= a) & (fund.index < z)]
    funding = float(fz.sum() / 8) if len(fz) else 0.0
    gross = (px / entry - 1) * 1e4
    return {"t": int(t[i]), "net_bps": gross - 2 * (FEE_BPS + SLIP_BPS) - funding * 1e4, "hours": (z - a) / 3600, "why": why,
            "caught_pump": bool(f.label.iloc[i]), "max_run_pct": (float(np.max(h[e:j + 1])) / entry - 1) * 100}


def main() -> dict:
    uni = json.loads((OUT / "universe.json").read_text())
    frames = [x for x in (frame(s) for s in uni) if x is not None]
    allf = pd.concat(frames, ignore_index=True)
    c1, c2 = allf.t.quantile(0.5), allf.t.quantile(0.75)
    tr, va = allf[allf.t < c1], allf[(allf.t >= c1) & (allf.t < c2)]
    base_tr, base_va = float(tr.label.mean()), float(va.label.mean())
    res = {"coins": len(frames), "hours": int(len(allf)), "pump_events": int(allf.pump.sum()), "base_rate_train": base_tr, "base_rate_validation": base_va,
           "lifts_train": lifts(tr, base_tr), "lifts_validation": lifts(va, base_va)}
    # rule candidates, fixed BEFORE any lift was read: volume building, price already turning up, range coiled, shorts crowded
    rules = {
        "vol6>=2": lambda d: d.vol6 >= 2,
        "vol6>=2 & ret24 3..10%": lambda d: (d.vol6 >= 2) & d.ret24.between(3, 10),
        "vol6>=2 & squeeze<0.8": lambda d: (d.vol6 >= 2) & (d["squeeze"] < 0.8),
        "vol6>=2 & funding<-0.01": lambda d: (d.vol6 >= 2) & (d.funding < -0.01),
        "vol6>=4 & ret24 3..10% & updays>=4": lambda d: (d.vol6 >= 4) & d.ret24.between(3, 10) & (d.updays >= 4),
    }
    res["rules"] = {}
    trades_by_rule = {}
    for name, fn in rules.items():
        rows = []
        for f in frames:
            fund = funding_series(f.symbol.iloc[0])
            sig = fn(f).fillna(False).to_numpy() & ~f.pump.to_numpy()      # never buy the pump bar itself
            nxt = -1
            for i in np.flatnonzero(sig):
                if i < nxt:
                    continue
                r = trade(f, i, fund)
                if r:
                    rows.append(r)
                    nxt = i + 72
        d = pd.DataFrame(rows).sort_values("t") if rows else pd.DataFrame(columns=["t", "net_bps", "caught_pump"])
        trades_by_rule[name] = d
        sel_tr, sel_va = fn(tr).fillna(False), fn(va).fillna(False)
        res["rules"][name] = {"precision_train": float(tr.label[sel_tr].mean()) if sel_tr.any() else None, "lift_train": float(tr.label[sel_tr].mean() / base_tr) if sel_tr.any() else None,
                              "lift_validation": float(va.label[sel_va].mean() / base_va) if sel_va.any() else None,
                              "train": stats(d[d.t < c1].net_bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].net_bps.to_numpy())}
    ok = [n for n in rules if res["rules"][n]["validation"].get("trades", 0) >= 30]
    if ok:
        ch = max(ok, key=lambda n: res["rules"][n]["validation"]["mean_net_pct"])
        h = trades_by_rule[ch][trades_by_rule[ch].t >= c2]
        r = np.sort(h.net_bps.to_numpy())
        lo_, hi_ = boot_ci(h.net_bps.to_numpy())
        res["chosen"], res["holdout"] = ch, {**stats(h.net_bps.to_numpy()), "boot90_pct": [lo_ / 100, hi_ / 100], "mean_without_best_pct": float(r[:-1].mean() / 100) if len(r) > 1 else None,
                                             "share_that_caught_a_pump": float(h.caught_pump.mean()), "median_hours": float(h.hours.median())}
    res["variants_added"] = len(rules)
    (REPORTS / "pre_pump.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    print({k: o[k] for k in ("coins", "hours", "pump_events", "base_rate_train", "base_rate_validation")})
    for col, b in o["lifts_train"].items():
        print(col, {k: (v["hours"], round(v["lift"] or 0, 2), round((o["lifts_validation"][col].get(k) or {}).get("lift") or 0, 2)) for k, v in b.items()})
    for n, s in o["rules"].items():
        print(f"{n:40s} lift tr {s['lift_train'] or 0:.2f} va {s['lift_validation'] or 0:.2f} | train {s['train'].get('mean_net_pct', 0):.2f}% ({s['train']['trades']}) val {s['validation'].get('mean_net_pct', 0):.2f}% ({s['validation']['trades']})")
    print("CHOSEN", o.get("chosen")); print(json.dumps(o.get("holdout"), default=str))
