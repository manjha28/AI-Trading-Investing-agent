"""Slower (4-hour) strategies on Delta coins: LONG coins that look close to pumping, using the [Smith] VWAP Deviation bands on 4h, and SHORT
patterns built to avoid squeezes (owner, 25 Nov 2026). Hypothetical research; decision support, not investment advice. No order path.

DATA: Delta's own candles: 15-minute history for the 183 crypto perpetuals (data/delta_15m) plus BTC/ETH/SOL/XRP from the 1-minute set, built into
4-hour bars; Delta's hourly funding. Coins listed today only (survivorship bias, stated).
4H INDICATORS: Smith bands (volume-weighted mean of log close over L bars, mean-absolute-deviation bands x2 and x3, L = 30 or 60), RSI14, ATR14,
EMA50, 4h volume vs its 20-bar average, 24h volume vs the 30-day daily average, 7-day return, funding (latest 8-hour rate), 3-bar pivots.
"NEAR PUMP" (known at the bar's close): 24h volume >= 2x the 30-day daily average AND the 7-day return between +5% and +40% (moving, not blown off).
LONGS (only on near-pump coins):
  L1 dip   a 4h bar dips to the lower 2-band and closes back above it with RSI14 < 45; stop = that bar's low - 1 ATR
  L2 break a 4h close above the upper 2-band on >= 2x average 4h volume; stop = the Smith mean
  L3 reclaim a close back above the mean after >= 3 closes below it; stop = the lowest low of the last 6 bars - 0.5 ATR
SHORTS (after a run-up: 7-day return >= +20%):
  S1 failed band   the high reached the upper 3-band and the close is back below the upper 2-band with RSI14 > 60; stop = highest high of the last 3 bars + 1 ATR
  S2 lower high    a confirmed 4h pivot high below the previous pivot high, then a red close below the Smith mean; stop = that lower high + 1 ATR
  S3 blow-off      a bar with >= 5x average volume and an upper wick >= 50% of its range, then (within 6 bars) a close below that bar's low;
                   stop = the blow-off high + 0.5 ATR
SQUEEZE GUARD (shorts, variant on/off): skip when funding is negative (shorts already crowded), when the last 4h volume is still >= 3x average
(the move is still being bought), or when the stop is more than 15% away.
EXITS: "mean" (longs: the upper 2-band; shorts: the Smith mean, else the trailing stop) | "trail" (3 x ATR from the best close; no target).
All exits also at 5 days (30 bars). Entries at the next 4h open.
COSTS: Delta taker 0.05% + 18% GST per fill; slippage 5 bps per fill on coins in the day's top-20 by turnover, 20 bps otherwise; funding paid/received.
SPLITS by time: train 50%, validation 25%, holdout 25%; per idea the variant with the best validation mean is chosen; holdout once."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .delta_all import DELTA_1M
from .delta_pump_intraday import OUT as D15, funding_series
from .round4_d2 import boot_ci
from .run import REPORTS

MAJORS = ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD")
FEE = 5.9


def load4h() -> dict[str, pd.DataFrame]:
    import httpx
    uni = json.loads((D15 / "universe.json").read_text())
    with httpx.Client(timeout=30) as cl:
        cvs = {p["symbol"]: float(p["contract_value"]) for p in cl.get("https://api.india.delta.exchange/v2/products", params={"contract_types": "perpetual_futures"}).json()["result"]}
    paths = {s: D15 / f"{s}_15m.pkl" for s in uni} | {s: DELTA_1M / f"{s}_1m.pkl" for s in MAJORS}
    out = {}
    for s, p in paths.items():
        d = pd.read_pickle(p).astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
        g = d.time // 14400 * 14400
        b = d.groupby(g).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("volume", "sum")).reset_index()
        if len(b) < 300:
            continue
        b["turn"] = b.v * cvs.get(s, 1.0) * b.c
        f = funding_series(s)
        b["fund"] = f.reindex(b.time.to_numpy() + 14400 - 3600, method="ffill").to_numpy() if len(f) else 0.0
        out[s] = b
    return out


def indicators(b: pd.DataFrame, L: int) -> dict[str, np.ndarray]:
    c, v, h, lo = b.c.to_numpy(), b.v.to_numpy(), b.h.to_numpy(), b.l.to_numpy()
    x = np.log(c)
    vs = pd.Series(v)
    mean = (pd.Series(v * x).rolling(L).sum() / vs.rolling(L).sum()).to_numpy()
    win = np.lib.stride_tricks.sliding_window_view(x, L)
    wv = np.lib.stride_tricks.sliding_window_view(v, L)
    dev = np.full(len(c), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        dev[L - 1:] = (wv * np.abs(win - mean[L - 1:, None])).sum(1) / wv.sum(1)
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.maximum(d, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    dn = pd.Series(np.maximum(-d, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    pc = np.r_[c[0], c[:-1]]
    atr = pd.Series(np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    v24 = vs.rolling(6).sum()
    v30 = vs.rolling(180, min_periods=60).sum().shift(6) / 30
    ph = np.zeros(len(c), bool)
    for i in range(3, len(c) - 3):
        ph[i] = h[i] == h[i - 3:i + 4].max()
    return {"mean": np.exp(mean), "u2": np.exp(mean + 2 * dev), "l2": np.exp(mean - 2 * dev), "u3": np.exp(mean + 3 * dev),
            "rsi": (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy(), "atr": atr,
            "vx4": (vs / vs.rolling(20).mean().shift(1)).to_numpy(), "vx24": (v24 / v30).to_numpy(), "ret7": c / np.r_[np.full(42, np.nan), c[:-42]] - 1, "ph": ph}


def signals(b: pd.DataFrame, ind: dict, idea: str, guard: bool) -> list[tuple[int, int, float]]:
    o, h, lo, c, fund = b.o.to_numpy(), b.h.to_numpy(), b.l.to_numpy(), b.c.to_numpy(), b.fund.to_numpy()
    m, u2, l2, u3, rsi, atr, vx4, vx24, r7, ph = (ind[k] for k in ("mean", "u2", "l2", "u3", "rsi", "atr", "vx4", "vx24", "ret7", "ph"))
    out = []
    blow = None
    piv_all = np.flatnonzero(ph)
    for i in range(200, len(c) - 1):
        if not np.isfinite(m[i]):
            continue
        near = vx24[i] >= 2 and 0.05 <= r7[i] <= 0.40
        if idea == "L1" and near and lo[i] <= l2[i] < c[i] and rsi[i] < 45:
            out.append((i, 1, lo[i] - atr[i]))
        elif idea == "L2" and near and c[i] > u2[i] and vx4[i] >= 2:
            out.append((i, 1, m[i]))
        elif idea == "L3" and near and c[i] > m[i] and (c[i - 3:i] < m[i - 3:i]).all():
            out.append((i, 1, lo[i - 5:i + 1].min() - 0.5 * atr[i]))
        if idea.startswith("S") and r7[i] >= 0.20:
            st = None
            if idea == "S1" and h[i - 2:i + 1].max() >= u3[i] and c[i] < u2[i] and rsi[i] > 60:
                st = h[i - 2:i + 1].max() + atr[i]
            elif idea == "S2":
                q = int(np.searchsorted(piv_all, i - 3, "right"))      # pivots are confirmed 3 bars later
                piv = piv_all[max(0, q - 2):q]
                if len(piv) >= 2 and h[piv[-1]] < h[piv[-2]] and c[i] < m[i] and c[i] < o[i] and i - piv[-1] <= 6:
                    st = h[piv[-1]] + atr[i]
            elif idea == "S3":
                rng = h[i] - lo[i]
                if vx4[i] >= 5 and rng > 0 and (h[i] - max(o[i], c[i])) / rng >= 0.5:
                    blow = (i, lo[i], h[i])
                if blow and i - blow[0] <= 6 and i > blow[0] and c[i] < blow[1]:
                    st, blow = blow[2] + 0.5 * atr[i], None
            if st is not None:
                if guard and (fund[i] < 0 or vx4[i] >= 3 or (st / c[i] - 1) > 0.15):
                    continue
                out.append((i, -1, st))
    return out


def trade(b: pd.DataFrame, ind: dict, i: int, side: int, stop: float, exit_: str, slip: float) -> tuple[float, int, float] | None:
    o, h, lo, c, fund = b.o.to_numpy(), b.h.to_numpy(), b.l.to_numpy(), b.c.to_numpy(), b.fund.to_numpy()
    e = i + 1
    entry = o[e] * (1 + side * slip / 1e4)
    if (entry - stop) * side <= 0:
        return None
    st, best, funding, mae = stop, entry, 0.0, 0.0
    for k in range(e, min(len(c), e + 30)):
        funding += (fund[k] if np.isfinite(fund[k]) else 0.0) / 2      # an 8-hour rate, 4 hours held
        mae = max(mae, ((h[k] / entry - 1) if side < 0 else (1 - lo[k] / entry)) * 100)
        if (side > 0 and lo[k] <= st) or (side < 0 and h[k] >= st):
            px = st if k == e else (min(o[k], st) if side > 0 else max(o[k], st))
            return ((px * (1 - side * slip / 1e4)) / entry - 1) * 1e4 * side - 2 * FEE - side * funding * 1e4, k, mae
        if exit_ == "mean":
            tgt = ind["u2"][k] if side > 0 else ind["mean"][k]
            if np.isfinite(tgt) and ((side > 0 and h[k] >= tgt > entry) or (side < 0 and lo[k] <= tgt < entry)):
                return ((tgt * (1 - side * slip / 1e4)) / entry - 1) * 1e4 * side - 2 * FEE - side * funding * 1e4, k, mae
        best = max(best, c[k]) if side > 0 else min(best, c[k])
        st = max(st, best - 3 * ind["atr"][k]) if side > 0 else min(st, best + 3 * ind["atr"][k])
    k = min(len(c) - 1, e + 29)
    return ((c[k] * (1 - side * slip / 1e4)) / entry - 1) * 1e4 * side - 2 * FEE - side * funding * 1e4, k, mae


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    return {"trades": int(len(r)), "mean_net_pct": float(r.mean() / 100), "median_net_pct": float(np.median(r) / 100),
            "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((r > 0).mean())}


def main() -> dict:
    bars = load4h()
    t_all = pd.concat([b[["time", "turn"]].assign(sym=s) for s, b in bars.items()])
    day = t_all.time // 86400
    top20 = t_all.groupby([day, "sym"]).turn.sum().groupby(level=0, group_keys=False).nlargest(20)
    liquid = {(int(d), s) for d, s in top20.index}
    rows: dict[str, list] = {}
    for L in (30, 60):
        inds = {s: indicators(b, L) for s, b in bars.items()}
        for idea in ("L1", "L2", "L3", "S1", "S2", "S3"):
            for guard in ((False,) if idea.startswith("L") else (False, True)):
                for ex in ("mean", "trail"):
                    name = f"{idea}|L={L}|exit={ex}" + (f"|guard={'on' if guard else 'off'}" if idea.startswith("S") else "")
                    acc = rows.setdefault(name, [])
                    for s, b in bars.items():
                        free = -1
                        for i, side, stop in signals(b, inds[s], idea, guard):
                            if i <= free:
                                continue
                            slip = 5.0 if (int(b.time.iloc[i] // 86400), s) in liquid else 20.0
                            r = trade(b, inds[s], i, side, stop, ex, slip)
                            if r:
                                acc.append((int(b.time.iloc[i]), s, r[0], r[2], slip == 5.0))
                                free = r[1]
    frames = {k: pd.DataFrame(v, columns=["t", "symbol", "bps", "mae_pct", "liquid"]).sort_values("t") for k, v in rows.items()}
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in frames.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"coins": len(bars), "variants_added": len(frames), "cuts": [str(pd.Timestamp(int(c1), unit="s").date()), str(pd.Timestamp(int(c2), unit="s").date())], "variants": {}, "chosen": {}}
    for k, d in frames.items():
        res["variants"][k] = {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy()),
                              "median_adverse_move_pct": float(d.mae_pct.median()) if len(d) else None, "share_adverse_over_15pct": float((d.mae_pct > 15).mean()) if len(d) else None}
    for idea in ("L1", "L2", "L3", "S1", "S2", "S3"):
        names = [k for k in frames if k.startswith(idea + "|") and res["variants"][k]["validation"].get("trades", 0) >= 30]
        if not names:
            continue
        ch = max(names, key=lambda k: res["variants"][k]["validation"]["mean_net_pct"])
        h = frames[ch][frames[ch].t >= c2]
        lo, hi = boot_ci(h.bps.to_numpy()) if len(h) > 4 else (None, None)
        res["chosen"][idea] = {"variant": ch, "holdout": {**stats(h.bps.to_numpy()), "boot90_pct": [None if lo is None else lo / 100, None if hi is None else hi / 100]},
                               "holdout_liquid_only": stats(h[h.liquid].bps.to_numpy()), "holdout_median_adverse_pct": float(h.mae_pct.median()) if len(h) else None}
    (REPORTS / "delta_runs").mkdir(parents=True, exist_ok=True)
    (REPORTS / "delta_runs" / "slow_lab.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    print(o["coins"], o["cuts"])
    for k, v in o["variants"].items():
        print(f"{k:34s} train {v['train'].get('mean_net_pct', 0):6.2f}% ({v['train']['trades']}) val {v['validation'].get('mean_net_pct', 0):6.2f}% ({v['validation']['trades']}) MAE med {v['median_adverse_move_pct'] or 0:.1f}%")
    for idea, c in o["chosen"].items():
        print("HOLDOUT", idea, c["variant"], c["holdout"], "liquid", c["holdout_liquid_only"])
