# Delta Exchange India intraday strategy lab: summary

*Hypothetical backtests on stored public candles. Nothing here is a real position, an order or money at risk, and none of it is mixed with real portfolio P&L. Decision support, not investment advice. No live order was sent.*

## 1. What this could and could not test
- Delta India publishes **candles only** (last price, mark price, funding). There are **no historical ticks, no L1/L2 quotes and no trade tape**. So F2 runs on 1-minute candles (not 1 s) and F6 cannot use signed order flow: it runs as a labelled **candle-direction proxy** (sign of the last 1-minute bar). Slippage cannot be measured and is an **assumed** 1.0 bp (never below one tick), doubled in the stress run.
- Only USD-quoted perpetuals exist on Delta India: `BTCUSDT` and `ETHUSDT` do not exist, so BTCUSD and ETHUSD are the two tests. No alt was added: its round-trip cost cannot be measured here, so the under-20-bps condition cannot be shown.
- Sizing uses a notional $1,000 wallet (your real balance is 0), 0.5% risk per trade, isolated margin at 10x, no compounding. This is a fixed research setting, not your account.
- F5 and F6 have no stop in the brief but the risk gate requires one: F5 uses 1.5 x ATR(1h), the F6 proxy 1.0 x ATR(1h). Fixed before any result was seen.
- The brief's funding-window rule (no orders within 2 minutes of a funding timestamp) also blocks F5's 00:00 UTC entry every day. That is the rule working as written, not a bug: it shows up in the rejection counts.

## 2. Data checks
| Symbol | First bar (UTC) | Last bar (UTC) | 1-min bars | Missing minutes | Missing-bar gaps | Largest gap (min) | Duplicate bars | Out of order | Funding points | Median funding %/8 h | Ticks | Quotes | Trades |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | 2023-12-29T13:26 | 2026-11-24T04:18 | 1,497,070 | 30,223 | 9 | 15802 | 0 | 0 | 25,683 | 0.0100 | none | none | none |
| ETHUSD | 2024-02-06T08:53 | 2026-11-24T04:18 | 1,467,381 | 4,025 | 6 | 3858 | 0 | 0 | 24,508 | 0.0100 | none | none | none |

Mark price: Delta public MARK:<symbol> candles (the exchange's own mark price, not rebuilt). Contract specs from the public product endpoint: contract_value=0.001; tick_size=0.5; mm_pct=0.0025; im_pct_min=0.005; slope_threshold_notional=100000; max_leverage=2E+2; taker_fee=0.0005; maker_fee=0.0002.

## 3. Cost model (fixed before any result)
Taker 0.05% and maker 0.02% on notional at both open and close (the exchange's published rates for these contracts; your account's own tier could not be read: the key has no balance and no fill report exists), GST 18% on fees, historical funding at 00/08/16 UTC from the FUNDING series, slippage max(1 tick, 1.0 bp) per taker fill, maker fills only when price trades strictly through the limit, 1-minute bar latency (250 ms is below the bar; the stress adds one bar). Stops trigger on mark price; stop and target in the same bar resolve to the stop. Stress runs: 2x slippage, 30% of maker fills turned taker, +1 bar latency, and all three together.

## 4. Variants tried
**56** variants in total (28 per symbol: F1 4, F2 8, F3 8, F4 3, F5 4, F6 1). Stress and latency reruns are the same chosen variants under harsher cost assumptions, not new variants.

## 5. Results by family and symbol
Choice rule: the variant with the highest MEDIAN monthly net P&L in the train windows (walk-forward) and over the whole first 75% (final choice). Holdout = the last 25%, run once per cost assumption with the chosen values only.

### 5a. Holdout scoreboard
| Symbol | Family | Chosen variant | Trades | Net bps/trade | Net P&L ($) | PF | Win rate | Positive months | Max DD (% of $1,000) | Net under 2x slippage ($) | Passed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | F1 | `F1|pct=10|atr=2.5` | 163 | -7.16 | -133.90 | 0.75 | 41.1% | 3/9 | 18.4 | -158.35 | fail |
| BTCUSD | F2 | `F2|sigma=6|m=0.6|hold=90` | 404 | -9.60 | -328.67 | 0.52 | 66.6% | 0/9 | 33.0 | -342.50 | fail |
| BTCUSD | F3 | `F3|pct=10|atr=3|hold=24h` | 89 | -1.38 | -13.36 | 0.92 | 43.8% | 3/9 | 4.4 | -16.11 | fail |
| BTCUSD | F4 | `F4|lookback=24h` | 595 | -6.25 | -274.96 | 0.77 | 27.2% | 1/9 | 32.6 | -305.78 | fail |
| BTCUSD | F5 | `F5|prior=30m|hold=30m` | 259 | -12.64 | -253.72 | 0.42 | 32.8% | 0/9 | 26.8 | -285.22 | fail |
| BTCUSD | F6 | `F6|proxy` | 1145 | -13.98 | -1,452.38 | 0.02 | 4.1% | 0/9 | 145.2 | -1,577.00 | fail |
| ETHUSD | F1 | `F1|pct=20|atr=2.5` | 304 | -8.79 | -164.43 | 0.84 | 35.9% | 4/9 | 24.0 | -198.65 | fail |
| ETHUSD | F2 | `F2|sigma=6|m=0.3|hold=90` | 498 | -13.86 | -443.56 | 0.50 | 65.3% | 0/9 | 45.2 | -456.44 | fail |
| ETHUSD | F3 | `F3|pct=5|atr=2|hold=8h` | 112 | -9.53 | -25.84 | 0.87 | 43.8% | 3/9 | 7.1 | -29.65 | fail |
| ETHUSD | F4 | `F4|lookback=12h` | 717 | -8.66 | -222.53 | 0.81 | 25.1% | 3/9 | 32.0 | -250.82 | fail |
| ETHUSD | F5 | `F5|prior=30m|hold=30m` | 254 | -8.80 | -144.37 | 0.57 | 35.8% | 2/9 | 16.5 | -170.49 | fail |
| ETHUSD | F6 | `F6|proxy` | 1209 | -14.07 | -1,147.44 | 0.03 | 6.7% | 0/9 | 114.7 | -1,242.44 | fail |

### 5b. Holdout detail (baseline costs)
| Symbol | Family | Gross bps/trade | Gross $ | Fees $ | GST $ | Funding $ | Slippage $ | Net $ | Avg win $ | Avg loss $ | Worst day $ | Sharpe (daily) | Liquidations | Funding rates missing |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | F1 | 5.57 | 104.22 | 172.68 | 31.08 | 1.89 | 32.46 | -133.90 | 6.09 | -5.65 | -23.80 | -3.04 | 0 | 0 |
| BTCUSD | F2 | -3.31 | -66.82 | 209.99 | 37.80 | 0.24 | 13.83 | -328.67 | 1.33 | -5.09 | -23.16 | -6.70 | 0 | 0 |
| BTCUSD | F3 | 7.80 | 12.15 | 19.29 | 3.47 | -0.01 | 2.75 | -13.36 | 3.89 | -3.30 | -10.29 | -0.63 | 0 | 0 |
| BTCUSD | F4 | 3.04 | 12.51 | 215.74 | 38.83 | 2.07 | 30.82 | -274.96 | 5.62 | -2.74 | -24.35 | -2.40 | 0 | 1 |
| BTCUSD | F5 | 1.17 | 13.38 | 193.54 | 34.84 | 0.00 | 38.71 | -253.72 | 2.14 | -2.50 | -7.28 | -5.82 | 0 | 0 |
| BTCUSD | F6 | -0.18 | -13.78 | 1,042.46 | 187.64 | 0.00 | 208.49 | -1,452.38 | 0.51 | -1.34 | -23.17 | -33.92 | 0 | 0 |
| ETHUSD | F1 | 3.73 | 111.59 | 201.30 | 36.23 | 0.93 | 37.56 | -164.43 | 7.70 | -5.15 | -18.76 | -1.86 | 0 | 0 |
| ETHUSD | F2 | -7.54 | -202.54 | 193.40 | 34.81 | -0.07 | 12.88 | -443.56 | 1.38 | -5.15 | -19.08 | -8.18 | 0 | 0 |
| ETHUSD | F3 | -0.87 | 7.27 | 26.71 | 4.81 | -2.22 | 3.81 | -25.84 | 3.61 | -3.22 | -17.73 | -1.08 | 0 | 0 |
| ETHUSD | F4 | 0.65 | 41.37 | 198.07 | 35.65 | 1.88 | 28.30 | -222.53 | 5.38 | -2.22 | -18.19 | -1.51 | 0 | 1 |
| ETHUSD | F5 | 5.00 | 55.08 | 144.53 | 26.02 | 0.00 | 28.91 | -144.37 | 2.13 | -2.08 | -6.85 | -3.61 | 0 | 0 |
| ETHUSD | F6 | -0.27 | -15.76 | 820.06 | 147.61 | 0.00 | 164.01 | -1,147.44 | 0.49 | -1.05 | -16.03 | -36.02 | 0 | 0 |

### 5c. Stress results on the holdout (net $ / trades)
| Symbol | Family | Baseline | 2x slippage | 30% maker→taker | +1 bar latency | All three |
|---|---|---|---|---|---|---|
| BTCUSD | F1 | -133.90 / 163 | -158.35 / 163 | -139.61 / 163 | -148.19 / 163 | -179.60 / 163 |
| BTCUSD | F2 | -328.67 / 404 | -342.50 / 404 | -424.22 / 404 | -519.56 / 332 | -626.04 / 330 |
| BTCUSD | F3 | -13.36 / 89 | -16.11 / 89 | -17.29 / 89 | -10.46 / 89 | -17.91 / 89 |
| BTCUSD | F4 | -274.96 / 595 | -305.78 / 595 | -315.92 / 595 | -260.66 / 586 | -340.34 / 586 |
| BTCUSD | F5 | -253.72 / 259 | -285.22 / 259 | -253.72 / 259 | -255.83 / 259 | -288.61 / 259 |
| BTCUSD | F6 | -1,452.38 / 1145 | -1,577.00 / 1117 | -1,452.38 / 1145 | -1,460.49 / 1132 | -1,606.96 / 1114 |
| ETHUSD | F1 | -164.43 / 304 | -198.65 / 304 | -174.48 / 304 | -165.60 / 304 | -209.85 / 304 |
| ETHUSD | F2 | -443.56 / 498 | -456.44 / 498 | -529.79 / 498 | -626.81 / 397 | -720.52 / 396 |
| ETHUSD | F3 | -25.84 / 112 | -29.65 / 112 | -31.09 / 112 | -39.61 / 112 | -49.87 / 112 |
| ETHUSD | F4 | -222.53 / 717 | -250.82 / 717 | -249.45 / 713 | -345.30 / 707 | -411.80 / 703 |
| ETHUSD | F5 | -144.37 / 254 | -170.49 / 254 | -144.37 / 254 | -139.30 / 254 | -165.60 / 254 |
| ETHUSD | F6 | -1,147.44 / 1209 | -1,242.44 / 1161 | -1,147.44 / 1209 | -1,180.05 / 1236 | -1,255.95 / 1147 |

### 5d. Walk-forward (out of sample, first 75%)
| Symbol | Family | OOS trades | OOS net $ | OOS net bps/trade | Positive test months | Folds |
|---|---|---|---|---|---|---|
| BTCUSD | F1 | 565 | -617.94 | -11.20 | 3/25 | 25 |
| BTCUSD | F2 | 893 | -798.68 | -11.06 | 1/25 | 25 |
| BTCUSD | F3 | 229 | -85.57 | -6.37 | 10/25 | 25 |
| BTCUSD | F4 | 1952 | -750.10 | -12.53 | 2/25 | 25 |
| BTCUSD | F5 | 735 | -642.24 | -14.03 | 3/25 | 25 |
| BTCUSD | F6 | 3536 | -3,569.52 | -13.90 | 0/25 | 25 |
| ETHUSD | F1 | 645 | 22.98 | 1.40 | 14/23 | 23 |
| ETHUSD | F2 | 952 | -514.42 | -11.04 | 2/23 | 23 |
| ETHUSD | F3 | 126 | -35.07 | -39.03 | 9/23 | 23 |
| ETHUSD | F4 | 1824 | -411.38 | -12.93 | 7/23 | 23 |
| ETHUSD | F5 | 680 | -351.81 | -11.22 | 3/23 | 23 |
| ETHUSD | F6 | 3823 | -2,597.33 | -14.08 | 0/23 | 23 |

### 5e. Pass-bar checks (holdout)
| Symbol | Family | Net>0 at 2x slippage | PF>1.2 | Enough trades | ≥60% months positive | DD<20% |
|---|---|---|---|---|---|---|
| BTCUSD | F1 | no | no | no | no | yes |
| BTCUSD | F2 | no | no | yes | no | no |
| BTCUSD | F3 | no | no | no | no | yes |
| BTCUSD | F4 | no | no | yes | no | no |
| BTCUSD | F5 | no | no | yes | no | no |
| BTCUSD | F6 | no | no | yes | no | no |
| ETHUSD | F1 | no | no | yes | no | no |
| ETHUSD | F2 | no | no | yes | no | no |
| ETHUSD | F3 | no | no | yes | no | yes |
| ETHUSD | F4 | no | no | yes | no | no |
| ETHUSD | F5 | no | no | yes | no | yes |
| ETHUSD | F6 | no | no | yes | no | no |

### 5f. Monthly P&L on the holdout ($, baseline)
**BTCUSD**

| Family | 2026-03 | 2026-04 | 2026-05 | 2026-06 | 2026-07 | 2026-08 | 2026-09 | 2026-10 | 2026-11 |
|---|---|---|---|---|---|---|---|---|---|
| F1 | 32.66 | -24.20 | -6.40 | -23.20 | 26.51 | -63.91 | -63.18 | 0.72 | -12.90 |
| F2 | -34.75 | -32.43 | -14.52 | -44.72 | -14.24 | -76.64 | -31.90 | -31.86 | -47.59 |
| F3 | 16.22 | -9.49 | -2.86 | 11.92 | -3.29 | -1.71 | -10.89 | -15.16 | 1.89 |
| F4 | -8.37 | -61.07 | -73.62 | 54.68 | -47.39 | -25.63 | -33.59 | -75.72 | -4.26 |
| F5 | -5.76 | -17.88 | -15.03 | -22.49 | -56.95 | -48.35 | -20.64 | -44.84 | -21.78 |
| F6 | -89.83 | -155.66 | -177.69 | -129.36 | -181.20 | -237.07 | -169.68 | -197.32 | -114.56 |

**ETHUSD**

| Family | 2026-03 | 2026-04 | 2026-05 | 2026-06 | 2026-07 | 2026-08 | 2026-09 | 2026-10 | 2026-11 |
|---|---|---|---|---|---|---|---|---|---|
| F1 | 17.88 | -84.64 | -35.82 | -12.46 | -17.79 | -48.32 | 0.55 | 11.75 | 4.43 |
| F2 | -30.89 | -106.27 | -30.53 | -38.64 | -62.55 | -59.12 | -64.16 | -35.52 | -15.89 |
| F3 | 0.31 | -42.18 | -1.73 | 16.76 | -7.28 | -5.54 | -15.58 | 46.27 | -16.87 |
| F4 | 14.63 | -3.44 | -92.76 | -12.22 | 8.33 | -46.36 | -31.49 | -100.35 | 41.14 |
| F5 | 0.70 | -14.88 | 3.80 | -6.42 | -24.04 | -43.16 | -14.45 | -40.97 | -4.94 |
| F6 | -64.72 | -129.22 | -141.47 | -113.31 | -145.29 | -170.47 | -129.30 | -150.03 | -103.62 |

### 5g. Risk-gate rejections on the holdout (baseline costs)
| Symbol | Family | Reason | Count |
|---|---|---|---|
| BTCUSD | F1 | skipped_position_open | 1,855 |
| BTCUSD | F1 | funding_window | 5 |
| BTCUSD | F2 | skipped_position_open | 108 |
| BTCUSD | F2 | funding_window | 4 |
| BTCUSD | F2 | daily_loss_limit | 3 |
| BTCUSD | F3 | skipped_position_open | 993 |
| BTCUSD | F3 | funding_window | 6 |
| BTCUSD | F3 | stop_too_close_to_liquidation | 2 |
| BTCUSD | F4 | skipped_position_open | 5,265 |
| BTCUSD | F4 | consecutive_losses | 187 |
| BTCUSD | F4 | funding_window | 103 |
| BTCUSD | F4 | funding_window+consecutive_losses | 16 |
| BTCUSD | F4 | daily_loss_limit+consecutive_losses | 1 |
| BTCUSD | F5 | funding_window | 260 |
| BTCUSD | F6 | consecutive_losses | 351,122 |
| BTCUSD | F6 | skipped_position_open | 3,331 |
| BTCUSD | F6 | funding_window+consecutive_losses | 3,003 |
| BTCUSD | F6 | daily_loss_limit+consecutive_losses | 2,133 |
| BTCUSD | F6 | funding_window | 764 |
| BTCUSD | F6 | funding_window+daily_loss_limit+consecutive_losses | 17 |
| ETHUSD | F1 | skipped_position_open | 3,531 |
| ETHUSD | F1 | funding_window | 9 |
| ETHUSD | F2 | skipped_position_open | 158 |
| ETHUSD | F2 | consecutive_losses | 6 |
| ETHUSD | F2 | funding_window | 4 |
| ETHUSD | F2 | stop_too_close_to_liquidation | 1 |
| ETHUSD | F2 | consecutive_losses+stop_too_close_to_liquidation | 1 |
| ETHUSD | F3 | skipped_position_open | 599 |
| ETHUSD | F3 | funding_window | 7 |
| ETHUSD | F3 | consecutive_losses | 4 |
| ETHUSD | F4 | skipped_position_open | 4,837 |
| ETHUSD | F4 | consecutive_losses | 296 |
| ETHUSD | F4 | funding_window | 136 |
| ETHUSD | F4 | funding_window+consecutive_losses | 22 |
| ETHUSD | F5 | funding_window | 253 |
| ETHUSD | F6 | consecutive_losses | 345,397 |
| ETHUSD | F6 | skipped_position_open | 3,497 |
| ETHUSD | F6 | funding_window+consecutive_losses | 2,948 |
| ETHUSD | F6 | funding_window | 739 |

## 6. Winner
**None passed.** No family on BTCUSD or ETHUSD met every bar. The paper phase was therefore not started.

## 7. Diagnosis
**None of the 12 family-and-symbol combinations passed, and none came close on net P&L.** Every holdout result is negative before any stress is applied.

**Why: costs are larger than the edge at this timescale.** A taker round trip costs 0.10% in fees plus 18% GST on them, which is **11.8 bps**, before slippage (about 1 to 3 bps more) and funding. Even a perfect maker-in, maker-out round trip costs 4.7 bps. The gross edge the families found, before any cost, was:

| Family | Gross edge (bps/trade, holdout) | Cost paid (bps/trade) |
|---|---|---|
| F1 compression breakout | +5.6 BTC, +3.7 ETH | about 12.8 to 12.5 |
| F2 liquidation fade (1-min candles) | −3.3 BTC, −7.5 ETH | about 6 to 6.3 (maker entry) plus larger stop losses |
| F3 funding contrarian | +7.8 BTC, −0.9 ETH | about 9.2 BTC, 8.7 ETH |
| F4 time-series momentum | +3.0 BTC, +0.7 ETH | about 9.3 BTC, 9.3 ETH |
| F5 session effects | +1.2 BTC, +5.0 ETH | about 13.8 BTC, 13.8 ETH |
| F6 candle-direction control | −0.2 BTC, −0.3 ETH | about 13.8 BTC, 13.8 ETH |

- **The control behaves as designed.** F6 has no edge (gross ≈ 0) and loses almost exactly the cost per trade (−14 bps/trade, win rate 4 to 7%, profit factor 0.02 to 0.03). That is the cost model working: a zero-edge strategy pays the round-trip cost.
- **F3 is the nearest miss** (BTC −1.4 bps/trade, −$13 on 89 trades; it also fell below the 100-trade minimum). It is the one family with a gross edge (7.8 bps) close to its costs, because it trades rarely, enters with maker orders and holds for hours so fees are spread over larger moves. It still lost money, was positive in only 3 of 9 holdout months, and needs more months of data to say anything about it.
- **F1 and F4 have small positive gross edges that taker fees erase.** F1's 2R target works as a maker exit, but its entries are taker breakouts.
- **F2 loses even gross.** The one-minute candle cannot place a limit "0.3 to 0.6 sigma further" the way a 1-second book could: a 66% win rate is paired with losses 4 times the size of wins, and the strategy is almost certainly mis-specified at this resolution. A faithful test needs tick data.
- **F5's 00:00 UTC entry never trades.** Every one of those signals falls inside the 2-minute funding window and is rejected by design (260 rejections on BTC), so only the 13:30 UTC entry runs.
- **No liquidation or gap-through-stop losses occurred**, and the stop-before-liquidation rule rejected only a handful of orders (a few F2 and F3 orders). Funding was small (under $3 per family), because holding periods are short and rates sit near 0.01% per 8 h.

**What this does not prove.** Slippage is assumed (1 bp, never under a tick), not measured; this setup cannot test F2 at 1 second or F6 on true order flow; your own fee tier could not be read (no fills on the account). A single top-of-book snapshot taken during the paper-engine test showed a 0.5 USD spread on BTCUSD (0.08 bps), so the 1 bp assumption looks conservative at small size, but one snapshot is not a measurement. The tests cover 2.9 years for BTC and 2.8 for ETH, one regime sequence, 28 variants per symbol.

**What would change the answer, in order of cost.** (1) Record the top of the book and the trade tape for a few weeks (the paper engine already records top-of-book each minute, which is the missing input for a measured slippage model). (2) A new, separately pre-registered maker-only version of F1/F3/F4: it would be a new family with its own variant count, and I have not run it, so as not to tune on this holdout. (3) Longer holds or wider targets, which dilute fixed costs but need many more months of data to test.


## 8. Paper phase and small-live plan
The paper engine (`app/delta_lab/paper.py`) is built and uses the same `risk_gate()` and `size_position()`. It starts only for a family that passes; it needs at least 4 weeks or 100 trades, whichever is later, and the comparison against the backtest described in the brief. A small-live plan (quarter size, 50 trades) will be proposed only after that, and not started without your approval.

Files: `reports/results.json`, `reports/paper_trades.csv` (holdout replay in the paper-trade schema), `reports/rejections.csv`, `reports/run.log`; code in `backend/app/delta_lab/`, tests in `backend/tests/test_delta_lab_*.py`.
