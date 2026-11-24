"""Pre-registered walk-forward + one-shot holdout. `python -m app.delta_lab.run` writes reports/ (summary.md, results.json, csvs).
No live order can be sent from here. Hypothetical; decision support, not investment advice."""
from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import numpy as np

from . import data, families
from .engine import CostModel, simulate
from .risk import RiskConfig
from .stats import median_month_score, month_of, summarize

REPORTS = Path(__file__).resolve().parents[3] / "reports"
SYMBOLS = ("BTCUSD", "ETHUSD")
FAMILIES = ("F1", "F2", "F3", "F4", "F5", "F6")
MIN_TRADES = {"F3": 100, "F4": 100}
CFG = RiskConfig(leverage=D(10), mode="paper")
BASE = CostModel()
STRESSES = {"baseline": BASE, "slippage_x2": replace(BASE, slippage_mult=2.0), "maker_to_taker_30pct": replace(BASE, maker_to_taker_share=0.30),
            "latency_plus_1_bar": replace(BASE, extra_latency_bars=1), "all_stresses": replace(BASE, slippage_mult=2.0, maker_to_taker_share=0.30, extra_latency_bars=1)}


def months_between(lo_ts: int, hi_ts: int) -> list[str]:
    out, cur = [], datetime.fromtimestamp(lo_ts, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(hi_ts, tz=timezone.utc)
    while cur <= end:
        out.append(cur.strftime("%Y-%m"))
        cur = cur.replace(year=cur.year + (cur.month == 12), month=1 if cur.month == 12 else cur.month + 1)
    return out


def data_checks(symbol: str, m) -> dict:
    t = m.t
    gaps = np.diff(t)
    raw = data.load(f"{symbol}_last_1m")
    fund = data.load(f"{symbol}_funding_1h")
    med = float(np.median(fund["close"].astype(float)))
    return {"symbol": symbol, "first": datetime.fromtimestamp(int(t[0]), tz=timezone.utc).isoformat(), "last": datetime.fromtimestamp(int(t[-1]), tz=timezone.utc).isoformat(),
            "bars_1m": int(len(t)), "duplicate_bars_in_download": int(len(raw) - raw["time"].nunique()), "out_of_order_after_sort": int((gaps <= 0).sum()),
            "gaps_over_5s": int((gaps > 60).sum()), "gaps_over_5min": int((gaps > 300).sum()), "largest_gap_min": float(gaps.max() / 60), "missing_minutes": int(((gaps // 60) - 1).clip(min=0).sum()),
            "zero_volume_bars": int((m.v == 0).sum()), "ticks": 0, "quotes_l1_l2": False, "trade_tape": False, "funding_points": int(len(fund)), "funding_median_pct_per_8h": med,
            "mark_price_source": "Delta public MARK:<symbol> candles (the exchange's own mark price, not rebuilt)", "spec": {k: str(getattr(m.spec, k)) for k in ("contract_value", "tick_size", "mm_pct", "im_pct_min", "slope_threshold_notional", "max_leverage", "taker_fee", "maker_fee")}}


def run_symbol(symbol: str, results: dict, log) -> dict:
    m = families.build_market(symbol)
    cut = int(len(m.t) * 0.75)
    cut_ts = int(m.t[cut])
    wf_months = months_between(int(m.t[0]), cut_ts - 1)
    results[symbol] = {"checks": data_checks(symbol, m), "cut_time": datetime.fromtimestamp(cut_ts, tz=timezone.utc).isoformat(), "families": {}}
    for fam in FAMILIES:
        t0 = time.time()
        vs = families.variants(m, fam)
        by_var: dict[str, list[dict]] = {}
        rej_total: dict[str, int] = {}
        for name, sigs in vs:
            trades, _rows, counts = simulate(m, sigs, BASE, CFG, window=(0, cut))
            by_var[name] = trades
        # ---- walk-forward on the first 75%: 3-month train, 1-month test, rolling monthly; pick by MEDIAN monthly net in the train window ----
        folds, oos = [], []
        for f in range(0, len(wf_months) - 3):
            train, test = wf_months[f:f + 3], wf_months[f + 3]
            scores = {n: median_month_score(tr, train) for n, tr in by_var.items()}
            best = max(scores, key=scores.get)
            sel = [t for t in by_var[best] if month_of(t["entry_time"]) == test]
            oos.extend(sel)
            folds.append({"train": train, "test": test, "chosen": best, "test_net": sum(t["net"] for t in sel), "test_trades": len(sel)})
        wf = summarize(oos, [f["test"] for f in folds])
        # ---- final choice: highest MEDIAN monthly net over the whole walk-forward region ----
        overall = {n: median_month_score(tr, wf_months) for n, tr in by_var.items()}
        chosen = max(overall, key=overall.get)
        chosen_sigs = dict(vs)[chosen]
        hold_months = months_between(cut_ts, int(m.t[-1]))
        holdout = {}
        for label, cost in STRESSES.items():
            trades, rows, counts = simulate(m, chosen_sigs, cost, CFG, window=(cut, len(m.t)))        # the holdout is run once per cost assumption, with the chosen values only
            holdout[label] = {"summary": summarize(trades, hold_months), "rejections": counts}
            if label == "baseline":
                holdout[label]["trades_rows"] = trades
                holdout[label]["rejection_rows"] = rows
        base = holdout["baseline"]["summary"]
        need = MIN_TRADES.get(fam, 200)
        stress2 = holdout["slippage_x2"]["summary"]
        checks = {
            "net_positive_under_2x_slippage": stress2["net"] > 0,
            "profit_factor_over_1_2": (base["profit_factor"] or 0) > 1.2,
            f"trades_at_least_{need}": base["trades"] >= need,
            "positive_in_60pct_of_months": (base["positive_month_share"] or 0) >= 0.6,
            "max_drawdown_under_20pct": base["max_drawdown_pct_of_start"] < 20 and base["trades"] > 0,
        }
        results[symbol]["families"][fam] = {"variants": len(vs), "variant_scores_median_month_net": overall, "chosen": chosen, "folds": folds, "walk_forward_oos": wf, "holdout": holdout, "pass_checks": checks, "passed": all(checks.values())}
        log(f"{symbol} {fam}: chosen {chosen} | WF OOS net {wf['net']:.1f} ({wf['trades']} trades) | holdout net {base['net']:.1f} ({base['trades']} trades) | passed={all(checks.values())} | {time.time() - t0:.0f}s")
        _save(results)
    return results


def _save(results: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    slim = json.loads(json.dumps(results, default=str))
    for sym in slim.values():
        for fam in sym["families"].values():
            for h in fam["holdout"].values():
                h.pop("trades_rows", None); h.pop("rejection_rows", None)
    (REPORTS / "results.json").write_text(json.dumps(slim, indent=1))


def write_csvs(results: dict) -> None:
    pt = REPORTS / "paper_trades.csv"
    with pt.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp_utc", "symbol", "family", "variant", "side", "intended_entry", "size_contracts", "stop", "target", "est_liquidation_price", "rejection_reason", "simulated_exit_time_utc", "exit_reason", "net_pnl_usd", "net_bps", "fees", "gst", "funding", "slippage", "source"])
        for sym, r in results.items():
            for fam, f in r["families"].items():
                for t in f["holdout"]["baseline"].get("trades_rows", []):
                    w.writerow([datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).isoformat(), sym, fam, t["variant"], t["side"], round(t["entry_px"], 2), t["qty"], "", "", "", "", datetime.fromtimestamp(t["exit_time"], tz=timezone.utc).isoformat(),
                                t["exit_reason"], round(t["net"], 4), round(t["bps_net"], 2), round(t["fees"], 4), round(t["gst"], 4), round(t["funding"], 4), round(t["slippage"], 4), "holdout replay in the paper-trade schema (not live paper)"])
    rj = REPORTS / "rejections.csv"
    with rj.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "family", "reason", "count", "source"])
        for sym, r in results.items():
            for fam, f in r["families"].items():
                for reason, cnt in sorted(f["holdout"]["baseline"]["rejections"].items(), key=lambda kv: -kv[1]):
                    w.writerow([sym, fam, reason, cnt, "holdout, baseline costs"])


if __name__ == "__main__":
    REPORTS.mkdir(parents=True, exist_ok=True)
    out: dict = {}
    logf = (REPORTS / "run.log").open("a")

    def log(msg: str) -> None:
        print(msg, flush=True)
        logf.write(msg + "\n"); logf.flush()
    for s in (sys.argv[1:] or SYMBOLS):
        run_symbol(s, out, log)
    write_csvs(out)
    _save(out)
    from .report import write_summary
    write_summary(out)
