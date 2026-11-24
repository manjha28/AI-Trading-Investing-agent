"""Round 2 outputs: reports/round2_summary.md, execution_compare.csv, delta_vs_binance_tracking.md (results.json is written by the runner)."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import data
from .round2 import BIN
from .round2_run import PRIOR_VARIANTS, deflated
from .run import REPORTS


def f(x, nd=2, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:,.{nd}f}"


def tracking() -> dict:
    out = {}
    for dsym, bsym in (("BTCUSD", "BTCUSDT"), ("ETHUSD", "ETHUSDT")):
        try:
            dl = data.load(f"{dsym}_last_1m").set_index("time")["close"]
            bl = pd.read_pickle(BIN / f"{bsym}_last_1m.pkl").set_index("time")["close"]
        except FileNotFoundError:
            continue
        j = pd.concat([dl.rename("d"), bl.rename("b")], axis=1, join="inner").dropna()
        diff = (j["d"] / j["b"] - 1) * 1e4
        absd = diff.abs()
        # change tracking: do the two move together minute to minute?
        ch = pd.concat([j["d"].pct_change().rename("d"), j["b"].pct_change().rename("b")], axis=1).dropna()
        out[dsym] = {"bars": int(len(j)), "from": datetime.fromtimestamp(int(j.index.min()), tz=timezone.utc).strftime("%Y-%m-%d"), "to": datetime.fromtimestamp(int(j.index.max()), tz=timezone.utc).strftime("%Y-%m-%d"),
                     "median_abs_bps": float(absd.median()), "p99_abs_bps": float(absd.quantile(0.99)), "median_signed_bps": float(diff.median()),
                     "median_abs_return_diff_bps": float(((ch["d"] - ch["b"]).abs() * 1e4).median()), "return_corr": float(ch["d"].corr(ch["b"]))}
    return out


def write_tracking() -> dict:
    t = tracking()
    L = ["# Delta India vs Binance USD-M: 1-minute close tracking", "", "Level difference = Delta close / Binance close − 1 on matching 1-minute bars (bps). Binance prices are USDT-quoted, Delta's are USD: part of the gap is the USDT/USD basis, which is not a trading edge.", "",
         "| Symbol | Overlap | 1-min bars | Median |Δ| (bps) | p99 |Δ| (bps) | Median signed Δ (bps) | Median |return diff| per minute (bps) | Return correlation |", "|---|---|---|---|---|---|---|---|"]
    for s, r in t.items():
        L.append(f"| {s} | {r['from']} to {r['to']} | {r['bars']:,} | {r['median_abs_bps']:.2f} | {r['p99_abs_bps']:.2f} | {r['median_signed_bps']:.2f} | {r['median_abs_return_diff_bps']:.2f} | {r['return_corr']:.4f} |")
    worst = max((r["median_abs_bps"] for r in t.values()), default=0)
    L += ["", ("**The median level gap is above 3 bps, so Binance-based results are treated as optimistic.**" if worst > 3 else "The median level gap is at or below 3 bps, so Binance prices are a reasonable stand-in for Delta's on 1-minute bars (the minute-to-minute return difference above is the more relevant number for fills)."), ""]
    (REPORTS / "delta_vs_binance_tracking.md").write_text("\n".join(L))
    return t


def write_all(results: dict) -> None:
    tr = write_tracking()
    fams = results["families"]
    rows = []
    for fam, subs in fams.items():
        for subj, r in subs.items():
            mm, tt = r["holdout"]["maker_maker"], r["holdout"]["taker_taker"]
            rows.append({"family": fam, "subject": subj, "variant": r["chosen"], "maker_trades": mm["summary"]["trades"], "maker_net_usd": mm["summary"]["net"], "maker_net_bps": mm["summary"]["net_bps_per_trade"], "maker_gross_bps": mm["summary"]["gross_bps_per_trade"],
                         "taker_trades": tt["summary"]["trades"], "taker_net_usd": tt["summary"]["net"], "taker_net_bps": tt["summary"]["net_bps_per_trade"], "taker_gross_bps": tt["summary"]["gross_bps_per_trade"],
                         "maker_fill_rate": mm["exec"]["maker_fill_rate"], "skipped_signal_rate": mm["exec"]["skipped_signal_rate"], "taker_exit_share": mm["exec"]["taker_exit_share"],
                         "adverse_1m_bps": mm["exec"]["adverse_1m_bps"], "adverse_5m_bps": mm["exec"]["adverse_5m_bps"], "adverse_30m_bps": mm["exec"]["adverse_30m_bps"]})
    with (REPORTS / "execution_compare.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])) if rows else None
        if w:
            w.writeheader(); w.writerows(rows)
    # variants and deflation
    per_symbol = sum(r["variants"] for subs in fams.values() for r in subs.values())
    total_variants = PRIOR_VARIANTS + per_symbol
    best, best_key = None, None
    sr_list = []
    for fam, subs in fams.items():
        for subj, r in subs.items():
            d = np.array(r["holdout"]["maker_maker"]["daily"])
            if len(d) > 10 and d.std(ddof=1) > 0:
                sr_list.append(d.mean() / d.std(ddof=1))
            n = r["holdout"]["maker_maker"]["summary"]["net"]
            if best is None or n > best:
                best, best_key = n, (fam, subj, r)
    sr_var = float(np.var(sr_list)) if len(sr_list) > 2 else 1e-4
    defl = deflated(np.array(best_key[2]["holdout"]["maker_maker"]["daily"]), total_variants, sr_var) if best_key else {}
    A = []
    L = A.append
    L("# Delta strategy lab, round 2: summary")
    L("")
    L("*Hypothetical backtests. Nothing here is a real position, an order or money at risk, and none of it is mixed with real portfolio P&L. Decision support, not investment advice. No live order was sent.*")
    L("")
    L("## 1. Setup and the deviations you should know about")
    L("- **Signals** come from Binance USD-M BTCUSDT and ETHUSDT (1-minute last and mark klines and funding history, 2020 onward, every zip checked against its published SHA-256). **Fills and costs** use the Delta cost model (maker 0.02%, taker 0.05%, GST 18%, Delta contract specs), applied to Binance prices; see `delta_vs_binance_tracking.md` for how closely the two track.")
    L("- **Maker/maker** = post-only limit re-set every bar to the previous close (a best-bid/best-ask proxy: 1-minute bars cannot re-price every 10 s), filled only when price trades strictly THROUGH it, cancelled and the signal skipped after N minutes ({2, 10}); target exits are resting limits; stop and time-stop pay taker. Breakout families (R2, R3) enter on stop orders and market orders, which cannot rest, so only their target exit is maker.")
    L("- **Delta's own funding history** is used only in round 1's Delta data; here funding is Binance's (label: *research funding*).")
    L("- **R6 and R0** use Binance aggTrades reduced to 1-second bars. See the data section for the date range actually downloaded. Mark price at 1 s is replaced by last price. R0 takes one signal every 5 minutes.")
    L("- **R5** sizes each leg by risk (the gate), so the legs have equal risk, not equal notional. R2, R3, R4 and R5 have stops the brief did not specify: R2 2.5 ATR(4h) as specified, R4 and R5 3 ATR(4h), fixed before any result was seen.")
    L("- Order-book recorder: running (see section 9). Slippage is still the assumed 1 bp until 2+ weeks of book data exist.")
    L("")
    L("## 2. Data and Delta-vs-Binance tracking")
    for s, r in tr.items():
        L(f"- {s}: {r['bars']:,} overlapping 1-min bars ({r['from']} to {r['to']}); median level gap {r['median_abs_bps']:.2f} bps, 99th percentile {r['p99_abs_bps']:.2f} bps; minute-return correlation {r['return_corr']:.4f}.")
    L("")
    L(f"## 3. Variants\nRound 1: {PRIOR_VARIANTS}. Round 2: {per_symbol} (counted per symbol, as in round 1). **Running total: {total_variants}.**")
    L("")
    L("## 4. Holdout scoreboard (maker/maker, baseline costs)")
    L("| Family | Symbol | Chosen variant | Trades | Gross bps | Net bps | Net $ | PF | Win | Quarters + | Max DD % | Net $ at 2x slip | Net $ 30% maker→taker | Taker/taker net $ | Passed |")
    L("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    winners = []
    for fam, subs in fams.items():
        for subj, r in subs.items():
            h = r["holdout"]; b = h["maker_maker"]["summary"]
            qs = [v for q, v in h["maker_maker"]["quarterly"].items() if q in r["hold_quarters"]] or [0.0]
            L(f"| {fam} | {subj} | `{r['chosen']}` | {b['trades']} | {f(b['gross_bps_per_trade'])} | {f(b['net_bps_per_trade'])} | {f(b['net'])} | {f(b['profit_factor'])} | {f(b['win_rate'], pct=True)} | {sum(1 for v in qs if v > 0)}/{len(qs)} | {f(b['max_drawdown_pct_of_start'], 1)} | {f(h['slippage_x2']['summary']['net'])} | {f(h['maker_to_taker_30pct']['summary']['net'])} | {f(h['taker_taker']['summary']['net'])} | {'**PASS**' if r['passed'] else 'fail'} |")
            if r["passed"]:
                winners.append((fam, subj, r["chosen"]))
    L("")
    L("## 5. What the execution change buys (holdout)")
    L("| Family | Symbol | Maker fill rate | Skipped signals | Exits paying taker | Adverse move after fill 1m / 5m / 30m (bps, + = against us) | Net bps maker/maker | Net bps taker/taker |")
    L("|---|---|---|---|---|---|---|---|")
    for rw in rows:
        L(f"| {rw['family']} | {rw['subject']} | {f(rw['maker_fill_rate'], pct=True)} | {f(rw['skipped_signal_rate'], pct=True)} | {f(rw['taker_exit_share'], pct=True)} | {f(rw['adverse_1m_bps'])} / {f(rw['adverse_5m_bps'])} / {f(rw['adverse_30m_bps'])} | {f(rw['maker_net_bps'])} | {f(rw['taker_net_bps'])} |")
    L("")
    L("## 6. Walk-forward (out of sample, 12 m train / 3 m test)")
    L("| Family | Symbol | OOS trades | OOS net $ | OOS net bps | Positive test quarters |")
    L("|---|---|---|---|---|---|")
    for fam, subs in fams.items():
        for subj, r in subs.items():
            w = r["walk_forward_oos"]
            L(f"| {fam} | {subj} | {w['trades']} | {f(w['net'])} | {f(w['net_bps_per_trade'])} | {r['wf_quarters_positive']} |")
    L("")
    L("## 7. Pass-bar checks")
    L("| Family | Symbol | Net>0 at 2x slippage | Net>0 with 30% maker→taker | PF>1.2 | Enough trades | ≥60% quarters positive | DD<20% |")
    L("|---|---|---|---|---|---|---|---|")
    for fam, subs in fams.items():
        for subj, r in subs.items():
            L(f"| {fam} | {subj} | " + " | ".join("yes" if v else "no" for v in r["checks"].values()) + " |")
    L("Trade minimum: 100, except R1 and R2 at 60 (multi-day holds).")
    L("")
    L("## 8. Multiple testing")
    if defl.get("deflated_sharpe_prob") is not None:
        L(f"Best holdout result by net $: **{best_key[0]} {best_key[1]}** (`{best_key[2]['chosen']}`, {f(best)} $). Daily Sharpe {defl['daily_sharpe']:.3f}. With {total_variants} variants tried, the deflated Sharpe probability (that it beats the best of that many zero-edge trials) is **{defl['deflated_sharpe_prob']:.2f}**, and the chance a best-of-{total_variants} zero-edge result looks this good is **{defl['p_luck_best_of_n']:.2f}**. (Variance of Sharpe across trials is estimated from the chosen holdout results, so this is approximate.)")
    L("")
    L("## 9. Break-even cost level (chosen variants, holdout)")
    L("| Family | Symbol | Gross edge (bps/trade) | Cost paid: fees+GST+slippage+funding (bps/trade) | Cost would need to fall to (bps) | Cost ratio needed |")
    L("|---|---|---|---|---|---|")
    for rw in rows:
        g, n = rw["maker_gross_bps"], rw["maker_net_bps"]
        if g is None or n is None:
            continue
        cost = g - n
        L(f"| {rw['family']} | {rw['subject']} | {f(g)} | {f(cost)} | {f(max(g, 0))} | {f(g / cost) + 'x' if cost > 0 and g > 0 else 'never (gross edge ≤ 0)'} |")
    L("")
    L("## 10. Winner")
    L(("Passed: " + ", ".join(f"{a} {b} (`{c}`)" for a, b, c in winners) + ". A paper run starts through the same paper engine; no live order is turned on.") if winners else "**None passed.** No paper run was started.")
    L("")
    dg = REPORTS / "round2_diagnosis.md"
    L("## 11. Diagnosis")
    L(dg.read_text() if dg.exists() else "(pending)")
    L("")
    L("## 12. Order-book recorder")
    st = BIN.parents[0] / "delta_book" / "status.json"
    L("Status file: `data/delta_book/status.json`:\n```\n" + (st.read_text() if st.exists() else "not running") + "\n```\nStart: `python -m app.delta_lab.recorder` (BTCUSD and ETHUSD; read-only, no keys); table: `python -m app.delta_lab.measure_costs`.")
    (REPORTS / "round2_summary.md").write_text("\n".join(A) + "\n")
