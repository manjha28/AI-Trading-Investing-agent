"""Builds reports/2026-11-26-trend-5ema-vwap.md from reports/trend_5ema.json and reports/trend_vwap.json. Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import trend_5ema as m
from . import trend_core as C
from .dsr_trades import dsr as dsr_fn
from .round4_d2 import boot_ci
from .run import REPORTS

PRIOR_TOTAL = 303        # 148 through round 3 + 155 from the later reports' own "variants_added" fields (reports/*.json)


def esc(x: str) -> str:
    return x.replace("|", " · ")


def f(x, nd=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{nd}f}"


def cell(s: dict) -> str:
    return f"{f(s.get('net_bps'))} ({s.get('trades', 0)})" if s.get("trades") else "— (0)"


def main() -> None:
    A, V = json.load(open(REPORTS / "trend_5ema.json")), json.load(open(REPORTS / "trend_vwap.json"))
    n_this = A["n_variants_this_run_AB"] + V["n_variants_this_run_C"]
    n_total = PRIOR_TOTAL + n_this
    allv = {**A["versions"], **{"C|" + k: v for k, v in V["versions"].items()}, **{"A|" + k: v for k, v in A["filters"].items()}}
    srs = np.array([v["validation"]["sr"] for v in allv.values() if v["validation"].get("trades", 0) >= 30 and v["validation"].get("sr") is not None])
    var_sr = float(srs.var())
    best_f = A["best_filter"]
    L: list[str] = []
    w = L.append

    # Part A best = best filter on the live rule, re-run for its final trades
    ref5 = m.pooled(lambda s, **k: m.run_e13(s, 300, "close", 0, "T3ref", best_f, False, fillcheck=False))
    fin5 = ref5[ref5.t >= C.SPLIT2].bps.to_numpy()
    bestA = {"key": f"5m|E1 live rule|filter={best_f}", "summary": A["filters"][f"5m|{best_f}"], "final_boot90": list(boot_ci(fin5)), "final_bps": fin5}
    bests = {"Part A (trend filter on the live rule)": bestA, "Part B (5 EMA entry/exit)": A["best"], "Part C fade": V["best"]["fade"], "Part C pullback": V["best"]["pullback"]}

    w("# Trend filter, 5 EMA entries and VWAP bands (Delta India data only), 26 Nov 2026\n")
    w("*Hypothetical backtest on completed bars; kept apart from real P&L. Decision support, not investment advice. **Nothing live was changed.** No order was placed.*\n")
    w("## Short answer\n")
    w(f"- **No version beats costs in a way I would trust.** {n_this} variants were tried in this run (about {n_total} in the running total). The validation-chosen best of each part has a final-check 90% interval that includes zero (see below).")
    w("- **Some limit-entry versions do look positive on the final check, and their interval excludes zero when all 10 coins are pooled, but the gain comes only from the five coins that Delta listed in the last 14 months (EVAA, AKE, BLESS, ARIA, AIOT).** On BTC, ETH, SOL, XRP and LISTA the same versions lose 4-5 bps per trade. Those five coins are in the list because they are in today's top 10 by turnover (a selection effect), they are thin (1 bp slippage and a fill at every touch are generous there), and they appear only in the final window. I treat this as a lead to test on paper, not as an edge.")
    w("- **The trend filter would have blocked today's long.** All 18 filter combinations block it (details below). On history the filter does **not** improve results: averaged over the 144 paired Part B versions it moves validation by about +0.4 bps and the final check by about -1.7 bps per trade, i.e. nothing.")
    w("- **Least bad for live: stop live 5 EMA and move it to paper.** If you keep it live, the single pullback limit (halfway up the alert candle) is the least bad entry, not the current close entry. Nothing was applied.\n")
    w("## Method, and what to distrust\n")
    w("- **Data:** Delta India 1-minute last-price candles only (public API), BTC, ETH, SOL, LISTA plus Delta's top-10 crypto perps by today's 24h turnover (EVAA, AIOT, BLESS, AKE, ARIA, XRP). Each coin starts when Delta listed it: BTC Dec 2023, ETH Feb 2024, SOL/XRP Apr 2024, LISTA Jul 2024, BLESS and EVAA Oct 2025, AIOT and ARIA Apr 2026, AKE Jul 2026. **The train period is therefore BTC/ETH/SOL/XRP/LISTA only**, and the top-10 list is picked from today's turnover (a selection bias).")
    w("- **Costs:** taker 0.05% and maker 0.02% per side, plus 18% GST, plus 1 bp slippage on every taker fill. A resting limit that fills is a maker fill with no slippage. **This is optimistic for limit orders:** a fill is assumed whenever price touches the limit; a real queue often does not fill at a touch, and the trades you miss are usually the winners.")
    w("- **Split (fixed before looking):** train < 2025-07-01, validation 2025-07-01 to 2026-03-31, final check from 2026-04-01 to now, by entry time. Versions were chosen on validation only (at least 300 trades). **The table shows the final check for every version for information; the headline uses only the validation-chosen one.** The final window overlaps the period used in today's earlier reports (they used Binance data for the same months), so it is not fresh data.")
    w("- **Definitions I had to choose** (the brief left room): B1 uses 2-bar fractal swing points on 1h bars, the last two highs and lows inside 24h; B2 is the 1h close vs EMA(50) with the EMA falling/rising (it is almost never neutral, so its two neutral options are identical); B3 uses the 4h EMA(20); B4 uses a rolling 24h VWAP of typical price. R1 = the latest 1h close beyond the last swing high (low); R2 = a higher low (lower high), then a 1h close beyond EMA(20). A 1h or 4h value is used only after that bar closed. The VWAP script's source was not supplied, so Part C uses the generic session VWAP, reset 00:00 UTC, with volume-weighted standard-deviation bands, and nothing is traded in a session's first 2 hours.")
    w("- **Other choices:** the 'nearest 1h support' target uses 1h swing lows of the last 7 days and the trade is skipped when none is 2R away; the trail moves behind the latest confirmed swing high (low) of the trading timeframe once +1R was reached; trades end after 48 candles; one position per coin; E2's daily reset means trades are closed at 00:00 UTC and the alert and break candles must be on the same UTC day; min-stop uses 3 x the trade's own round-trip cost (about 41 bps taker both ways).\n")

    w("## Part A. Trend filter\n")
    w("### Today's long (BTCUSD, 25 Nov)\n")
    w("- **Which source?** The numbers match a **5-minute** breakout candle: it closed at **11:50 UTC (17:20 IST)** at 58,731.0 with the alert candle's low at 58,431.5 (your stop); the target 59,629.5 is exactly 3 x the distance from 58,731 to the stop, i.e. measured from the signal close, not from your fill at 58,762. A 15m scanner is ruled out: its only long that hour (12:00 UTC) had its stop at 58,427. The live 5m test and a 5m scanner run the same rule, so **I cannot tell which of the two sent it**: the local database has no orders and the production query I tried was blocked, so I did not look further. Please check the live ledger's `forward_test_id` / `scanner_id` for that order.")
    w("- **The fill was 31 points (5.3 bps) worse than the signal close** (58,762 vs 58,731). That moved reward:risk from 3.0 to 2.6. My model assumes 1 bp slippage; this one fill was five times that. One sample, but worth watching.")
    w("- **Every bias said bearish** at that moment: B1 lower highs and lower lows, B2 price under a falling 1h EMA(50), B3 4h close under EMA(20), B4 price (58,613) 1.7% under the 24h VWAP (59,605). No reversal had happened (no 1h close above the last swing high at 59,448.5, no higher low with a close above the 1h EMA(20)). **All 18 filter combinations block this long.** The 5m short at 12:00 UTC was allowed by all of them.\n")
    w("### Each filter on the live rule (enter at the close, 3R from the signal close, no other checks). Net bps per trade (trades)\n")
    w("| Filter (bias \\| reversal rule \\| neutral) | 5m validation | 5m final | 15m validation | 15m final |\n|---|---|---|---|---|")
    for k in ["none"] + C.FILTER_NAMES:
        a, b = A["filters"][f"5m|{k}"], A["filters"][f"15m|{k}"]
        w(f"| {esc(k)}{' **(chosen on validation)**' if k == best_f else ''} | {cell(a['validation'])} | {cell(a['final'])} | {cell(b['validation'])} | {cell(b['final'])} |")
    w("")
    w("**How much the filter changes the live rule** (5m, validation / final, net bps per trade; longs and shorts separately):\n")
    w("| | all | longs | shorts |\n|---|---|---|---|")
    for sg in ("validation", "final"):
        for nm, k in (("no filter", "none"), (f"filter {best_f}", best_f)):
            s = A["filters"][f"5m|{k}"][sg]
            w(f"| {sg}, {esc(nm)} | {cell(s)} | {cell(s['long'])} | {cell(s['short'])} |")
    w("\nR3 (no counter-trend trades at all) sits within about a bp of R1 and R2 in the table: confirmed reversals are too rare to matter. 'No trade when neutral' halves the trade count and moves the result about 2 bps, better on 5m validation and worse on 15m, which is noise at this size.\n")

    w("## Part B. 5 EMA entries and exits\n")
    w("Rows are the unfiltered, no-min-stop versions (fill check on), net bps per trade (trades), pooled over all coins. The full list of every version, with the filter and min-stop switches, is in the appendix.\n")
    w("| Version | 5m val | 5m final | 15m val | 15m final |\n|---|---|---|---|---|")
    asis = {tf: A["versions"][f"{tf}|E1|asis_live_no_checks|filter=none|minstop=0"] for tf in m.TFS}
    w(f"| E1 today's live rule, no checks | {cell(asis['5m']['validation'])} | {cell(asis['5m']['final'])} | {cell(asis['15m']['validation'])} | {cell(asis['15m']['final'])} |")
    labels = ["E1", "E2"] + [f"E3|{lv}|w{W}" for lv in ("broken", "half", "ema") for W in (3, 6)]
    for lab in labels:
        for ex in (["algotest"] if lab == "E2" else ["T2", "T3", "T3ref", "SR", "TR"]):
            row = []
            for tf in m.TFS:
                kk = f"{tf}|{lab}|{ex}|filter=none|minstop=0"
                row += [cell(A["versions"][kk]["validation"]), cell(A["versions"][kk]["final"])]
            w(f"| {esc(lab)} {ex} | " + " | ".join(row) + " |")
    w("")
    pos = [(k, v) for k, v in A["versions"].items() if v["final"].get("trades", 0) >= 200 and v["final"]["net_bps"] > 0]
    posv = [(k, v) for k, v in pos if v["validation"].get("net_bps", -1) > 0]
    n_e3 = sum(1 for k, _ in pos if "|E3|" in k)
    n_ms = sum(1 for k, _ in pos if "minstop=1" in k)
    w(f"- **Positive on the final check (at least 200 trades): {len(pos)} of {sum(1 for v in A['versions'].values() if v['final'].get('trades', 0) >= 200)} Part B versions; positive on both validation and final: {len(posv)}.** {n_e3} of the {len(pos)} are limit-entry (E3) versions and {n_ms} have the min-stop rule on (it keeps only the wider-stop signals). All rest on the assumption that every touched limit fills at no slippage.")
    w("\n### Where the positive results come from\n")
    w("Three representative versions (5m, no trend filter, min-stop on), final check, net bps per trade with 90% bootstrap interval:\n")
    w("| Version | all 10 coins | only BTC, ETH, SOL, XRP, LISTA | only the 5 young coins |\n|---|---|---|---|")
    old = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "LISTAUSD"]
    for lab, (ent, W_, ex_) in {"E3 pullback to the broken alert low, wait 3, break-even + trail": ("broken", 3, "TR"), "E3 limit halfway up the alert candle, wait 6, 2R target": ("half", 6, "T2"), "E1 close entry, break-even + trail": ("close", 0, "TR")}.items():
        d_ = m.pooled(lambda s_, **k: m.run_e13(s_, 300, ent, W_, ex_, "none", True))
        d_ = d_[d_.t >= C.SPLIT2]
        cells = []
        for part in (d_, d_[d_.symbol.isin(old)], d_[~d_.symbol.isin(old)]):
            b_ = part.bps.to_numpy()
            lo_, hi_ = boot_ci(b_)
            cells.append(f"{f(b_.mean())} [{f(lo_)}, {f(hi_)}] ({len(b_)})")
        w(f"| {lab} | " + " | ".join(cells) + " |")
    w("\nThe young coins were listed from Oct 2025 (BLESS, EVAA) to Jul 2026 (AKE), so most of their history sits in the final window.\n")
    w("- **Entry matters most.** On 5m validation: close entry (today's rule) about -15, AlgoTest exact about -12.5, pullback limits about -6.5 to -8. On the final check the close entry is worse (about -19) while the limits stay around -4 to -8.")
    w("- **Exit matters less, except the break-even trail**, which is the best exit in every row (close entry -15 becomes -9; pullbacks -7 become -4.5 to -6). Fixed 2R, 3R and 'nearest support' are within about 1 bp of each other. The trail's gain is partly that more trades end near break-even and so lose about the cost instead of a full stop.\n")

    w("## Part C. VWAP bands\n")
    w("Net bps per trade (trades), pooled, best of the filter / hours switches on validation shown in the appendix; here are the unfiltered, all-hours rows:\n")
    w("| Version | 1m val | 1m final | 5m val | 5m final | 15m val | 15m final |\n|---|---|---|---|---|---|---|")
    names = [f"fade k={k} stop={s} target={t}" for k in (2, 3) for s in ("next", "half") for t in ("vwap", "1s")] + ["pullback target=1sigma", "pullback target=2sigma"]
    for nm in names:
        row = []
        for tf in m.TFS if False else ("1m", "5m", "15m"):
            kk = f"{tf}|filter=none|hours=all|{nm}"
            row += [cell(V["versions"][kk]["validation"]), cell(V["versions"][kk]["final"])]
        w(f"| {nm} | " + " | ".join(row) + " |")
    v1 = [v["validation"]["net_bps"] for k, v in V["versions"].items() if k.startswith("1m|") and v["validation"].get("trades", 0) >= 300]
    vf = [v["validation"]["net_bps"] for k, v in V["versions"].items() if "fade" in k and v["validation"].get("trades", 0) >= 300]
    tf_ = [v["train"]["net_bps"] for k, v in V["versions"].items() if "fade" in k and v["train"].get("trades", 0) >= 300]
    vp = [v["validation"]["net_bps"] for k, v in V["versions"].items() if "pullback" in k and v["validation"].get("trades", 0) >= 300]
    w(f"\n- **1m, plainly: fees are bigger than the 1m move.** A taker round trip costs about 13.8 bps (fees, GST, slippage) and a maker entry plus taker exit about 9.3. The best 1m version on validation (at least 300 trades) made {f(max(v1))} bps per trade.")
    w(f"- **Fade:** every fade version with enough trades lost on train (best {f(max(tf_))}) and on validation (best {f(max(vf))}). Its final-check gains (up to about +19 bps with the filter and 08-20 UTC hours) come from versions that were negative on validation; that is what noise looks like.")
    w(f"- **Pullback:** the closest family to its costs: validation {f(min(vp))} to {f(max(vp))} bps across versions, helped by maker entries; final check about +1 for the validation-chosen one, with longs and shorts of opposite sign.\n")

    w("## Best version per part (chosen on validation, final check shown once)\n")
    w(f"Deflated Sharpe uses N = {n_total} trials and the variance of per-trade Sharpe across this run's variants ({var_sr:.5f}).\n")
    for nm, b in bests.items():
        s, fr = b["summary"], np.array(b["final_bps"])
        d = dsr_fn(fr, n_total, var_sr) if len(fr) > 30 else {}
        w(f"### {nm}: `{b['key']}`\n")
        w("| segment | trades | net bps | win rate | profit factor | worst drawdown (bps; per-trade results added up across all coins, one contract each) | longs net (n) | shorts net (n) |\n|---|---|---|---|---|---|---|---|")
        for sg in ("train", "validation", "final"):
            x = s[sg]
            w(f"| {sg} | {x.get('trades', 0)} | {f(x.get('net_bps'))} | {f(100 * x['win_rate'], 0) + '%' if x.get('trades') else '—'} | {f(x.get('pf'), 2)} | {f(x.get('max_dd_bps'), 0)} | {cell(x.get('long', {}))} | {cell(x.get('short', {}))} |")
        w(f"\n- **Final check bootstrap 90% interval for the mean: {f(b['final_boot90'][0])} to {f(b['final_boot90'][1])} bps.** Deflated Sharpe (probability the edge is real after {n_total} trials): **{f(d.get('deflated_sharpe_prob', float('nan')) if d else None, 3)}**.")
        w("- Final-check by coin: " + "; ".join(f"{k} {cell(v['final'])}" for k, v in s.get("per_symbol", {}).items() if v["final"].get("trades")))
        q = s.get("per_quarter", {})
        w("- By quarter: " + "; ".join(f"{k} {cell(v)}" for k, v in q.items()) + "\n")

    w("## Recommendation (nothing applied)\n")
    w("1. **Stop live 5 EMA and run it as paper**, with the checks below, until a version holds up on data it was not chosen on. Nothing here shows an edge that survives costs, selection across ~750 variants, a 90% interval that excludes zero, and the coins that existed from the start.")
    w("2. If you want it live regardless, the **least bad** of your three options is the **single pullback limit** (halfway up the alert candle, wait 3 candles), with the fill check and the min-stop rule: about -5 to -8 bps on 5m validation (and the final check) versus -15 and -19 for today's close entry. Its optimism is the maker-fill assumption, and a limit that is never touched makes no trade, so it trades less.")
    w("3. Add the **fill check** (skip when reward:risk from the actual fill is under 1:2, using the real fill) and the **min-stop rule** to whatever stays on; they cost nothing and remove the 1:1 and 1:12 style trades. The trend filter is optional: it blocked today's long but does not improve results on history.")
    w("\n## What you need to decide\n")
    w("- Pause the 5 EMA live test and 5m scanner Live switch (keep paper)? Or keep live and switch the entry to the single pullback limit?")
    w("- Whether the trend filter should be added anyway as a safety rule (it blocked today's long, and costs nothing measurable), recognising it is not shown to add edge.")
    w("- Whether to spend time on measuring real slippage and real limit fill rates from your own ledger (today's fill was 5.3 bps worse than the signal): that is the biggest unknown behind every limit-entry result here.\n")

    w("## Appendix: every version (net bps per trade, trades). Filter 'best' = " + best_f + "\n")
    w("| Version | val | final | final longs | final shorts | val PF | final PF | final win |\n|---|---|---|---|---|---|---|---|")
    for k, v in list(A["versions"].items()) + [("C|" + k, v) for k, v in V["versions"].items()]:
        fi, va = v["final"], v["validation"]
        w(f"| {esc(k)} | {cell(va)} | {cell(fi)} | {cell(fi.get('long', {}))} | {cell(fi.get('short', {}))} | {f(va.get('pf'), 2)} | {f(fi.get('pf'), 2)} | {f(100 * fi['win_rate'], 0) + '%' if fi.get('trades') else '—'} |")
    w("\nCode: `backend/app/delta_lab/trend_core.py`, `trend_5ema.py`, `trend_vwap.py`, `trend_report.py`, `trend_dl.py`; numbers: `reports/trend_5ema.json`, `reports/trend_vwap.json`. " + f"{n_this} variants added to the running total.")
    (REPORTS.parent / "reports" / "2026-11-26-trend-5ema-vwap.md").write_text("\n".join(L))
    json.dump({"n_this": n_this, "n_total": n_total, "var_sr": var_sr}, open(REPORTS / "trend_meta.json", "w"))
    print("\n".join(L[:12]), len(L))


if __name__ == "__main__":
    main()
