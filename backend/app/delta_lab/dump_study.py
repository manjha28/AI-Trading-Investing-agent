"""Which pumps dump? Volume spike + price jump on Delta's own coins, what follows, and a short rule built from it. Hypothetical research only.

DATA: Delta India's 15m candles (resampled to 1h) and hourly FUNDING for the 183 crypto USD perpetuals downloaded earlier today (only coins
listed today: survivorship bias). EVENT (known at the close of 1h bar e): close >= +J% above the close 3 hours earlier (J = 15) AND bar e's
volume >= 10 x the median hourly volume of the prior 3 days. One event per coin per 3 days.
PART 1, anatomy: for every event, the return from e's close to +4h / +24h / +72h and the worst squeeze (highest high / close - 1) within
24h, split by features KNOWN at e's close or one hour later: upper-wick share of bar e, the next hour's volume vs e's, whether the next hour
closes below e's midpoint, jump size, funding at e, and the 7-day run-up before the jump. Patterns are read on the TRAIN events only.
PART 2, short rule (variants fixed before the holdout is looked at):
  CONFIRM = "none" (short e+1 open) | "mid" (first 1h close below e's midpoint within 6h, short next open) | "lower_high" (after e, first 1h bar
  whose high is below the post-event peak and that closes red, within 12h)
  STOP = the post-event peak high x (1 + 0.5%)
  EXIT = "base" (target = the close 3 hours before e, i.e. the pre-pump level, else time stop) | "trail" (stop trails to the highest high of the
  last 4 hours once in profit) ; time stop 3 days.
COSTS: taker 0.05% + 18% GST per fill, 20 bps slippage per fill, Delta funding (shorts RECEIVE a positive rate, pay a negative one).
SPLITS by event time: train 50%, validation 25%, holdout 25%; choose on validation (>= 20 trades), holdout once."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .delta_absorption import hourly
from .delta_pump_intraday import FEE_BPS, OUT, SLIP_BPS, funding_series, stats
from .round4_d2 import boot_ci
from .run import REPORTS

J, VX = 15.0, 10.0


def events(sym: str) -> tuple[pd.DataFrame | None, list[int]]:
    b = hourly(sym)
    if b is None:
        return None, []
    c, v = b.close.to_numpy(), b.volume.to_numpy()
    med = pd.Series(v).rolling(72, min_periods=24).median().shift(1).to_numpy()
    out, last = [], -10**9
    for e in range(80, len(b) - 2):
        if e - last < 72 or not np.isfinite(med[e]) or med[e] <= 0 or c[e - 3] <= 0:
            continue
        if c[e] / c[e - 3] - 1 >= J / 100 and v[e] >= VX * med[e]:
            out.append(e)
            last = e
    return b, out


def features(b: pd.DataFrame, e: int, fund: pd.Series) -> dict:
    o, h, lo, c, v, t = (b[k].to_numpy() for k in ("open", "high", "low", "close", "volume", "time"))
    rng = h[e] - lo[e]
    fz = fund[(fund.index <= t[e] + 3600)]
    n = len(b)
    fwd = {f"ret_{k}h_pct": (c[e + k] / c[e] - 1) * 100 if e + k < n else None for k in (4, 24, 72)}
    return {"t": int(t[e]), "jump_pct": (c[e] / c[e - 3] - 1) * 100, "wick": (h[e] - c[e]) / rng if rng > 0 else 0.0, "next_vol_ratio": v[e + 1] / v[e] if v[e] > 0 else None,
            "next_below_mid": bool(c[e + 1] < (h[e] + lo[e]) / 2), "funding_pct": float(fz.iloc[-1] * 100) if len(fz) else None,
            "runup7d_pct": (c[e] / c[max(0, e - 168)] - 1) * 100, "squeeze24h_pct": (float(np.max(h[e + 1:min(n, e + 25)])) / c[e] - 1) * 100 if e + 1 < n else None, **fwd}


def short_trade(b: pd.DataFrame, e: int, fund: pd.Series, confirm: str, exit_: str) -> dict | None:
    o, h, lo, c, t = (b[k].to_numpy() for k in ("open", "high", "low", "close", "time"))
    n = len(b)
    peak, s = h[e], None
    if confirm == "none":
        s = e
    elif confirm == "mid":
        mid = (h[e] + lo[e]) / 2
        for k in range(e + 1, min(n - 1, e + 7)):
            peak = max(peak, h[k])
            if c[k] < mid:
                s = k
                break
    else:
        for k in range(e + 1, min(n - 1, e + 13)):
            if h[k] < peak and c[k] < o[k]:
                s = k
                break
            peak = max(peak, h[k])
    if s is None or s + 1 >= n:
        return None
    entry, stop = o[s + 1], peak * 1.005
    if entry >= stop:
        return None
    base = c[e - 3]
    exit_px, exit_i, why = None, None, "time"
    for k in range(s + 1, min(n, s + 1 + 72)):
        if h[k] >= stop:
            exit_px, exit_i, why = max(o[k], stop) if k > s + 1 else stop, k, "stop"
            break
        if exit_ == "base" and lo[k] <= base:
            exit_px, exit_i, why = min(o[k], base) if k > s + 1 else base, k, "target"
            break
        if exit_ == "trail" and c[k] < entry:
            stop = min(stop, float(np.max(h[max(s + 1, k - 3):k + 1])) * 1.005)
        exit_px, exit_i = c[k], k
    if exit_px is None:
        return None
    a, z = int(t[s + 1]), int(t[exit_i]) + 3600
    f = fund[(fund.index >= a) & (fund.index < z)]
    funding = float(f.sum() / 8) if len(f) else 0.0                # shorts receive this when positive
    gross = (entry / exit_px - 1) * 1e4
    return {"net_bps": gross - 2 * (FEE_BPS + SLIP_BPS) + funding * 1e4, "why": why, "hours": (z - a) / 3600, "squeeze_stop": why == "stop"}


def main() -> dict:
    uni = json.loads((OUT / "universe.json").read_text())
    feats, trades = [], {f"{cf}|{ex}": [] for cf in ("none", "mid", "lower_high") for ex in ("base", "trail")}
    for s in uni:
        b, evs = events(s)
        if b is None or not evs:
            continue
        fund = funding_series(s)
        for e in evs:
            feats.append({"symbol": s, **features(b, e, fund)})
            for name in trades:
                cf, ex = name.split("|")
                r = short_trade(b, e, fund, cf, ex)
                if r:
                    trades[name].append({"symbol": s, "t": int(b.time.iloc[e]), **r})
    F = pd.DataFrame(feats).sort_values("t")
    c1, c2 = F.t.quantile(0.5), F.t.quantile(0.75)
    tr = F[F.t < c1]
    q = lambda x: {"n": int(x.notna().sum()), "median": float(x.median()), "share_down": float((x < 0).mean())}   # noqa: E731
    anatomy = {"events": int(len(F)), "coins": int(F.symbol.nunique()), "train_events": int(len(tr)),
               "all_train": {k: q(tr[k]) for k in ("ret_4h_pct", "ret_24h_pct", "ret_72h_pct", "squeeze24h_pct")}, "by_feature_train_ret_24h": {}}
    for col, cuts in (("wick", [0, 0.25, 0.5, 1.01]), ("next_vol_ratio", [0, 0.3, 0.7, 100]), ("jump_pct", [0, 25, 50, 10**6]), ("runup7d_pct", [-100, 20, 80, 10**6]),
                      ("funding_pct", [-100, -0.01, 0.01, 100])):
        g = tr.groupby(pd.cut(tr[col], cuts), observed=True)["ret_24h_pct"]
        anatomy["by_feature_train_ret_24h"][col] = {str(k): {"n": int(len(x)), "median": float(x.median()), "share_down": float((x < 0).mean())} for k, x in g}
    g = tr.groupby("next_below_mid")["ret_24h_pct"]
    anatomy["by_feature_train_ret_24h"]["next_hour_closes_below_mid"] = {str(k): {"n": int(len(x)), "median": float(x.median()), "share_down": float((x < 0).mean())} for k, x in g}
    res = {"variants_added": len(trades), "cuts": [pd.Timestamp(int(c1), unit="s").isoformat(), pd.Timestamp(int(c2), unit="s").isoformat()], "anatomy": anatomy, "splits": {}}
    data = {n: pd.DataFrame(r).sort_values("t") if r else pd.DataFrame(columns=["t", "net_bps"]) for n, r in trades.items()}
    for n, d in data.items():
        res["splits"][n] = {"train": stats(d[d.t < c1].net_bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].net_bps.to_numpy())}
    ok = [n for n in data if res["splits"][n]["validation"].get("trades", 0) >= 20]
    if ok:
        chosen = max(ok, key=lambda n: res["splits"][n]["validation"]["mean_net_pct"])
        h = data[chosen][data[chosen].t >= c2]
        r = np.sort(h.net_bps.to_numpy())
        lo_, hi_ = boot_ci(h.net_bps.to_numpy())
        res["chosen"], res["holdout"] = chosen, {**stats(h.net_bps.to_numpy()), "boot90_pct": [lo_ / 100, hi_ / 100], "mean_without_best_pct": float(r[:-1].mean() / 100) if len(r) > 1 else None,
                                                 "exits": h.why.value_counts().to_dict(), "median_hours": float(h.hours.median())}
    (REPORTS / "dump_study.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    print(json.dumps(o["anatomy"], indent=1, default=str)[:3500])
    for n, s in o["splits"].items():
        print(f"{n:20s} train {s['train'].get('mean_net_pct', 0):6.2f}% ({s['train']['trades']})  val {s['validation'].get('mean_net_pct', 0):6.2f}% ({s['validation']['trades']}) pf {s['validation'].get('pf') or 0:.2f}")
    print("CHOSEN", o.get("chosen")); print(json.dumps(o.get("holdout"), default=str))
