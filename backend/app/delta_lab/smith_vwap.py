"""The "[Smith] VWAP Deviation +" TradingView script the owner shared (25 Nov 2026), replicated exactly and backtested as real trades on Delta data.
Hypothetical research; decision support, not investment advice.

INDICATOR (as the script computes it; defaults length 60, 2 and 3 sigma, RSI 14 with 70/30, volume filter 1.2 x SMA20, market-type filter on):
  x = log(close); mean = volume-weighted mean of x over the last 60 bars; dev = volume-weighted mean ABSOLUTE deviation of x from that mean;
  bands = exp(mean +/- 2 dev), exp(mean +/- 3 dev).
  LONG  = close crosses down through the lower 2- or 3-dev band (close <= band and previous close > band) AND RSI14 <= 30 AND volume >= 1.2 x SMA20(volume)
          AND (market not trending OR trending up); trending = |EMA21 - EMA50| > 1.5 x ATR20, direction = sign(EMA21 - EMA50).  SHORT mirrors it (RSI >= 70).
  The script's "win rate" counts a signal as a win if any close in the next 5 bars crosses the mean. No costs, no stop: that is not a profit figure.
TRADES (what the script does not do): enter at the next bar's open (taker). EXIT variants:
  E1 mean: take profit at the mean (limit, maker fee); stop at the 3-dev band (at least 10 bps from entry); cap 30 bars
  E2 five: the script's own horizon: exit at the mean if touched within 5 bars, else at the 5th bar's close; stop as E1
  E3 ratchet: stop as E1, break-even at +1R, then trail 1R behind the best price, cap 30 bars
Timeframes 1m (the owner's chart), 3m, 5m; filters as the script (all on) and with the volume filter off and the market-type filter off.
COSTS: Delta taker 0.05% + 18% GST and 1 bp slippage per taker fill; maker 0.02% + GST for the limit take-profit at the mean; 3 bps slippage on stops.
DATA: Delta India 1-minute candles (data/delta_1m), BTC, ETH, SOL and XRP (liquid coins only). SPLITS by time: train 50%, validation 25%, holdout 25%."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .delta_all import delta_load
from .round4_d2 import boot_ci
from .run import REPORTS

COINS = ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD")          # liquid coins only (owner, 25 Nov 2026)
TAKER, MAKER = 0.0005 * 1.18 * 1e4 + 1.0, 0.0002 * 1.18 * 1e4


def bars(sym: str, mins: int) -> pd.DataFrame:
    d = delta_load(sym)
    if mins == 1:
        return d.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"})
    g = d.time // (60 * mins) * (60 * mins)
    return d.groupby(g).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("volume", "sum")).reset_index()


def indicator(b: pd.DataFrame, length: int = 60) -> dict[str, np.ndarray]:
    c, v, h, lo = b.c.to_numpy(), b.v.to_numpy(), b.h.to_numpy(), b.l.to_numpy()
    x = np.log(c)
    vs = pd.Series(v)
    mean = (pd.Series(v * x).rolling(length).sum() / vs.rolling(length).sum()).to_numpy()
    dev = np.full(len(c), np.nan)
    for a in range(length - 1, len(c), 200_000):                    # volume-weighted mean |x - mean_t| over the window, in chunks
        z = min(len(c), a + 200_000)
        idx = np.arange(a, z)
        win = np.lib.stride_tricks.sliding_window_view(x, length)[idx - length + 1]
        wv = np.lib.stride_tricks.sliding_window_view(v, length)[idx - length + 1]
        with np.errstate(invalid="ignore", divide="ignore"):
            dev[a:z] = (wv * np.abs(win - mean[idx, None])).sum(1) / wv.sum(1)
    delta = np.diff(c, prepend=c[0])
    up = pd.Series(np.maximum(delta, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    dn = pd.Series(np.maximum(-delta, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()
    e21, e50 = pd.Series(c).ewm(span=21, adjust=False).mean().to_numpy(), pd.Series(c).ewm(span=50, adjust=False).mean().to_numpy()
    pc = np.r_[c[0], c[:-1]]
    atr20 = pd.Series(np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))).ewm(alpha=1 / 20, adjust=False).mean().to_numpy()
    return {"mean": np.exp(mean), "u2": np.exp(mean + 2 * dev), "l2": np.exp(mean - 2 * dev), "u3": np.exp(mean + 3 * dev), "l3": np.exp(mean - 3 * dev),
            "rsi": rsi, "vavg": vs.rolling(20).mean().to_numpy(), "trending": np.abs(e21 - e50) > 1.5 * atr20, "dir": np.sign(e21 - e50)}


def signals(b: pd.DataFrame, ind: dict, vol_filter: bool, regime: bool) -> list[tuple[int, int]]:
    c, v = b.c.to_numpy(), b.v.to_numpy()
    pc = np.r_[c[0], c[:-1]]
    l2, l3, u2, u3 = ind["l2"], ind["l3"], ind["u2"], ind["u3"]
    pl2, pl3, pu2, pu3 = (np.r_[np.nan, a[:-1]] for a in (l2, l3, u2, u3))
    volq = (v >= 1.2 * ind["vavg"]) if vol_filter else np.ones(len(c), bool)
    lok = (~ind["trending"] | (ind["dir"] > 0)) if regime else np.ones(len(c), bool)
    sok = (~ind["trending"] | (ind["dir"] < 0)) if regime else np.ones(len(c), bool)
    lt = ((c <= l2) & (pc > pl2)) | ((c <= l3) & (pc > pl3))
    st = ((c >= u2) & (pc < pu2)) | ((c >= u3) & (pc < pu3))
    longs = np.flatnonzero(lt & (ind["rsi"] <= 30) & volq & lok)
    shorts = np.flatnonzero(st & (ind["rsi"] >= 70) & volq & sok)
    return sorted([(int(i), 1) for i in longs] + [(int(i), -1) for i in shorts])


def trade(b: pd.DataFrame, ind: dict, i: int, side: int, ex: str) -> tuple[float, int, bool] | None:
    o, h, lo, c = b.o.to_numpy(), b.h.to_numpy(), b.l.to_numpy(), b.c.to_numpy()
    e = i + 1
    if e >= len(c):
        return None
    entry = o[e] * (1 + side * 1e-4)
    band3 = ind["l3"][i] if side > 0 else ind["u3"][i]
    stop = min(band3, entry * (1 - 1e-3)) if side > 0 else max(band3, entry * (1 + 1e-3))   # the 3-dev band, at least 10 bps away
    R = abs(entry - stop)
    cap = 5 if ex == "five" else 30
    best, st = entry, stop
    j = min(len(c) - 1, i + 5)                                       # the script's own "win": a close beyond the mean within 5 bars
    win = bool(np.isfinite(ind["mean"][j]) and ((side > 0 and c[i + 1:j + 1].max() > ind["mean"][j]) or (side < 0 and c[i + 1:j + 1].min() < ind["mean"][j])))
    for k in range(e, min(len(c), e + cap)):
        if (side > 0 and lo[k] <= st) or (side < 0 and h[k] >= st):
            px = st if k == e else (min(o[k], st) if side > 0 else max(o[k], st))
            return (px * (1 - side * 3e-4) / entry - 1) * 1e4 * side - TAKER - 0.0005 * 1.18 * 1e4, k, win
        m = ind["mean"][k]
        if ex in ("mean", "five") and np.isfinite(m) and ((side > 0 and h[k] >= m > entry) or (side < 0 and lo[k] <= m < entry)):
            return (m / entry - 1) * 1e4 * side - TAKER - MAKER, k, win
        if ex == "ratchet":
            best = max(best, h[k]) if side > 0 else min(best, lo[k])
            if (best - entry) * side >= R:
                st = max(st, entry, best - R) if side > 0 else min(st, entry, best + R)
    k = min(len(c) - 1, e + cap - 1)
    return (c[k] * (1 - side * 1e-4) / entry - 1) * 1e4 * side - 2 * TAKER, k, win


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    return {"trades": int(len(r)), "net_bps": float(r.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((r > 0).mean())}


def main() -> dict:
    data = {}
    for mins in (1, 3, 5):
        for sym in COINS:
            b = bars(sym, mins)
            ind = indicator(b)
            for vf, rg in ((True, True), (False, True), (True, False)):
                sig = signals(b, ind, vf, rg)
                for ex in ("mean", "five", "ratchet"):
                    name = f"{mins}m|exit={ex}|volfilter={'on' if vf else 'off'}|regime={'on' if rg else 'off'}"
                    free = -1
                    rows = data.setdefault(name, [])
                    for i, side in sig:
                        if i <= free:
                            continue
                        r = trade(b, ind, i, side, ex)
                        if r:
                            rows.append((int(b.time.iloc[i]), sym, side, r[0], r[2]))
                            free = r[1]
    frames = {k: pd.DataFrame(v, columns=["t", "symbol", "side", "bps", "script_win"]).sort_values("t") for k, v in data.items()}
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in frames.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"variants_added": len(frames), "splits": {}}
    for k, d in frames.items():
        res["splits"][k] = {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy()),
                            "script_style_win_rate_all": float(d.script_win.mean()) if len(d) else None}
    ok = [k for k in frames if res["splits"][k]["validation"].get("trades", 0) >= 50]
    ch = max(ok, key=lambda k: res["splits"][k]["validation"]["net_bps"])
    h = frames[ch][frames[ch].t >= c2]
    res["chosen"] = ch
    res["holdout"] = {**stats(h.bps.to_numpy()), "boot90": list(boot_ci(h.bps.to_numpy())), "per_symbol": {s: stats(h[h.symbol == s].bps.to_numpy()) for s in COINS}}
    base = "1m|exit=mean|volfilter=on|regime=on"
    hb = frames[base][frames[base].t >= c2]
    res["holdout_script_defaults_1m"] = {**stats(hb.bps.to_numpy()), "script_style_win_rate": float(hb.script_win.mean()) if len(hb) else None}
    (REPORTS / "delta_runs").mkdir(parents=True, exist_ok=True)
    (REPORTS / "delta_runs" / "smith_vwap.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    for k, s in o["splits"].items():
        print(f"{k:48s} train {s['train'].get('net_bps', 0):6.1f} ({s['train']['trades']})  val {s['validation'].get('net_bps', 0):6.1f} ({s['validation']['trades']}) scriptwin {s['script_style_win_rate_all'] or 0:.2f}")
    print("CHOSEN", o["chosen"], json.dumps(o["holdout"], default=str)[:600])
    print("SCRIPT DEFAULTS 1m holdout", o["holdout_script_defaults_1m"])
