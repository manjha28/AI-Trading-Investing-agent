# Literature strategies on NSE equities: swing and positional, daily bars (26 Nov 2026)

*Hypothetical backtests on completed daily bars; kept apart from real P&L. Decision support, not investment advice. **Nothing was traded or changed.***

## Short answer

- **Nothing survives delivery costs on the holdout.** 54 variants in this part (running total about 1757). Not one variant was positive on validation and holdout with a holdout interval above zero. **I do not propose any paper forward test for equities.**
- Costs are the main problem for the fast setups: about 42 bps per round trip (STT 0.1% each way, exchange, SEBI, stamp, GST, plus 10 bps slippage per side). Anything held 2 to 10 days must earn more than that on average.
- The slow setups (monthly momentum, trend) were excellent in 2022-24 and stopped working afterwards. This is a regime story: the train window is a strong mid/small-cap rally.
- **Turn of month on Nifty** is the only effect positive in all three periods (net +12, +109, +6 bps per 4-day hold after 5 bps of futures-style costs), but its holdout t-statistic is 0.2. Post-earnings drift could not be tested: the database holds fiscal period ends, not announcement dates.

## Method, data and what to distrust

- **Data:** daily bars from the app's database (Dhan source, real bars only), 264 stock instruments, 22 Feb 2021 to 23 Nov 2026. The database is the owner's stored universe, **not the Nifty 200**; index membership is not stored. 'Nifty 200 proxy' = the 200 highest by trailing 120-day median turnover at each date, 'large' = the top 50 of those, 'mid' = ranks 51-200. Survivorship: stocks present today only. One close-to-close jump above 35% was treated as a corporate-action artefact and removed.
- **Benchmark (NIFTYBEES, a Nifty 50 ETF, buy and hold; the Dhan index series ends 19-20 Nov):** train annual return 18.0%, Sharpe 1.5; validation 4.1%, 0.34; holdout 2.2%, 0.17 (max drawdown 15.3%, 16.2%, 15.7%).
- **Split:** 50/25/25 of 1 Mar 2022 to 23 Nov 2026: train to 2024-07-12, validation to 2025-09-16, holdout after. **These years were used in earlier tests.** Chosen on validation Sharpe (at least 8 validation trades); the tables show the holdout of all variants for information.
- **Costs per side (fraction of traded value):** STT 0.1% on buy and sell, exchange 0.00297%, SEBI 0.0001%, stamp 0.015% on buys, GST 18% on exchange and SEBI charges, brokerage assumed Rs 0 (please check your Dhan plan), plus 10 bps slippage: about 21 bps per side. Long-only cash segment (no shorting). 20 equal slots of 5% each; entries at the close of the signal day.
- **Definitions** are the textbook forms (Jegadeesh-Titman 12-1 momentum, George-Hwang 52-week-high, relative strength vs Nifty, 200-day trend, Turtle 55/20, Connors RSI(2)/IBS/down days, Crabel NR7/inside bar, Bollinger squeeze); I did not read primary sources. Cells: trades / net bps per trade / win rate / profit factor / max drawdown % / Sharpe. Sharpe and returns use 248 trading days.
- **Deflated Sharpe:** N = 1757; variance of daily Sharpe across this part's variants = 0.00360.

## 2.1 Momentum and relative strength (monthly, top 10/20/30)

**Verdict.** **No.** Large in train (+374 to +2,357 bps per monthly holding across the 12 variants: 2022-24 was a strong mid/small-cap run), mixed to negative in validation (-304 to +250), and on the holdout -297 to +178 with no interval above zero; the three chosen on validation lost (-297, -52, -79). The Nifty ETF itself earned only +2% a year on the holdout.

**Top three on validation, with year and size splits:**

| Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | Nifty down >5% in 30d: days / sum return | holdout: large vs mid net bps (n) | net bps per year (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| relative strength 12m vs Nifty · top 10 of the turnover-ranked top 200, monthly | 55 | -296.6 | [-697.7, 140.8] | -0.79 | -20.5% | 33.5% | 3.9 | 0.000 | 93 d / -52.3% | large -393.4 (13); mid -266.6 (42) | 2022: 472.9 (35); 2023: 3811.0 (29); 2024: 676.8 (29); 2025: -161.2 (32); 2026: -158.5 (42) |
| 12-1 month momentum · top 10 of the turnover-ranked top 200, monthly | 67 | -51.6 | [-374.3, 263.8] | -0.28 | -6.9% | 29.7% | 4.7 | 0.000 | 93 d / -48.5% | large -316.8 (13); mid 12.2 (54) | 2022: 1348.6 (33); 2023: 3554.7 (30); 2024: 758.9 (30); 2025: 98.6 (40); 2026: -35.5 (48) |
| relative strength 12m vs Nifty · top 30 of the turnover-ranked top 200, monthly | 99 | -78.7 | [-373.8, 217.4] | -0.13 | -2.7% | 22.4% | 7.0 | 0.000 | 93 d / -49.7% | large 85.6 (31); mid -153.7 (68) | 2022: 931.2 (101); 2023: 2500.4 (78); 2024: -311.0 (76); 2025: 290.4 (91); 2026: 8.4 (65) |

**Every variant (12):**

| Variant | Train | Validation | Holdout |
|---|---|---|---|
| 12-1 month momentum · top 10 of the turnover-ranked top 200, monthly | 76 / 2357.3 / 63% / 5.74 / 24.3% / 2.72 | 38 / 12.0 / 37% / 1.02 / 44.2% / 0.62 | 67 / -51.6 / 52% / 0.92 / 29.7% / -0.28 |
| 12-1 month momentum · top 20 of the turnover-ranked top 200, monthly | 157 / 1544.0 / 63% / 3.93 / 27.3% / 2.24 | 68 / -185.3 / 44% / 0.8 / 41.8% / -0.18 | 96 / -47.0 / 48% / 0.93 / 25.1% / -0.16 |
| 12-1 month momentum · top 30 of the turnover-ranked top 200, monthly | 231 / 1390.1 / 61% / 3.93 / 27.4% / 2.07 | 98 / 52.6 / 46% / 1.07 / 36.8% / -0.02 | 106 / 26.0 / 48% / 1.03 / 22.4% / -0.01 |
| 52-week-high proximity · top 10 of the turnover-ranked top 200, monthly | 213 / 373.9 / 56% / 2.17 / 15.6% / 1.8 | 94 / -202.4 / 43% / 0.66 / 37.0% / -0.67 | 104 / 11.8 / 46% / 1.03 / 19.0% / -0.02 |
| 52-week-high proximity · top 20 of the turnover-ranked top 200, monthly | 328 / 464.4 / 56% / 2.41 / 14.1% / 1.81 | 139 / -81.3 / 43% / 0.86 / 26.6% / -0.12 | 147 / 95.2 / 44% / 1.2 / 17.4% / 0.26 |
| 52-week-high proximity · top 30 of the turnover-ranked top 200, monthly | 396 / 627.7 / 55% / 2.75 / 16.6% / 2.03 | 170 / -132.5 / 36% / 0.81 / 29.8% / -0.25 | 181 / 100.6 / 49% / 1.2 / 16.1% / 0.19 |
| relative strength 6m vs Nifty · top 10 of the turnover-ranked top 200, monthly | 114 / 900.0 / 54% / 2.37 / 32.8% / 1.68 | 68 / -303.7 / 46% / 0.63 / 50.1% / -0.58 | 70 / -166.0 / 46% / 0.75 / 31.3% / -0.59 |
| relative strength 6m vs Nifty · top 20 of the turnover-ranked top 200, monthly | 186 / 1289.6 / 53% / 3.45 / 25.3% / 2.08 | 103 / -209.1 / 46% / 0.72 / 40.7% / -0.22 | 99 / 178.2 / 52% / 1.3 / 21.0% / 0.11 |
| relative strength 6m vs Nifty · top 30 of the turnover-ranked top 200, monthly | 269 / 1255.4 / 56% / 3.77 / 28.7% / 1.96 | 135 / -184.0 / 39% / 0.74 / 34.7% / -0.04 | 133 / 143.7 / 46% / 1.22 / 23.0% / 0.09 |
| relative strength 12m vs Nifty · top 10 of the turnover-ranked top 200, monthly | 77 / 1941.5 / 57% / 3.63 / 24.6% / 2.31 | 35 / 48.4 / 40% / 1.08 / 40.8% / 0.63 | 55 / -296.6 / 36% / 0.64 / 33.5% / -0.79 |
| relative strength 12m vs Nifty · top 20 of the turnover-ranked top 200, monthly | 156 / 1308.5 / 49% / 3.15 / 28.0% / 1.88 | 70 / -144.1 / 33% / 0.84 / 43.2% / -0.13 | 88 / -209.7 / 42% / 0.72 / 27.6% / -0.48 |
| relative strength 12m vs Nifty · top 30 of the turnover-ranked top 200, monthly | 222 / 1251.2 / 51% / 3.31 / 24.6% / 1.86 | 90 / 249.5 / 42% / 1.34 / 37.4% / 0.03 | 99 / -78.7 / 48% / 0.89 / 22.4% / -0.13 |

## 2.2 Trend filters (200-day, golden cross, Turtle 55/20; with and without a volume filter)

**Verdict.** **No.** The best by validation Sharpe, the 50/200 golden cross, made +213 bps per trade on the holdout (interval -96 to +528) but -424 on validation. The Donchian 55/20 versions are negative on validation (-276) and on the holdout (-71) in the volume-filtered row shown. A volume filter does not help.

**Top three on validation, with year and size splits:**

| Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | Nifty down >5% in 30d: days / sum return | holdout: large vs mid net bps (n) | net bps per year (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 50/200 golden cross | 186 | 213.1 | [-95.6, 528.0] | 0.16 | 2.9% | 18.7% | 13.1 | 0.000 | 93 d / -46.0% | large 317.0 (42); mid 182.9 (144) | 2022: 1923.3 (260); 2023: 3675.2 (155); 2024: -754.6 (105); 2025: 10.3 (176); 2026: 406.5 (139) |
| 50/200 golden cross + volume > 1.5x its 20-day average at entry | 176 | 152.7 | [-137.2, 460.6] | 0.13 | 2.4% | 18.3% | 12.4 | 0.000 | 93 d / -47.0% | large 382.7 (40); mid 85.0 (136) | 2022: 2037.7 (242); 2023: 3475.2 (155); 2024: -833.6 (100); 2025: 99.4 (166); 2026: 253.5 (134) |
| Donchian 55/20 (Turtle) + volume > 1.5x its 20-day average at entry | 310 | -71.3 | [-230.6, 94.9] | -0.4 | -6.8% | 21.7% | 21.8 | 0.000 | 93 d / -42.0% | large -11.5 (92); mid -96.5 (218) | 2022: 301.0 (255); 2023: 1422.3 (330); 2024: 82.7 (311); 2025: -182.8 (277); 2026: 49.8 (235) |

**Every variant (6):**

| Variant | Train | Validation | Holdout |
|---|---|---|---|
| Close above the 200-day average | 1860 / 489.1 / 20% / 2.8 / 26.8% / 1.81 | 958 / -114.6 / 12% / 0.66 / 38.6% / -0.98 | 1046 / -6.9 / 14% / 0.98 / 24.0% / -0.29 |
| 50/200 golden cross | 353 / 2737.9 / 58% / 5.86 / 24.8% / 2.07 | 179 / -424.4 / 27% / 0.65 / 31.0% / -0.11 | 186 / 213.1 / 40% / 1.25 / 18.7% / 0.16 |
| Donchian 55/20 (Turtle) | 825 / 830.2 / 50% / 2.69 / 18.2% / 2.07 | 330 / -243.8 / 33% / 0.65 / 34.9% / -0.86 | 355 / -80.4 / 34% / 0.87 / 23.6% / -0.47 |
| Close above the 200-day average + volume > 1.5x its 20-day average at entry | 1252 / 737.9 / 26% / 2.91 / 25.2% / 1.94 | 588 / -169.1 / 14% / 0.65 / 38.7% / -0.96 | 637 / 26.4 / 18% / 1.07 / 21.8% / -0.05 |
| 50/200 golden cross + volume > 1.5x its 20-day average at entry | 423 / 2342.3 / 55% / 4.84 / 25.2% / 2.04 | 172 / -421.0 / 28% / 0.65 / 31.2% / -0.11 | 176 / 152.7 / 40% / 1.19 / 18.3% / 0.13 |
| Donchian 55/20 (Turtle) + volume > 1.5x its 20-day average at entry | 783 / 820.8 / 48% / 2.6 / 20.2% / 1.99 | 313 / -275.9 / 32% / 0.62 / 32.7% / -0.82 | 310 / -71.3 / 36% / 0.89 / 21.7% / -0.4 |

## 2.3 Short-term mean reversion (RSI(2), IBS, three down days)

**Verdict.** **No, and clearly.** At about 42 bps per round trip, a 2-5 day hold on an oversold stock does not pay: holdout -52 (IBS), -70 (RSI(2) in an uptrend) and -106 (three down days) bps per trade, with intervals entirely below zero. Even in train they were near zero after costs (+3 to +34 bps).

**Top three on validation, with year and size splits:**

| Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | Nifty down >5% in 30d: days / sum return | holdout: large vs mid net bps (n) | net bps per year (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| IBS below 0.2, no trend filter · hold 5d | 5121 | -52.3 | [-67.2, -38.6] | -0.76 | -14.7% | 32.7% | 360.0 | 0.000 | 93 d / -43.5% | large -30.1 (1310); mid -60.0 (3811) | 2022: -1.8 (3722); 2023: 64.5 (4134); 2024: -20.2 (4391); 2025: -81.3 (4188); 2026: -42.4 (3888) |
| Buy after 3 down days, no trend filter · hold 5d | 3359 | -105.7 | [-122.4, -88.2] | -1.5 | -28.4% | 36.0% | 236.1 | 0.000 | 93 d / -48.4% | large -90.3 (731); mid -110.0 (2628) | 2022: -10.8 (2308); 2023: 23.7 (2543); 2024: -58.1 (2850); 2025: -97.3 (2849); 2026: -114.2 (2470) |
| RSI(2) below 10 in an uptrend (above 200-day) · hold 5d | 1188 | -69.8 | [-97.1, -42.7] | -1.53 | -24.4% | 29.2% | 83.5 | 0.000 | 93 d / -41.0% | large -77.4 (323); mid -67.0 (865) | 2022: -17.5 (838); 2023: 38.9 (1312); 2024: -56.4 (1739); 2025: -85.0 (888); 2026: -73.7 (849) |

**Every variant (10):**

| Variant | Train | Validation | Holdout |
|---|---|---|---|
| RSI(2) below 10 in an uptrend (above 200-day) · hold 2d | 3468 / -16.6 / 50% / 0.9 / 22.0% / 0.13 | 1587 / -80.9 / 45% / 0.58 / 46.1% / -2.48 | 1373 / -52.1 / 46% / 0.66 / 31.5% / -1.97 |
| IBS below 0.2, in an uptrend · hold 2d | 9636 / -27.0 / 46% / 0.84 / 58.7% / -0.83 | 3872 / -82.2 / 39% / 0.58 / 70.7% / -3.12 | 3562 / -50.8 / 42% / 0.69 / 52.6% / -2.41 |
| Buy after 3 down days, in an uptrend · hold 2d | 4021 / -17.9 / 48% / 0.89 / 28.8% / -0.16 | 1681 / -86.3 / 42% / 0.55 / 54.6% / -3.01 | 1530 / -32.8 / 46% / 0.77 / 23.8% / -1.32 |
| IBS below 0.2, no trend filter · hold 2d | 15818 / -25.8 / 45% / 0.84 / 49.8% / -0.37 | 8170 / -76.1 / 41% / 0.61 / 57.0% / -2.03 | 8117 / -54.7 / 42% / 0.68 / 51.2% / -1.97 |
| Buy after 3 down days, no trend filter · hold 2d | 7260 / -26.2 / 47% / 0.84 / 36.2% / -0.02 | 3934 / -89.4 / 43% / 0.57 / 64.0% / -2.54 | 3905 / -61.2 / 44% / 0.64 / 47.0% / -2.13 |
| RSI(2) below 10 in an uptrend (above 200-day) · hold 5d | 3055 / 9.2 / 49% / 1.04 / 21.8% / 1.21 | 1378 / -109.0 / 42% / 0.61 / 41.8% / -1.67 | 1188 / -69.8 / 42% / 0.71 / 29.2% / -1.53 |
| IBS below 0.2, in an uptrend · hold 5d | 6313 / 38.4 / 50% / 1.17 / 31.7% / 0.95 | 2525 / -98.6 / 42% / 0.66 / 47.1% / -1.8 | 2401 / -50.9 / 43% / 0.79 / 29.5% / -0.96 |
| Buy after 3 down days, in an uptrend · hold 5d | 3567 / 19.4 / 49% / 1.09 / 26.2% / 1.3 | 1480 / -118.6 / 40% / 0.59 / 48.3% / -2.16 | 1352 / -62.0 / 43% / 0.74 / 26.1% / -1.26 |
| IBS below 0.2, no trend filter · hold 5d | 10061 / 34.4 / 49% / 1.16 / 26.8% / 1.02 | 5117 / -81.5 / 43% / 0.72 / 39.0% / -1.05 | 5121 / -52.3 / 42% / 0.79 / 32.7% / -0.76 |
| Buy after 3 down days, no trend filter · hold 5d | 6279 / 2.7 / 48% / 1.01 / 28.1% / 0.95 | 3374 / -104.9 / 41% / 0.64 / 41.7% / -1.4 | 3359 / -105.7 / 39% / 0.6 / 36.0% / -1.5 |

## 2.4 Gap-up on volume (post-earnings drift not testable, see below)

**Verdict.** **No.** Gap-ups on 3x volume are flat to negative after costs on validation and holdout (validation -84 to -102, holdout -24 to -73 bps; large caps slightly better than mid caps on few trades). Entering at the close of the gap day (the only fill a daily bar proves) gives up the move.

**Top three on validation, with year and size splits:**

| Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | Nifty down >5% in 30d: days / sum return | holdout: large vs mid net bps (n) | net bps per year (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Gap up >= 4% on volume >= 3x average, enter at the close · hold 10d | 164 | -27.6 | [-154.4, 101.1] | -0.16 | -1.4% | 7.7% | 11.5 | 0.000 | 93 d / -13.1% | large 52.2 (15); mid -35.6 (149) | 2022: -65.5 (138); 2023: 343.2 (103); 2024: -93.5 (165); 2025: -70.0 (108); 2026: 5.5 (139) |
| Gap up >= 2% on volume >= 3x average, enter at the close · hold 10d | 344 | -42.2 | [-123.4, 43.3] | -0.36 | -5.3% | 20.4% | 24.2 | 0.000 | 93 d / -24.9% | large 48.6 (46); mid -56.3 (298) | 2022: 93.1 (316); 2023: 190.7 (274); 2024: 44.5 (335); 2025: -126.3 (262); 2026: 5.9 (284) |
| Gap up >= 4% on volume >= 3x average, enter at the close · hold 5d | 167 | -23.8 | [-122.5, 76.2] | -0.12 | -0.8% | 5.3% | 11.7 | 0.000 | 93 d / -11.7% | large -71.6 (15); mid -19.1 (152) | 2022: -122.7 (142); 2023: 124.5 (107); 2024: -89.4 (167); 2025: -38.3 (108); 2026: -14.5 (142) |

**Every variant (4):**

| Variant | Train | Validation | Holdout |
|---|---|---|---|
| Gap up >= 2% on volume >= 3x average, enter at the close · hold 5d | 825 / 35.0 / 48% / 1.12 / 13.6% / 0.51 | 349 / -84.3 / 40% / 0.75 / 20.2% / -1.61 | 353 / -72.5 / 42% / 0.76 / 16.1% / -1.0 |
| Gap up >= 2% on volume >= 3x average, enter at the close · hold 10d | 784 / 134.5 / 49% / 1.41 / 16.5% / 1.34 | 341 / -83.8 / 43% / 0.8 / 25.5% / -1.11 | 344 / -42.2 / 45% / 0.89 / 20.4% / -0.36 |
| Gap up >= 4% on volume >= 3x average, enter at the close · hold 5d | 341 / -22.1 / 45% / 0.94 / 11.8% / -0.23 | 157 / -83.5 / 41% / 0.79 / 13.1% / -1.13 | 167 / -23.8 / 47% / 0.92 / 5.3% / -0.12 |
| Gap up >= 4% on volume >= 3x average, enter at the close · hold 10d | 332 / 69.3 / 48% / 1.17 / 14.4% / 0.5 | 156 / -101.8 / 42% / 0.79 / 14.5% / -0.88 | 164 / -27.6 / 44% / 0.93 / 7.7% / -0.16 |

## 2.5 Volatility contraction and breakouts (NR7, inside bar, Bollinger squeeze, 20-day high on volume)

**Verdict.** **No.** Squeeze breakouts (-83 to -93 bps on the holdout, intervals below zero) and 20-day highs on volume (-54, interval -104 to -2) lose after costs; NR7/inside-bar versions are in the table and are also negative on the holdout.

**Top three on validation, with year and size splits:**

| Variant | Holdout trades | net bps | 90% interval | Sharpe | ann. return | max DD | trades/month | deflated Sharpe | Nifty down >5% in 30d: days / sum return | holdout: large vs mid net bps (n) | net bps per year (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Bollinger squeeze (width in bottom 10% of 6 months) then close above the upper band · hold 10d | 380 | -92.6 | [-159.2, -26.3] | -1.3 | -12.8% | 20.6% | 26.7 | 0.000 | 93 d / -20.0% | large -58.5 (104); mid -105.5 (276) | 2022: 58.5 (415); 2023: 139.3 (400); 2024: 28.4 (299); 2025: -113.5 (431); 2026: -44.1 (238) |
| Bollinger squeeze (width in bottom 10% of 6 months) then close above the upper band · hold 5d | 392 | -83.3 | [-128.8, -39.4] | -1.64 | -11.5% | 14.6% | 27.6 | 0.000 | 93 d / -12.3% | large -46.6 (107); mid -97.1 (285) | 2022: 1.0 (438); 2023: 36.9 (418); 2024: 29.4 (307); 2025: -81.7 (445); 2026: -50.5 (247) |
| 20-day high on rising volume (>1.5x average and above yesterday) · hold 10d | 881 | -53.9 | [-104.4, -2.1] | -1.07 | -18.8% | 29.9% | 61.9 | 0.000 | 93 d / -37.3% | large 6.9 (234); mid -75.9 (647) | 2022: 49.6 (712); 2023: 167.9 (946); 2024: 31.4 (889); 2025: -95.4 (713); 2026: -37.5 (676) |

**Every variant (8):**

| Variant | Train | Validation | Holdout |
|---|---|---|---|
| NR7 then close above the setup high (stop: close below the setup low) · hold 5d | 4090 / 21.3 / 44% / 1.1 / 28.2% / 0.32 | 2057 / -27.8 / 42% / 0.89 / 45.9% / -1.78 | 1915 / -56.8 / 37% / 0.76 / 50.0% / -2.49 |
| NR7 then close above the setup high (stop: close below the setup low) · hold 10d | 3678 / 50.5 / 42% / 1.19 / 26.9% / 0.69 | 1900 / -53.7 / 36% / 0.83 / 47.8% / -1.81 | 1766 / -44.0 / 33% / 0.85 / 41.4% / -1.86 |
| Inside bar then close above the setup high (stop: close below the setup low) · hold 5d | 3523 / 10.8 / 45% / 1.05 / 35.2% / -0.38 | 1757 / -57.9 / 39% / 0.77 / 50.6% / -2.4 | 1693 / -72.4 / 36% / 0.71 / 50.6% / -2.35 |
| Inside bar then close above the setup high (stop: close below the setup low) · hold 10d | 3178 / 73.8 / 45% / 1.29 / 22.9% / 0.82 | 1595 / -80.9 / 35% / 0.76 / 45.3% / -1.54 | 1558 / -72.9 / 32% / 0.77 / 46.3% / -1.8 |
| Bollinger squeeze (width in bottom 10% of 6 months) then close above the upper band · hold 5d | 1017 / 16.1 / 46% / 1.07 / 9.6% / 0.33 | 444 / -18.7 / 41% / 0.92 / 10.9% / -0.4 | 392 / -83.3 / 39% / 0.66 / 14.6% / -1.64 |
| 20-day high on rising volume (>1.5x average and above yesterday) · hold 5d | 2544 / 33.4 / 46% / 1.14 / 19.3% / 0.63 | 953 / -65.8 / 41% / 0.77 / 34.9% / -1.85 | 986 / -57.6 / 43% / 0.78 / 24.4% / -1.43 |
| Bollinger squeeze (width in bottom 10% of 6 months) then close above the upper band · hold 10d | 975 / 81.2 / 51% / 1.3 / 14.5% / 0.81 | 426 / -35.4 / 41% / 0.89 / 14.0% / -0.39 | 380 / -92.6 / 41% / 0.72 / 20.6% / -1.3 |
| 20-day high on rising volume (>1.5x average and above yesterday) · hold 10d | 2196 / 117.8 / 49% / 1.4 / 21.9% / 1.05 | 856 / -95.3 / 42% / 0.76 / 37.8% / -1.37 | 881 / -53.9 / 44% / 0.85 / 29.9% / -1.07 |

## 2.4b Post-earnings drift

Not tested. The database stores fiscal period ends for fundamentals but no result announcement dates (the governance-events table is empty), and I will not infer dates.

## 2.6 Seasonality on the Nifty 50 index (futures-style costs, 5 bps per round trip)

Dhan index daily bars, 2015-01-01 to 2026-11-20; split 50/25/25 by calendar (2020-12-14 and 2023-12-04). A trade is one window (a single weekday, the four days around the month end, or Monday to expiry day; expiry = last Thursday, last Tuesday from Sep 2025). Cells: trades / net bps / win rate / profit factor. Nifty-futures trading would also need margin and roll; this table is for the index move only.

| Effect | Direction | Train | Validation | Holdout | t (train / val / holdout) |
|---|---|---|---|---|---|
| Monday | long | 289 / -14.9 / 53% / 0.71 | 150 / -2.1 / 57% / 0.95 | 146 / 1.0 / 51% / 1.03 | -1.8 / -0.2 / 0.1 |
| Monday | short | 289 / 4.9 / 45% / 1.12 | 150 / -7.9 / 37% / 0.83 | 146 / -11.0 / 43% / 0.73 | 0.6 / -0.9 / -1.4 |
| Tuesday | long | 293 / 3.3 / 49% / 1.1 | 144 / 23.8 / 58% / 2.26 | 150 / -10.3 / 41% / 0.69 | 0.5 / 3.6 / -1.5 |
| Tuesday | short | 293 / -13.3 / 44% / 0.68 | 144 / -33.8 / 35% / 0.31 | 150 / 0.3 / 49% / 1.01 | -2.2 / -5.1 / 0.0 |
| Wednesday | long | 300 / 2.5 / 48% / 1.08 | 148 / -1.0 / 47% / 0.97 | 145 / 7.9 / 54% / 1.33 | 0.4 / -0.2 / 1.2 |
| Wednesday | short | 300 / -12.5 / 44% / 0.69 | 148 / -9.0 / 48% / 0.75 | 145 / -17.9 / 41% / 0.52 | -2.2 / -1.4 / -2.7 |
| Thursday | long | 296 / -2.1 / 49% / 0.94 | 148 / -7.5 / 49% / 0.79 | 146 / -1.6 / 51% / 0.95 | -0.3 / -1.0 / -0.2 |
| Thursday | short | 296 / -7.9 / 46% / 0.81 | 148 / -2.5 / 46% / 0.93 | 146 / -8.4 / 40% / 0.76 | -1.3 / -0.3 / -1.3 |
| Friday | long | 290 / 6.4 / 54% / 1.2 | 145 / -8.4 / 46% / 0.79 | 144 / -1.8 / 49% / 0.95 | 1.1 / -1.0 / -0.3 |
| Friday | short | 290 / -16.4 / 39% / 0.63 | 145 / -1.6 / 49% / 0.96 | 144 / -8.2 / 46% / 0.78 | -2.7 / -0.2 / -1.1 |
| Turn of month (last trading day + first 3 of the next month) | long | 72 / 12.0 / 47% / 1.15 | 36 / 109.3 / 75% / 4.25 | 36 / 5.7 / 58% / 1.08 | 0.5 / 3.4 / 0.2 |
| Turn of month (last trading day + first 3 of the next month) | short | 72 / -22.0 / 49% / 0.77 | 36 / -119.3 / 25% / 0.21 | 36 / -15.7 / 42% / 0.8 | -0.9 / -3.7 / -0.5 |
| Expiry week (Monday to expiry day) | long | 71 / 1.1 / 52% / 1.01 | 36 / -31.0 / 50% / 0.67 | 36 / 18.6 / 56% / 1.49 | 0.0 / -0.9 / 1.0 |
| Expiry week (Monday to expiry day) | short | 71 / -11.1 / 44% / 0.87 | 36 / 21.0 / 50% / 1.31 | 36 / -28.6 / 42% / 0.54 | -0.4 / 0.6 / -1.6 |

Only turn-of-month long is positive net in all three periods; its holdout is +5.7 bps with t = 0.2. Tuesday long is strong in validation (+24 bps, t 3.6) but +3 in train and -10 in the holdout, which is what chance produces across 14 tested rows.

Code: `backend/app/equity_lab/eq_data.py`, `eq_run.py`, `eq_report.py` (shared engine: `backend/app/delta_lab/lit_core.py`); numbers: `reports/literature_equity.json`. 54 variants added to the running total.