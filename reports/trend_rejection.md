# Trend + rejection shorts (owner's rule, 25 Nov 2026)

*Hypothetical backtest on completed historical bars; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

**The rule as written down before testing** (full definitions are in `backend/app/delta_lab/trend_rejection.py`):
- **Trend:** 1h lower highs and lower lows mean shorts only; the mirror means longs only; otherwise no trade.
- **Entry:** a 5m or 15m candle tags the nearest resistance (the last 1h swing high, or session VWAP) and closes back below it, as a red candle. Short at that close.
- **Stop:** just above the rejection high.
- **Exit:** no target. Stop to break-even at +1R, then trail it over each new lower swing high.
- **Skip:** trades whose stop is under 2x costs, and (variant) trades with less than 2R of room to the last swing low.

**Data:** BTC and ETH, Binance 1-minute data from Jan 2023 to Nov 2026, Delta fees + GST + slippage. Split by time 50/25/25, chosen on validation. **6 variants added to the running total.** Today's 5 EMA test already used this same data, so this holdout is not fresh.

| Variant | Train net bps (trades) | Validation net bps (trades) | Validation PF | Validation win rate |
|---|---|---|---|---|
| 5m|trail_5m|room2R=off | -13.0 (1820) | -11.4 (923) | 0.68 | 22% |
| 5m|trail_5m|room2R=on | -13.9 (1173) | -10.8 (637) | 0.68 | 22% |
| 5m|trail_15m|room2R=off | -12.2 (1758) | -13.4 (895) | 0.64 | 14% |
| 5m|trail_15m|room2R=on | -11.9 (1149) | -12.6 (623) | 0.64 | 13% |
| 15m|trail_15m|room2R=off | -13.6 (2475) | -10.6 (1137) | 0.75 | 20% |
| 15m|trail_15m|room2R=on | -12.1 (1430) | -9.4 (690) | 0.75 | 18% |

**Holdout (15m|trail_15m|room2R=on):** 704 trades, **-11.7 net bps per trade**, PF 0.67, win rate 19%, bootstrap 90% -17.2 to -5.9 bps. Shorts -12.6 bps (404), longs -10.5 bps (300). BTC -7.8, ETH -14.0. Median stop 42 bps, median hold 67 minutes, average result +0.02R before costs.

## Reading
- **As written mechanically, the rule loses about 9-14 bps per trade in every variant and split.** That is roughly the cost of a round trip: before costs it is about break-even (average +0.02R).
- About 4 in 5 trades end at the stop or at break-even. The trend rides that do happen are not big enough to pay for them.
- **What this does NOT show:** your discretionary reading of the chart. You judge "real" rejections and reversals in ways these definitions don't capture: wick size, volume, how many times a level held, the higher-timeframe picture. Before anything goes live, the useful next step is to log your own calls as paper trades (you mark entries, the app records them with the same costs) for a few weeks. That gives fresh data that hasn't been tuned on.

Numbers: `reports/trend_rejection.json`.
