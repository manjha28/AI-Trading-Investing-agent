"""Follow-up: does filling stops/targets EXACTLY at the barrier (the Phase 0 convention) manufacture positive gross expectancy for random entries?
A driftless path with absorbing barriers has expected terminal R = 0 (optional stopping). Exact-barrier fills ignore overshoot: when a bar OPENS beyond the stop the
real exit is the open (worse than -1R). Here, a bar that opens beyond the barrier is filled at its open (stop side AND target side), everything else unchanged.
Writes diagnostics/phase0_gap_fill_check.json. Measurement only."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from phase0_base_rates import DISTS, EQ, EQ_WINDOW, FU, FU_WINDOW  # noqa: E402
from phase0_common import build_sessions, equity_rows, futures_contract, futures_rows  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session as Db  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import barrier_study as bs  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase0_gap_fill_check.json"
CELLS = [(0.005, 1.0, 240), (0.003, 2.0, 60), (0.012, 3.0, 240), (0.002, 1.5, 30), (0.001, 1.5, 15)]
log = lambda *a: print(*a, flush=True)  # noqa: E731


def both_R(part: bs.SessionArrays, cat: dict, s: float, r: float, H: int, d: int) -> tuple[np.ndarray, np.ndarray]:
    """(R with exact-barrier fills, R with gap-through fills) per entry for direction d."""
    xs, xt = round(s, 10), round(s * r, 10)
    ts, tt = (cat["down"][xs], cat["up"][xt]) if d > 0 else (cat["up"][xs], cat["down"][xt])
    vl = cat[("vl", H)]
    loss, win = (ts < vl) & (ts <= tt), (tt < vl) & (tt < ts)
    to = ~(loss | win)
    E, xc = cat["E"], cat[("xc", H)]
    with np.errstate(invalid="ignore", divide="ignore"):
        r_to = d * (xc - E) / (s * E)
        o = part.o_abs
        i_s = np.clip(part.e + np.minimum(ts, 10**6), 0, bs.SLOTS - 1)
        i_t = np.clip(part.e + np.minimum(tt, 10**6), 0, bs.SLOTS - 1)
        r_loss_gap = d * (o[i_s] - E) / (s * E)            # exit at the touching bar's open when that open is already beyond the stop
        r_win_gap = d * (o[i_t] - E) / (s * E)
        stop_lvl, tgt_lvl = E * (1 - d * s), E * (1 + d * xt)
        beyond_stop = (o[i_s] < stop_lvl) if d > 0 else (o[i_s] > stop_lvl)
        beyond_tgt = (o[i_t] > tgt_lvl) if d > 0 else (o[i_t] < tgt_lvl)
    exact = np.where(win, r, 0.0) + np.where(loss, -1.0, 0.0) + np.where(to, np.nan_to_num(r_to), 0.0)
    gap = (np.where(win, np.where(beyond_tgt, r_win_gap, r), 0.0) + np.where(loss, np.where(beyond_stop, np.minimum(r_loss_gap, -1.0), -1.0), 0.0)
           + np.where(to, np.nan_to_num(r_to), 0.0))
    return exact, gap


def run(label: str, syms, loader):
    acc = {c: {d: [0, 0.0, 0.0, 0, 0] for d in (1, -1)} for c in CELLS}     # n, sum exact, sum gap, gaps on stop, gaps on target
    for sym in syms:
        for sess in loader(sym):
            part = bs.SessionArrays(sess, DISTS)
            cat = bs.concat_sessions([part], DISTS)
            ok = cat["entry_ok"]
            for c in CELLS:
                for d in (1, -1):
                    ex, gp = both_R(part, cat, *c, d)
                    a = acc[c][d]
                    a[0] += int(ok.sum()); a[1] += float(ex[ok].sum()); a[2] += float(gp[ok].sum())
    log(f"\n{label}: average of long and short, GROSS expectancy (R) per random entry")
    log(f"  {'cell':<18}{'exact-barrier fills':>21}{'gap-through fills':>20}{'overshoot effect':>18}   long exact/gap   short exact/gap")
    out = {}
    for c in CELLS:
        n = acc[c][1][0]
        ex = (acc[c][1][1] + acc[c][-1][1]) / (2 * n); gp = (acc[c][1][2] + acc[c][-1][2]) / (2 * n)
        log(f"  {c[0] * 100:.2f}%/r{c[1]:g}/H{c[2]:<5}{ex:>+21.4f}{gp:>+20.4f}{gp - ex:>+18.4f}   {acc[c][1][1] / n:+.4f}/{acc[c][1][2] / n:+.4f}   {acc[c][-1][1] / n:+.4f}/{acc[c][-1][2] / n:+.4f}")
        out[f"{c[0] * 100:.2f}%/r{c[1]:g}/H{c[2]}"] = {"n": n, "gross_exact": ex, "gross_gap_fill": gp, "long": [acc[c][1][1] / n, acc[c][1][2] / n], "short": [acc[c][-1][1] / n, acc[c][-1][2] / n]}
    return out


def main() -> None:
    eng = create_engine(settings.database_url)
    with Db(eng) as db:
        res = {"equity": run("EQUITY CASH", EQ, lambda s: build_sessions(equity_rows(db, s, *EQ_WINDOW))[0]),
               "futures": run("FUTURES", FU, lambda u: build_sessions(futures_rows(db, futures_contract(db, u).id, *FU_WINDOW))[0])}
    OUT.write_text(json.dumps(res, indent=1))
    log(f"saved {OUT}")


if __name__ == "__main__":
    main()
