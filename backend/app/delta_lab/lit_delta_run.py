"""Runs Part 1 of the 2026-11-26 literature round and writes reports/2026-11-26-literature-delta.md plus reports/literature_delta.json. Hypothetical; decision support, not investment advice."""
from __future__ import annotations

import json

import numpy as np

from . import lit_core as K
from . import lit_misc as M
from . import lit_r3 as R3
from . import lit_trend as T
from . import lit_xs as X
from .run import REPORTS

TITLES = {
    "trend": "1.1 Time-series trend following (signal, mode, vol target)", "trend_stops": "1.1b Trend entries with ATR trailing stops and a chandelier exit",
    "xs_momentum": "1.2 Cross-sectional momentum (weekly)", "xs_reversal": "1.2b Short-term reversal (buy last week's losers)", "funding_carry": "1.3a Funding carry (perp-only, BTC hedge)",
    "funding_contrarian": "1.3b Funding contrarian", "squeeze": "1.4a Bollinger squeeze then breakout", "atr_expansion": "1.4b ATR-expansion breakout",
    "nr7_inside": "1.4c NR7 / inside bar then break", "pairs": "1.6 Pairs / stat-arb", "r3_exits": "1.7 Exit variants on the frozen R3 entry (BTC+ETH)"}


def cell(e: dict) -> str:
    if not e.get("trades"):
        return "0"
    s = f"{e['trades']} / {e.get('net_bps', '—')} / {round(100 * e.get('win', 0))}% / {e.get('pf') if e.get('pf') is not None else '—'}"
    if "max_dd_pct" in e:
        s += f" / {e['max_dd_pct']}% / {e.get('sharpe')}"
    elif "max_dd_bps" in e:
        s += f" / {int(e['max_dd_bps'])}bp"
    return s


def main() -> None:
    rows, store = [], {}
    for tf in ("1d", "4h"):
        for u in ("U4", "ALL"):
            n0 = len(rows)
            T.run_trend(tf if tf == "1d" else "4h", u, store, rows)
            for r in rows[n0:]:
                r["name"] = f"{u} {tf} | {r['name']}"
        n0 = len(rows)
        T.run_vol(tf if tf == "1d" else "4h", store, rows)
        for r in rows[n0:]:
            r["name"] = f"ALL {tf} | {r['name']}"
    X.run_xs(store, rows)
    pair_rows: list = []
    M.run_pairs(store, rows, pair_rows)
    r3: list = []
    r3_extra: dict = {}
    R3.run_r3(r3, r3_extra)
    for r in r3:
        r["key"] = len(rows)
        rows.append(r)
    tod: dict = {}
    M.run_tod(tod)
    n_hyp = 3 * (24 + 7)
    n_this = len(rows) + n_hyp
    n_total = K.RUNNING_TOTAL_BEFORE + n_this
    srs = np.array([r["eval"]["validation"].get("sharpe", 0) / np.sqrt(365) for r in rows if r["family"] != "r3_exits" and r["eval"]["validation"].get("trades", 0) >= 8])
    var_sr = float(srs.var())
    out = {"n_variants": n_this, "n_total": n_total, "var_sr_daily": var_sr, "rows": [], "best": {}, "pairs": pair_rows, "tod": tod}
    from .round4_d2 import boot_ci as _bc
    for r in rows:
        item = {k: r[k] for k in ("family", "name", "params", "eval", "key")}
        e = r["eval"]
        if r["family"] != "r3_exits" and e["validation"].get("net_bps", -1) > 0 and e["holdout"].get("net_bps", -1) > 0 and e["holdout"].get("trades", 0) >= 5:
            res, p = store[r["key"]]
            h = res["ep"][(res["ep"].t >= p.cuts[2]) & (res["ep"].t < p.cuts[3])]
            lo, hi = _bc(h.bps.to_numpy())
            item["boot_holdout"] = [round(lo, 1), round(hi, 1)]
        out["rows"].append(item)
    fams = list(dict.fromkeys(r["family"] for r in rows))
    for fam in fams:
        rs = [r for r in rows if r["family"] == fam and r["eval"]["validation"].get("trades", 0) >= (8 if fam != "r3_exits" else 20)]
        if not rs:
            continue
        if fam == "r3_exits":
            rs.sort(key=lambda r: -r["eval"]["validation"].get("net_bps", -1e9))
        else:
            rs.sort(key=lambda r: -r["eval"]["validation"].get("sharpe", -1e9))
        def make(r):
            item = {"name": r["name"], "eval": r["eval"]}
            if fam == "r3_exits":
                d = r3_extra[r["name"]]
                h = d[d.t >= int(R3.T_START + K.split_idx(np.arange(R3.T_START, int(R3.LD.T1), 86400), "2024-04-10", None)[2] * 86400)]
                from .round4_d2 import boot_ci
                lo, hi = boot_ci(h.bps.to_numpy())
                item["extras"] = {"boot90": [round(lo, 1), round(hi, 1)], "per_coin": {s: {"trades": int(len(g)), "net_bps": round(float(g.bps.mean()), 1)} for s, g in h.groupby("sym")}}
            else:
                res, p = store[r["key"]]
                cuts, bpd = p.cuts, getattr(p, "bpd", 1)
                item["extras"] = K.holdout_extras(res, cuts, bpd, p.names, p.btc_down)
                hd = res["net"][cuts[2] // bpd: cuts[3] // bpd]
                item["extras"]["dsr"] = K.dsr_prob(hd, n_total, var_sr)
                item["extras"]["turn_per_day"] = r.get("turn_per_day")
            return item
        best = [make(r) for r in rs[:3]]
        if fam in ("trend", "trend_stops"):
            grp = {}
            for r in rs:
                grp.setdefault(f"{r['params']['universe']} {r['params']['tf']}", r)
            out.setdefault("best_groups", {})[fam] = {g: make(r) for g, r in grp.items()}
        out["best"][fam] = best
    (REPORTS / "literature_delta.json").write_text(json.dumps(out, indent=1, default=str))
    print(n_this, n_total, "done")


if __name__ == "__main__":
    main()
