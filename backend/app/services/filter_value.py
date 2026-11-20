"""Filter-value test, reusable across strategies: does a filter's pass/reject split actually separate forward outcomes?

Input records (one per candidate):
    {"states": {"<filter>": "pass" | "fail" | other}, "instrument": str, "t_min": float, "r_net": float, "win": 0|1}
``other`` states (data unavailable, not applicable) are excluded from BOTH groups for that filter.

For each filter the PASSED and REJECTED groups are compared on win rate and mean net R. Candidates cluster in time, so each group is first thinned to
NON-OVERLAPPING candidates (per instrument, at least ``min_gap_min`` minutes apart, earliest first) and every statistic uses the thinned sample.
Difference = passed - rejected; SE is Welch's; the t-statistic is difference / SE. Nothing here changes any threshold.

Verdict (|t| >= 2 is the bar, stated in the output): ``earns_its_rejections`` (passed better), ``no_separation`` (cannot tell), ``rejected_outperformed`` (the filter removes the better trades)."""
from __future__ import annotations

import math
from typing import Iterable

T_BAR = 2.0


def thin(records: list[dict], min_gap_min: float) -> list[dict]:
    last: dict[str, float] = {}
    kept = []
    for r in sorted(records, key=lambda x: (x["instrument"], x["t_min"])):
        t = last.get(r["instrument"])
        if t is None or r["t_min"] - t >= min_gap_min:
            kept.append(r)
            last[r["instrument"]] = r["t_min"]
    return kept


def _stats(xs: list[float]) -> tuple[int, float | None, float | None]:
    n = len(xs)
    if n == 0:
        return 0, None, None
    m = sum(xs) / n
    v = (sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else None
    return n, m, v


def compare(passed: list[dict], rejected: list[dict], key: str) -> dict:
    n1, m1, v1 = _stats([r[key] for r in passed])
    n2, m2, v2 = _stats([r[key] for r in rejected])
    if m1 is None or m2 is None or v1 is None or v2 is None:
        return {"n_passed": n1, "n_rejected": n2, "passed": m1, "rejected": m2, "diff": None, "se": None, "t": None}
    se = math.sqrt(v1 / n1 + v2 / n2)
    return {"n_passed": n1, "n_rejected": n2, "passed": m1, "rejected": m2, "diff": m1 - m2, "se": se, "t": ((m1 - m2) / se) if se > 0 else None}


def filter_value(records: list[dict], filters: Iterable[str], min_gap_min: float) -> list[dict]:
    rows = []
    for name in filters:
        P = [r for r in records if r["states"].get(name) == "pass"]
        R = [r for r in records if r["states"].get(name) == "fail"]
        Pt, Rt = thin(P, min_gap_min), thin(R, min_gap_min)
        e = compare(Pt, Rt, "r_net")
        w = compare(Pt, Rt, "win")
        t = e["t"]
        verdict = ("insufficient_data" if t is None else "earns_its_rejections" if t >= T_BAR else "rejected_outperformed" if t <= -T_BAR else "no_separation")
        rows.append({"filter": name, "n_passed_raw": len(P), "n_rejected_raw": len(R), "n_passed": e["n_passed"], "n_rejected": e["n_rejected"],
                     "p_win_passed": w["passed"], "p_win_rejected": w["rejected"], "p_win_diff": w["diff"], "p_win_t": w["t"],
                     "E_R_net_passed": e["passed"], "E_R_net_rejected": e["rejected"], "E_R_net_diff": e["diff"], "E_R_net_se": e["se"], "E_R_net_t": t,
                     "verdict": verdict, "t_bar": T_BAR})
    return rows
