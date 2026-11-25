# "[Smith] VWAP Deviation +" replicated and traded, Delta BTC/ETH/SOL/XRP (25 Nov 2026)

*Hypothetical backtest on completed bars; kept apart from real P&L. Decision support, not investment advice.*

- **Replication:** the script's exact rules (60-bar volume-weighted log mean, mean-absolute-deviation bands at 2 and 3, RSI 14 at 70/30, volume >= 1.2x SMA20, its trending/ranging filter). Entry at the next bar's open.
- **Exits tested:**
  - take profit at the mean (limit) with the stop at the 3-band;
  - the script's own 5-bar horizon;
  - a ratchet.
- **Variations:** 1m (the owner's chart), 3m and 5m; with the volume filter off; with the market filter off. 27 variants.
- **Data and costs:** Delta 1-minute candles for BTC, ETH, SOL and XRP; Delta fees + GST and slippage.

| | Net bps per trade |
|---|---|
| Script defaults, 1m, exit at the mean: holdout | **-14.1** (7,953 trades, PF 0.25) |
| Best on validation (1m, market filter off, exit at the mean): holdout | **-13.6** (11,080; 90% range -14.1 to -13.2) |
| All 27 variants, train and validation | between -12.5 and -16.6 |

**Why the TradingView table shows a 62% win rate:**
1. **It isn't a trade result.** The script counts a "win" if any close in the next 5 bars crosses the mean. It has no stop and no costs.
2. **That count is unreliable anyway.** The script calls `ta.highest(close, 5)` inside an `if` block. Pine only feeds a function called that way on the bars where the condition is true, so it compares closes from earlier signals, not the last 5 bars. That inflates the number.
3. **Measured correctly on Delta 1m:** price at a signal sits a median **25 bps** away from the mean, and only **about 5%** of signals reach the mean within 5 bars.

**Verdict:** no edge. Before costs the moves average about zero, and Delta's round trip is about 12-14 bps. A 1-minute mean-reversion strategy cannot pay that.

Code: `backend/app/delta_lab/smith_vwap.py`; numbers: `reports/delta_runs/smith_vwap.json`.
