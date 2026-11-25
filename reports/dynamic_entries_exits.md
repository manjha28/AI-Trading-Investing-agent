# Dynamic entries and exits on every crypto strategy, Delta data (25 Nov 2026)

*Hypothetical backtests on completed bars; kept apart from real P&L. Decision support, not investment advice.*

**Entry styles:**
- **N0 base:** take the signal as it comes.
- **N1 confirm:** the next candle must close in the trade's direction.
- **N2 volume:** the signal candle has at least 1.5x its 20-bar average volume.
- **N3 RSI:** RSI on the trade's side of 50.
- **N4 trend:** price on the trade's side of EMA200, with EMA200 sloping that way.
- **N5:** N2 + N3 + N4 together.

**Exit styles** (the stop is always live):
- **X0 fixed:** the strategy's own target.
- **X1 ratchet:** stop to break-even at +1R, then trail 1R behind the best price.
- **X2 trend-flip:** exit on a close back across EMA9. This books profit OR cuts the loss on a reversal.
- **X3 RSI flip:** exit when RSI crosses back through 50.
- **X4 volume reversal:** exit on a candle against the trade with 2x volume.
- **X5:** X1 + X2 + X4, whichever comes first.

36 combinations per strategy, 7 strategy runs, **252 variants added**. Delta fees + GST; slippage 1-3 bps on the 10 liquid coins, 20 bps on the pump/dump coins. The pair is chosen on validation and checked once on the holdout.

| Strategy | Best pair (validation) | Train / validation net bps | **Holdout net bps** (trades, 90% range) | Base N0+X0 holdout |
|---|---|---|---|---|
| 5 EMA 5m | N2 volume + X1 ratchet | -8.0 / -16.3 | **-12.4** (11,346; -15.5 to -9.3) | -16.6 |
| **5 EMA 15m** | **N5 all confirmations + X1 ratchet** | **+4.2 / +0.6** | **+0.5** (986; -17.8 to +19.0) | -19.4 |
| **VWAP band fade 5m** | **N5 volume+RSI+trend + X1 ratchet** | **+13.5 / +3.2** | **+2.5** (158; -27.9 to +41.7) | -16.7 |
| VWAP pullback 5m | N1 confirm + X0 | -14.1 / -17.5 | -15.0 (1,997) | -14.0 |
| Trend rejection 15m | N5 + X0 | -10.3 / -7.5 | -27.4 (218) | -14.4 |
| Pump ride 15m (thin coins) | N5 + X0 | -121.6 / +27.0 | -158 (232) | -142 |
| Dump short 1h (thin coins) | N0 + X1 ratchet | positive / positive | -78 (101; -301 to +147) | -111 |

## Reading
- **One pattern repeats:** "all three confirmations (volume + RSI + trend) on entry, plus the ratchet exit" turns the two biggest losers into about break-even. 5 EMA 15m goes from -19 to +0.5 bps; the VWAP fade goes from -17 to +2.5. Those are the only two pairs positive in both train and validation.
- **But it is break-even, not profit:** both holdout ranges straddle zero widely, and the VWAP fade has only 158 trades.
- **The pure reversal exits** (EMA9 flip, RSI flip, volume reversal) cut losses early but also cut winners, so they didn't beat the ratchet anywhere.
- **The thin-coin strategies** (pump ride, dump short) lose heavily once realistic 20 bps slippage per fill is counted.
- **Worth a paper test:** 5 EMA 15m and the VWAP fade with N5 entries + ratchet exits, frozen as they are. Nothing here justifies real money.

Numbers: `reports/delta_runs/dynamic_lab.json`; code: `backend/app/delta_lab/dynamic_lab.py`.
