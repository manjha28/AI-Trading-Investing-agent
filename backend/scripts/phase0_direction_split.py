"""Follow-up: long-only vs short-only random-entry expectancy at the Phase 0 headline cells, plus r_timeout_mean by direction and sample-period drift.
Measurement only. Writes diagnostics/phase0_direction_split.json."""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from phase0_base_rates import DISTS, EQ, EQ_WINDOW, FU, FU_WINDOW  # noqa: E402
from phase0_common import build_sessions, equity_rows, futures_contract, futures_rows, round_trip_cost_fraction  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session as Db  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import barrier_study as bs  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase0_direction_split.json"
CELLS = {"equity": [(0.005, 1.0, 240), (0.012, 3.0, 240), (0.003, 2.0, 60)], "futures": [(0.005, 1.0, 240), (0.012, 2.6, 240), (0.003, 2.0, 60)]}
log = lambda *a: print(*a, flush=True)  # noqa: E731


def new_acc():
    return {d: {"n": 0, "w": 0, "l": 0, "t": 0, "sum_rto": 0.0, "units": []} for d in (1, -1)}


def measure(sessions, cells, f_cost):
    """Per cell, per direction: overlapping counts, timeout R, and non-overlapping (stride) unit values tagged by day for clustering."""
    out = {c: new_acc() for c in cells}
    drift = {"session_ret_mean_pct": [], "fwd_ret_by_H": {}}
    for sess in sessions:
        cat = bs.concat_sessions([bs.SessionArrays(sess, DISTS)], DISTS)
        v = ~np.isnan(sess.o)
        if v.any():
            drift["session_ret_mean_pct"].append((float(sess.c[np.flatnonzero(v).max()]) / float(sess.o[np.flatnonzero(v).min()]) - 1) * 100)
        for (s, r, H) in cells:
            ok = cat["entry_ok"]
            st = ok & cat[("st", H)]
            for d in (1, -1):
                w, l, t, rto = bs.label_cell(cat, s, r, H, d)
                a = out[(s, r, H)][d]
                a["n"] += int(ok.sum()); a["w"] += int((w & ok).sum()); a["l"] += int((l & ok).sum()); a["t"] += int((t & ok).sum())
                a["sum_rto"] += float(np.nan_to_num(rto)[t & ok].sum())
                R = np.where(w, r, 0.0) + np.where(l, -1.0, 0.0) + np.where(t, np.nan_to_num(rto), 0.0)
                a["units"].extend((sess.day, float(x)) for x in (R[st] - f_cost / s))
    return out, drift


def summarize(acc: dict, s: float, r: float, H: int, f: float) -> dict:
    rows = {}
    for d, name in ((1, "long"), (-1, "short")):
        a = acc[d]
        n = a["n"]
        pw, pl, pt = a["w"] / n, a["l"] / n, a["t"] / n
        r_to = a["sum_rto"] / a["t"] if a["t"] else 0.0
        gross = pw * r - pl + pt * r_to
        net = gross - f
        units = a["units"]
        by = defaultdict(list)
        for day, x in units:
            by[day].append(x)
        N = len(units); m = sum(x for _, x in units) / N; G = len(by)
        se = math.sqrt(sum(sum(x - m for x in v) ** 2 for v in by.values()) * G / (G - 1)) / N
        rows[name] = {"n": n, "p_win": pw, "p_loss": pl, "p_timeout": pt, "p_win_resolved": pw / (pw + pl), "r_timeout_mean": r_to, "E_R_gross": gross, "E_R_net": net,
                      "E_R_net_nonoverlap": m, "E_R_net_se_day_clustered": se, "t": m / se if se > 0 else None, "n_nonoverlap": N, "days": G}
    return rows


def run_group(label, symbols, loader, cells):
    pooled = {c: new_acc() for c in cells}
    per_symbol = {}
    drift_all = {}
    for sym in symbols:
        sessions, f_cost, extra = loader(sym)
        acc, drift = measure(sessions, cells, f_cost)
        per_symbol[sym] = {"cells": {f"{c[0] * 100:.2f}%/r{c[1]:g}/H{c[2]}": summarize(acc[c], *c, f_cost / c[0]) for c in cells},
                           "mean_session_return_pct": float(np.mean(drift["session_ret_mean_pct"])), "sessions": len(sessions), **extra}
        for c in cells:
            for d in (1, -1):
                for k in ("n", "w", "l", "t", "sum_rto"):
                    pooled[c][d][k] += acc[c][d][k]
                pooled[c][d]["units"].extend(acc[c][d]["units"])
        drift_all[sym] = per_symbol[sym]["mean_session_return_pct"]
    f_mean = float(np.mean([v["f_cost"] for v in per_symbol.values()]))
    pooled_rows = {f"{c[0] * 100:.2f}%/r{c[1]:g}/H{c[2]}": summarize(pooled[c], *c, f_mean / c[0]) for c in cells}
    log(f"\n{'=' * 130}\n{label}: long-only vs short-only random entries (every valid 1-min bar), pooled over {len(symbols)} instruments\n{'=' * 130}")
    log(f"{'cell':<18}{'dir':<7}{'n':>11}{'p_win':>8}{'p_loss':>8}{'p_to':>7}{'resolved win':>13}{'r_timeout':>10}{'E_gross':>9}{'E_net':>8}{'E_net(no) ± day-SE':>22}{'t':>7}")
    for cell, dirs in pooled_rows.items():
        for name in ("long", "short"):
            x = dirs[name]
            log(f"{cell:<18}{name:<7}{x['n']:>11,}{x['p_win']:>8.4f}{x['p_loss']:>8.4f}{x['p_timeout']:>7.3f}{x['p_win_resolved']:>13.4f}{x['r_timeout_mean']:>+10.4f}{x['E_R_gross']:>+9.4f}{x['E_R_net']:>+8.4f}"
                f"{x['E_R_net_nonoverlap']:>+14.4f} ± {x['E_R_net_se_day_clustered']:.4f}{x['t']:>7.1f}")
        gap = dirs["long"]["E_R_gross"] - dirs["short"]["E_R_gross"]
        log(f"{'':<18}{'avg':<7}{'':>11}{'':>8}{'':>8}{'':>7}{'':>13}{'':>10}{(dirs['long']['E_R_gross'] + dirs['short']['E_R_gross']) / 2:>+9.4f}   long-short gross gap {gap:+.4f} R")
    log(f"  mean session return (open->close, %) by instrument: " + ", ".join(f"{k} {v:+.3f}" for k, v in drift_all.items()) + f"   | mean {np.mean(list(drift_all.values())):+.3f}%")
    return {"pooled": pooled_rows, "per_symbol": per_symbol}


def main() -> None:
    eng = create_engine(settings.database_url)
    res = {}
    with Db(eng) as db:
        def eq_loader(sym):
            sessions, _ = build_sessions(equity_rows(db, sym, *EQ_WINDOW))
            price = float(np.nanmedian(np.concatenate([x.c for x in sessions])))
            return sessions, round_trip_cost_fraction("equity_cash", "cash", price, date(2026, 7, 15), notional=1_000_000.0)[0], {}

        def fu_loader(u):
            c = futures_contract(db, u)
            sessions, _ = build_sessions(futures_rows(db, c.id, *FU_WINDOW))
            price = float(np.nanmedian(np.concatenate([x.c for x in sessions])))
            return sessions, round_trip_cost_fraction("futures", "index_future" if c.instrument_type == "FUTIDX" else "stock_future", price, date(2026, 11, 2), units=int(c.lot_size))[0], {}

        for label, syms, ld, cells in (("EQUITY CASH", EQ, eq_loader, CELLS["equity"]), ("FUTURES", FU, fu_loader, CELLS["futures"])):
            # loaders return only (sessions, f_cost, extra); attach f_cost so group-level f can be averaged
            def wrapped(sym, ld=ld):
                s, f, e = ld(sym)
                return s, f, {**e, "f_cost": f}
            res[label] = run_group(label, syms, wrapped, cells)
    OUT.write_text(json.dumps(res, indent=1, default=lambda o: None if isinstance(o, float) and not math.isfinite(o) else str(o)))
    log(f"saved {OUT}")


if __name__ == "__main__":
    main()
