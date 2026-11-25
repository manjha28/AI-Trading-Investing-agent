"""Ride the pump (long) on ILLIQUID coins that suddenly trade heavy volume. Hypothetical research; decision support, not investment advice.

The owner's idea (25 Nov 2026): instead of shorting the pumps, buy the early stage of the pump and ride it for days.
The catch to avoid: picking coins that are KNOWN (in hindsight) to have gone up 100-200% is look-ahead. So the entry has to be decided
from what is visible at a daily close, before anyone knows how far the pump will go.

DATA: Binance USDT-M daily candles + funding for every contract the archive lists, delisted ones included (the round 4 D3 download, 920
contracts, Jan 2022 to Oct 2026). Daily bars only, so entries are at the NEXT day's open after the signal day closes.
UNIVERSE at signal time: illiquid = median daily quote volume over the 60 days ending 5 days before the signal < 5 M USDT; >= 70 days history.
ENTRY (signal day d, all known at d's close):
  - volume ignition: quote volume on d >= V x the 60-day median (V = 5 or 10)
  - price ignition: close on d >= +X% vs the close 1 day earlier (X = 20 or 40), and close in the top third of d's range (buyers in control)
  - not already extended: close on d < +100% vs the lowest close of the prior 10 days (so this is the START of a move, not day 3 of it)
  - buy at d+1 open. One position per coin; 20-day refractory after an exit.
EXIT (daily, completed bars only):
  - initial stop: d's low (the ignition candle's low); a daily low through it exits at the stop (or the open if it gapped through)
  - trail: TRAIL = "2d_low" (stop = the lowest low of the last 2 completed days) or "3atr" (highest close since entry - 3 x ATR14)
  - time stop H days (7 or 14), exit at that day's close
COSTS: Delta-style taker 0.05% + 18% GST per fill, slippage 20 bps per fill (illiquid coins on a spike day; 8 bps used in D3 is too kind),
funding paid by longs at every 8-hour time held (summed daily rate).
SPLITS: by signal date: train 50%, validation 25%, holdout 25%. Choose on validation (>= 30 trades), holdout once."""
from __future__ import annotations

import json
import pickle

import numpy as np
import pandas as pd

from .round4_d2 import boot_ci
from .round4_d3 import D3_DIR, STABLE, _norm_t, make_coin
from .run import REPORTS

FEE_BPS = 5.0 * 1.18
SLIP_BPS = 20.0


def signals(cn, V: float, X: float) -> list[int]:
    out, last = [], -10**9
    med = pd.Series(cn.qv).rolling(60).median().shift(5).to_numpy()
    for d in range(70, len(cn.t) - 2):
        if d - last < 20 or not np.isfinite(med[d]) or med[d] <= 0 or med[d] >= 5_000_000:
            continue
        if cn.qv[d] < V * med[d] or cn.c[d - 1] <= 0:
            continue
        rng = cn.h[d] - cn.l[d]
        if cn.c[d] / cn.c[d - 1] - 1 < X / 100 or rng <= 0 or (cn.c[d] - cn.l[d]) / rng < 2 / 3:
            continue
        if cn.c[d] / np.min(cn.c[d - 10:d]) - 1 >= 1.0:
            continue
        out.append(d)
        last = d
    return out


def ride(cn, d: int, trail: str, H: int) -> dict | None:
    e = d + 1
    if e >= len(cn.t):
        return None
    entry = cn.o[e]
    stop = cn.l[d]
    if entry <= stop:
        return None
    best_close, funding = entry, 0.0
    exit_px, exit_i, why = None, None, "time"
    for i in range(e, min(len(cn.t), e + H)):
        funding += cn.funding_by_day[i]
        if cn.l[i] <= stop:
            exit_px, exit_i, why = min(cn.o[i], stop) if i > e else stop, i, "stop"
            break
        best_close = max(best_close, cn.c[i])
        if trail == "2d_low":
            stop = max(stop, np.min(cn.l[max(e, i - 1):i + 1]))
        else:
            stop = max(stop, best_close - 3 * cn.atr[i])
        exit_px, exit_i = cn.c[i], i
    if exit_px is None:
        return None
    gross = (exit_px / entry - 1) * 1e4
    net = gross - 2 * (FEE_BPS + SLIP_BPS) - funding * 1e4
    run_max = (np.max(cn.h[e:exit_i + 1]) / entry - 1) * 100
    return {"symbol": cn.sym, "t": int(cn.t[d]), "net_bps": net, "gross_bps": gross, "days": exit_i - e + 1, "why": why, "max_run_pct": run_max,
            "became_100pct": bool(np.max(cn.h[e:min(len(cn.t), e + 14)]) / cn.c[d - 1] >= 2.0)}


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    eq = np.r_[0, np.cumsum(r)]
    return {"trades": int(len(r)), "mean_net_pct": float(r.mean() / 100), "median_net_pct": float(np.median(r) / 100), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None,
            "win_rate": float((r > 0).mean()), "worst_pct": float(r.min() / 100), "best_pct": float(r.max() / 100), "max_dd_pct_summed": float((np.maximum.accumulate(eq) - eq).max() / 100)}


def main() -> dict:
    raw, last_day = {}, 0
    for p in sorted(D3_DIR.glob("*.pkl")):
        k, f = pickle.load(open(p, "rb"))
        raw[p.stem] = (k, f)
        last_day = max(last_day, int(_norm_t(k.t.to_numpy())[-1]))
    coins = [c for c in (make_coin(s, k, f, last_day) for s, (k, f) in raw.items() if not any(s.startswith(x) for x in STABLE)) if c is not None]
    grid = [(V, X, tr, H) for V in (5, 10) for X in (20, 40) for tr in ("2d_low", "3atr") for H in (7, 14)]
    data = {}
    for V, X, tr, H in grid:
        rows = [r for cn in coins for d in signals(cn, V, X) if (r := ride(cn, d, tr, H))]
        data[f"vol>={V}x|day>=+{X}%|trail={tr}|max={H}d"] = pd.DataFrame(rows).sort_values("t") if rows else pd.DataFrame(columns=["t", "net_bps"])
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in data.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"coins": len(coins), "variants_added": len(grid), "cuts": [pd.Timestamp(int(c1), unit="s").strftime("%Y-%m-%d"), pd.Timestamp(int(c2), unit="s").strftime("%Y-%m-%d")], "splits": {}}
    for n, d in data.items():
        res["splits"][n] = {"train": stats(d[d.t < c1].net_bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].net_bps.to_numpy())}
    ok = [n for n in data if res["splits"][n]["validation"].get("trades", 0) >= 30]
    chosen = max(ok, key=lambda n: res["splits"][n]["validation"]["mean_net_pct"]) if ok else None
    if chosen:
        h = data[chosen][data[chosen].t >= c2]
        r = h.net_bps.to_numpy()
        lo, hi = boot_ci(r)
        res["chosen"], res["holdout"] = chosen, {**stats(r), "boot90_pct": [lo / 100, hi / 100], "exits": h.why.value_counts().to_dict(), "median_days": float(h.days.median()),
                                                  "share_that_reached_2x_within_14d": float(h.became_100pct.mean()), "by_year": {str(y): stats(g.net_bps.to_numpy()) for y, g in h.groupby(pd.to_datetime(h.t, unit="s").dt.year)}}
    (REPORTS / "pump_ride.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    out = main()
    for n, s in out["splits"].items():
        print(f"{n:45s} train {s['train'].get('mean_net_pct', 0):6.1f}% ({s['train']['trades']})  val {s['validation'].get('mean_net_pct', 0):6.1f}% ({s['validation']['trades']}) pf {s['validation'].get('pf') or 0:.2f}")
    print("CHOSEN", out.get("chosen")); print(json.dumps(out.get("holdout"), default=str))
