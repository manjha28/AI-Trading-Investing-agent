# Pump ride on Delta coins: 15m/30m pump bars, and the BTW "absorption then breakout" rule (25 Nov 2026)

*Hypothetical backtest on completed bars of Delta India's own candles and funding; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

- **Data:** 183 of the 205 crypto USD perpetuals listed on Delta today (22 older ones skipped because the download stalled). Up to 2 years of history. Only coins still listed today are included, which flatters the results.
- **Costs:** fees + GST and 20 bps slippage per fill, plus funding while long.
- Rules and every number are in `backend/app/delta_lab/delta_pump_intraday.py`, `delta_absorption.py`, `reports/delta_pump_intraday.json` and `reports/delta_absorption.json`.
- **48 variants added to the running total.**

## Test 1: buy a 15m or 30m bar with 10-20x turnover that closes +5% or +10%, then trail
- Every version is between -1.4% and +2.1% per trade on validation, i.e. about zero.
- **Chosen version** (15m, 20x volume, +10% bar, 3-ATR 1h trail): **32 trades, mean -0.7%, median -5.9%, 28% winners.** Every trade ended at its stop, with a median hold of 1.5 hours.
- **The pumps don't keep going after the bar that announces them.** Entering at the next bar's open, the stop is usually hit within hours. The few big winners (AIN, VELVET) don't pay for the rest.

## Test 2: the BTW rule (decline, then a heavy-volume flat block, then a close above the block)
- **It catches your example:** BTWUSD 18 Nov, entry 0.4969, **+79.6% net over 6.75 days** (3-ATR 4h trail).
- **But across all coins, the 3-ATR versions lose 0.8-3.0% per trade on validation** (222-713 trades per version). The same setup fails far more often than it works. BTW also triggered twice earlier (Oct and Nov), and both lost 10-19%.
- **Fault in my "1-day low" trail:** it only counted lows since the entry, so it acted like a 1-hour stop (median hold 2 hours). Those 8 versions, including the one "chosen" on validation (-0.7% per trade on the holdout), do not test your idea. The 3-ATR versions are the valid ones.

## Reading
- Neither rule beats costs on Delta's coins. The pumps that look obvious on a chart, like BTW, are a small minority of the setups that look the same at the moment of entry.
- These numbers support no live trading. If you want, the next step is the 1-day-low trail fixed and re-run, or the BTW rule as an "Unproven" paper scanner from today.
