"""Builds reports/2026-11-26-literature-equity.md from reports/literature_equity.json. Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json

from app.delta_lab.lit_delta_run import cell

from .eq_run import REPORTS

TITLES = {"momentum": "2.1 Momentum and relative strength (monthly, top 10/20/30)", "trend": "2.2 Trend filters (200-day, golden cross, Turtle 55/20; with and without a volume filter)",
          "mean_reversion": "2.3 Short-term mean reversion (RSI(2), IBS, three down days)", "gap": "2.4 Gap-up on volume (post-earnings drift not testable, see below)",
          "vol_contraction": "2.5 Volatility contraction and breakouts (NR7, inside bar, Bollinger squeeze, 20-day high on volume)"}
VERDICT = {
    "momentum": "**No.** Large in train (+374 to +2,357 bps per monthly holding across the 12 variants: 2022-24 was a strong mid/small-cap run), mixed to negative in validation (-304 to +250), and on the holdout -297 to +178 with no interval above zero; the three chosen on validation lost (-297, -52, -79). The Nifty ETF itself earned only +2% a year on the holdout.",
    "trend": "**No.** The best by validation Sharpe, the 50/200 golden cross, made +213 bps per trade on the holdout (interval -96 to +528) but -424 on validation. The Donchian 55/20 versions are negative on validation (-276) and on the holdout (-71) in the volume-filtered row shown. A volume filter does not help.",
    "mean_reversion": "**No, and clearly.** At about 42 bps per round trip, a 2-5 day hold on an oversold stock does not pay: holdout -52 (IBS), -70 (RSI(2) in an uptrend) and -106 (three down days) bps per trade, with intervals entirely below zero. Even in train they were near zero after costs (+3 to +34 bps).",
    "gap": "**No.** Gap-ups on 3x volume are flat to negative after costs on validation and holdout (validation -84 to -102, holdout -24 to -73 bps; large caps slightly better than mid caps on few trades). Entering at the close of the gap day (the only fill a daily bar proves) gives up the move.",
    "vol_contraction": "**No.** Squeeze breakouts (-83 to -93 bps on the holdout, intervals below zero) and 20-day highs on volume (-54, interval -104 to -2) lose after costs; NR7/inside-bar versions are in the table and are also negative on the holdout.",
}


def main() -> None:
    o = json.load(open(REPORTS / "literature_equity.json"))
    L: list[str] = []
    w = L.append
    w("# Literature strategies on NSE equities: swing and positional, daily bars (26 Nov 2026)\n")
    w("*Hypothetical backtests on completed daily bars; kept apart from real P&L. Decision support, not investment advice. **Nothing was traded or changed.***\n")
    w("## Short answer\n")
    w(f"- **Nothing survives delivery costs on the holdout.** {o['n_variants']} variants in this part (running total about {o['n_total']}). Not one variant was positive on validation and holdout with a holdout interval above zero. **I do not propose any paper forward test for equities.**")
    w("- Costs are the main problem for the fast setups: about 42 bps per round trip (STT 0.1% each way, exchange, SEBI, stamp, GST, plus 10 bps slippage per side). Anything held 2 to 10 days must earn more than that on average.")
    w("- The slow setups (monthly momentum, trend) were excellent in 2022-24 and stopped working afterwards. This is a regime story: the train window is a strong mid/small-cap rally.")
    w("- **Turn of month on Nifty** is the only effect positive in all three periods (net +12, +109, +6 bps per 4-day hold after 5 bps of futures-style costs), but its holdout t-statistic is 0.2. Post-earnings drift could not be tested: the database holds fiscal period ends, not announcement dates.\n")
    w("## Method, data and what to distrust\n")
    w("- **Data:** daily bars from the app's database (Dhan source, real bars only), 264 stock instruments, 22 Feb 2021 to 23 Nov 2026. The database is the owner's stored universe, **not the Nifty 200**; index membership is not stored. 'Nifty 200 proxy' = the 200 highest by trailing 120-day median turnover at each date, 'large' = the top 50 of those, 'mid' = ranks 51-200. Survivorship: stocks present today only. One close-to-close jump above 35% was treated as a corporate-action artefact and removed.")
    w("- **Benchmark (NIFTYBEES, a Nifty 50 ETF, buy and hold; the Dhan index series ends 19-20 Nov):** train annual return {0}%, Sharpe {1}; validation {2}%, {3}; holdout {4}%, {5} (max drawdown {6}%, {7}%, {8}%).".format(
        o["bench"]["train"]["ann_ret_pct"], o["bench"]["train"]["sharpe"], o["bench"]["validation"]["ann_ret_pct"], o["bench"]["validation"]["sharpe"], o["bench"]["holdout"]["ann_ret_pct"], o["bench"]["holdout"]["sharpe"],
        o["bench"]["train"]["max_dd_pct"], o["bench"]["validation"]["max_dd_pct"], o["bench"]["holdout"]["max_dd_pct"]))
    w(f"- **Split:** 50/25/25 of 1 Mar 2022 to 23 Nov 2026: train to {o['cuts'][1]}, validation to {o['cuts'][2]}, holdout after. **These years were used in earlier tests.** Chosen on validation Sharpe (at least 8 validation trades); the tables show the holdout of all variants for information.")
    w("- **Costs per side (fraction of traded value):** STT 0.1% on buy and sell, exchange 0.00297%, SEBI 0.0001%, stamp 0.015% on buys, GST 18% on exchange and SEBI charges, brokerage assumed Rs 0 (please check your Dhan plan), plus 10 bps slippage: about 21 bps per side. Long-only cash segment (no shorting). 20 equal slots of 5% each; entries at the close of the signal day.")
    w("- **Definitions** are the textbook forms (Jegadeesh-Titman 12-1 momentum, George-Hwang 52-week-high, relative strength vs Nifty, 200-day trend, Turtle 55/20, Connors RSI(2)/IBS/down days, Crabel NR7/inside bar, Bollinger squeeze); I did not read primary sources. Cells: trades / net bps per trade / win rate / profit factor / max drawdown % / Sharpe. Sharpe and returns use 248 trading days.")
    w(f"- **Deflated Sharpe:** N = {o['n_total']}; variance of daily Sharpe across this part's variants = {o['var_sr_daily']:.5f}.\n")
    for fam, title in TITLES.items():
        rows = [r for r in o["rows"] if r["family"] == fam]
        w(f"## {title}\n")
        w(f"**Verdict.** {VERDICT[fam]}\n")
        w("**Top three on validation, with year and size splits:**\n")
        w("| Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | Nifty down >5% in 30d: days / sum return | holdout: large vs mid net bps (n) | net bps per year (n) |\n|---|---|---|---|---|---|---|---|---|---|---|---|")
        for x in o["best"][fam]:
            e, ex = x["eval"], x["extras"]
            h = e["holdout"]
            sz = "; ".join(f"{k.split(' ')[0]} {v['net_bps']} ({v['trades']})" for k, v in ex["by_size"].items())
            yr = "; ".join(f"{k}: {v['net_bps']} ({v['trades']})" for k, v in ex["per_year"].items())
            rg = ex["nifty_down5_days"]
            w(f"| {x['name'].replace('|', '·')} | {h.get('trades')} | {h.get('net_bps')} | {ex['boot90']} | {h.get('sharpe')} | {h.get('ann_ret_pct')}% | {h.get('max_dd_pct')}% | {h.get('trades_per_month')} | {ex['dsr']:.3f} | {rg['days']} d / {rg['sum_pct']}% | {sz} | {yr} |")
        w("")
        w(f"**Every variant ({len(rows)}):**\n")
        w("| Variant | Train | Validation | Holdout |\n|---|---|---|---|")
        for r in rows:
            e = r["eval"]
            w(f"| {r['name'].replace('|', '·')} | {cell(e['train'])} | {cell(e['validation'])} | {cell(e['holdout'])} |")
        w("")
    w("## 2.4b Post-earnings drift\n\nNot tested. The database stores fiscal period ends for fundamentals but no result announcement dates (the governance-events table is empty), and I will not infer dates.\n")
    s = o["seasonality"]
    w("## 2.6 Seasonality on the Nifty 50 index (futures-style costs, 5 bps per round trip)\n")
    w(f"Dhan index daily bars, {s[-1]['span']}; split 50/25/25 by calendar ({s[-1]['cuts'][1]} and {s[-1]['cuts'][2]}). A trade is one window (a single weekday, the four days around the month end, or Monday to expiry day; expiry = last Thursday, last Tuesday from Sep 2025). Cells: trades / net bps / win rate / profit factor. Nifty-futures trading would also need margin and roll; this table is for the index move only.\n")
    w("| Effect | Direction | Train | Validation | Holdout | t (train / val / holdout) |\n|---|---|---|---|---|---|")
    for r in s[:-1]:
        c = lambda p: cell(r[p]) if r[p].get("trades") else "0"
        w(f"| {r['effect']} | {r['direction']} | {c('train')} | {c('validation')} | {c('holdout')} | {r['train'].get('t')} / {r['validation'].get('t')} / {r['holdout'].get('t')} |")
    w("\nOnly turn-of-month long is positive net in all three periods; its holdout is +5.7 bps with t = 0.2. Tuesday long is strong in validation (+24 bps, t 3.6) but +3 in train and -10 in the holdout, which is what chance produces across 14 tested rows.\n")
    w(f"Code: `backend/app/equity_lab/eq_data.py`, `eq_run.py`, `eq_report.py` (shared engine: `backend/app/delta_lab/lit_core.py`); numbers: `reports/literature_equity.json`. {o['n_variants']} variants added to the running total.")
    (REPORTS / "2026-11-26-literature-equity.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
