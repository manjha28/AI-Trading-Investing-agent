"""Owner's marked shorts of 24-25 Nov 2026 (5m BTCUSD), written as a rule WITH volume, then backtested. Hypothetical research only.

What the owner's four marked shorts had in common (read from Delta's 5m candles; times UTC):
  #1 03:20 rally into 59,640 (a LOWER high under 59,729), up-bars on 1.3-1.8x volume, rejection candle closes 59,521 -> short about 59,500
  #2 04:15-04:20 rally to 59,377 (lower high), rejection close 59,245 -> short about 59,235
  #3 06:10 rally to 59,328 on 2.0x volume (lower high), rejection close 59,229 -> short about 59,294
  #4 07:05 rally to 59,392 on 2.1x volume, rejection close 59,221 -> short about 59,184; then a 6.4x-volume breakdown candle at 07:35 -> HOLD
The owner exits #1-#3 near the nearest support, but wants to stay in when a breakdown candle comes on very high volume, with the stop moved into profit.

RULE (fixed before testing; longs are NOT taken, as the owner asked, unless the trend filter flips up, see TREND):
1. TREND: on 5m bars, pivots with 3 bars each side; the last two confirmed pivot highs are falling AND the last two pivot lows are falling
   (a lower-high / lower-low staircase over roughly the last day). Shorts only in that state. (Mirror for longs = variant.)
2. SETUP: a rally bar whose high comes within 0.15 x ATR(14) of the last confirmed pivot high, or pokes above it by at most 0.5 x ATR, without
   closing above it (a failed retest / lower high). VOLUME variant: that rally bar's volume >= 1.5 x its 20-bar average (buyers spent).
3. TRIGGER: the same or the next bar closes red AND below the rally bar's midpoint. Short at the next 1-minute open.
4. STOP: rally high + 0.1 x ATR. R = stop - entry. Skip if R < 1 x round-trip cost (about 14 bps) or R > 3 x ATR.
5. EXIT: target = the last confirmed pivot low (at least 1R away, else 2R). VOLUME-HOLD variant: if a red bar with volume >= 3 x average closes
   through that target, do not exit there; move the stop to that bar's high and then trail it to each new confirmed lower pivot high.
   ADVERSE-VOLUME variant: exit at the close of any green bar with volume >= 2 x average that closes above the entry.
   Hard time stop: 1 day.
DATA: Binance USD-M 1-minute klines (BTC, ETH), Jan 2023 to Nov 2026, the same data as today's other tests (the holdout is NOT fresh).
COSTS: Delta taker 0.05% + 18% GST per fill, slippage 1 bp entry/target, 3 bps stop/trail/adverse exits.
SPLITS: by entry time, pooled: train 50%, validation 25%, holdout 25%; choose on validation, holdout once."""
from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
import pandas as pd

from .five_ema_exits import FEE, load as _load, resample
from .round4_d2 import boot_ci
from .run import REPORTS

load = lru_cache(maxsize=4)(_load)
SYMBOLS = ("BTCUSD", "ETHUSD")
K = 3
COST_RT = 2 * FEE + 4.0


def bars5(sym: str) -> pd.DataFrame:
    m = load(sym)
    g = (m["time"] // 300) * 300
    b = m.groupby(g).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum"), n=("time", "size")).reset_index().rename(columns={"time": "t"})
    return b[b["n"] == 5].reset_index(drop=True)


def run(sym: str, vol_setup: bool, vol_hold: bool, adverse: bool, longs: bool) -> list[dict]:
    m = load(sym)
    t1, o1, h1, l1, c1 = (m[k].to_numpy() for k in ("time", "open", "high", "low", "close"))
    idx1 = {int(x): i for i, x in enumerate(t1)}
    b = bars5(sym)
    t, o, h, lo, c, v = (b[k].to_numpy() for k in ("t", "open", "high", "low", "close", "volume"))
    n = len(b)
    pc = np.r_[c[0], c[:-1]]
    atr = pd.Series(np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    vavg = pd.Series(v).rolling(20).mean().shift(1).to_numpy()
    ph = np.zeros(n, bool); pl = np.zeros(n, bool)
    for i in range(K, n - K):
        ph[i] = h[i] == h[i - K:i + K + 1].max(); pl[i] = lo[i] == lo[i - K:i + K + 1].min()
    ph_i, pl_i = np.flatnonzero(ph), np.flatnonzero(pl)
    out, busy = [], -1
    for j in range(40, n - 2):
        for trig in (j, j + 1):                                         # trigger on the rally bar itself or the next bar
            if trig <= busy:
                continue
            known = trig - K                                             # pivots confirmed by the trigger bar's close
            lim = min(known, j - 1)
            nh, nl = int(np.searchsorted(ph_i, lim, "right")), int(np.searchsorted(pl_i, lim, "right"))
            a_h, a_l = ph_i[max(0, nh - 2):nh], pl_i[max(0, nl - 2):nl]
            if len(a_h) < 2 or len(a_l) < 2:
                break
            down = h[a_h[-1]] < h[a_h[-2]] and lo[a_l[-1]] < lo[a_l[-2]]
            up = h[a_h[-1]] > h[a_h[-2]] and lo[a_l[-1]] > lo[a_l[-2]]
            side = "short" if down else "long" if (up and longs) else None
            if side is None or not np.isfinite(vavg[j]):
                break
            A = atr[j]
            if side == "short":
                lvl = h[a_h[-1]]
                setup = (lvl - 0.15 * A <= h[j] <= lvl + 0.5 * A) and c[j] < lvl
                fired = c[trig] < o[trig] and c[trig] < (h[j] + lo[j]) / 2
            else:
                lvl = lo[a_l[-1]]
                setup = (lvl - 0.5 * A <= lo[j] <= lvl + 0.15 * A) and c[j] > lvl
                fired = c[trig] > o[trig] and c[trig] > (h[j] + lo[j]) / 2
            if not setup:
                break
            if vol_setup and v[j] < 1.5 * vavg[j]:
                break
            if not fired:
                continue
            k = idx1.get(int(t[trig]) + 300)
            if k is None:
                break
            sg = -1 if side == "short" else 1
            entry = o1[k] * (1 - sg * 1e-4)
            stop = (max(h[j], h[trig]) + 0.1 * A) if side == "short" else (min(lo[j], lo[trig]) - 0.1 * A)
            R = (stop - entry) * -sg
            if R <= 0 or R / entry * 1e4 < COST_RT or R > 3 * A:                    # stop at least one round-trip cost away (the owner's marked trades had stops of 6-35 bps)
                break
            sup = lo[a_l[-1]] if side == "short" else h[a_h[-1]]
            room = (entry - sup) if side == "short" else (sup - entry)
            target = sup if room >= R else entry + sg * 2 * R
            res = manage(side, entry, stop, target, trig, k, t, o, h, lo, c, v, vavg, ph_i, pl_i, t1, o1, h1, l1, c1, vol_hold, adverse)
            res.update(symbol=sym, side=side, t=int(t[trig]) + 300, R_bps=R / entry * 1e4)
            out.append(res)
            busy = res["exit_bar"]
            break
    return out


def manage(side, entry, stop, target, trig, k, t, o, h, lo, c, v, vavg, ph_i, pl_i, t1, o1, h1, l1, c1, vol_hold, adverse):
    sg = -1 if side == "short" else 1
    R = (stop - entry) * -sg
    st, trailing = stop, False
    last_bar = min(len(t) - 1, trig + 288)
    for bi in range(trig + 1, last_bar + 1):
        k0 = int(np.searchsorted(t1, t[bi]))
        for x in range(k0, min(len(t1), k0 + 5)):
            if (side == "short" and h1[x] >= st) or (side == "long" and l1[x] <= st):
                px = max(o1[x], st) if side == "short" else min(o1[x], st)
                return _res(entry, px, sg, 3, "trail" if trailing else "stop", bi, R)
            if not trailing and ((side == "short" and l1[x] <= target) or (side == "long" and h1[x] >= target)):
                if not vol_hold:
                    px = min(o1[x], target) if side == "short" else max(o1[x], target)
                    return _res(entry, px, sg, 1, "target", bi, R)
                break                                                    # with volume-hold the decision waits for this bar's close
        # bar close decisions
        big = np.isfinite(vavg[bi]) and v[bi] >= 3 * vavg[bi]
        through = (c[bi] <= target) if side == "short" else (c[bi] >= target)
        touched = (lo[bi] <= target) if side == "short" else (h[bi] >= target)
        if vol_hold and not trailing and touched:
            if through and big and ((c[bi] < o[bi]) if side == "short" else (c[bi] > o[bi])):
                trailing, st = True, (h[bi] if side == "short" else lo[bi])
            else:
                return _res(entry, c[bi], sg, 1, "target", bi, R)
        if adverse and np.isfinite(vavg[bi]) and v[bi] >= 2 * vavg[bi] and ((c[bi] > o[bi] and c[bi] > entry) if side == "short" else (c[bi] < o[bi] and c[bi] < entry)):
            return _res(entry, c[bi], sg, 3, "adverse_volume", bi, R)
        if trailing:
            piv = ph_i if side == "short" else pl_i
            q = int(np.searchsorted(piv, bi - K, "right"))
            if q and piv[q - 1] > trig:
                p = (h if side == "short" else lo)[piv[q - 1]]
                st = min(st, p) if side == "short" else max(st, p)
    return _res(entry, c[last_bar], sg, 1, "time", last_bar, R)


def _res(entry, px, sg, slip, why, bar, R):
    fill = px * (1 - sg * slip * 1e-4)
    return {"bps": (fill - entry) / entry * 1e4 * sg - 2 * FEE, "r_mult": (fill - entry) * sg / R, "why": why, "exit_bar": bar}


def stats(b: np.ndarray) -> dict:
    if len(b) == 0:
        return {"trades": 0}
    w, l = b[b > 0], b[b <= 0]
    eq = np.r_[0, np.cumsum(b)]
    return {"trades": int(len(b)), "net_bps": float(b.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((b > 0).mean()),
            "max_dd_bps": float((np.maximum.accumulate(eq) - eq).max())}


def main() -> dict:
    grid = [(vs, vh, adv, lg) for vs in (False, True) for vh in (False, True) for adv in (False, True) for lg in (False, True)]
    data = {}
    for vs, vh, adv, lg in grid:
        name = f"volsetup={'on' if vs else 'off'}|volhold={'on' if vh else 'off'}|adverse={'on' if adv else 'off'}|longs={'on' if lg else 'off'}"
        rows = []
        for s in SYMBOLS:
            rows += run(s, vs, vh, adv, lg)
        data[name] = pd.DataFrame(rows).sort_values("t")
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in data.values()]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"variants_added": len(grid), "cuts": [int(c1), int(c2)], "splits": {}}
    for name, d in data.items():
        res["splits"][name] = {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy())}
    ok = lambda n: res["splits"][n]["validation"].get("trades", 0) >= 30                 # noqa: E731
    chosen = max((n for n in data if ok(n)), key=lambda n: res["splits"][n]["validation"]["net_bps"])
    h = data[chosen][data[chosen].t >= c2]
    res["chosen"] = chosen
    res["holdout"] = {**stats(h.bps.to_numpy()), "boot90": list(boot_ci(h.bps.to_numpy())), "per_symbol": {s: stats(h[h.symbol == s].bps.to_numpy()) for s in SYMBOLS},
                      "exits": h.why.value_counts().to_dict(), "avg_r": float(h.r_mult.mean()), "median_R_bps": float(h.R_bps.median())}
    (REPORTS / "lower_high_volume.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    r = main()
    for nme, s in r["splits"].items():
        print(f"{nme:58s} train {s['train'].get('net_bps', 0):6.1f} ({s['train']['trades']})  val {s['validation'].get('net_bps', 0):6.1f} ({s['validation']['trades']}) pf {s['validation'].get('pf') or 0:.2f}")
    print("CHOSEN", r["chosen"]); print(json.dumps(r["holdout"], default=str))
