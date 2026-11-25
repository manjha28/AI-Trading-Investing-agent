"""Builds reports/2026-11-26-literature-delta.md from reports/literature_delta.json. Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json

from .lit_delta_run import TITLES, cell
from .run import REPORTS

VERDICT = {
    "trend": "**Mostly no.** The variant chosen on validation in each universe/timeframe group lost on the holdout (all coins daily -172 bps per trade, interval -450 to +103; four majors daily 90-day momentum -588, interval below zero). One exception that I only noticed after seeing the holdout, so it does not count as chosen: 4h Donchian 50/25 long-short on the four majors, no stop, was positive in train, validation and holdout (+879 bps per trade, 16 holdout trades, interval +297 to +1,468).",
    "trend_stops": "**The best result of the round, and still not proof.** Chosen on validation, the 4h Donchian 50/25 with a 3 x ATR trailing stop on BTC/ETH/SOL/XRP, long-short, 15% vol target: train +214, validation +459, holdout +525 bps per trade (16 holdout trades, interval +119 to +953, holdout Sharpe 1.8, deflated Sharpe 0.05). The same family (Donchian 50/25 or SMA 20/100 with an ATR or chandelier stop, 4h or daily, majors only) gives 14 of the 20 variants whose holdout interval is above zero, and most of them are also positive in train; the long-only and regime versions are weaker. Caveats: about 15 trades per period (eight months of holdout is about sixteen entries), four highly correlated coins, 2024-2026 had strong trends, and the 14 variants are one idea, not 14 independent results. On all coins daily the same rules lose in train (-626 to -860) and win later, so they are not stable.",
    "xs_momentum": "**No.** Long-top/short-bottom baskets earn well in train and validation (the 7-14 day vol-adjusted ranks) and mostly give it back on the holdout (the validation-best 7-day variant -25; the third best +211, interval -368 to +869); no interval is above zero. The liquid universe is only 6 to 18 coins, so 'top 10' collapses to a smaller N. Short legs on thin coins are where the squeeze risk sits.",
    "xs_reversal": "**No.** Buying last week's losers lost in train and validation and 'worked' only in the holdout, and only through the short leg (shorting winners); the interval includes zero.",
    "funding_carry": "**One technical pass, weak.** Short the top-decile 7-day funding coins each week (perp-only): train -774, validation +234, holdout +1,635 bps per trade (16 weekly baskets, interval +84 to +3,109). Funding collected is only about 280 bps of the 1,635; the rest is price, driven by a few coins (ARC +6,136, HUSD +3,738, AIN -3,733). Unstable across periods and the deflated Sharpe is 0.25. The BTC hedge makes almost no difference: the hedge leg is not what drives results.",
    "funding_contrarian": "**No.** Positive in the holdout for most settings but negative or flat in train/validation, and the holdout interval of each of the top three includes zero.",
    "squeeze": "**No.** Positive in the holdout (the 4h squeeze-and-break, +158 bps per trade, interval +37 to +292) but negative on validation (-33), so it fails the rule (choose on validation, confirm on holdout). Long breaks do the work; shorts lose.",
    "atr_expansion": "**No.** The 4h 2 x ATR expansion made +168 in the holdout (interval +68 to +266) but lost -116 on validation. Daily versions are flat to negative.",
    "nr7_inside": "**No.** Negative on the holdout for every version, with intervals below zero for the best three: trade costs plus whipsaws.",
    "pairs": "**No.** Only one pair (XRP/XLM) passed the train cointegration and stability tests, and it lost on validation and holdout. BTC/ETH and SOL/ETH do not pass (train ADF -1.9 against -3.37 needed); traded anyway they lose a little on every setting.",
    "r3_exits": "**No exit rescues R3 here.** On Delta data the frozen R3 baseline earns +84 bps per trade on validation but -5.6 on the holdout (68 trades, interval -42 to +32), against the +39 and +52 reference holdout recorded for the earlier Binance-signal study (maker/maker costs, 1 bp slippage). At 2 bps slippage on Delta data and these dates it does not clear costs on the holdout. The only holdout-positive variants (2R target, partial profit, profit-reversal exit) are +4 to +6 bps; I did not compute their intervals, and with 68 trades they cannot exclude zero. The frozen test was not touched.",
}


def main() -> None:
    o = json.load(open(REPORTS / "literature_delta.json"))
    L: list[str] = []
    w = L.append
    w("# Literature strategies on Delta India: slow, low-turnover (26 Nov 2026)\n")
    w("*Hypothetical backtests on completed bars; kept apart from real P&L. Decision support, not investment advice. **Nothing was traded, armed, deployed or changed.***\n")
    w("## Short answer\n")
    w(f"- **Nothing clears the bar.** {o['n_variants']} variants in this part (running total about {o['n_total']} with equity still to come). The bar: chosen on validation, positive on the holdout, bootstrap 90% interval above zero, and a deflated Sharpe that survives the running total. No strategy meets all four.")
    w("- **Closest to a yes: slow trend following with a trailing stop on BTC, ETH, SOL and XRP.** Chosen on validation, the 4h Donchian 50/25 + 3 x ATR trail, long-short, was positive in train, validation and holdout (+525 bps per trade on the holdout, interval +119 to +953) and it is one of a cluster that behaves the same way. But it has about 15 trades per period and a deflated Sharpe of 0.05 after the running total. Of the 53 variants positive on validation and holdout, 20 have a holdout interval above zero; with ~600 variants a pure-noise set would show about 15 such rows, so the count alone proves nothing, but the cluster being the same idea in train too does count for something.")
    w("- **Weekly short-the-highest-funding basket:** technically passes (holdout interval +84 to +3,109, 16 baskets) but was -774 in train and its gains come from a handful of small-coin price moves; funding itself is about 280 of the 1,635 bps. I would not trust it.")
    w("- Everything else (cross-sectional momentum and reversal, funding contrarian, squeeze, ATR expansion, NR7/inside bar, pairs, hour/weekday, R3 exit variants) fails: negative on validation or holdout, or intervals that include zero.")
    w("- The frozen R3 does not repeat its earlier holdout on Delta data with 2 bps slippage (-5.6 bps per trade on the holdout).\n")
    w("## Method, data and what to distrust\n")
    w("- **Sources.** I could not open the papers (I have no page-fetching tool without spawning a sub-agent, which you asked me not to do), so none of the named papers' own rules were read. The definitions are the standard textbook forms of the ideas those papers use: Donchian breakouts, SMA crossovers, EMA(100) trend, time-series momentum with volatility targeting, cross-sectional return ranks as in Liu-Tsyvinski-Wu, funding carry, and pairs by Engle-Granger cointegration. Where a paper's own rule differs, my result may too. The README of statarb-crypto was not read either. Other ideas used: Bollinger squeeze, NR7 and inside bar (Crabel), ATR-expansion breakouts.")
    w("- **Data:** Delta India candles and exact funding only. Daily/4h bars built from Delta 1-minute (BTC, ETH, SOL, XRP and 6 more, from Dec 2023 to Apr 2024 onward) and Delta 15-minute files (183 coins from Nov 2024). 187 crypto perps; the number with history grows from 3-5 (2024) to 187 (Nov 2026), so most coins, and all of the cross-section, exist only in 2025-2026. Today's listing is the universe, so delisted coins are missing (survivorship) and the young coins are today's winners by turnover.")
    w("- **Costs:** taker 0.05% + 18% GST, plus 2 bps slippage, on every change of position (7.9 bps per side); exact funding paid and received at 00/08/16 UTC; no maker fills were used in this part. Position weights are sized at entry and held; daily bars use the close as the fill price.")
    w("- **Split:** 50/25/25 of the calendar span. Trend, squeeze, ATR, NR7 and R3: 10 Apr 2024 to 24 Nov 2026 (train to about 24 Jul 2025, validation to about 19 Mar 2026, holdout after). Cross-sectional, funding and pairs: 29 Dec 2024 to 24 Nov 2026 (train to 12 Dec 2025, validation to 4 Jun 2026). **These years were used in all earlier tests today, so the holdout is not untouched.** Best variants are chosen on validation Sharpe (R3: validation net bps) with at least 8 validation trades; the tables also show the holdout of every variant for information.")
    w("- **'Trade' =** one coin's continuous same-side position (an episode). Net bps are on the notional held, after costs and funding. Max drawdown and Sharpe come from the portfolio's daily net returns (cumulative return, not compounded). Vol targets 10% and 20% scale position size only, so their per-trade bps and Sharpe are identical; both rows are kept and counted.")
    w("- **Trend universes:** U4 = BTC, ETH, SOL, XRP; ALL = every coin with 200 days of its own history at that time. 'LO+BTC regime' is long-only and flat while BTC is below its 100-day EMA. Liquidity floor (cross-sectional and funding tests): 30-day mean turnover of USD 2M per day; that leaves 6 to 18 coins, so top/bottom 10 shrinks to the number available.")
    w(f"- **Deflated Sharpe:** N = {o['n_total']} trials; variance of daily Sharpe across this part's variants = {o['var_sr_daily']:.5f}. Bootstrap 90% intervals are for the mean net bps per trade on the holdout.\n")
    w("## Variants positive on validation and on the holdout, with the holdout interval above zero\n")
    w("Selected after seeing the holdout (so this is a list of leads, not a result). Vol-target 10% and 20% rows are scale duplicates.\n")
    w("| Variant | Train net bps | Validation | Holdout (trades) | Holdout 90% interval |\n|---|---|---|---|---|")
    pos = [r for r in o["rows"] if r.get("boot_holdout") and r["boot_holdout"][0] > 0]
    for r in pos:
        e = r["eval"]
        w(f"| {r['name'].replace('|', '·')} | {e['train'].get('net_bps')} | {e['validation'].get('net_bps')} | {e['holdout'].get('net_bps')} ({e['holdout'].get('trades')}) | {r['boot_holdout']} |")
    n_both = sum(1 for r in o["rows"] if r.get("boot_holdout"))
    w(f"\n{n_both} variants were positive on validation and holdout; {len(pos)} of them have an interval above zero; {sum(1 for r in pos if r['eval']['train'].get('net_bps', -1) > 0)} of those are also positive in train.\n")
    for fam, title in TITLES.items():
        rows = [r for r in o["rows"] if r["family"] == fam]
        if not rows:
            continue
        w(f"## {title}\n")
        w(f"**Verdict.** {VERDICT[fam]}\n")
        best = o["best"].get(fam, [])
        if fam in ("trend", "trend_stops") and fam in o.get("best_groups", {}):
            w("**Best variant per universe/timeframe on validation (chosen on validation Sharpe; holdout shown once):**\n")
            w("| Group | Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | long / short net bps | BTC down 20% days / sum return | funding bps per trade (paid +) |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for g, x in o["best_groups"][fam].items():
                e, ex = x["eval"], x["extras"]
                h = e["holdout"]
                w(f"| {g} | {x['name']} | {h.get('trades')} | {h.get('net_bps')} | {ex['boot90']} | {h.get('sharpe')} | {h.get('ann_ret_pct')}% | {h.get('max_dd_pct')}% | {h.get('trades_per_month')} | {ex['dsr']:.3f} | {e['holdout_long'].get('net_bps')} ({e['holdout_long'].get('trades')}) / {e['holdout_short'].get('net_bps')} ({e['holdout_short'].get('trades')}) | {ex['btc_down20']['days']} d / {ex['btc_down20']['sum_pct']}% | {ex['decomp']['funding_bps_per_trade']} |")
            w("")
        w("**Top three on validation:**\n")
        w("| Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | long / short net bps | per-coin holdout (most trades) | BTC down 20%: days / sum return | gross vs funding (bps per trade) |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for x in best:
            e, ex = x["eval"], x["extras"]
            h = e["holdout"]
            pc = "; ".join(f"{k} {v['net_bps']} ({v['trades']})" for k, v in list(ex.get("per_coin", {}).items())[:5])
            reg = ex.get("btc_down20", {})
            dec = ex.get("decomp", {})
            dsr_v = ex.get("dsr")
            w(f"| {x['name']} | {h.get('trades')} | {h.get('net_bps')} | {ex['boot90']} | {h.get('sharpe', '—')} | {h.get('ann_ret_pct', '—')}% | {h.get('max_dd_pct', h.get('max_dd_bps', '—'))} | {h.get('trades_per_month', '—')} | {f'{dsr_v:.3f}' if dsr_v is not None else '—'} | {e['holdout_long'].get('net_bps')} ({e['holdout_long'].get('trades')}) / {e['holdout_short'].get('net_bps')} ({e['holdout_short'].get('trades')}) | {pc} | {reg.get('days', '—')} d / {reg.get('sum_pct', '—')}% | {dec.get('gross_bps_per_trade', '—')} / funding paid {dec.get('funding_bps_per_trade', '—')} |")
        w("")
        w(f"**Every variant ({len(rows)}).** Cells: trades / net bps per trade / win rate / profit factor / max drawdown / Sharpe (drawdown in % of capital for portfolio rows, bps for R3).\n")
        w("| Variant | Train | Validation | Holdout |\n|---|---|---|---|")
        for r in rows:
            e = r["eval"]
            w(f"| {r['name'].replace('|', '·')} | {cell(e['train'])} | {cell(e['validation'])} | {cell(e['holdout'])} |")
        w("")
        if fam == "pairs":
            pr = o["pairs"][0]
            w(f"Pairs tested: {pr['tested']} among the {len(pr['liquid_pool'])} most liquid coins with full train coverage; selected: {pr['selected']}; owner-named pairs' train ADF: {pr['named_train_adf']} (need below -3.37).\n")
    w("## 1.5 Hour-of-day and weekday effects (BTC, ETH, SOL)\n")
    cons = []
    for sym, rec in o["tod"].items():
        for key in ("hour", "weekday"):
            tab = rec[key]
            for b in tab["train"]:
                v = [tab[p].get(b) for p in ("train", "validation", "holdout")]
                if all(x is not None for x in v) and (all(x[0] > 0 for x in v) or all(x[0] < 0 for x in v)):
                    cons.append((sym, key, b, [x[0] for x in v], [x[1] for x in v]))
    big = [c for c in cons if min(abs(x) for x in c[3]) > 15.8]
    w(f"{3 * 31} buckets were tested (24 hours and 7 weekdays for each coin; mean 1-hour return in bps, t-statistics in brackets in the JSON). **{len(cons)} keep the same sign in all three periods, which is what chance gives about a quarter of the time; {len(big)} of them average more than the 15.8 bps a one-hour round trip costs in every period.** Nothing was traded, as the brief requires a pattern that holds in all three periods and beats costs. Consistent-sign buckets (bps in train / validation / holdout):\n")
    w("| Coin | Type | Bucket (UTC hour or weekday, 0 = Mon) | Mean bps per hour in the three periods | t-stats |\n|---|---|---|---|---|")
    for c in sorted(cons, key=lambda c: -min(abs(x) for x in c[3]))[:25]:
        w(f"| {c[0]} | {c[1]} | {c[2]} | {c[3]} | {c[4]} |")
    w("")
    w("Asia (00:00), London (08:00) and New York (13:00-14:00 UTC) opens are the hours 0, 8, 13 and 14 in the same tables (full tables in `reports/literature_delta.json`).\n")
    w("## Paper forward test: what I would propose\n")
    w("**I have added nothing; this needs your OK.** One candidate meets the stated rule (chosen on validation, holdout interval above zero): **4h BTC/ETH/SOL/XRP, Donchian 50/25 breakout, long-short, 3 x ATR trailing stop, 15% volatility target, 8 bps per side costs, paper only.** At about 15 entries a period it would need roughly a year of paper to say more than this backtest does, so I would treat the paper run as a data-gathering step, not a go signal, and set it against the deflated Sharpe of 0.05. The funding basket meets the rule on paper but I would not propose it (negative in train, driven by a few coins).\n")
    w(f"Code: `backend/app/delta_lab/lit_data.py`, `lit_core.py`, `lit_trend.py`, `lit_xs.py`, `lit_misc.py`, `lit_r3.py`, `lit_delta_run.py`, `lit_delta_report.py`; numbers: `reports/literature_delta.json`. {o['n_variants']} variants added to the running total.")
    (REPORTS / "2026-11-26-literature-delta.md").write_text("\n".join(L))
    print(len(L), len(cons), len(big))


if __name__ == "__main__":
    main()
