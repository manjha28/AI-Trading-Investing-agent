# Delta strategy lab, round 2: summary

*Hypothetical backtests. Nothing here is a real position, an order or money at risk, and none of it is mixed with real portfolio P&L. Decision support, not investment advice. No live order was sent.*

## 1. Setup and the deviations you should know about
- **Signals** come from Binance USD-M BTCUSDT and ETHUSDT (1-minute last and mark klines and funding history, 2020 onward, every zip checked against its published SHA-256). **Fills and costs** use the Delta cost model (maker 0.02%, taker 0.05%, GST 18%, Delta contract specs), applied to Binance prices; see `delta_vs_binance_tracking.md` for how closely the two track.
- **Maker/maker** = post-only limit re-set every bar to the previous close (a best-bid/best-ask proxy: 1-minute bars cannot re-price every 10 s), filled only when price trades strictly THROUGH it, cancelled and the signal skipped after N minutes ({2, 10}); target exits are resting limits; stop and time-stop pay taker. Breakout families (R2, R3) enter on stop orders and market orders, which cannot rest, so only their target exit is maker.
- **Delta's own funding history** is used only in round 1's Delta data; here funding is Binance's (label: *research funding*).
- **R6 and R0** use Binance aggTrades reduced to 1-second bars. The ticks cover 1 Sept 2025 to 23 Nov 2026 only (download time), so they have a single walk-forward fold. Mark price at 1 s is replaced by last price. R0 takes one signal every 5 minutes.
- **R5** sizes each leg by risk (the gate), so the legs have equal risk, not equal notional. R2, R3, R4 and R5 have stops the brief did not specify: R2 2.5 ATR(4h) as specified, R4 and R5 3 ATR(4h), fixed before any result was seen.
- Order-book recorder: running (see section 9). Slippage is still the assumed 1 bp until 2+ weeks of book data exist.

## 2. Data and Delta-vs-Binance tracking
- BTCUSD: 1,495,371 overlapping 1-min bars (2023-12-29 to 2026-11-22); median level gap 1.01 bps, 99th percentile 42.99 bps; minute-return correlation 0.9521.
- ETHUSD: 1,465,682 overlapping 1-min bars (2024-02-06 to 2026-11-22); median level gap 1.87 bps, 99th percentile 21.75 bps; minute-return correlation 0.9382.

## 3. Variants
Round 1: 56. Round 2: 92 (counted per symbol, as in round 1). **Running total: 148.**

## 4. Holdout scoreboard (maker/maker, baseline costs)
| Family | Symbol | Chosen variant | Trades | Gross bps | Net bps | Net $ | PF | Win | Quarters + | Max DD % | Net $ at 2x slip | Net $ 30% maker→taker | Taker/taker net $ | Passed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R1 | BTCUSD | `R1|pct=10|atr4h=2|hold=72h|N=2m` | 148 | 21.32 | 13.03 | 61.46 | 1.22 | 43.2% | 5/8 | 4.3 | 58.80 | 57.93 | 49.09 | **PASS** |
| R1 | ETHUSD | `R1|pct=10|atr4h=2|hold=72h|N=2m` | 127 | -22.35 | -31.19 | -35.35 | 0.89 | 37.8% | 3/8 | 7.8 | -37.20 | -37.95 | -36.84 | fail |
| R2 | BTCUSD | `R2|donchian=20|exit=10` | 44 | -7.85 | -21.12 | -3.64 | 0.96 | 29.5% | 4/8 | 3.8 | -11.67 | -3.64 | -3.64 | fail |
| R2 | ETHUSD | `R2|donchian=20|exit=10` | 27 | 94.95 | 76.85 | 100.58 | 2.20 | 25.9% | 4/7 | 3.2 | 99.77 | 100.58 | 100.58 | fail |
| R3 | BTCUSD | `R3|3R/48h` | 101 | 52.06 | 38.94 | 145.99 | 1.54 | 42.6% | 7/8 | 4.1 | 142.93 | 144.39 | 140.35 | **PASS** |
| R3 | ETHUSD | `R3|3R/48h` | 94 | 65.41 | 51.71 | 107.23 | 1.45 | 46.8% | 6/8 | 4.6 | 102.10 | 106.89 | 105.13 | fail |
| R4 | BTCUSD | `R4|weekend|N=2m` | 62 | -37.48 | -46.17 | -25.85 | 0.54 | 35.5% | 2/8 | 2.8 | -26.48 | -26.68 | -28.69 | fail |
| R4 | ETHUSD | `R4|weekend|N=2m` | 16 | -25.21 | -34.66 | -6.44 | 0.64 | 50.0% | 3/6 | 1.2 | -6.62 | -6.71 | -7.23 | fail |
| R5 | PAIR | `R5|z168h|in=2.0|N=2m` | 114 | -18.50 | -27.79 | -38.57 | 0.80 | 43.9% | 2/8 | 4.9 | -39.98 | -40.46 | -37.85 | fail |
| R6 | BTCUSD | `R6|sigma=6|m=0.6|hold=90` | 491 | -0.22 | -6.37 | -438.73 | 0.50 | 67.8% | 0/2 | 44.9 | -459.24 | -562.39 | -753.80 | fail |
| R6 | ETHUSD | `R6|sigma=6|m=0.6|hold=90` | 550 | -1.36 | -7.58 | -465.36 | 0.54 | 67.1% | 0/2 | 47.3 | -483.92 | -563.82 | -683.56 | fail |
| R0 | BTCUSD | `R0|order_flow_10s` | 478 | -0.51 | -14.31 | -690.85 | 0.01 | 1.3% | 0/2 | 69.1 | -762.80 | -690.85 | -690.85 | fail |
| R0 | ETHUSD | `R0|order_flow_10s` | 499 | -0.18 | -13.98 | -517.52 | 0.01 | 3.2% | 0/2 | 51.8 | -564.46 | -517.52 | -517.52 | fail |

## 5. What the execution change buys (holdout)
| Family | Symbol | Maker fill rate | Skipped signals | Exits paying taker | Adverse move after fill 1m / 5m / 30m (bps, + = against us) | Net bps maker/maker | Net bps taker/taker |
|---|---|---|---|---|---|---|---|
| R1 | BTCUSD | 100.0% | 0.0% | 100.0% | -0.41 / -0.72 / 1.17 | 13.03 | 5.68 |
| R1 | ETHUSD | 99.2% | 0.8% | 100.0% | -0.40 / -0.78 / -2.29 | -31.19 | -30.67 |
| R2 | BTCUSD | — | 0.0% | 100.0% | — / — / — | -21.12 | -21.12 |
| R2 | ETHUSD | — | 0.0% | 100.0% | — / — / — | 76.85 | 76.85 |
| R3 | BTCUSD | — | 87.6% | 74.3% | — / — / — | 38.94 | 37.77 |
| R3 | ETHUSD | — | 89.5% | 84.0% | — / — / — | 51.71 | 50.99 |
| R4 | BTCUSD | 100.0% | 0.0% | 100.0% | 2.60 / 1.49 / 5.25 | -46.17 | -50.42 |
| R4 | ETHUSD | 100.0% | 0.0% | 100.0% | 3.66 / 5.65 / 5.98 | -34.66 | -39.21 |
| R5 | PAIR | 95.8% | 4.2% | 100.0% | 0.59 / 1.88 / 3.09 | -27.79 | -24.20 |
| R6 | BTCUSD | 86.1% | 13.9% | 31.6% | 0.78 / 0.94 / 0.06 | -6.37 | -13.98 |
| R6 | ETHUSD | 89.7% | 10.3% | 32.9% | 0.55 / 0.64 / 0.48 | -7.58 | -14.79 |
| R0 | BTCUSD | — | 0.0% | 100.0% | — / — / — | -14.31 | -14.31 |
| R0 | ETHUSD | — | 0.0% | 100.0% | — / — / — | -13.98 | -13.98 |

## 6. Walk-forward (out of sample, 12 m train / 3 m test)
| Family | Symbol | OOS trades | OOS net $ | OOS net bps | Positive test quarters |
|---|---|---|---|---|---|
| R1 | BTCUSD | 245 | 195.68 | 42.16 | 11/17 |
| R1 | ETHUSD | 147 | 57.66 | 14.60 | 7/17 |
| R2 | BTCUSD | 51 | -0.71 | -3.21 | 6/17 |
| R2 | ETHUSD | 49 | 33.01 | -14.38 | 6/17 |
| R3 | BTCUSD | 249 | 65.82 | 10.54 | 9/17 |
| R3 | ETHUSD | 241 | 160.91 | 36.89 | 9/17 |
| R4 | BTCUSD | 100 | 6.71 | 1.24 | 8/17 |
| R4 | ETHUSD | 34 | 10.79 | 11.21 | 5/17 |
| R5 | PAIR | 258 | 27.01 | 14.88 | 8/17 |
| R6 | BTCUSD | 99 | -104.31 | -8.88 | 0/1 |
| R6 | ETHUSD | 123 | -99.32 | -7.77 | 0/1 |
| R0 | BTCUSD | 131 | -182.01 | -13.79 | 0/1 |
| R0 | ETHUSD | 136 | -148.37 | -15.39 | 0/1 |

## 7. Pass-bar checks
| Family | Symbol | Net>0 at 2x slippage | Net>0 with 30% maker→taker | PF>1.2 | Enough trades | ≥60% quarters positive | DD<20% |
|---|---|---|---|---|---|---|---|
| R1 | BTCUSD | yes | yes | yes | yes | yes | yes |
| R1 | ETHUSD | no | no | no | yes | no | yes |
| R2 | BTCUSD | no | no | no | no | no | yes |
| R2 | ETHUSD | yes | yes | yes | no | no | yes |
| R3 | BTCUSD | yes | yes | yes | yes | yes | yes |
| R3 | ETHUSD | yes | yes | yes | no | yes | yes |
| R4 | BTCUSD | no | no | no | no | no | yes |
| R4 | ETHUSD | no | no | no | no | no | yes |
| R5 | PAIR | no | no | no | yes | no | yes |
| R6 | BTCUSD | no | no | no | yes | no | no |
| R6 | ETHUSD | no | no | no | yes | no | no |
| R0 | BTCUSD | no | no | no | yes | no | no |
| R0 | ETHUSD | no | no | no | yes | no | no |
Trade minimum: 100, except R1 and R2 at 60 (multi-day holds).

## 8. Multiple testing
Best holdout result by net $: **R3 BTCUSD** (`R3|3R/48h`, 145.99 $). Daily Sharpe 0.189. With 148 variants tried, the deflated Sharpe probability (that it beats the best of that many zero-edge trials) is **0.00**, and the chance a best-of-148 zero-edge result looks this good is **0.99**. (Variance of Sharpe across trials is estimated from the chosen holdout results, so this is approximate.)

## 9. Break-even cost level (chosen variants, holdout)
| Family | Symbol | Gross edge (bps/trade) | Cost paid: fees+GST+slippage+funding (bps/trade) | Cost would need to fall to (bps) | Cost ratio needed |
|---|---|---|---|---|---|
| R1 | BTCUSD | 21.32 | 8.29 | 21.32 | 2.57x |
| R1 | ETHUSD | -22.35 | 8.84 | 0.00 | never (gross edge ≤ 0) |
| R2 | BTCUSD | -7.85 | 13.27 | 0.00 | never (gross edge ≤ 0) |
| R2 | ETHUSD | 94.95 | 18.11 | 94.95 | 5.24x |
| R3 | BTCUSD | 52.06 | 13.12 | 52.06 | 3.97x |
| R3 | ETHUSD | 65.41 | 13.70 | 65.41 | 4.77x |
| R4 | BTCUSD | -37.48 | 8.69 | 0.00 | never (gross edge ≤ 0) |
| R4 | ETHUSD | -25.21 | 9.45 | 0.00 | never (gross edge ≤ 0) |
| R5 | PAIR | -18.50 | 9.30 | 0.00 | never (gross edge ≤ 0) |
| R6 | BTCUSD | -0.22 | 6.15 | 0.00 | never (gross edge ≤ 0) |
| R6 | ETHUSD | -1.36 | 6.21 | 0.00 | never (gross edge ≤ 0) |
| R0 | BTCUSD | -0.51 | 13.80 | 0.00 | never (gross edge ≤ 0) |
| R0 | ETHUSD | -0.18 | 13.80 | 0.00 | never (gross edge ≤ 0) |

## 10. Winner
Passed: R1 BTCUSD (`R1|pct=10|atr4h=2|hold=72h|N=2m`), R3 BTCUSD (`R3|3R/48h`). A paper run starts through the same paper engine; no live order is turned on.

## 11. Diagnosis
**Two cells pass the pre-registered bar, but the evidence does not yet justify trusting either, and nothing is live.** R1 BTCUSD (funding-extreme fade, maker entry) and R3 BTCUSD (hourly compression breakout, 3R/48 h) cleared every check on the holdout. Everything else failed. A paper run of R3 BTCUSD has been started on live Delta data (no orders are sent); R1 BTCUSD was not started because its pass is the thinner one.

**Why I would not rely on them yet**
- **Multiple testing.** After 148 variants (56 from round 1, 92 now), a best holdout Sharpe this size is what pure luck produces about 99% of the time (deflated Sharpe probability 0.00, section 8). That estimate is rough (about 100 trading days of holdout P&L), but it points the same way as the other warnings.
- **R1 BTCUSD is marginal**: profit factor 1.22 against a 1.2 bar, 5 of 8 quarters positive, and **R1 ETHUSD, the same strategy, loses** (−31 net bps/trade). A real effect would normally show on both.
- **R3 is more consistent**: both symbols are positive on the holdout (BTC +146 $, ETH +107 $) and in walk-forward, and it is positive in 7 of 8 quarters on BTC. ETH misses only on trade count (94 against the minimum of 100), which I did not relax. But the top 5 trades are 48% of BTC's net profit (still +76 $ without them), and R3's gross edge (52 bps) is large enough that I would want to see it survive the paper run before believing it.
- **The maker model is optimistic.** With one-minute bars the best bid and ask are approximated by the previous close, and a limit fills whenever price trades even one tick through it. That gives 96–100% fill rates and *favourable* adverse selection for R1, R4 and R5, which real books will not give. The recorder (running now, healthy) exists to measure this; until it has 2+ weeks of data, treat every maker/taker gap below as an upper bound. R2 and R3 use stop-entry or market entries, so they are not affected on entry (only the target exit is maker, and the maker/taker gap is small for them).

**What the execution change bought** (maker/maker vs taker/taker, holdout net bps/trade): R1 BTC +13.0 vs +5.7; R4 BTC −46 vs −50; R5 −27.8 vs −24.2 (maker was worse: the fills it got were the unprofitable ones); R3, R2: almost no difference because their entries cannot rest.

**The control and the tick families.** R0 (order-flow direction, 60 s hold, real 1-second Binance data) loses its cost almost exactly: gross −0.5 and −0.2 bps, net −14.3 and −14.0 bps against a modelled cost of 13.8 bps per trade, win rate 1 to 3%. That is the cost model and fill model behaving correctly. **R6 (liquidation-cascade fade on real ticks) has no edge** (gross −0.2 and −1.4 bps, 67% win rate with losses much larger than wins): the one-minute-candle version in round 1 was not hiding a good strategy.

**Costs vs edge** (section 9): R1 BTC and R3 would break even at costs 2.6x and 4x higher than now. R2 ETH has a large gross edge (95 bps) but only 27 trades, so it is not testable. R4 (weekend and session effects), R5 (ETH/BTC ratio) and R6 have no gross edge at all: no fee level rescues them.

**Limits to keep in mind.** The tick families cover only 15 months (Sept 2025 on) because of download time, so they have one walk-forward fold; signals come from Binance and are executed on Binance prices with Delta's costs (median 1-minute level gap 1.0 bps BTC and 1.9 bps ETH, p99 43 and 22 bps); funding is Binance's; slippage is still the assumed 1 bp; leverage is fixed at 10x, so the liquidation rule rejects many wide-stop signals (about 15% of R1 BTC signals), which favours the low-volatility periods that remain.

**Next, in order:** (1) let the recorder run 2+ weeks and replace the assumed slippage and the previous-close maker proxy with measured values, then re-run R1 and R3 once (no new variants); (2) let the R3 BTCUSD paper run reach 4 weeks or 50 trades before reading it (`reports/paper_weekly.md`); (3) a small-live plan is not proposed: nothing has passed paper yet.


## 12. Order-book recorder
Status file: `data/delta_book/status.json`:
```
{"as_of": 1795504420.667468, "uptime_s": 3860, "connected": {"book": true, "trades": true}, "counts": {"book_rows": 7688, "trades": 23734, "reconnects": 0, "gaps": 2}, "seconds_since_last_book": {"BTCUSD": 0.3, "ETHUSD": 0.5}, "seconds_since_last_trade": {"BTCUSD": 0.6, "ETHUSD": 1.1}, "healthy": true}
```
Start: `python -m app.delta_lab.recorder` (BTCUSD and ETHUSD; read-only, no keys); table: `python -m app.delta_lab.measure_costs`.
