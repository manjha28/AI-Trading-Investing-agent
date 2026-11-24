"""Round 2 runner: quarterly walk-forward (12 m train / 3 m test) on the first 75%, one holdout run per cost assumption, maker vs taker comparison,
deflation for the number of variants, outputs under reports/round2_*. Same pass bar as round 1 plus the 30%-maker-to-taker condition.
Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import csv
import json
import math
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from statistics import NormalDist

import numpy as np

from . import round2 as r2
from .engine import CostModel, simulate
from .risk import RiskConfig
from .run import REPORTS
from .stats import START_BALANCE, summarize

CFG = RiskConfig(leverage=D(10), mode="paper")
BASE = CostModel()
STRESSES = {"maker_maker": BASE, "taker_taker": replace(BASE, all_taker=True), "slippage_x2": replace(BASE, slippage_mult=2.0),
            "maker_to_taker_30pct": replace(BASE, maker_to_taker_share=0.30), "all_stresses": replace(BASE, slippage_mult=2.0, maker_to_taker_share=0.30)}
MIN_TRADES = {"R1": 60, "R2": 60}
PRIOR_VARIANTS = 56
N = NormalDist()


def quarter(ts: int) -> str:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def quarters_between(lo: int, hi: int) -> list[str]:
    out, y, q = [], datetime.fromtimestamp(lo, tz=timezone.utc).year, (datetime.fromtimestamp(lo, tz=timezone.utc).month - 1) // 3 + 1
    end = quarter(hi)
    while True:
        out.append(f"{y}-Q{q}")
        if out[-1] == end:
            return out
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def by_quarter(trades: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trades:
        k = quarter(t["entry_time"])
        out[k] = out.get(k, 0.0) + t["net"]
    return out


def score(trades: list[dict], quarters: list[str]) -> float:
    q = by_quarter(trades)
    return float(np.median([q.get(x, 0.0) for x in quarters]))


def exec_metrics(trades: list[dict], counts: dict) -> dict:
    n = len(trades)
    maker = [t for t in trades if t["entry_maker"]]
    unfilled = counts.get("unfilled_signal", 0)
    sig = n + unfilled
    return {"maker_fill_rate": (len(maker) / (len(maker) + unfilled)) if (maker or unfilled) and any(True for _ in maker) else None, "skipped_signal_rate": unfilled / sig if sig else None,
            "taker_exit_share": sum(1 for t in trades if t["exit_reason"] != "target") / n if n else None,
            **{f"adverse_{k}m_bps": (float(np.mean([t[f"adverse_{k}m_bps"] for t in maker])) if maker else None) for k in (1, 5, 30)}}


def deflated(best_daily: np.ndarray, n_trials: int, sr_var: float) -> dict:
    """Probability the best result is better than the best of n_trials zero-edge trials (deflated Sharpe, daily). Also the simple best-of-N luck bound."""
    T = len(best_daily)
    if T < 10 or best_daily.std(ddof=1) == 0:
        return {"deflated_sharpe_prob": None, "p_luck_best_of_n": None}
    sr = best_daily.mean() / best_daily.std(ddof=1)
    g = 0.5772156649
    sr0 = math.sqrt(max(sr_var, 1e-12)) * ((1 - g) * N.inv_cdf(1 - 1 / n_trials) + g * N.inv_cdf(1 - 1 / (n_trials * math.e)))
    skew = float(((best_daily - best_daily.mean()) ** 3).mean() / best_daily.std() ** 3)
    kurt = float(((best_daily - best_daily.mean()) ** 4).mean() / best_daily.std() ** 4)
    denom = math.sqrt(max(1e-9, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    dsr = N.cdf((sr - sr0) * math.sqrt(T - 1) / denom)
    p_single = 1 - N.cdf(sr * math.sqrt(T))
    return {"daily_sharpe": float(sr), "benchmark_sr0": float(sr0), "deflated_sharpe_prob": float(dsr), "p_luck_best_of_n": float(1 - (1 - p_single) ** n_trials)}


def daily_pnl(trades: list[dict]) -> np.ndarray:
    d: dict[int, float] = {}
    for t in trades:
        d[t["exit_time"] // 86400] = d.get(t["exit_time"] // 86400, 0.0) + t["net"]
    return np.array(list(d.values()))


def run_family(fam: str, markets: dict, cuts: dict, quarters: list[str], log) -> dict:
    """Returns {symbol_or_PAIR: {...}} for one family."""
    btc, eth = markets["BTCUSD"], markets["ETHUSD"]
    grid = r2.GRIDS2[fam]
    names = [r2.variant_names(fam, g) for g in grid]

    def sigs_for(g):
        if fam == "R5":
            return r2.r5_signals(btc, eth, *g)
        fn = {"R1": r2.r1, "R2": r2.r2, "R3": r2.r3, "R4": r2.r4}[fam]
        return {s: fn(markets[s], *g) for s in markets}
    cache = {names[i]: sigs_for(g) for i, g in enumerate(grid)}
    subjects = ["PAIR"] if fam == "R5" else list(markets)

    def sim(name, subject, cost, hold=False):
        out_t, out_c = [], {}
        for s in (markets if subject == "PAIR" else [subject]):
            m = markets[s]
            sigs = cache[name][s]
            win = (cuts[s], len(m.t)) if hold else (0, cuts[s])
            t, _rows, c = simulate(m, sigs, cost, CFG, window=win)
            out_t += t
            for k, v in c.items():
                out_c[k] = out_c.get(k, 0) + v
        return sorted(out_t, key=lambda x: x["entry_time"]), out_c
    res = {}
    for subject in subjects:
        by_var = {n: sim(n, subject, BASE)[0] for n in names}
        folds, oos = [], []
        for f in range(0, len(quarters) - 4):
            train, test = quarters[f:f + 4], quarters[f + 4]
            sc = {n: score(tr, train) for n, tr in by_var.items()}
            best = max(sc, key=sc.get)
            sel = [t for t in by_var[best] if quarter(t["entry_time"]) == test]
            oos += sel
            folds.append({"train": train, "test": test, "chosen": best, "test_net": sum(t["net"] for t in sel), "test_trades": len(sel)})
        overall = {n: score(tr, quarters) for n, tr in by_var.items()}
        chosen = max(overall, key=overall.get)
        hold_q = quarters_between(int(markets["BTCUSD"].t[cuts["BTCUSD"]]), int(markets["BTCUSD"].t[-1]))
        hold = {}
        for label, cost in STRESSES.items():
            tr, cnt = sim(chosen, subject, cost, hold=True)
            hold[label] = {"summary": summarize(tr), "quarterly": by_quarter(tr), "exec": exec_metrics(tr, cnt), "counts": cnt, "daily": daily_pnl(tr).tolist()}
            if label == "maker_maker":
                hold[label]["trades_rows"] = tr
        b = hold["maker_maker"]
        need = MIN_TRADES.get(fam, 100)
        qpos = [v for q, v in b["quarterly"].items() if q in hold_q] or [0.0]
        checks = {"net_positive_2x_slippage": hold["slippage_x2"]["summary"]["net"] > 0, "net_positive_30pct_maker_to_taker": hold["maker_to_taker_30pct"]["summary"]["net"] > 0,
                  "profit_factor_over_1_2": (b["summary"]["profit_factor"] or 0) > 1.2, f"trades_at_least_{need}": b["summary"]["trades"] >= need,
                  "positive_in_60pct_of_quarters": sum(1 for v in qpos if v > 0) / len(qpos) >= 0.6, "max_drawdown_under_20pct": b["summary"]["trades"] > 0 and b["summary"]["max_drawdown_pct_of_start"] < 20}
        wf = summarize(oos)
        res[subject] = {"variants": len(names), "chosen": chosen, "scores": overall, "folds": folds, "walk_forward_oos": wf, "wf_quarters_positive": f"{sum(1 for f in folds if f['test_net'] > 0)}/{len(folds)}",
                        "holdout": hold, "hold_quarters": hold_q, "checks": checks, "passed": all(checks.values())}
        log(f"{fam} {subject}: chosen {chosen} | WF OOS net {wf['net']:.1f} ({wf['trades']}) | holdout maker {b['summary']['net']:.1f} ({b['summary']['trades']}) vs taker {hold['taker_taker']['summary']['net']:.1f} | passed={all(checks.values())}")
    return res


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    logf = (REPORTS / "round2_run.log").open("a")

    def log(msg: str) -> None:
        print(msg, flush=True); logf.write(msg + "\n"); logf.flush()
    t0 = time.time()
    markets = {"BTCUSD": r2.build_binance_market("BTCUSDT", "BTCUSD"), "ETHUSD": r2.build_binance_market("ETHUSDT", "ETHUSD")}
    cuts = {s: int(len(m.t) * 0.75) for s, m in markets.items()}
    lo, hi = int(markets["BTCUSD"].t[0]), int(markets["BTCUSD"].t[cuts["BTCUSD"]])
    quarters = quarters_between(lo, hi - 1)
    log(f"markets built in {time.time() - t0:.0f}s; walk-forward quarters {quarters[0]}..{quarters[-1]} ({len(quarters)}); holdout starts {datetime.fromtimestamp(hi, tz=timezone.utc):%Y-%m-%d}")
    results = {"families": {}}
    existing = REPORTS / "round2_results.json"
    if sys.argv[1:] and existing.exists():                 # a partial run (for example ticks only) keeps the families already computed
        results = json.loads(existing.read_text())
    fams = [a for a in sys.argv[1:] if a != "ticks"] or ["R1", "R2", "R3", "R4", "R5"]
    for fam in fams:
        results["families"][fam] = run_family(fam, markets, cuts, quarters, log)
        _save(results)
    from .round2_ticks import run_tick_families
    results["families"].update(run_tick_families(markets, log)) if "ticks" in (sys.argv[1:] or ["ticks"]) or not sys.argv[1:] else None
    _save(results)
    write_outputs(results)


def _slim(results: dict) -> dict:
    slim = json.loads(json.dumps(results, default=str))
    for fam in slim["families"].values():
        for subj in fam.values():
            for h in subj["holdout"].values():
                h.pop("trades_rows", None)
    return slim


def _save(results: dict) -> None:
    (REPORTS / "round2_results.json").write_text(json.dumps(_slim(results), indent=1))


# the output writers live in round2_report.py
def write_outputs(results: dict) -> None:
    from .round2_report import write_all
    write_all(results)



if __name__ == "__main__":
    main()
