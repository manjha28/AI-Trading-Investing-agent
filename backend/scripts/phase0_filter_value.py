"""Filter-value test on VAR candidates (measurement only; no thresholds changed). Writes diagnostics/phase0_filter_value.json.

Every VAR candidate (a setup that reached its trigger bar) is followed forward with the SAME triple-barrier rules as Phase 0, entered at the open of the 1-minute bar
after its trigger bar, in its own direction. For each filter, candidates it PASSED are compared with candidates it REJECTED at matched (s, r, H); the comparison uses
non-overlapping (thinned) candidates and the generic app.services.filter_value module."""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from phase0_common import IST, OPEN_MIN, build_sessions, equity_rows, futures_rows_by_symbol, round_trip_cost_fraction  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session as Db  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import barrier_study as bs  # noqa: E402
from app.services import filter_value as fv  # noqa: E402
from app.services.var.backtest import instrument_data_from_underlying, run_backtest  # noqa: E402
from app.services.var.config import VarConfig  # noqa: E402
from app.services.var.loader import LOOKBACK_CALENDAR_DAYS, load_equity, load_underlying  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase0_filter_value.json"
EQ = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "BHARTIARTL", "ITC", "SBIN", "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR"]
CELLS = [(0.0030, 2.0, 60), (0.0050, 2.0, 120), (0.0020, 1.5, 30)]       # matched barrier cells, all taken from the Phase 0 grid
FILTERS = ["regime", "relative_volume", "sweep_extension", "first_target_rr"]
EXTRA = ["net_reward_after_costs"]                                       # a COST filter: measured for information only, never to be loosened
DISTS = bs.distinct_distances()
log = lambda *a: print(*a, flush=True)  # noqa: E731


def candidates_from(res) -> list[dict]:
    out = []
    for r in res.rejections:
        if r.get("stage") == "strategy":
            out.append({"instrument": r["instrument"], "setup": r["setup"], "direction": r["direction"], "bar_end": r["bar_end"], "states": dict(r.get("conditions", []))})
    for s in res.signals:
        st = {k: "pass" for k in ("regime", "relative_volume", "first_target_rr", "net_reward_after_costs", "vwap_slope", "vwap_side")}
        if s["setup"] == "FAILED_AUCTION_REVERSAL":
            st["sweep_extension"] = "pass"
        out.append({"instrument": s["instrument"], "setup": s["setup"], "direction": s["direction"], "bar_end": s["bar_end"], "states": st})
    return out


def label_candidates(cands: list[dict], sessions_by_inst: dict, f_by_inst: dict, cell: tuple) -> tuple[list[dict], int]:
    s, r, H = cell
    cache: dict = {}
    recs, dropped = [], 0
    for c in cands:
        sessions = sessions_by_inst.get(c["instrument"])
        be = datetime.fromisoformat(c["bar_end"]).astimezone(IST)
        if sessions is None:
            dropped += 1
            continue
        sess = sessions.get(be.date())
        slot = be.hour * 60 + be.minute - OPEN_MIN
        if sess is None or not (0 <= slot < bs.SLOTS):
            dropped += 1
            continue
        key = (c["instrument"], be.date())
        if key not in cache:
            cache[key] = bs.concat_sessions([bs.SessionArrays(sess, DISTS)], DISTS)
        cat = cache[key]
        if not cat["entry_ok"][slot]:
            dropped += 1
            continue
        w, l, t, rto = bs.label_cell(cat, s, r, H, 1 if c["direction"] == "LONG" else -1)
        R = r if w[slot] else (-1.0 if l[slot] else float(np.nan_to_num(rto[slot])))
        f = f_by_inst[c["instrument"]] / s
        recs.append({**c, "t_min": be.timestamp() / 60.0, "r_net": R - f, "win": 1.0 if w[slot] else 0.0})
    return recs, dropped


def table(title: str, recs: list[dict], H: int, names: list[str]) -> list[dict]:
    rows = fv.filter_value(recs, names, min_gap_min=float(H))
    log(f"\n  {title}")
    log(f"    {'filter':<24}{'n pass/rej (raw)':>18}{'thinned':>14}{'p_win pass':>11}{'p_win rej':>10}{'E_R_net pass':>13}{'E_R_net rej':>12}{'diff':>8}{'SE':>8}{'t':>7}  verdict")
    for r in rows:
        f = lambda x, p=3: "—" if x is None else f"{x:.{p}f}"  # noqa: E731
        log(f"    {r['filter']:<24}{str(r['n_passed_raw']) + '/' + str(r['n_rejected_raw']):>18}{str(r['n_passed']) + '/' + str(r['n_rejected']):>14}{f(r['p_win_passed']):>11}{f(r['p_win_rejected']):>10}"
            f"{f(r['E_R_net_passed'], 3):>13}{f(r['E_R_net_rejected'], 3):>12}{f(r['E_R_net_diff'], 3):>8}{f(r['E_R_net_se'], 3):>8}{f(r['E_R_net_t'], 2):>7}  {r['verdict']}")
    return rows


def main() -> None:
    result: dict = {"cells": [{"s_pct": c[0] * 100, "r": c[1], "H": c[2]} for c in CELLS], "t_bar": fv.T_BAR, "datasets": {}}
    eng = create_engine(settings.database_url)
    with Db(eng) as db:
        # ---------------- equity cash (Jan-Jul 2026, 12 symbols), variant A defaults
        start, end = date(2026, 1, 1), date(2026, 7, 31)
        loaded = [load_equity(db, s, start - timedelta(days=LOOKBACK_CALENDAR_DAYS), end) for s in EQ]
        data = [instrument_data_from_underlying(u) for u in loaded if u.series]
        log("running VAR equity-cash backtest for candidates ...")
        res_eq = run_backtest(data, VarConfig(), Decimal("2000000"), start=start, end=end)
        cands_eq = candidates_from(res_eq)
        sess_eq, f_eq = {}, {}
        for s in EQ:
            sessions, _ = build_sessions(equity_rows(db, s, start, end))
            sess_eq[f"{s}-EQ"] = {x.day: x for x in sessions}
            price = float(np.nanmedian(np.concatenate([x.c for x in sessions])))
            f_eq[f"{s}-EQ"] = round_trip_cost_fraction("equity_cash", "cash", price, date(2026, 7, 15), notional=1_000_000.0)[0]
        # ---------------- index futures
        fs, fe = date(2026, 8, 1), date(2026, 11, 17)
        floaded = [load_underlying(db, u, fs - timedelta(days=LOOKBACK_CALENDAR_DAYS), fe) for u in ("NIFTY", "BANKNIFTY")]
        fdata = [instrument_data_from_underlying(u) for u in floaded if u.series]
        log("running VAR index-futures backtest for candidates ...")
        res_fu = run_backtest(fdata, VarConfig(), Decimal("2000000"), start=fs, end=fe)
        cands_fu = candidates_from(res_fu)
        sess_fu, f_fu = {}, {}
        for sym in sorted({c["instrument"] for c in cands_fu}):
            rows = futures_rows_by_symbol(db, sym, fs, fe)
            sessions, _ = build_sessions(rows)
            sess_fu[sym] = {x.day: x for x in sessions}
            price = float(np.nanmedian(np.concatenate([x.c for x in sessions])))
            lot = next(cs.contract.lot_size for u in floaded for cs in u.series if cs.contract.symbol == sym)
            f_fu[sym] = round_trip_cost_fraction("futures", "index_future", price, date(2026, 11, 2), units=int(lot))[0]
    for name, cands, sess, fcost in (("equity_cash", cands_eq, sess_eq, f_eq), ("index_futures", cands_fu, sess_fu, f_fu)):
        log(f"\n{'=' * 120}\nFILTER VALUE — {name}: {len(cands)} VAR candidates (variant A, default thresholds, unchanged)\n{'=' * 120}")
        result["datasets"][name] = {"candidates": len(cands), "cells": {}}
        for cell in CELLS:
            recs, dropped = label_candidates(cands, sess, fcost, cell)
            s, r, H = cell
            log(f"\n[{name}] matched barrier cell s={s * 100:.2f}% r={r:g} H={H}  — labelled {len(recs)} candidates ({dropped} dropped: entry bar missing / zero volume)")
            allrows = table("all setups", recs, H, FILTERS + EXTRA)
            by_setup = {}
            for setup in sorted({x["setup"] for x in recs}):
                sub = [x for x in recs if x["setup"] == setup]
                by_setup[setup] = table(f"{setup} only ({len(sub)} candidates)", sub, H, FILTERS + EXTRA)
            result["datasets"][name]["cells"][f"s{s * 100:.2f}_r{r:g}_H{H}"] = {"labelled": len(recs), "dropped": dropped, "all": allrows, "by_setup": by_setup,
                                                                               "base": {"p_win": float(np.mean([x["win"] for x in recs])), "E_R_net": float(np.mean([x["r_net"] for x in recs]))}}
            log(f"    all candidates: p_win {np.mean([x['win'] for x in recs]):.3f}, E_R_net {np.mean([x['r_net'] for x in recs]):+.3f}  (overlapping, unfiltered base)")
    OUT.write_text(json.dumps(result, indent=1, default=lambda o: None if isinstance(o, float) and not math.isfinite(o) else str(o)))
    log(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
