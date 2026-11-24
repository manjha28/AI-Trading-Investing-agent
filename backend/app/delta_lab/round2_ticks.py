"""R6 (liquidation-cascade fade) and R0 (order-flow control) on Binance aggTrades reduced to 1-second bars (2024-01 on; the raw tick files are not kept).
Mark price is not available at 1 s, so the last price stands in for it (declared). Triggers are evaluated every second; R0 takes one signal every 5 minutes
(the 4-losses-in-a-row daily lock makes it trade only a few times a day regardless). Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import families
from .engine import Leg, Market, Signal, simulate
from .round2 import BIN

GRID_R6 = [(s, m, h) for s in (4, 6) for m in (0.3, 0.6) for h in (30, 90)]


def month_market(sym: str, delta_sym: str, month: str, minute_market: Market) -> Market | None:
    d = BIN / f"{sym}_1s"
    frames = [pd.read_pickle(p) for p in sorted(d.glob(f"{month}-*.pkl"))]
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates("time").sort_values("time")
    t0, t1 = int(df["time"].iloc[0]) // 86400 * 86400, (int(df["time"].iloc[-1]) // 86400 + 1) * 86400
    grid = pd.RangeIndex(t0, t1)
    df = df.set_index("time").reindex(grid)
    c = df["close"].ffill().bfill()
    o = df["open"].fillna(c); h = df["high"].fillna(c); lo = df["low"].fillna(c)
    vol = df["volume"].fillna(0.0).to_numpy(float)
    spec, tick, cv = families.spec_from_json(delta_sym)
    t = grid.to_numpy(np.int64)
    m = Market(delta_sym, spec, tick, cv, t, o.to_numpy(float), h.to_numpy(float), lo.to_numpy(float), c.to_numpy(float), vol, o.to_numpy(float), h.to_numpy(float), lo.to_numpy(float), c.to_numpy(float),
               minute_market.funding)
    pos = np.clip(np.searchsorted(minute_market.t, t, side="right") - 1, 0, len(minute_market.t) - 1)
    m.atr_1h, m.atr_4h = minute_market.atr_1h[pos], minute_market.atr_4h[pos]
    m.buy = df["buy"].fillna(0.0).to_numpy(float)
    m.sell = df["sell"].fillna(0.0).to_numpy(float)
    m.minute_pos = pos
    return m


def r6_signals(m: Market, mm: Market, sig_k: int, mult: float, hold_min: int) -> list[Signal]:
    c = m.c
    ret60 = np.full(len(c), np.nan); ret60[60:] = c[60:] / c[:-60] - 1
    cm = pd.Series(mm.c)
    sigma_min = cm.pct_change().rolling(1440).std().shift(1).to_numpy()
    avg_vol_min = pd.Series(mm.v).rolling(1440).mean().shift(1).to_numpy()
    sigma, avgv = sigma_min[m.minute_pos], avg_vol_min[m.minute_pos]
    cs = np.concatenate([[0.0], np.cumsum(m.v)])
    vol60 = np.full(len(c), np.nan); vol60[60:] = cs[61:] - cs[1:-59] if len(c) > 60 else np.nan
    vol60 = np.full(len(c), np.nan); vol60[60:] = cs[60:len(c)] - cs[0:len(c) - 60]
    cond = (np.abs(ret60) > sig_k * sigma) & (vol60 > 5 * avgv) & np.isfinite(sigma) & np.isfinite(avgv)
    out, last = [], -10**9
    for i in np.flatnonzero(cond):
        if i - last < 300 or i + hold_min * 60 + 400 >= len(c):
            continue
        last = i
        move = c[i] - c[i - 60]
        s = float(sigma[i])
        if move > 0:
            entry = c[i] * (1 + mult * s); leg = Leg("short", entry, entry + 1.5 * abs(move), c[i] - 0.5 * abs(move))
        else:
            entry = c[i] * (1 - mult * s); leg = Leg("long", entry, entry - 1.5 * abs(move), c[i] + 0.5 * abs(move))
        out.append(Signal(int(i), "R6", f"R6|sigma={sig_k}|m={mult}|hold={hold_min}", [leg], "limit", valid_bars=300, max_hold=hold_min * 60))
    return out


def r0_signals(m: Market) -> list[Signal]:
    flow10 = np.full(len(m.c), np.nan)
    cb, cs_ = np.concatenate([[0.0], np.cumsum(m.buy)]), np.concatenate([[0.0], np.cumsum(m.sell)])
    flow10[10:] = (cb[10:len(m.c)] - cb[:len(m.c) - 10]) - (cs_[10:len(m.c)] - cs_[:len(m.c) - 10])
    out = []
    for i in range(300, len(m.c) - 120, 300):
        a = m.atr_1h[i]
        if not np.isfinite(a) or a <= 0 or flow10[i] == 0 or not np.isfinite(flow10[i]):
            continue
        px = m.c[i]
        out.append(Signal(i, "R0", "R0|order_flow_10s", [Leg("long", px, px - a, None) if flow10[i] > 0 else Leg("short", px, px + a, None)], "market", max_hold=60))
    return out


def run_tick_families(markets: dict, log) -> dict:
    from .round2_run import BASE, CFG, STRESSES, deflated, exec_metrics, daily_pnl, by_quarter, quarter, quarters_between, score, summarize
    out: dict = {"R6": {}, "R0": {}}
    for sym, mm in markets.items():
        files = sorted((BIN / f"{'BTCUSDT' if sym == 'BTCUSD' else 'ETHUSDT'}_1s").glob("*.pkl"))
        if len(files) < 400:
            log(f"{sym}: only {len(files)} days of 1-second bars; tick families skipped")
            continue
        months = sorted({f.name[:7] for f in files})
        cut = int(len(months) * 0.75)
        train_months, hold_months = months[:cut], months[cut:]
        ticker = "BTCUSDT" if sym == "BTCUSD" else "ETHUSDT"
        variants = {f"R6|sigma={g[0]}|m={g[1]}|hold={g[2]}": g for g in GRID_R6} | {"R0|order_flow_10s": None}
        by_var: dict[str, list[dict]] = {n: [] for n in variants}
        for mon in train_months:
            m1 = month_market(ticker, sym, mon, mm)
            if m1 is None:
                continue
            for name, g in variants.items():
                sigs = r0_signals(m1) if g is None else r6_signals(m1, mm, *g)
                tr, _r, _c = simulate(m1, sigs, BASE, CFG)
                by_var[name] += tr
            del m1
        qs = quarters_between(int(datetime.strptime(train_months[0], "%Y-%m").replace(tzinfo=timezone.utc).timestamp()), int(datetime.strptime(hold_months[0], "%Y-%m").replace(tzinfo=timezone.utc).timestamp()) - 1)
        for fam, names in (("R6", [n for n in variants if n.startswith("R6")]), ("R0", ["R0|order_flow_10s"])):
            folds, oos = [], []
            for f in range(0, len(qs) - 4):
                train, test = qs[f:f + 4], qs[f + 4]
                sc = {n: score(by_var[n], train) for n in names}
                best = max(sc, key=sc.get)
                sel = [t for t in by_var[best] if quarter(t["entry_time"]) == test]
                oos += sel
                folds.append({"train": train, "test": test, "chosen": best, "test_net": sum(t["net"] for t in sel), "test_trades": len(sel)})
            overall = {n: score(by_var[n], qs) for n in names}
            chosen = max(overall, key=overall.get)
            g = variants[chosen]
            hold, rows = {}, {k: [] for k in STRESSES}
            counts = {k: {} for k in STRESSES}
            for mon in hold_months:
                m1 = month_market(ticker, sym, mon, mm)
                if m1 is None:
                    continue
                sigs = r0_signals(m1) if g is None else r6_signals(m1, mm, *g)
                for label, cost in STRESSES.items():
                    tr, _r, cnt = simulate(m1, sigs, cost, CFG)
                    rows[label] += tr
                    for k, v in cnt.items():
                        counts[label][k] = counts[label].get(k, 0) + v
                del m1
            for label in STRESSES:
                hold[label] = {"summary": summarize(rows[label]), "quarterly": by_quarter(rows[label]), "exec": exec_metrics(rows[label], counts[label]), "counts": counts[label], "daily": daily_pnl(rows[label]).tolist()}
            hold["maker_maker"]["trades_rows"] = rows["maker_maker"]
            b = hold["maker_maker"]
            need = 100
            qpos = list(b["quarterly"].values()) or [0.0]
            checks = {"net_positive_2x_slippage": hold["slippage_x2"]["summary"]["net"] > 0, "net_positive_30pct_maker_to_taker": hold["maker_to_taker_30pct"]["summary"]["net"] > 0,
                      "profit_factor_over_1_2": (b["summary"]["profit_factor"] or 0) > 1.2, f"trades_at_least_{need}": b["summary"]["trades"] >= need,
                      "positive_in_60pct_of_quarters": sum(1 for v in qpos if v > 0) / len(qpos) >= 0.6, "max_drawdown_under_20pct": b["summary"]["trades"] > 0 and b["summary"]["max_drawdown_pct_of_start"] < 20}
            wf = summarize(oos)
            out[fam][sym] = {"variants": len(names), "chosen": chosen, "scores": overall, "folds": folds, "walk_forward_oos": wf, "wf_quarters_positive": f"{sum(1 for f in folds if f['test_net'] > 0)}/{len(folds)}",
                             "holdout": hold, "hold_quarters": quarters_between(int(datetime.strptime(hold_months[0], '%Y-%m').replace(tzinfo=timezone.utc).timestamp()), int(mm.t[-1])), "checks": checks, "passed": all(checks.values()),
                             "note": "1-second bars from Binance aggTrades; mark price replaced by last price; WF has few folds (2024-01 on)"}
            log(f"{fam} {sym}: chosen {chosen} | WF OOS net {wf['net']:.1f} ({wf['trades']}) | holdout net {b['summary']['net']:.1f} ({b['summary']['trades']}) | passed={all(checks.values())}")
    return {k: v for k, v in out.items() if v}
