# 5 EMA alert candle: why the 05:16 order had a ~12R target, and which exit works (25 Nov 2026)

*Hypothetical backtest on completed historical bars; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

## 1. How the live order sets its stop and target (code: `delta_execution/service.py` `maybe_place`, `crypto_delta/forward.py` `replay`)
- **Alert candle:** a completed candle that is entirely above EMA(5), i.e. its low is above the EMA. **Entry signal:** a later candle trades below the alert low.
- **Live entry:** the app only acts on COMPLETED candles. It takes the close of the breakout candle as the reference price and sends a marketable limit order 0.3% through that price.
- **Stop** = the alert candle's high. **Target** = reference - 3 x (stop - reference). This is the intended 3R, measured from the candle close and attached to the entry as a bracket (fixed prices).
- **The 05:16 UTC order, from Delta's own candles:**
  - The alert candle was 05:05, with high 59,253.5, which became the stop.
  - The breakout candle was 05:10, with low 59,072 (below the alert low of 59,174) and close **59,119**.
  - Target = 59,119 - 3 x 134.5 = **58,715.5**, so the intended trade was 3R with R = 134.5.
- **Why it became ~12R:**
  - The check ran at 05:16:26 (its time stamp), but the order reached Delta at **05:20:52**: the scheduler pass was slow, and the freshness check used the pass's start time.
  - By then price had bounced to about 59,211, and the marketable limit (sell at 58,941 or better) filled at **59,211.5**.
  - The bracket prices were already fixed from 59,119, so the real R was only 42 points (59,253.5 - 59,211.5), while the target was 496 away: **11.8R**.
  - The 05:15 candle's high of 59,249 came within 4.5 points of the stop.
- **Verdict:** this is not the intended rule. It is a bug: the order was sent late, and the stop and target are not re-derived from the price actually paid.
  - The late send is **fixed** (deployed today, commit f87a626): freshness is now judged at the moment of sending, so this order would now be refused as stale (sent more than one 5m candle after the candle closed).
  - The second part, R shrinking when the fill drifts toward the stop, is **not fixed**. A proposal is below.

## 2. Exit variants (same entries, only the exit changes)
**Entries** (as live): sell side only, entry at the breakout candle close (next 1-minute open + 1 bp), stop at the alert high, one position per symbol.
**Symbols:** BTC, ETH, SOL and LISTA, i.e. the live test's coin plus the scanner's 4. **Bars:** 5m and 15m from Binance 1-minute data, Jan 2023 to Nov 2026.
**Costs:** Delta taker 0.05% + 18% GST on every fill, 1-3 bps slippage, about 14 bps per round trip.
**Splits:** pooled per timeframe, by time: 50% train, 25% validation, 25% holdout. The exit was chosen on validation; the holdout was used once.
**Variants:** 15 per timeframe, so **30 added to the running total**: fixed 1/1.5/2/3R; target at the nearest swing low, previous-day low, session VWAP or round number (at least 1R away, else 3R); nearest of those; half at 1R or the nearest level with the rest trailed; ATR or prior-candle trailing stop; time stop after 6 or 12 bars.

### 5m (75793 entries; median R 25 bps, 10% of trades have R under 7 bps)

| Exit | Train net bps (trades) | Validation net bps | Validation PF |
|---|---|---|---|
| fixed_1R | -14.5 (37896) | -15.2 | 0.47 |
| fixed_1.5R | -14.4 (37896) | -15.7 | 0.52 |
| fixed_2R | -14.8 (37896) | -15.9 | 0.54 |
| fixed_3R | -14.7 (37896) | -15.8 | 0.58 |
| level_swing | -15.1 (37896) | -16.1 | 0.57 |
| level_prev_day | -15.6 (37896) | -16.3 | 0.58 |
| level_vwap | -14.7 (37896) | -15.9 | 0.57 |
| level_round | -14.9 (37896) | -16.2 | 0.57 |
| level_nearest | -15.1 (37896) | -16.0 | 0.55 |
| partial_1R_trail | -15.4 (37896) | -16.1 | 0.25 |
| partial_level_trail | -15.6 (37896) | -16.3 | 0.26 |
| trail_atr_1.5 | -15.6 (37896) | -15.9 | 0.46 |
| trail_prior_candle | -15.7 (37896) | -16.3 | 0.27 |
| time_6 | -15.7 (37896) | -16.3 | 0.40 |
| time_12 | -15.5 (37896) | -16.7 | 0.47 |

**Holdout (touched once):**

 | Exit | Trades | Net bps | PF | Win rate | Max DD (bps, summed) | Bootstrap 90% |
|---|---|---|---|---|---|---|
| fixed_1R (chosen) | 18949 | -14.1 | 0.42 | 35% | 267190 | -14.7 to -13.6 |
| fixed_3R (current rule) | 18949 | -14.2 | 0.55 | 25% | 269521 | -15.0 to -13.4 |

### 15m (23911 entries; median R 44 bps, 10% of trades have R under 12 bps)

| Exit | Train net bps (trades) | Validation net bps | Validation PF |
|---|---|---|---|
| fixed_1R | -13.8 (11955) | -14.9 | 0.64 |
| fixed_1.5R | -14.5 (11955) | -14.9 | 0.69 |
| fixed_2R | -14.4 (11955) | -14.2 | 0.72 |
| fixed_3R | -14.2 (11955) | -14.1 | 0.74 |
| level_swing | -15.3 (11955) | -14.8 | 0.73 |
| level_prev_day | -15.9 (11955) | -14.7 | 0.74 |
| level_vwap | -14.8 (11955) | -14.1 | 0.74 |
| level_round | -14.7 (11955) | -14.3 | 0.73 |
| level_nearest | -15.7 (11955) | -15.8 | 0.69 |
| partial_1R_trail | -15.4 (11955) | -16.6 | 0.41 |
| partial_level_trail | -16.0 (11955) | -16.6 | 0.43 |
| trail_atr_1.5 | -16.8 (11955) | -16.7 | 0.62 |
| trail_prior_candle | -16.3 (11955) | -16.8 | 0.43 |
| time_6 | -15.9 (11955) | -17.2 | 0.55 |
| time_12 | -16.4 (11955) | -17.4 | 0.62 |

**Holdout (touched once):**

 | Exit | Trades | Net bps | PF | Win rate | Max DD (bps, summed) | Bootstrap 90% |
|---|---|---|---|---|---|---|
| level_vwap (chosen) | 5979 | -12.6 | 0.72 | 29% | 75082 | -14.8 to -10.0 |
| fixed_3R (current rule) | 5979 | -13.1 | 0.72 | 27% | 78196 | -15.4 to -10.5 |

## 3. Verdict
- **No exit makes 5 EMA profitable.** Every one of the 30 variants loses about 12-17 bps per trade, on both timeframes and on all 4 coins, in train, validation and holdout.
- The chosen exits only tie the current 3R rule on the holdout: 5m fixed 1R -14.1 vs -14.2; 15m VWAP -12.6 vs -13.1, with the bootstrap ranges overlapping.
- **Why:** before costs the entry is about break-even, and the round trip costs about 14 bps. The stop is very close: a median of 25 bps on 5m, and 1 trade in 10 risks under 7 bps, i.e. less than the fees. Moving the target cannot fix an entry with no edge.
- **Proposal (not applied):**
  1. Keep the stop and target tied to the price actually filled: refuse to send if price has already moved more than about 0.25R toward the stop since the candle close (fail closed), or re-derive the bracket from the live price at send time.
  2. Because no exit gives an edge, **I'd recommend not keeping 5 EMA live**: stop the live test and leave the scanner's Live off, or keep them only as small paid experiments.
  3. An entry filter such as "skip trades whose stop is smaller than about 3x the round-trip cost" is the obvious next idea. **It is untested:** the holdout is now used up, so it needs a rule fixed in advance and new paper data.

Code: `backend/app/delta_lab/five_ema_exits.py`; numbers: `reports/five_ema_exits.json`.
