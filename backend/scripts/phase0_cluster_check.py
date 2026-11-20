"""Day-clustered re-estimate of the Phase 0 headline cells. Symbols trade the same days and move together, so pooling them as independent samples overstates certainty;
here every (symbol, entry) unit is grouped by trading DAY and the standard error is computed across days (cluster-robust). Writes diagnostics/phase0_cluster_check.json."""
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
from phase0_base_rates import DISTS, EQ, EQ_WINDOW, FU, FU_WINDOW, REF_CELL  # noqa: E402
from phase0_common import build_sessions, equity_rows, futures_contract, futures_rows, round_trip_cost_fraction  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session as Db  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import barrier_study as bs  # noqa: E402

BASE = json.loads((Path(__file__).resolve().parents[2] / "diagnostics" / "phase0_base_rates.json").read_text())


def cells_for(pool: dict) -> dict[str, tuple]:
    best = pool["best"]
    bn = max((c for c in pool["cells"] if c.get("n")), key=lambda c: c["E_R_net"])
    return {"min_required_uplift_cell": (best["s_pct"] / 100, best["r"], best["H"]), "best_E_R_net_cell": (bn["s_pct"] / 100, bn["r"], bn["H"]), "reference_cell": REF_CELL}


def clustered(units: list[tuple[date, float]]) -> dict:
    """units: (day, per-entry net R). Cluster-robust SE of the mean with clusters = days."""
    by = defaultdict(list)
    for d, x in units:
        by[d].append(x)
    N = len(units)
    m = sum(x for _, x in units) / N
    G = len(by)
    ss = sum((sum(x - m for x in v)) ** 2 for v in by.values())
    se = math.sqrt(ss * G / (G - 1)) / N if G > 1 else float("nan")
    return {"entries": N, "days": G, "mean_E_R_net": m, "se_clustered": se, "t": m / se if se and se > 0 else None, "ci95": [m - 1.96 * se, m + 1.96 * se]}


def run(label: str, symbols: list, loader, cost_fn, pool: dict) -> dict:
    out = {}
    cells = cells_for(pool)
    units: dict[str, list] = {k: [] for k in cells}
    for sym in symbols:
        sessions, f_cost = loader(sym)
        for sess in sessions:
            part = bs.SessionArrays(sess, DISTS)
            cat = bs.concat_sessions([part], DISTS)
            for name, (s, r, H) in cells.items():
                f = f_cost / s
                ok = cat["entry_ok"] & cat[("st", H)]
                if not ok.any():
                    continue
                un = np.zeros(len(ok))
                for d in (1, -1):
                    w, l, t, rto = bs.label_cell(cat, s, r, H, d)
                    un += 0.5 * (np.where(w, r, 0.0) + np.where(l, -1.0, 0.0) + np.where(t, np.nan_to_num(rto), 0.0))
                for v in un[ok]:
                    units[name].append((sess.day, float(v) - f))
    for name, cell in cells.items():
        res = clustered(units[name])
        res["cell"] = {"s_pct": cell[0] * 100, "r": cell[1], "H": cell[2]}
        out[name] = res
        print(f"{label:<9}{name:<26} s={cell[0] * 100:.2f}% r={cell[1]:g} H={cell[2]:<4} entries {res['entries']:>6,} days {res['days']:>3}  E_R_net {res['mean_E_R_net']:+.4f}  clustered SE {res['se_clustered']:.4f}  t={res['t']:.2f}  95% CI [{res['ci95'][0]:+.3f}, {res['ci95'][1]:+.3f}]", flush=True)
    return out


def main() -> None:
    eng = create_engine(settings.database_url)
    res = {}
    with Db(eng) as db:
        cache: dict = {}

        def eq_loader(sym):
            sessions, _ = build_sessions(equity_rows(db, sym, *EQ_WINDOW))
            price = float(np.nanmedian(np.concatenate([s.c for s in sessions])))
            return sessions, round_trip_cost_fraction("equity_cash", "cash", price, date(2026, 7, 15), notional=1_000_000.0)[0]

        def fu_loader(u):
            c = futures_contract(db, u)
            sessions, _ = build_sessions(futures_rows(db, c.id, *FU_WINDOW))
            price = float(np.nanmedian(np.concatenate([s.c for s in sessions])))
            return sessions, round_trip_cost_fraction("futures", "index_future" if c.instrument_type == "FUTIDX" else "stock_future", price, date(2026, 11, 2), units=int(c.lot_size))[0]

        res["equity"] = run("equity", EQ, eq_loader, None, BASE["pooled_equity"])
        res["futures"] = run("futures", FU, fu_loader, None, BASE["pooled_futures"])
        _ = cache
    (Path(__file__).resolve().parents[2] / "diagnostics" / "phase0_cluster_check.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
