"""Phase 0 base rates (measurement only; no thresholds tuned, no strategy involved). Writes diagnostics/phase0_base_rates.json and prints readable tables.

Run from backend/:  python scripts/phase0_base_rates.py
Question: across (stop width, reward ratio, horizon), does ANY barrier configuration clear Indian trading costs on this instrument, for entries taken at random?"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time as clock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from phase0_common import build_sessions, equity_rows, futures_contract, futures_rows, round_trip_cost_fraction  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session as Db  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import barrier_study as bs  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase0_base_rates.json"
EQ = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "BHARTIARTL", "ITC", "SBIN", "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR"]
FU = ["NIFTY", "BANKNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS", "BHARTIARTL", "AXISBANK"]
EQ_WINDOW = (date(2026, 1, 1), date(2026, 7, 31))
FU_WINDOW = (date(2026, 8, 1), date(2026, 11, 17))
NOTIONALS = (200_000.0, 1_000_000.0, 4_000_000.0)       # equity-cash cost sensitivity; 1,000,000 is the primary
REF_CELL = (0.0030, 2.0, 60)
DISTS = bs.distinct_distances()
log = lambda *a: print(*a, flush=True)  # noqa: E731


def inf_s(x: float) -> str:
    return "inf" if x == math.inf else f"{x:.3f}"


def best_cell(rows: list[dict]) -> dict | None:
    ok = [r for r in rows if r.get("n", 0) > 0 and r["p_win"] > 0]
    return min(ok, key=lambda r: r["required_uplift"]) if ok else None


def measure(name: str, sessions, segment: str, itype: str, price_med: float, on: date, *, units: int | None = None, notionals=(None,)) -> dict:
    parts = [bs.SessionArrays(s, DISTS) for s in sessions]
    cat = bs.concat_sessions(parts, DISTS)
    acc = bs.study_cells(cat)
    per_cost = {}
    for nt in notionals:
        frac, unv, comp = round_trip_cost_fraction(segment, itype, price_med, on, notional=nt, units=units)
        per_cost[nt if nt is not None else "1lot"] = {"cost_rt_frac": frac, "cost_rt_pct": frac * 100, "unverified": unv, "components": comp}
    primary_key = 1_000_000.0 if 1_000_000.0 in per_cost else "1lot"
    frac = per_cost[primary_key]["cost_rt_frac"]
    rows = [bs.cell_report(a, s, r, H, frac) for (s, r, H), a in acc.items()]
    sens = {}
    for k, c in per_cost.items():
        rr = [bs.cell_report(a, s, r, H, c["cost_rt_frac"]) for (s, r, H), a in acc.items()]
        b = best_cell(rr)
        sens[str(k)] = {"cost_rt_pct": c["cost_rt_pct"], "min_required_uplift": b["required_uplift"] if b else None, "cell": [b["s_pct"], b["r"], b["H"]] if b else None}
    b = best_cell(rows)
    top5 = sorted([r for r in rows if r.get("n", 0) > 0 and r["p_win"] > 0], key=lambda r: r["required_uplift"])[:5]
    best_net = max([r for r in rows if r.get("n", 0) > 0], key=lambda r: r["E_R_net"])
    return {"name": name, "entries": int(cat["entry_ok"].sum()), "sessions": len(sessions), "price_median": price_med, "cost": per_cost[primary_key], "cost_sensitivity": sens,
            "cells": rows, "best_uplift_cell": b, "top5": top5, "best_E_R_net_cell": best_net,
            "sigma_H30": bs.sigma_seasonality(cat, 30), "sigma_H60": bs.sigma_seasonality(cat, 60), "_acc": acc, "_frac": frac}


def pooled(results: list[dict]) -> dict:
    acc: dict = {}
    for res in results:
        for k, a in res["_acc"].items():
            t = acc.setdefault(k, {"n": 0, "wins": 0, "loss": 0, "timeout": 0, "sum_r_timeout": 0.0, "bucket": {x: np.zeros(bs.N_BUCKETS) for x in ("n", "w", "l", "t", "rto")},
                                   "no": {"n": 0, "sum_R": 0.0, "sum_R2": 0.0, "sum_W": 0.0, "sum_W2": 0.0}})
            for f in ("n", "wins", "loss", "timeout", "sum_r_timeout"):
                t[f] += a[f]
            for x in ("n", "w", "l", "t", "rto"):
                t["bucket"][x] += a["bucket"][x]
            for f in ("n", "sum_R", "sum_R2", "sum_W", "sum_W2"):
                t["no"][f] += a["no"][f]
    frac = statistics.mean(r["_frac"] for r in results)
    rows = [bs.cell_report(a, s, r, H, frac) for (s, r, H), a in acc.items()]
    b = best_cell(rows)
    return {"acc": acc, "cost_frac": frac, "cells": rows, "best": b, "top5": sorted([r for r in rows if r.get("n", 0) > 0 and r["p_win"] > 0], key=lambda r: r["required_uplift"])[:5]}


def cell_str(r: dict | None) -> str:
    return "—" if not r else f"s={r['s_pct']:.2f}% r={r['r']:g} H={r['H']}"


def print_group(title: str, results: list[dict], pool: dict) -> None:
    log(f"\n{'=' * 118}\n{title}\n{'=' * 118}")
    log(f"{'instrument':<12}{'sess':>5}{'entries':>9}{'cost rt%':>9}  {'MIN required uplift':>20}  {'at cell':<22}{'p_win':>7}{'p_be':>7}{'p_to':>6}{'f':>6}{'E_R_net':>9}{'uplift@UB95':>12}{'best E_R_net':>14}")
    for r in sorted(results, key=lambda x: x["best_uplift_cell"]["required_uplift"] if x["best_uplift_cell"] else 9e9):
        b = r["best_uplift_cell"]
        if not b:
            log(f"{r['name']:<12}{r['sessions']:>5}{r['entries']:>9}  (no cell with a positive win rate)")
            continue
        bn = r["best_E_R_net_cell"]
        log(f"{r['name']:<12}{r['sessions']:>5}{r['entries']:>9}{r['cost']['cost_rt_pct']:>9.4f}  {b['required_uplift']:>20.3f}  {cell_str(b):<22}{b['p_win']:>7.3f}{b['p_breakeven']:>7.3f}{b['p_timeout']:>6.2f}{b['f']:>6.3f}{b['E_R_net']:>9.3f}"
            f"{inf_s(b.get('required_uplift_if_p_win_at_upper95', math.inf)):>12}{bn['E_R_net']:>+9.3f} @{cell_str(bn)[:20]}")
    mins = [r["best_uplift_cell"]["required_uplift"] for r in results if r["best_uplift_cell"]]
    pb = pool["best"]
    log(f"\n  >>> MINIMUM required uplift across all instruments in this group: {min(mins):.3f}   |   pooled across the group: {pb['required_uplift']:.3f} at {cell_str(pb)}"
        f"  (p_win {pb['p_win']:.3f}, break-even p {pb['p_breakeven']:.3f}, E_R_net {pb['E_R_net']:+.3f}, n={pb['n']:,}, non-overlap entries={pb['n_nonoverlap']:,})")
    log("  pooled top-5 cells by lowest required uplift:")
    for t in pool["top5"]:
        log(f"     {cell_str(t):<22} uplift {t['required_uplift']:.3f}  p_win {t['p_win']:.3f}  p_be {t['p_breakeven']:.3f}  f {t['f']:.3f}  E_R_net {t['E_R_net']:+.3f}  "
            f"E_R_net(non-overlap) {t.get('E_R_net_nonoverlap', float('nan')):+.3f} ± {t.get('E_R_net_se', float('nan')):.3f}  t={t.get('E_R_net_t') if t.get('E_R_net_t') is None else round(t['E_R_net_t'], 2)}")
    pos = [r for r in pool["cells"] if r.get("n") and r["E_R_net"] > 0]
    log(f"  pooled cells with E_R_net > 0 (of {len(pool['cells'])}): {len(pos)}" + ("" if not pos else "  e.g. " + ", ".join(f"{cell_str(r)} ({r['E_R_net']:+.3f})" for r in sorted(pos, key=lambda x: -x['E_R_net'])[:3])))


def print_buckets(title: str, pool: dict, cells: list[tuple]) -> None:
    for (s, r, H) in cells:
        rows = bs.bucket_report(pool["acc"][(s, r, H)], s, r, H, pool["cost_frac"])
        log(f"\n  {title} — time of day (entry bucket) for s={s * 100:.2f}% r={r:g} H={H}  [pooled]")
        log(f"    {'bucket':<8}{'n':>10}{'p_win':>8}{'E_R_net':>10}")
        for x in rows:
            log(f"    {x['bucket']:<8}{x['n']:>10,}{x['p_win']:>8.3f}{x['E_R_net']:>+10.3f}")
        best, worst = max(rows, key=lambda x: x["E_R_net"]), min(rows, key=lambda x: x["E_R_net"])
        log(f"    best {best['bucket']} ({best['E_R_net']:+.3f}) vs worst {worst['bucket']} ({worst['E_R_net']:+.3f}); spread {best['E_R_net'] - worst['E_R_net']:.3f} R")


def print_sigma(title: str, results: list[dict], key: str) -> None:
    by: dict[str, list[float]] = {}
    for r in results:
        rows = r[key]
        if not rows:
            continue
        mean = statistics.mean(x["sigma_pct"] for x in rows)
        for x in rows:
            by.setdefault(x["bucket"], []).append(x["sigma_pct"] / mean)
    log(f"\n  {title} — {key[-3:]} return sigma seasonality (ratio to each instrument's own average, mean across instruments)")
    log("    " + "  ".join(f"{b}:{statistics.mean(v):.2f}" for b, v in sorted(by.items())))


def clean(o):
    """Strict JSON: non-finite floats -> null, numpy -> python."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if hasattr(o, "tolist"):
        return clean(o.tolist())
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return o if isinstance(o, (int, str, bool)) or o is None else str(o)


def main() -> None:
    t0 = clock.time()
    out: dict = {"generated_for": "Phase 0", "grid": {"s_pct": [x * 100 for x in bs.S_GRID], "r": list(bs.R_GRID), "H_min": list(bs.H_GRID)},
                 "method": "every valid bar, both directions, entry at the bar open, stop before target in a bar, strict touches, vertical barrier capped by session end, no filters, raw s",
                 "windows": {"equity": [str(d) for d in EQ_WINDOW], "futures": [str(d) for d in FU_WINDOW]}, "integrity": {}}
    eng = create_engine(settings.database_url)
    eq_res, fu_res = [], []
    with Db(eng) as db:
        for sym in EQ:
            rows = equity_rows(db, sym, *EQ_WINDOW)
            sessions, integ = build_sessions(rows)
            out["integrity"][f"equity:{sym}"] = integ
            if not sessions:
                log(sym, "no usable sessions"); continue
            price = float(np.nanmedian(np.concatenate([s.c for s in sessions])))
            res = measure(sym, sessions, "equity_cash", "cash", price, date(2026, 7, 15), notionals=NOTIONALS)
            eq_res.append(res)
            log(f"equity {sym}: {len(sessions)} sessions, {res['entries']:,} entries, {round(clock.time() - t0)} s")
        for u in FU:
            c = futures_contract(db, u)
            if c is None:
                log(u, "no contract"); continue
            sessions, integ = build_sessions(futures_rows(db, c.id, *FU_WINDOW))
            integ["contract"] = c.trading_symbol
            out["integrity"][f"futures:{u}"] = integ
            if not sessions:
                log(u, "no usable sessions"); continue
            price = float(np.nanmedian(np.concatenate([s.c for s in sessions])))
            res = measure(u, sessions, "futures", "index_future" if c.instrument_type == "FUTIDX" else "stock_future", price, date(2026, 11, 2), units=int(c.lot_size))
            fu_res.append(res)
            log(f"futures {u}: {len(sessions)} sessions, {res['entries']:,} entries, {round(clock.time() - t0)} s")
    peq, pfu = pooled(eq_res), pooled(fu_res)
    print_group(f"EQUITY CASH (NSE intraday/MIS) — 12 symbols, {EQ_WINDOW[0]} to {EQ_WINDOW[1]}, cost at a Rs 1,000,000 notional (verified rates)", eq_res, peq)
    print_group(f"INDEX + STOCK FUTURES — {FU_WINDOW[0]} to {FU_WINDOW[1]}, cost for 1 lot (STT 0.05% verified_secondary; stamp OWNER-STATED/unverified)", fu_res, pfu)
    log("\nEquity-cash cost sensitivity (per-symbol min required uplift at three notionals):")
    for r in eq_res:
        s = r["cost_sensitivity"]
        log(f"  {r['name']:<12}" + "   ".join(f"Rs{int(float(k)) // 1000}k: cost {v['cost_rt_pct']:.4f}% -> {v['min_required_uplift']:.3f}" for k, v in s.items()))
    for title, pool in (("EQUITY", peq), ("FUTURES", pfu)):
        print_buckets(title, pool, [(pool["best"]["s_pct"] / 100, pool["best"]["r"], pool["best"]["H"]), REF_CELL])
    for title, res in (("EQUITY", eq_res), ("FUTURES", fu_res)):
        print_sigma(title, res, "sigma_H30")
        print_sigma(title, res, "sigma_H60")
    log("\nDATA INTEGRITY")
    for k, v in out["integrity"].items():
        zv = v["zero_volume_bars"]
        log(f"  {k:<22} days stored {v['days_stored']:>3} used {v['days_used']:>3} short-excluded {v['days_excluded_short']:>3} | missing slots in used days {v['missing_slots_in_used']:>5} "
            f"| zero-volume bars {zv:>6} ({(100 * zv / v['bars']) if v['bars'] else 0:.1f}%): flat-carry {v['zero_volume_flat_carry']}, price-moved {v['zero_volume_moved']} | invalid OHLC {v['invalid_ohlc']}")
    for res in eq_res + fu_res:
        res.pop("_acc")
    out["equity"] = {r["name"]: {k: v for k, v in r.items() if k != "_frac"} for r in eq_res}
    out["futures"] = {r["name"]: {k: v for k, v in r.items() if k != "_frac"} for r in fu_res}
    for nm, pool in (("equity", peq), ("futures", pfu)):
        out[f"pooled_{nm}"] = {"cost_frac": pool["cost_frac"], "best": pool["best"], "top5": pool["top5"], "cells": pool["cells"],
                               "buckets_best": bs.bucket_report(pool["acc"][(pool["best"]["s_pct"] / 100, pool["best"]["r"], pool["best"]["H"])], pool["best"]["s_pct"] / 100, pool["best"]["r"], pool["best"]["H"], pool["cost_frac"]),
                               "buckets_reference": bs.bucket_report(pool["acc"][REF_CELL], *REF_CELL, pool["cost_frac"])}
    out["seconds"] = round(clock.time() - t0)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(clean(out), indent=1))
    log(f"\nsaved {OUT}  ({out['seconds']} s)")


if __name__ == "__main__":
    main()
