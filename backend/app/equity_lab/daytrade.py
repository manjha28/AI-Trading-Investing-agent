"""Classic day-trading rules on NSE stocks, many variations each (owner, 25 Nov 2026). Hypothetical research; decision support, not investment advice.

DATA: Dhan 1-minute history kept as 5-minute bars (app.equity_lab.fetch), 09:15-15:30 IST, about one year, the Market watch stocks. Universe per day:
the 150 most traded stocks by median daily turnover over the prior 20 sessions (liquid names only; no look-ahead).
FILLS: stop-type entries fill at the level the first 5-minute bar trades through it (at that bar's open if it gapped through), market entries at the next
bar's open. Inside a bar the stop is assumed hit before the target (conservative). Every trade is flat by 15:15 IST (MIS square-off).
COSTS (Dhan NSE intraday, app.services.transaction_costs schedule "dhan_nse_intraday_2026_11", at a 50,000 INR order): brokerage 0.03% per side (under the
Rs 20 cap), STT 0.025% on the sell side, exchange 0.00307% per side, SEBI 0.0001% per side, stamp 0.003% on the buy side, GST 18% on brokerage+exchange+SEBI
= about 10.6 bps a round trip; plus 2 bps slippage per side = about 14.6 bps.
STRATEGIES (each with its variation grid):
  ORB      opening range of the first 15 or 30 minutes; long above / short below; stop = the other side or the middle of the range; exit 1R, 2R or at 15:15;
           optional filter: range narrower than the 20-day average first-range.
  PDHL     previous day's high / low broken after 09:30; stop 0.5% or the opening-range middle; exit 2R or 15:15.
  GAP      opening gap >= 1% or 2%: FADE (short a gap up / buy a gap down at 09:20, target the previous close, stop beyond the first-5-min extreme)
           or GO (enter in the gap's direction after the first 15 minutes hold, stop at the 15-minute range's other side, exit 15:15).
  VWAP     after 10:00 with price above a rising VWAP (below a falling one), enter when a bar touches VWAP and closes back on the trend side; stop the
           bar's extreme; exit 2R or 15:15.
  NR7      (Crabel) only on days after the narrowest range of the last 7 days: ORB-30 rules.
  CAMARILLA levels from the previous day (H3/L3 = close +/- 0.275 x range, H4/L4 = +/- 0.55 x range): FADE at H3/L3 (stop H4/L4, target the
           previous close) or BREAK of H4/L4 (stop H3/L3, exit 15:15).
SPLITS by date: train 50%, validation 25%, holdout 25% (pooled over stocks); each strategy's variant is chosen on validation, holdout touched once."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.delta_lab.round4_d2 import boot_ci
from app.delta_lab.run import REPORTS

DATA = Path(__file__).resolve().parents[3] / "data" / "equity_5m"
COST_RT = 10.6 + 4.0          # bps per round trip (costs above + 2 bps slippage per side)
SQ = 15 * 60 + 15             # 15:15 IST in minutes


def load_all() -> dict[str, pd.DataFrame]:
    out = {}
    for f in sorted(DATA.glob("*.npz")):
        z = np.load(f)
        d = pd.DataFrame({k: z[k] for k in ("t", "o", "h", "l", "c", "v")})
        ts = pd.to_datetime(d.t, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        d["day"], d["mins"] = ts.dt.date, ts.dt.hour * 60 + ts.dt.minute
        out[f.stem] = d
    return out


def universe(all_: dict[str, pd.DataFrame]) -> dict:
    """{day: set of symbols} = the 150 most traded by median daily turnover over the prior 20 sessions."""
    turn = {s: (d.c * d.v).groupby(d.day).sum() for s, d in all_.items()}
    t = pd.DataFrame(turn).sort_index()
    med = t.rolling(20, min_periods=10).median().shift(1)
    return {day: set(row.dropna().sort_values(ascending=False).index[:150]) for day, row in med.iterrows()}


def _exit(d: pd.DataFrame, i0: int, side: int, entry: float, stop: float, target: float | None) -> tuple[float, str]:
    """Walk 5-minute bars from i0 (the entry bar, entry already filled) to 15:15. Returns (exit price, reason)."""
    h, lo, c, o, mins = d.h.to_numpy(), d.l.to_numpy(), d.c.to_numpy(), d.o.to_numpy(), d.mins.to_numpy()
    day = d.day.iloc[i0]
    days = d.day.to_numpy()
    for k in range(i0, len(d)):
        if days[k] != day:
            return c[k - 1], "eod"
        if mins[k] >= SQ:
            return o[k], "eod"
        if (side > 0 and lo[k] <= stop) or (side < 0 and h[k] >= stop):
            return (min(o[k], stop) if side > 0 else max(o[k], stop)) if k > i0 else stop, "stop"
        if target is not None and ((side > 0 and h[k] >= target) or (side < 0 and lo[k] <= target)):
            return (max(o[k], target) if side > 0 else min(o[k], target)) if k > i0 else target, "target"
    return c[-1], "eod"


def _bps(side: int, entry: float, px: float) -> float:
    return (px / entry - 1) * 1e4 * side - COST_RT


def day_groups(d: pd.DataFrame):
    starts = np.flatnonzero(np.r_[True, d.day.to_numpy()[1:] != d.day.to_numpy()[:-1]])
    ends = np.r_[starts[1:], len(d)]
    return list(zip(starts, ends))


def run_symbol(sym: str, d: pd.DataFrame, uni: dict) -> dict[str, list[dict]]:
    res: dict[str, list[dict]] = {}
    groups = day_groups(d)
    h, lo, c, o, v, mins = (d[k].to_numpy() for k in ("h", "l", "c", "o", "v", "mins"))
    daily = [(d.day.iloc[a], o[a], h[a:b].max(), lo[a:b].min(), c[b - 1], a, b) for a, b in groups]
    or30w = [h[a:min(b, a + 6)].max() - lo[a:min(b, a + 6)].min() for a, b in groups]
    for gi in range(8, len(daily)):
        day, op, _dh, _dl, _dc, a, b = daily[gi]
        if sym not in uni.get(day, ()) or b - a < 60:
            continue
        _pday, _po, ph, pl, pc, _pa, _pb = daily[gi - 1]
        rngs = [x[2] - x[3] for x in daily[gi - 7:gi]]
        nr7 = rngs[-1] == min(rngs)
        avg_or30 = float(np.mean(or30w[max(0, gi - 20):gi]))

        def add(name, side, i0, entry, stop, target):
            if entry <= 0 or (side > 0 and stop >= entry) or (side < 0 and stop <= entry):
                return
            px, why = _exit(d, i0, side, entry, stop, target)
            res.setdefault(name, []).append({"symbol": sym, "day": str(day), "bps": _bps(side, entry, px), "why": why, "side": side})

        # ---- ORB (and NR7) ----
        for n in (15, 30):
            nb = n // 5
            orh, orl = h[a:a + nb].max(), lo[a:a + nb].min()
            for stop_kind in ("other", "mid"):
                for ex in ("1R", "2R", "eod"):
                    for filt in ("none", "narrow"):
                        if filt == "narrow" and not (orh - orl < avg_or30 * (0.5 if n == 15 else 1.0)):
                            continue
                        name = f"ORB{n}|stop={stop_kind}|exit={ex}|filter={filt}"
                        for k in range(a + nb, b):
                            if mins[k] >= SQ:
                                break
                            if h[k] > orh:
                                entry = max(o[k], orh) * (1 + 2e-4)
                                stop = orl if stop_kind == "other" else (orh + orl) / 2
                                tgt = None if ex == "eod" else entry + float(ex[0]) * (entry - stop)
                                add(name, 1, k, entry, stop, tgt)
                                if n == 30 and stop_kind == "other" and ex == "2R" and filt == "none" and nr7:
                                    add("NR7|ORB30|stop=other|exit=2R", 1, k, entry, stop, tgt)
                                break
                            if lo[k] < orl:
                                entry = min(o[k], orl) * (1 - 2e-4)
                                stop = orh if stop_kind == "other" else (orh + orl) / 2
                                tgt = None if ex == "eod" else entry - float(ex[0]) * (stop - entry)
                                add(name, -1, k, entry, stop, tgt)
                                if n == 30 and stop_kind == "other" and ex == "2R" and filt == "none" and nr7:
                                    add("NR7|ORB30|stop=other|exit=2R", -1, k, entry, stop, tgt)
                                break
        # ---- previous day high / low ----
        or_mid = (h[a:a + 3].max() + lo[a:a + 3].min()) / 2
        for stop_kind in ("0.5pct", "ormid"):
            for ex in ("2R", "eod"):
                name = f"PDHL|stop={stop_kind}|exit={ex}"
                for k in range(a + 3, b):
                    if mins[k] >= SQ:
                        break
                    if h[k] > ph and o[a] < ph:
                        entry = max(o[k], ph) * (1 + 2e-4)
                        stop = entry * 0.995 if stop_kind == "0.5pct" else or_mid
                        add(name, 1, k, entry, stop, None if ex == "eod" else entry + 2 * (entry - stop))
                        break
                    if lo[k] < pl and o[a] > pl:
                        entry = min(o[k], pl) * (1 - 2e-4)
                        stop = entry * 1.005 if stop_kind == "0.5pct" else or_mid
                        add(name, -1, k, entry, stop, None if ex == "eod" else entry - 2 * (stop - entry))
                        break
        # ---- gaps ----
        gap = op / pc - 1
        for g in (0.01, 0.02):
            if abs(gap) >= g and b - a > 4:
                side = -1 if gap > 0 else 1                       # fade
                entry = o[a + 1] * (1 + side * 2e-4)              # 2 bps worse than the 09:20 open, either side
                stop = h[a] * 1.001 if side < 0 else lo[a] * 0.999
                add(f"GAP|fade|gap>={int(g * 100)}%", side, a + 1, entry, stop, pc)
                hold = (lo[a:a + 3].min() > op * (1 - 0.002)) if gap > 0 else (h[a:a + 3].max() < op * (1 + 0.002))
                if hold:
                    side = 1 if gap > 0 else -1                   # go
                    e = o[a + 3] * (1 + side * 2e-4)
                    st = lo[a:a + 3].min() if side > 0 else h[a:a + 3].max()
                    add(f"GAP|go|gap>={int(g * 100)}%", side, a + 3, e, st, None)
        # ---- VWAP pullback with the trend ----
        pv = np.cumsum(((h[a:b] + lo[a:b] + c[a:b]) / 3) * v[a:b])
        vv = np.cumsum(v[a:b])
        vw = np.where(vv > 0, pv / np.maximum(vv, 1e-9), np.nan)
        for ex in ("2R", "eod"):
            name = f"VWAP|pullback|exit={ex}"
            for k in range(a + 9, b - 1):
                j = k - a
                if mins[k] >= SQ:
                    break
                rising = vw[j] > vw[j - 6]
                if rising and lo[k] <= vw[j] < c[k] and c[k - 1] > vw[j - 1]:
                    entry = o[k + 1] * (1 + 2e-4)
                    add(name, 1, k + 1, entry, lo[k], None if ex == "eod" else entry + 2 * (entry - lo[k]))
                    break
                if (not rising) and h[k] >= vw[j] > c[k] and c[k - 1] < vw[j - 1]:
                    entry = o[k + 1] * (1 - 2e-4)
                    add(name, -1, k + 1, entry, h[k], None if ex == "eod" else entry - 2 * (h[k] - entry))
                    break
        # ---- Camarilla ----
        r = ph - pl
        h3, l3, h4, l4 = pc + 0.275 * r, pc - 0.275 * r, pc + 0.55 * r, pc - 0.55 * r
        for k in range(a + 1, b):
            if mins[k] >= SQ:
                break
            if h[k] >= h3 and c[k] < h3 and o[a] < h3:
                add("CAMARILLA|fade_H3L3", -1, k + 1, o[min(k + 1, b - 1)] * (1 - 2e-4), h4, pc)
                break
            if lo[k] <= l3 and c[k] > l3 and o[a] > l3:
                add("CAMARILLA|fade_H3L3", 1, k + 1, o[min(k + 1, b - 1)] * (1 + 2e-4), l4, pc)
                break
        for k in range(a + 1, b):
            if mins[k] >= SQ:
                break
            if h[k] > h4 and o[a] < h4:
                add("CAMARILLA|break_H4L4", 1, k, max(o[k], h4) * (1 + 2e-4), h3, None)
                break
            if lo[k] < l4 and o[a] > l4:
                add("CAMARILLA|break_H4L4", -1, k, min(o[k], l4) * (1 - 2e-4), l3, None)
                break
    return res


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    return {"trades": int(len(r)), "net_bps": float(r.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((r > 0).mean())}


def main() -> dict:
    all_ = load_all()
    uni = universe(all_)
    trades: dict[str, list[dict]] = {}
    for s, d in all_.items():
        for k, v in run_symbol(s, d, uni).items():
            trades.setdefault(k, []).extend(v)
    days = sorted({x["day"] for v in trades.values() for x in v})
    c1, c2 = days[int(len(days) * 0.5)], days[int(len(days) * 0.75)]
    res = {"stocks": len(all_), "days": len(days), "cost_round_trip_bps": COST_RT, "cuts": [c1, c2], "variants_added": len(trades), "strategies": {}}
    for name, rows in sorted(trades.items()):
        df = pd.DataFrame(rows)
        res["strategies"][name] = {"train": stats(df[df.day < c1].bps.to_numpy()), "validation": stats(df[(df.day >= c1) & (df.day < c2)].bps.to_numpy())}
    fams = sorted({n.split("|")[0] for n in trades})
    res["chosen"] = {}
    for fam in fams:
        cands = [n for n in trades if n.split("|")[0] == fam and res["strategies"][n]["validation"].get("trades", 0) >= 50]
        if not cands:
            continue
        ch = max(cands, key=lambda n: res["strategies"][n]["validation"]["net_bps"])
        df = pd.DataFrame(trades[ch])
        h = df[df.day >= c2].bps.to_numpy()
        res["chosen"][fam] = {"variant": ch, "holdout": {**stats(h), "boot90": list(boot_ci(h)) if len(h) > 4 else None, "long": stats(df[(df.day >= c2) & (df.side > 0)].bps.to_numpy()),
                                                         "short": stats(df[(df.day >= c2) & (df.side < 0)].bps.to_numpy())}}
    (REPORTS / "equity_daytrade.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    for n, s in o["strategies"].items():
        print(f"{n:45s} train {s['train'].get('net_bps', 0):6.1f} ({s['train']['trades']})  val {s['validation'].get('net_bps', 0):6.1f} ({s['validation']['trades']})")
    print(json.dumps(o["chosen"], default=str))
