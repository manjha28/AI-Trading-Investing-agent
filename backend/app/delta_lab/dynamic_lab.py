"""Dynamic entries and dynamic exits on every crypto strategy, Delta's own data only (owner, 25 Nov 2026). Hypothetical research; decision support, not advice.

ENTRY styles (applied to each strategy's raw signal, all known at the signal bar's close; fills at the next bar's open):
  N0 base | N1 confirm (the NEXT bar must close in the trade's direction, then enter at the bar after) | N2 volume (signal bar volume >= 1.5 x its
  20-bar average) | N3 momentum (RSI14 < 50 for shorts, > 50 for longs) | N4 trend (price on the trade's side of EMA200 and EMA200 sloping that way)
  | N5 = N2 + N3 + N4 together.
EXIT styles (the strategy's stop is always live; a bar touching it exits there first):
  X0 fixed (the strategy's own target; 48-bar cap) | X1 ratchet (stop to break-even at +1R, then trail 1R behind the best price; no target)
  | X2 trend-flip (exit at the close when price closes back across EMA9 against the trade: books profit OR cuts the loss on a reversal)
  | X3 momentum-flip (exit when RSI14 crosses back through 50 against the trade) | X4 volume-reversal (exit at the close of a bar that moves
  against the trade on >= 2 x average volume) | X5 = X1 + X2 + X4 (whichever comes first).
STRATEGIES (raw signals): 5 EMA alert candle (5m, 15m; both sides), VWAP band fade (5m: touch +/-2 sigma, close back inside; stop +/-3 sigma; target
  VWAP), VWAP trend pullback (5m: >= 6 bars on one side, touch VWAP, close back; stop bar extreme; target +/-1 sigma), trend rejection (15m: EMA50
  sloping, bar reaches the 20-bar swing extreme and closes back; stop beyond it; target 2R) on the 10 coins with Delta 1-minute data; dump short
  (1h: +15% in 3h on 10x volume, then first close below the pump bar's middle; stop above the peak; target the pre-pump close) and pump ride
  (15m: 10x volume, +5% bar near its high; stop the bar low; target 3R) on the 183 coins with Delta 15-minute data.
COSTS: Delta taker 0.05% + 18% GST per fill; slippage 1 bp (3 bps on stop exits) on the 10 liquid coins, 20 bps per fill on the dump/pump coins.
SPLITS by signal time per strategy: train 50%, validation 25%, holdout 25%; the entry x exit pair is chosen on validation (>= 50 trades), holdout once.
36 combinations per strategy x 7 strategy runs = 252 variants added to the running total."""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .delta_all import COINS, delta_load
from .delta_pump_intraday import OUT as D15
from .round4_d2 import boot_ci
from .run import REPORTS

FEE = 0.0005 * 1.18 * 1e4
ENTRIES = ("N0", "N1", "N2", "N3", "N4", "N5")
EXITS = ("X0", "X1", "X2", "X3", "X4", "X5")


@dataclass
class B:
    t: np.ndarray; o: np.ndarray; h: np.ndarray; l: np.ndarray; c: np.ndarray; v: np.ndarray
    ema5: np.ndarray; ema9: np.ndarray; ema50: np.ndarray; ema200: np.ndarray; rsi: np.ndarray; atr: np.ndarray; vavg: np.ndarray
    vwap: np.ndarray; sd: np.ndarray


def _ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def build(d: pd.DataFrame, secs: int) -> B | None:
    g = d.time // secs * secs
    b = d.groupby(g).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("volume", "sum")).reset_index()
    if len(b) < 400:
        return None
    t, o, h, lo, c, v = (b[k].to_numpy(float) if k != "time" else b[k].to_numpy(np.int64) for k in ("time", "o", "h", "l", "c", "v"))
    delta = np.diff(c, prepend=c[0])
    up = pd.Series(np.maximum(delta, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    dn = pd.Series(np.maximum(-delta, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()
    pc = np.r_[c[0], c[:-1]]
    atr = pd.Series(np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    day = t // 86400
    tp = (h + lo + c) / 3
    pv, vv = pd.Series(tp * v).groupby(day).cumsum().to_numpy(), pd.Series(v).groupby(day).cumsum().to_numpy()
    vw = np.where(vv > 0, pv / np.maximum(vv, 1e-12), c)
    p2 = pd.Series(tp * tp * v).groupby(day).cumsum().to_numpy()
    sd = np.sqrt(np.maximum(np.where(vv > 0, p2 / np.maximum(vv, 1e-12), 0) - vw * vw, 0))
    return B(t, o, h, lo, c, v, _ema(c, 5), _ema(c, 9), _ema(c, 50), _ema(c, 200), rsi, atr, pd.Series(v).rolling(20).mean().shift(1).to_numpy(), vw, sd)


# ---------------- raw signals: (i, side, stop, target) known at bar i's close ----------------
def sig_ema5(b: B):
    out, sell, buy = [], None, None
    for i in range(210, len(b.c) - 2):
        if sell and b.l[i] < sell[0]:
            out.append((i, -1, sell[1], None)); sell = None
        if buy and b.h[i] > buy[0]:
            out.append((i, 1, buy[1], None)); buy = None
        if b.l[i] > b.ema5[i]:
            sell = (b.l[i], b.h[i])
        if b.h[i] < b.ema5[i]:
            buy = (b.h[i], b.l[i])
    return out


def sig_vwap_fade(b: B):
    out = []
    for i in range(210, len(b.c) - 2):
        s = b.sd[i]
        if s <= 0:
            continue
        if b.h[i] >= b.vwap[i] + 2 * s and b.c[i] < b.vwap[i] + 2 * s:
            out.append((i, -1, b.vwap[i] + 3 * s, b.vwap[i]))
        elif b.l[i] <= b.vwap[i] - 2 * s and b.c[i] > b.vwap[i] - 2 * s:
            out.append((i, 1, b.vwap[i] - 3 * s, b.vwap[i]))
    return out


def sig_vwap_pull(b: B):
    out = []
    above = pd.Series(b.c > b.vwap).rolling(6).sum().shift(1).to_numpy()
    for i in range(210, len(b.c) - 2):
        s = b.sd[i]
        if s <= 0:
            continue
        if above[i] == 6 and b.l[i] <= b.vwap[i] < b.c[i]:
            out.append((i, 1, b.l[i] - 0.1 * b.atr[i], b.vwap[i] + s))
        elif above[i] == 0 and b.h[i] >= b.vwap[i] > b.c[i]:
            out.append((i, -1, b.h[i] + 0.1 * b.atr[i], b.vwap[i] - s))
    return out


def sig_rejection(b: B):
    out = []
    hi20 = pd.Series(b.h).rolling(20).max().shift(1).to_numpy()
    lo20 = pd.Series(b.l).rolling(20).min().shift(1).to_numpy()
    for i in range(210, len(b.c) - 2):
        a = 0.1 * b.atr[i]
        if b.ema50[i] < b.ema50[i - 10] and b.h[i] >= hi20[i] - a and b.c[i] < hi20[i] and b.c[i] < b.o[i]:
            out.append((i, -1, max(b.h[i], hi20[i]) + a, None))
        elif b.ema50[i] > b.ema50[i - 10] and b.l[i] <= lo20[i] + a and b.c[i] > lo20[i] and b.c[i] > b.o[i]:
            out.append((i, 1, min(b.l[i], lo20[i]) - a, None))
    return out


def sig_dump(b: B):
    out, last = [], -10**9
    med = pd.Series(b.v).rolling(72, min_periods=24).median().shift(1).to_numpy()
    n = len(b.c)
    for e in range(80, n - 8):
        if e - last < 72 or not np.isfinite(med[e]) or med[e] <= 0 or not (b.c[e] / b.c[e - 3] - 1 >= 0.15 and b.v[e] >= 10 * med[e]):
            continue
        last = e
        mid, peak = (b.h[e] + b.l[e]) / 2, b.h[e]
        for j in range(e + 1, min(n - 2, e + 7)):
            peak = max(peak, b.h[j])
            if b.c[j] < mid:
                out.append((j, -1, peak * 1.005, b.c[e - 3]))
                break
    return out


def sig_pump(b: B):
    out, last = [], -10**9
    med = pd.Series(b.v * b.c).rolling(288, min_periods=96).median().shift(1).to_numpy()
    for i in range(210, len(b.c) - 2):
        if i - last < 96 or not np.isfinite(med[i]) or med[i] <= 0:
            continue
        rng = b.h[i] - b.l[i]
        if b.v[i] * b.c[i] >= 10 * med[i] and b.c[i] / b.o[i] - 1 >= 0.05 and rng > 0 and (b.c[i] - b.l[i]) / rng >= 2 / 3:
            out.append((i, 1, b.l[i], None))
            last = i
    return out


# ---------------- entry filters and exits ----------------
def entry_ok(b: B, i: int, side: int, style: str) -> int | None:
    """Return the bar index whose OPEN is the entry, or None to skip."""
    vol = np.isfinite(b.vavg[i]) and b.v[i] >= 1.5 * b.vavg[i]
    mom = (b.rsi[i] < 50) if side < 0 else (b.rsi[i] > 50)
    trend = (b.c[i] < b.ema200[i] and b.ema200[i] < b.ema200[i - 20]) if side < 0 else (b.c[i] > b.ema200[i] and b.ema200[i] > b.ema200[i - 20])
    if style == "N0":
        return i + 1
    if style == "N1":
        j = i + 1
        ok = (b.c[j] < b.o[j]) if side < 0 else (b.c[j] > b.o[j])
        return j + 1 if ok and j + 1 < len(b.c) else None
    if style == "N2":
        return i + 1 if vol else None
    if style == "N3":
        return i + 1 if mom else None
    if style == "N4":
        return i + 1 if trend else None
    return i + 1 if (vol and mom and trend) else None


def run_trade(b: B, e: int, side: int, stop: float, target: float | None, style: str, slip_in: float, slip_stop: float) -> tuple[float, int] | None:
    n = len(b.c)
    entry = b.o[e] * (1 + side * slip_in / 1e4)
    R = (entry - stop) * side
    if R <= 0:
        return None
    tgt = target if target is not None else entry + side * 2 * R
    if (tgt - entry) * side <= 0:
        return None
    st, best = stop, entry
    ratchet = style in ("X1", "X5")
    for k in range(e, min(n, e + 48)):
        # stop first
        if (side > 0 and b.l[k] <= st) or (side < 0 and b.h[k] >= st):
            px = (min(b.o[k], st) if side > 0 else max(b.o[k], st)) if k > e else st
            return ((px * (1 - side * slip_stop / 1e4)) / entry - 1) * 1e4 * side - 2 * FEE, k
        if style == "X0" and ((side > 0 and b.h[k] >= tgt) or (side < 0 and b.l[k] <= tgt)):
            px = (max(b.o[k], tgt) if side > 0 else min(b.o[k], tgt)) if k > e else tgt
            return ((px * (1 - side * slip_in / 1e4)) / entry - 1) * 1e4 * side - 2 * FEE, k
        out = False
        if style in ("X2", "X5") and ((side > 0 and b.c[k] < b.ema9[k]) or (side < 0 and b.c[k] > b.ema9[k])) and k > e:
            out = True
        if style == "X3" and k > e and ((side > 0 and b.rsi[k] < 50 <= b.rsi[k - 1]) or (side < 0 and b.rsi[k] > 50 >= b.rsi[k - 1])):
            out = True
        if style in ("X4", "X5") and np.isfinite(b.vavg[k]) and b.v[k] >= 2 * b.vavg[k] and ((side > 0 and b.c[k] < b.o[k]) or (side < 0 and b.c[k] > b.o[k])):
            out = True
        if out:
            px = b.c[k] * (1 - side * slip_stop / 1e4)
            return (px / entry - 1) * 1e4 * side - 2 * FEE, k
        if ratchet:
            best = max(best, b.h[k]) if side > 0 else min(best, b.l[k])
            if (best - entry) * side >= R:
                st = max(st, entry, best - R) if side > 0 else min(st, entry, best + R)
    px = b.c[min(n - 1, e + 47)] * (1 - side * slip_in / 1e4)
    return (px / entry - 1) * 1e4 * side - 2 * FEE, min(n - 1, e + 47)


def run_strategy(series: dict[str, B], sigfn, slip_in: float, slip_stop: float) -> dict[str, pd.DataFrame]:
    rows: dict[str, list] = {f"{n}|{x}": [] for n in ENTRIES for x in EXITS}
    for sym, b in series.items():
        sigs = sigfn(b)
        for n in ENTRIES:
            for x in EXITS:
                free = -1
                for i, side, stop, target in sigs:
                    if i <= free:
                        continue
                    e = entry_ok(b, i, side, n)
                    if e is None or e >= len(b.c):
                        continue
                    r = run_trade(b, e, side, stop, target, x, slip_in, slip_stop)
                    if r is None:
                        continue
                    rows[f"{n}|{x}"].append((int(b.t[i]), sym, side, r[0]))
                    free = r[1]
    return {k: pd.DataFrame(v, columns=["t", "symbol", "side", "bps"]).sort_values("t") for k, v in rows.items()}


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    return {"trades": int(len(r)), "net_bps": float(r.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((r > 0).mean())}


def evaluate(data: dict[str, pd.DataFrame]) -> dict:
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in data.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    sp = {k: {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy())} for k, d in data.items()}
    ok = [k for k in sp if sp[k]["validation"].get("trades", 0) >= 50]
    out = {"cuts": [int(c1), int(c2)], "splits": sp}
    if ok:
        ch = max(ok, key=lambda k: sp[k]["validation"]["net_bps"])
        h = data[ch][data[ch].t >= c2].bps.to_numpy()
        out["chosen"], out["holdout"] = ch, {**stats(h), "boot90": list(boot_ci(h)) if len(h) > 4 else None}
        base = data["N0|X0"][data["N0|X0"].t >= c2].bps.to_numpy()
        out["holdout_base_N0_X0"] = stats(base)
    return out


def main() -> dict:
    out_dir = REPORTS / "delta_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    m1 = {s: delta_load(s) for s in COINS}
    res = {"variants_added": len(ENTRIES) * len(EXITS) * 7}
    for tf, secs in (("5m", 300), ("15m", 900)):
        ser = {s: x for s, x in ((s, build(d, secs)) for s, d in m1.items()) if x is not None}
        res[f"ema5_{tf}"] = evaluate(run_strategy(ser, sig_ema5, 1, 3))
        if tf == "5m":
            res["vwap_fade_5m"] = evaluate(run_strategy(ser, sig_vwap_fade, 1, 3))
            res["vwap_pullback_5m"] = evaluate(run_strategy(ser, sig_vwap_pull, 1, 3))
        else:
            res["rejection_15m"] = evaluate(run_strategy(ser, sig_rejection, 1, 3))
    uni = json.loads((D15 / "universe.json").read_text())
    m15 = {}
    for s in uni:
        d = pd.read_pickle(D15 / f"{s}_15m.pkl")
        if len(d) >= 96 * 20:
            m15[s] = d.astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
    ser15 = {s: x for s, x in ((s, build(d, 900)) for s, d in m15.items()) if x is not None}
    res["pump_ride_15m"] = evaluate(run_strategy(ser15, sig_pump, 20, 20))
    ser1h = {s: x for s, x in ((s, build(d, 3600)) for s, d in m15.items()) if x is not None}
    res["dump_short_1h"] = evaluate(run_strategy(ser1h, sig_dump, 20, 20))
    (out_dir / "dynamic_lab.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    for k, v in o.items():
        if isinstance(v, dict) and "chosen" in v:
            print(k, v["chosen"], {x: round(y, 2) if isinstance(y, float) else y for x, y in v["holdout"].items() if x != "boot90"}, [round(z, 1) for z in (v["holdout"]["boot90"] or [])],
                  "base", round(v["holdout_base_N0_X0"].get("net_bps", 0), 1))
