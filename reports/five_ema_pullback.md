# 5 EMA: short on the pullback instead of at the candle close (25 Nov 2026)

*Hypothetical backtest on completed bars; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

**Same signal as live:** an alert candle entirely above EMA(5), then a candle breaking below the alert low. The **baseline** shorts at that breakout candle's close (as live does). The **pullback** versions instead rest a sell limit, filled as maker, at a pullback level (the broken alert low, halfway up the alert candle, or the EMA(5)) and wait 3 or 6 candles. No fill means no trade, and price hitting the alert high first also cancels. Stop: the alert high. Target: 3R from the fill, or the baseline's own target.
**Data:** BTC, ETH, SOL, LISTA; 5m and 15m; Binance 1-minute data from 2023 to Nov 2026; Delta fees + GST and slippage. These years were already used today, so the holdout is not fresh. **24 variants added to the running total.**

## 5m

| Version | Train net bps (trades) | Validation net bps (trades) | Validation PF |
|---|---|---|---|
| close|wait=0|target=3R | -14.5 (37195) | -16.1 (18598) | 0.57 |
| alert_low|wait=3|target=3R | -9.8 (18985) | -10.6 (9448) | 0.61 |
| alert_low|wait=3|target=fixed | -9.5 (18399) | -10.0 (9164) | 0.66 |
| alert_low|wait=6|target=3R | -9.6 (20863) | -10.7 (10378) | 0.60 |
| alert_low|wait=6|target=fixed | -9.4 (20061) | -10.2 (9996) | 0.65 |
| half|wait=3|target=3R | -8.6 (20855) | -9.1 (10207) | 0.48 |
| half|wait=3|target=fixed | -8.9 (20109) | -8.9 (9921) | 0.57 |
| half|wait=6|target=3R | -8.6 (24686) | -9.0 (12025) | 0.49 |
| half|wait=6|target=fixed | -9.0 (23484) | -8.9 (11521) | 0.58 |
| ema|wait=3|target=3R | -9.3 (16403) | -11.9 (7938) | 0.64 |
| ema|wait=3|target=fixed | -9.1 (16029) | -11.5 (7763) | 0.66 |
| ema|wait=6|target=3R | -9.3 (17502) | -11.9 (8431) | 0.64 |
| ema|wait=6|target=fixed | -9.3 (17055) | -11.5 (8237) | 0.66 |

- **Holdout half|wait=6|target=fixed:** 11019 trades, -9.0 net bps per trade, PF 0.52, win rate 15%, bootstrap 90% -9.6 to -8.3.
- **Holdout close|wait=0|target=3R:** 18598 trades, -14.1 net bps per trade, PF 0.55, win rate 25%, bootstrap 90% -14.9 to -13.4.

## 15m

| Version | Train net bps (trades) | Validation net bps (trades) | Validation PF |
|---|---|---|---|
| close|wait=0|target=3R | -14.0 (11790) | -14.1 (5898) | 0.74 |
| alert_low|wait=3|target=3R | -8.9 (5748) | -11.9 (2939) | 0.72 |
| alert_low|wait=3|target=fixed | -8.4 (5615) | -12.4 (2859) | 0.72 |
| alert_low|wait=6|target=3R | -8.5 (6271) | -11.6 (3195) | 0.72 |
| alert_low|wait=6|target=fixed | -8.1 (6096) | -11.9 (3097) | 0.73 |
| half|wait=3|target=3R | -9.9 (7088) | -9.8 (3659) | 0.61 |
| half|wait=3|target=fixed | -11.0 (6860) | -9.7 (3534) | 0.67 |
| half|wait=6|target=3R | -9.6 (8425) | -10.0 (4269) | 0.61 |
| half|wait=6|target=fixed | -10.4 (8049) | -10.0 (4073) | 0.67 |
| ema|wait=3|target=3R | -10.0 (4790) | -9.5 (2439) | 0.80 |
| ema|wait=3|target=fixed | -9.5 (4697) | -9.9 (2391) | 0.80 |
| ema|wait=6|target=3R | -10.2 (5053) | -9.7 (2576) | 0.80 |
| ema|wait=6|target=fixed | -9.9 (4936) | -9.9 (2528) | 0.81 |

- **Holdout ema|wait=3|target=3R:** 2400 trades, -9.2 net bps per trade, PF 0.78, win rate 28%, bootstrap 90% -12.4 to -5.9.
- **Holdout close|wait=0|target=3R:** 5897 trades, -13.8 net bps per trade, PF 0.71, win rate 27%, bootstrap 90% -16.1 to -11.3.

## Reading
- **The pullback entry is clearly better: it loses about 9 bps per trade instead of about 14** (5m and 15m, every split).
- About 3.5 bps of that comes from paying the maker fee instead of the taker fee. The rest comes from the better price and from skipping the 40-50% of breakouts that never pull back.
- **It still loses after costs**, so it does not make 5 EMA profitable on its own. If 5 EMA stays live, entering on a limit at the pullback (halfway up the alert candle on 5m, the EMA(5) on 15m) is the better way to run it.

Code: `backend/app/delta_lab/five_ema_pullback.py`; numbers: `reports/five_ema_pullback.json`.
