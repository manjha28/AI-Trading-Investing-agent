"""Builds reports/summary.md from results.json. All figures are hypothetical backtests on stored public candles."""
from __future__ import annotations

import json
from pathlib import Path

REPORTS = Path(__file__).resolve().parents[3] / "reports"


def _f(x, nd=2, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:,.{nd}f}"


def write_summary(results: dict | None = None) -> Path:
    results = results or json.loads((REPORTS / "results.json").read_text())
    L: list[str] = []
    A = L.append
    A("# Delta Exchange India intraday strategy lab: summary")
    A("")
    A("*Hypothetical backtests on stored public candles. Nothing here is a real position, an order or money at risk, and none of it is mixed with real portfolio P&L. Decision support, not investment advice. No live order was sent.*")
    A("")
    A("## 1. What this could and could not test")
    A("- Delta India publishes **candles only** (last price, mark price, funding). There are **no historical ticks, no L1/L2 quotes and no trade tape**. So F2 runs on 1-minute candles (not 1 s) and F6 cannot use signed order flow: it runs as a labelled **candle-direction proxy** (sign of the last 1-minute bar). Slippage cannot be measured and is an **assumed** 1.0 bp (never below one tick), doubled in the stress run.")
    A("- Only USD-quoted perpetuals exist on Delta India: `BTCUSDT` and `ETHUSDT` do not exist, so BTCUSD and ETHUSD are the two tests. No alt was added: its round-trip cost cannot be measured here, so the under-20-bps condition cannot be shown.")
    A("- Sizing uses a notional $1,000 wallet (your real balance is 0), 0.5% risk per trade, isolated margin at 10x, no compounding. This is a fixed research setting, not your account.")
    A("- F5 and F6 have no stop in the brief but the risk gate requires one: F5 uses 1.5 x ATR(1h), the F6 proxy 1.0 x ATR(1h). Fixed before any result was seen.")
    A("- The brief's funding-window rule (no orders within 2 minutes of a funding timestamp) also blocks F5's 00:00 UTC entry every day. That is the rule working as written, not a bug: it shows up in the rejection counts.")
    A("")
    A("## 2. Data checks")
    A("| Symbol | First bar (UTC) | Last bar (UTC) | 1-min bars | Missing minutes | Missing-bar gaps | Largest gap (min) | Duplicate bars | Out of order | Funding points | Median funding %/8 h | Ticks | Quotes | Trades |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s, r in results.items():
        c = r["checks"]
        A(f"| {s} | {c['first'][:16]} | {c['last'][:16]} | {c['bars_1m']:,} | {c['missing_minutes']:,} | {c['gaps_over_5s']:,} | {c['largest_gap_min']:.0f} | {c['duplicate_bars_in_download']} | {c['out_of_order_after_sort']} | {c['funding_points']:,} | {c['funding_median_pct_per_8h']:.4f} | none | none | none |")
    A("")
    A("Mark price: " + next(iter(results.values()))["checks"]["mark_price_source"] + ". Contract specs from the public product endpoint: " + "; ".join(f"{k}={v}" for k, v in next(iter(results.values()))["checks"]["spec"].items()) + ".")
    A("")
    A("## 3. Cost model (fixed before any result)")
    A("Taker 0.05% and maker 0.02% on notional at both open and close (the exchange's published rates for these contracts; your account's own tier could not be read: the key has no balance and no fill report exists), GST 18% on fees, historical funding at 00/08/16 UTC from the FUNDING series, slippage max(1 tick, 1.0 bp) per taker fill, maker fills only when price trades strictly through the limit, 1-minute bar latency (250 ms is below the bar; the stress adds one bar). Stops trigger on mark price; stop and target in the same bar resolve to the stop. Stress runs: 2x slippage, 30% of maker fills turned taker, +1 bar latency, and all three together.")
    A("")
    total_variants = sum(f["variants"] for r in results.values() for f in r["families"].values())
    A(f"## 4. Variants tried\n**{total_variants}** variants in total ({total_variants // len(results)} per symbol: F1 4, F2 8, F3 8, F4 3, F5 4, F6 1). Stress and latency reruns are the same chosen variants under harsher cost assumptions, not new variants.")
    A("")
    A("## 5. Results by family and symbol")
    A("Choice rule: the variant with the highest MEDIAN monthly net P&L in the train windows (walk-forward) and over the whole first 75% (final choice). Holdout = the last 25%, run once per cost assumption with the chosen values only.")
    A("")
    A("### 5a. Holdout scoreboard")
    A("| Symbol | Family | Chosen variant | Trades | Net bps/trade | Net P&L ($) | PF | Win rate | Positive months | Max DD (% of $1,000) | Net under 2x slippage ($) | Passed |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|")
    winners = []
    for s, r in results.items():
        for fam, f in r["families"].items():
            b = f["holdout"]["baseline"]["summary"]; s2 = f["holdout"]["slippage_x2"]["summary"]
            A(f"| {s} | {fam} | `{f['chosen']}` | {b['trades']} | {_f(b['net_bps_per_trade'])} | {_f(b['net'])} | {_f(b['profit_factor'])} | {_f(b['win_rate'], pct=True)} | {b['positive_months']}/{b['months']} | {_f(b['max_drawdown_pct_of_start'], 1)} | {_f(s2['net'])} | {'**PASS**' if f['passed'] else 'fail'} |")
            if f["passed"]:
                winners.append((s, fam, f["chosen"]))
    A("")
    A("### 5b. Holdout detail (baseline costs)")
    A("| Symbol | Family | Gross bps/trade | Gross $ | Fees $ | GST $ | Funding $ | Slippage $ | Net $ | Avg win $ | Avg loss $ | Worst day $ | Sharpe (daily) | Liquidations | Funding rates missing |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s, r in results.items():
        for fam, f in r["families"].items():
            b = f["holdout"]["baseline"]["summary"]
            A(f"| {s} | {fam} | {_f(b['gross_bps_per_trade'])} | {_f(b['gross'])} | {_f(b['fees'])} | {_f(b['gst'])} | {_f(b['funding'])} | {_f(b['slippage'])} | {_f(b['net'])} | {_f(b['avg_win'])} | {_f(b['avg_loss'])} | {_f(b['worst_day'])} | {_f(b['sharpe_daily'])} | {b['liquidations']} | {b['funding_missing']} |")
    A("")
    A("### 5c. Stress results on the holdout (net $ / trades)")
    A("| Symbol | Family | Baseline | 2x slippage | 30% maker→taker | +1 bar latency | All three |")
    A("|---|---|---|---|---|---|---|")
    for s, r in results.items():
        for fam, f in r["families"].items():
            h = f["holdout"]
            A(f"| {s} | {fam} | " + " | ".join(f"{_f(h[k]['summary']['net'])} / {h[k]['summary']['trades']}" for k in ("baseline", "slippage_x2", "maker_to_taker_30pct", "latency_plus_1_bar", "all_stresses")) + " |")
    A("")
    A("### 5d. Walk-forward (out of sample, first 75%)")
    A("| Symbol | Family | OOS trades | OOS net $ | OOS net bps/trade | Positive test months | Folds |")
    A("|---|---|---|---|---|---|---|")
    for s, r in results.items():
        for fam, f in r["families"].items():
            w = f["walk_forward_oos"]
            A(f"| {s} | {fam} | {w['trades']} | {_f(w['net'])} | {_f(w['net_bps_per_trade'])} | {w['positive_months']}/{w['months']} | {len(f['folds'])} |")
    A("")
    A("### 5e. Pass-bar checks (holdout)")
    A("| Symbol | Family | Net>0 at 2x slippage | PF>1.2 | Enough trades | ≥60% months positive | DD<20% |")
    A("|---|---|---|---|---|---|---|")
    for s, r in results.items():
        for fam, f in r["families"].items():
            c = list(f["pass_checks"].values())
            A(f"| {s} | {fam} | " + " | ".join("yes" if v else "no" for v in c) + " |")
    A("")
    A("### 5f. Monthly P&L on the holdout ($, baseline)")
    for s, r in results.items():
        months = sorted({m for f in r["families"].values() for m in f["holdout"]["baseline"]["summary"]["monthly"]})
        A(f"**{s}**\n")
        A("| Family | " + " | ".join(months) + " |")
        A("|---|" + "---|" * len(months))
        for fam, f in r["families"].items():
            mm = f["holdout"]["baseline"]["summary"]["monthly"]
            A(f"| {fam} | " + " | ".join(_f(mm.get(m)) if m in mm else "—" for m in months) + " |")
        A("")
    A("### 5g. Risk-gate rejections on the holdout (baseline costs)")
    A("| Symbol | Family | Reason | Count |")
    A("|---|---|---|---|")
    for s, r in results.items():
        for fam, f in r["families"].items():
            for reason, cnt in sorted(f["holdout"]["baseline"]["rejections"].items(), key=lambda kv: -kv[1])[:6]:
                A(f"| {s} | {fam} | {reason} | {cnt:,} |")
    A("")
    A("## 6. Winner")
    if winners:
        A("Passed the holdout bar: " + ", ".join(f"{s} {fam} (`{v}`)" for s, fam, v in winners) + ". Paper phase: see section 8.")
    else:
        A("**None passed.** No family on BTCUSD or ETHUSD met every bar. The paper phase was therefore not started.")
    A("")
    diag = REPORTS / "diagnosis.md"
    A("## 7. Diagnosis")
    A(diag.read_text() if diag.exists() else "(written after the run)")
    A("")
    A("## 8. Paper phase and small-live plan")
    A("The paper engine (`app/delta_lab/paper.py`) is built and uses the same `risk_gate()` and `size_position()`. It starts only for a family that passes; it needs at least 4 weeks or 100 trades, whichever is later, and the comparison against the backtest described in the brief. A small-live plan (quarter size, 50 trades) will be proposed only after that, and not started without your approval.")
    A("")
    A("Files: `reports/results.json`, `reports/paper_trades.csv` (holdout replay in the paper-trade schema), `reports/rejections.csv`, `reports/run.log`; code in `backend/app/delta_lab/`, tests in `backend/tests/test_delta_lab_*.py`.")
    out = REPORTS / "summary.md"
    out.write_text("\n".join(L) + "\n")
    return out


if __name__ == "__main__":
    print(write_summary())
