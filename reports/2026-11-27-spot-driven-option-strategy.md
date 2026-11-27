# Spot-driven features: do they predict beyond costs?

Research only. Decision support, not investment advice. Hypothetical; no order was placed and nothing was deployed.

## Verdict

**NO** — no spot feature set beat the cost of trading it out of sample. Step 3 (instrument choice) was not run, and no paper test is proposed.

- Chosen on validation: `boosting | big move | helpful features (30) | top 2%`, 60-minute horizon.
- Final check (after 2025-06-26), non-overlapping signals: {"signals": 191, "hit_rate": 43.5, "avg_move_pct": -0.0378, "net_futures": -0.0995, "beats_futures_pct": 37.7, "ci90_futures": "-0.1571 to -0.0354", "net_atm": -0.0749, "beats_atm_pct": 40.8, "ci90_atm": "-0.1327 to -0.0106", "net_spread": -0.2691, "beats_spread_pct": 22.0, "ci90_spread": "-0.3288 to -0.2015"}
- Deflated Sharpe (probability the true Sharpe beats the best expected from luck; futures hurdle, N = 33,759): validation 0.0, final 0.0.
- Tried here: 25,743 rule cells (25,305 had enough trades to be judged) and 72 model settings. Running total 33,759.

`net_*` is the average signed index move minus that instrument's break-even move, in % of the index. Above zero means it paid for itself.

## Data and method

- **NIFTY**: 1395 days, 2021-04-01 to 2026-11-25. Constituents found 15 of 15.
  - Volume proxies (days, first, last): constituents (1393, '2021-04-01', '2026-11-25'), ATM option volume (1393, '2021-04-01', '2026-11-25'), nearest future (37, '2026-09-30', '2026-11-25').
  - Rank correlation of same-time relative volume (value, bars): constituents/options (0.2, 103307), constituents/futures (0.21, 1944), options/futures (0.12, 2025).
- **BANKNIFTY**: 1292 days, 2021-08-04 to 2026-11-25. Constituents found 12 of 12.
  - Volume proxies (days, first, last): constituents (1288, '2021-08-07', '2026-11-25'), ATM option volume (1290, '2021-08-04', '2026-11-25'), nearest future (37, '2026-09-30', '2026-11-25').
  - Rank correlation of same-time relative volume (value, bars): constituents/options (0.11, 95418), constituents/futures (0.37, 1944), options/futures (0.43, 2025).
- **SENSEX**: 1309 days, 2021-08-04 to 2026-11-25. Constituents found 30 of 30.
  - Volume proxies (days, first, last): constituents (1307, '2021-08-04', '2026-11-25'), ATM option volume (872, '2023-05-15', '2026-11-25'), nearest future (58, '2026-08-31', '2026-11-25').
  - Rank correlation of same-time relative volume (value, bars): constituents/options (0.16, 64277), constituents/futures (0.24, 3286), options/futures (0.22, 3430).
- The futures proxy covers only a few weeks (expired contracts cannot be downloaded), so it is compared above but not modelled.
- The constituent proxy is summed traded value (price × shares), so cheap shares do not dominate. VWAP of the index is weighted by it.
- Constituents and weights are today's, as recalled and not verified (survivorship bias). SENSEX uses NSE listings. Share prices are Dhan intraday, not re-adjusted here.
- Option features use ATM±4 strikes (BANKNIFTY monthly: ±2 only). SENSEX options start in May 2023.
- Splits: train to 2024-01-29, validation to 2025-06-26, final after. Models: 12-month rolling window, next month predicted, from 2022-04.
- Rules: direction and bucket edges from train only; screened on validation with whole days as the unit; leaders re-judged without overlapping signals.
- The break-even move that labels the `big move` target was estimated on the whole train split, so early walk-forward months see a cost level (not a price) from later.
- Lot sizes, expiry weekdays and statutory rates are as recalled, not re-verified. Slippage is assumed: Dhan has no historical bid/ask.
- The final period is not untouched history: the earlier scalp study already looked at it for a different strategy. Here it was evaluated for one candidate only.
- The `big move` target uses the debit-spread break-even, which came out HIGHER than the ATM option's (very high for BANKNIFTY). It was not re-run with the ATM hurdle. All bars are Dhan data; Yahoo was not used.

## Break-even move (train split, actual premiums)

| underlying | horizon | instrument | slippage | trades | avg_net_points | points_per_1pct_move | breakeven_move_pct |
|---|---|---|---|---|---|---|---|
| NIFTY | 15 | atm | 0.5% | 99896 | -2.16 | 83.9 | 0.0257 |
| NIFTY | 15 | atm | 1.5% | 99896 | -3.85 | 83.2 | 0.0462 |
| NIFTY | 15 | spread | 0.5% | 99859 | -3.43 | 33.7 | 0.1023 |
| NIFTY | 15 | spread | 1.5% | 99859 | -6.07 | 32.5 | 0.1871 |
| NIFTY | 30 | atm | 0.5% | 95836 | -2.31 | 84.9 | 0.0271 |
| NIFTY | 30 | atm | 1.5% | 95836 | -4.0 | 84.1 | 0.0476 |
| NIFTY | 30 | spread | 0.5% | 95691 | -3.45 | 34.5 | 0.1012 |
| NIFTY | 30 | spread | 1.5% | 95691 | -6.09 | 33.2 | 0.1847 |
| NIFTY | 60 | atm | 0.5% | 87432 | -2.54 | 85.8 | 0.0297 |
| NIFTY | 60 | atm | 1.5% | 87432 | -4.25 | 84.9 | 0.05 |
| NIFTY | 60 | spread | 0.5% | 86808 | -3.37 | 35.2 | 0.1018 |
| NIFTY | 60 | spread | 1.5% | 86808 | -6.04 | 33.8 | 0.1847 |
| BANKNIFTY | 15 | atm | 0.5% | 84160 | -8.94 | 187.5 | 0.0477 |
| BANKNIFTY | 15 | atm | 1.5% | 84160 | -19.72 | 185.9 | 0.1061 |
| BANKNIFTY | 15 | spread | 0.5% | 67397 | -14.83 | 26.1 | 0.6143 |
| BANKNIFTY | 15 | spread | 1.5% | 67397 | -34.74 | 20.3 | 1.7565 |
| BANKNIFTY | 30 | atm | 0.5% | 79159 | -9.47 | 189.8 | 0.0499 |
| BANKNIFTY | 30 | atm | 1.5% | 79159 | -20.22 | 188.1 | 0.1075 |
| BANKNIFTY | 30 | spread | 0.5% | 59262 | -13.96 | 26.2 | 0.6088 |
| BANKNIFTY | 30 | spread | 1.5% | 59262 | -33.89 | 20.2 | 1.7562 |
| BANKNIFTY | 60 | atm | 0.5% | 69142 | -10.87 | 190.8 | 0.057 |
| BANKNIFTY | 60 | atm | 1.5% | 69142 | -21.58 | 189.0 | 0.1141 |
| BANKNIFTY | 60 | spread | 0.5% | 48646 | -12.96 | 27.1 | 0.5908 |
| BANKNIFTY | 60 | spread | 1.5% | 48646 | -32.93 | 21.2 | 1.6631 |
| SENSEX | 15 | atm | 0.5% | 25224 | -8.43 | 299.2 | 0.0281 |
| SENSEX | 15 | atm | 1.5% | 25224 | -13.26 | 296.4 | 0.0446 |
| SENSEX | 15 | spread | 0.5% | 24735 | -14.33 | 94.7 | 0.1519 |
| SENSEX | 15 | spread | 1.5% | 24735 | -22.37 | 89.7 | 0.2499 |
| SENSEX | 30 | atm | 0.5% | 24129 | -8.96 | 309.7 | 0.0289 |
| SENSEX | 30 | atm | 1.5% | 24129 | -13.81 | 306.7 | 0.045 |
| SENSEX | 30 | spread | 0.5% | 23488 | -14.15 | 100.3 | 0.1443 |
| SENSEX | 30 | spread | 1.5% | 23488 | -22.23 | 94.9 | 0.2375 |
| SENSEX | 60 | atm | 0.5% | 21924 | -10.39 | 314.7 | 0.0328 |
| SENSEX | 60 | atm | 1.5% | 21924 | -15.26 | 311.5 | 0.0487 |
| SENSEX | 60 | spread | 0.5% | 21060 | -13.85 | 100.7 | 0.1471 |
| SENSEX | 60 | spread | 1.5% | 21060 | -21.98 | 94.9 | 0.2411 |

Index futures hurdle (assumed, round trip): 0.0420% before Oct 2024, 0.0495% to Mar 2026, 0.0795% after.

## Leaders on validation (non-overlapping)

| candidate | horizon | signals | hit_rate | avg_move_pct | net_futures | beats_futures_pct | ci90_futures | net_atm | beats_atm_pct | ci90_atm | net_spread | beats_spread_pct | ci90_spread |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| boosting | big move | helpful features (30) | top 2% | 60 | 204 | 60.3 | 0.0681 | 0.0225 | 55.4 | -0.0183 to 0.0644 | 0.0326 | 57.4 | -0.0079 to 0.0746 | -0.1338 | 38.2 | -0.1822 to -0.0821 |
| logistic | sign | all features (43) | top 2% | 60 | 411 | 56.7 | 0.076 | 0.0313 | 49.1 | -0.0207 to 0.0868 | 0.0326 | 48.9 | -0.0196 to 0.0886 | -0.2696 | 23.4 | -0.3315 to -0.2046 |
| boosting | sign | helpful features (22) | top 2% | 30 | 454 | 63.0 | 0.0429 | -0.0032 | 50.0 | -0.0231 to 0.0167 | 0.0072 | 53.5 | -0.0130 to 0.0268 | -0.2506 | 19.6 | -0.2856 to -0.2183 |
| logistic | sign | helpful features (35) | top 2% | 30 | 802 | 57.4 | 0.0455 | 0.0006 | 48.6 | -0.0236 to 0.0263 | 0.009 | 50.5 | -0.0158 to 0.0346 | -0.2654 | 21.2 | -0.2993 to -0.2320 |
| logistic | sign | all features (43) | top 2% | 30 | 919 | 53.2 | 0.0453 | 0.0009 | 45.3 | -0.0256 to 0.0283 | 0.0098 | 47.3 | -0.0166 to 0.0370 | -0.2459 | 22.4 | -0.2776 to -0.2142 |
| boosting | big move | all features (43) | top 5% | 60 | 666 | 55.0 | 0.0448 | -0.0008 | 47.9 | -0.0289 to 0.0272 | 0.008 | 48.8 | -0.0202 to 0.0361 | -0.1804 | 30.0 | -0.2123 to -0.1476 |
| or15_position = 1 & iv_chg_day [-0.006452, inf) | 60 | 697 | 59.1 | 0.0429 | -0.0032 | 49.1 | -0.0331 to 0.0250 | 0.0039 | 50.5 | -0.0260 to 0.0323 | -0.2209 | 27.0 | -0.2557 to -0.1897 |
| ma50_dist [0.1618, inf) & pcr_chg [0.03238, inf) | 60 | 1409 | 55.4 | 0.0327 | -0.0134 | 45.4 | -0.0321 to 0.0051 | -0.0075 | 46.6 | -0.0261 to 0.0111 | -0.253 | 20.4 | -0.2741 to -0.2324 |
| since_day_low [140, inf) & iv_chg_day [-0.006452, inf) | 60 | 526 | 59.3 | 0.0478 | 0.0015 | 50.0 | -0.0334 to 0.0352 | 0.0079 | 51.7 | -0.0269 to 0.0415 | -0.2338 | 25.9 | -0.2735 to -0.1963 |
| boosting | big move | all features (43) | top 2% | 60 | 209 | 56.5 | 0.056 | 0.011 | 51.2 | -0.0322 to 0.0531 | 0.0205 | 51.7 | -0.0227 to 0.0626 | -0.1452 | 34.4 | -0.1974 to -0.0942 |
| logistic | big move | helpful features (39) | top 5% | 30 | 1302 | 53.1 | 0.027 | -0.0177 | 42.9 | -0.0322 to -0.0022 | -0.0048 | 45.9 | -0.0192 to 0.0109 | -0.181 | 23.3 | -0.2012 to -0.1597 |
| or15_mid_dist [0.1668, inf) & iv_chg_day [-0.006452, inf) | 60 | 745 | 59.3 | 0.0398 | -0.0064 | 48.5 | -0.0342 to 0.0196 | -0.0001 | 50.2 | -0.0276 to 0.0263 | -0.2409 | 26.0 | -0.2747 to -0.2089 |
| boosting | sign | helpful features (22) | top 5% | 30 | 1289 | 58.4 | 0.0229 | -0.023 | 42.6 | -0.0343 to -0.0114 | -0.0124 | 46.8 | -0.0235 to -0.0009 | -0.2615 | 16.4 | -0.2783 to -0.2445 |
| logistic | big move | all features (43) | top 2% | 30 | 676 | 55.9 | 0.0347 | -0.0093 | 45.7 | -0.0318 to 0.0138 | 0.0032 | 48.4 | -0.0197 to 0.0267 | -0.1666 | 23.7 | -0.1999 to -0.1323 |
| dist_prev_low [0.9672, inf) & swing_5m = -1 | 60 | 901 | 57.9 | 0.0292 | -0.017 | 47.7 | -0.0342 to 0.0007 | -0.0121 | 48.8 | -0.0294 to 0.0055 | -0.2781 | 17.8 | -0.3001 to -0.2569 |

## Best single-feature buckets on validation (overlapping signals)

| horizon | rule | direction | train_n | train_move_pct | valid_n | valid_move_pct | valid_low90_pct | valid_hit |
|---|---|---|---|---|---|---|---|---|
| 60 | ma50_dist [0.3045, inf) | 1.0 | 24504 | 0.0018 | 12569 | 0.0288 | 0.007 | 56.3 |
| 60 | dist_prev_high [0.2441, inf) | 1.0 | 23361 | 0.0038 | 10190 | 0.0274 | 0.007 | 57.7 |
| 60 | oi_tilt_chg [0.08938, inf) | 1.0 | 16110 | 0.0015 | 14866 | 0.021 | 0.0044 | 54.4 |
| 30 | vwap_slope [0.008229, 0.02891) | 1.0 | 23760 | 0.0063 | 12589 | 0.0097 | 0.0032 | 53.0 |
| 15 | vwap_slope [0.008229, 0.02891) | 1.0 | 24908 | 0.0036 | 13124 | 0.006 | 0.0026 | 51.9 |
| 30 | oi_tilt_chg [0.08938, inf) | 1.0 | 17491 | 0.0029 | 15908 | 0.0104 | 0.0016 | 53.5 |
| 15 | pcr_chg [0.07551, inf) | 1.0 | 16934 | 0.001 | 14480 | 0.0057 | 0.0014 | 53.9 |
| 60 | atr_rank [0.6007, 0.8093) | 1.0 | 22849 | 0.0014 | 13515 | 0.0191 | 0.0012 | 52.7 |
| 60 | or15_mid_dist [0.3404, inf) | 1.0 | 21349 | 0.008 | 13172 | 0.0223 | 0.001 | 54.5 |
| 60 | ce_oi_chg [-inf, -0.01518) | 1.0 | 13309 | 0.0064 | 10666 | 0.0159 | 0.0009 | 53.2 |
| 30 | pcr_chg [0.07551, inf) | 1.0 | 16145 | 0.0023 | 13833 | 0.0087 | 0.0009 | 53.5 |
| 30 | oi_tilt_chg [0.02228, 0.08938) | 1.0 | 17466 | 0.0058 | 11367 | 0.0072 | 0.0009 | 51.2 |
| 60 | atr_pct [0.06932, 0.09033) | -1.0 | 23254 | 0.0007 | 12631 | 0.0125 | 0.0009 | 51.0 |
| 15 | minutes [220, 290) | 1.0 | 26588 | 0.0021 | 14570 | 0.0057 | 0.0008 | 52.1 |
| 15 | oi_tilt_chg [0.08938, inf) | 1.0 | 18205 | 0.002 | 16494 | 0.005 | 0.0007 | 52.8 |

## All model settings

| model | horizon | train_oos_signals | train_oos_hit_rate | train_oos_avg_move_pct | valid_signals | valid_hit_rate | valid_avg_move_pct | valid_low90_pct | valid_net_futures | valid_net_spread | valid_net_atm |
|---|---|---|---|---|---|---|---|---|---|---|---|
| logistic | sign | all features (43) | top 10% | 15 | 4780 | 53.4 | 0.003 | 5248 | 53.8 | 0.0062 | 0.0003 | -0.0392 | -0.2997 | -0.0283 |
| logistic | sign | all features (43) | top 5% | 15 | 2444 | 52.8 | 0.0015 | 2956 | 54.0 | 0.0102 | 0.0019 | -0.0348 | -0.2993 | -0.0245 |
| logistic | sign | all features (43) | top 2% | 15 | 1030 | 51.7 | -0.0018 | 1335 | 55.4 | 0.0227 | 0.0089 | -0.0217 | -0.2823 | -0.0118 |
| logistic | sign | helpful features (31) | top 10% | 15 | 5041 | 51.6 | 0.0015 | 5023 | 54.3 | 0.0033 | -0.0026 | -0.0423 | -0.314 | -0.0317 |
| logistic | sign | helpful features (31) | top 5% | 15 | 2726 | 51.5 | 0.0027 | 2743 | 54.9 | 0.0056 | -0.003 | -0.0398 | -0.3255 | -0.03 |
| logistic | sign | helpful features (31) | top 2% | 15 | 1098 | 52.5 | 0.0027 | 1210 | 56.1 | 0.0175 | 0.0022 | -0.0274 | -0.3223 | -0.0185 |
| logistic | sign | all features (43) | top 10% | 30 | 3158 | 52.3 | 0.0084 | 3277 | 53.7 | 0.0158 | 0.0051 | -0.0298 | -0.2875 | -0.0204 |
| logistic | sign | all features (43) | top 5% | 30 | 1689 | 52.3 | 0.0055 | 1927 | 51.9 | 0.0237 | 0.0079 | -0.0214 | -0.2806 | -0.0124 |
| logistic | sign | all features (43) | top 2% | 30 | 783 | 54.3 | 0.019 | 919 | 53.2 | 0.0453 | 0.0184 | 0.0009 | -0.2459 | 0.0098 |
| logistic | sign | helpful features (35) | top 10% | 30 | 3211 | 53.4 | 0.0082 | 3108 | 53.0 | 0.011 | 0.0005 | -0.0347 | -0.2926 | -0.0251 |
| logistic | sign | helpful features (35) | top 5% | 30 | 1816 | 52.8 | 0.0071 | 1780 | 54.1 | 0.0217 | 0.0059 | -0.0237 | -0.2873 | -0.0147 |
| logistic | sign | helpful features (35) | top 2% | 30 | 844 | 55.0 | 0.018 | 802 | 57.4 | 0.0455 | 0.0199 | 0.0006 | -0.2654 | 0.009 |
| logistic | sign | all features (43) | top 10% | 60 | 1774 | 54.8 | 0.0301 | 1698 | 52.7 | 0.0184 | -0.0027 | -0.0273 | -0.2961 | -0.0233 |
| logistic | sign | all features (43) | top 5% | 60 | 1003 | 54.7 | 0.0413 | 946 | 51.2 | 0.0252 | -0.0036 | -0.0203 | -0.3062 | -0.0175 |
| logistic | sign | all features (43) | top 2% | 60 | 437 | 56.5 | 0.0535 | 411 | 56.7 | 0.076 | 0.0232 | 0.0313 | -0.2696 | 0.0326 |
| logistic | sign | helpful features (29) | top 10% | 60 | 1868 | 52.0 | 0.0156 | 1570 | 54.4 | 0.023 | 0.0016 | -0.0229 | -0.2939 | -0.0189 |
| logistic | sign | helpful features (29) | top 5% | 60 | 1024 | 52.6 | 0.0286 | 868 | 55.3 | 0.0307 | -0.0033 | -0.0152 | -0.3123 | -0.0127 |
| logistic | sign | helpful features (29) | top 2% | 60 | 427 | 55.0 | 0.053 | 355 | 55.5 | 0.0645 | 0.0073 | 0.0191 | -0.3041 | 0.0198 |
| logistic | big move | all features (43) | top 10% | 15 | 3643 | 50.9 | 0.006 | 4089 | 52.0 | 0.0102 | 0.0034 | -0.0348 | -0.2144 | -0.0209 |
| logistic | big move | all features (43) | top 5% | 15 | 1839 | 50.2 | 0.0065 | 2449 | 52.1 | 0.0128 | 0.0033 | -0.0315 | -0.1997 | -0.0177 |
| logistic | big move | all features (43) | top 2% | 15 | 793 | 52.7 | 0.0157 | 1296 | 53.7 | 0.0196 | 0.004 | -0.0242 | -0.188 | -0.0108 |
| logistic | big move | helpful features (35) | top 10% | 15 | 3548 | 52.6 | 0.0116 | 3859 | 52.3 | 0.0124 | 0.0056 | -0.0326 | -0.2075 | -0.0185 |
| logistic | big move | helpful features (35) | top 5% | 15 | 1867 | 52.0 | 0.014 | 2272 | 53.0 | 0.0148 | 0.0051 | -0.0296 | -0.1921 | -0.0155 |
| logistic | big move | helpful features (35) | top 2% | 15 | 832 | 52.6 | 0.0246 | 1187 | 51.5 | 0.0142 | -0.0004 | -0.0296 | -0.1909 | -0.0161 |
| logistic | big move | all features (43) | top 10% | 30 | 2444 | 51.3 | 0.0194 | 2383 | 52.2 | 0.0199 | 0.0089 | -0.0253 | -0.2105 | -0.013 |
| logistic | big move | all features (43) | top 5% | 30 | 1309 | 53.0 | 0.0317 | 1365 | 52.8 | 0.0264 | 0.0107 | -0.0182 | -0.1874 | -0.0057 |
| logistic | big move | all features (43) | top 2% | 30 | 582 | 53.6 | 0.0538 | 676 | 55.9 | 0.0347 | 0.0117 | -0.0093 | -0.1666 | 0.0032 |
| logistic | big move | helpful features (39) | top 10% | 30 | 2464 | 52.4 | 0.0257 | 2322 | 51.9 | 0.0174 | 0.0062 | -0.0278 | -0.2098 | -0.0153 |
| logistic | big move | helpful features (39) | top 5% | 30 | 1383 | 53.3 | 0.0397 | 1302 | 53.1 | 0.027 | 0.0124 | -0.0177 | -0.181 | -0.0048 |
| logistic | big move | helpful features (39) | top 2% | 30 | 627 | 55.2 | 0.0644 | 599 | 52.9 | 0.0275 | 0.0061 | -0.0167 | -0.1571 | -0.0033 |
| logistic | big move | all features (43) | top 10% | 60 | 1479 | 55.1 | 0.0372 | 1458 | 51.9 | 0.0214 | 0.0022 | -0.024 | -0.2258 | -0.0167 |
| logistic | big move | all features (43) | top 5% | 60 | 787 | 55.3 | 0.0467 | 823 | 53.0 | 0.0262 | 0.0011 | -0.0186 | -0.2173 | -0.0116 |
| logistic | big move | all features (43) | top 2% | 60 | 355 | 55.2 | 0.0454 | 388 | 53.6 | 0.03 | -0.0088 | -0.0139 | -0.1883 | -0.0065 |
| logistic | big move | helpful features (38) | top 10% | 60 | 1517 | 53.7 | 0.0287 | 1390 | 52.2 | 0.0211 | 0.0016 | -0.0242 | -0.226 | -0.0169 |
| logistic | big move | helpful features (38) | top 5% | 60 | 812 | 55.4 | 0.0436 | 807 | 50.1 | 0.0177 | -0.0084 | -0.027 | -0.2265 | -0.0202 |
| logistic | big move | helpful features (38) | top 2% | 60 | 357 | 58.5 | 0.0657 | 387 | 52.5 | 0.0264 | -0.0118 | -0.0174 | -0.1959 | -0.0103 |
| boosting | sign | all features (43) | top 10% | 15 | 6193 | 51.5 | -0.0004 | 4592 | 56.5 | 0.0064 | 0.0009 | -0.0398 | -0.2842 | -0.0274 |
| boosting | sign | all features (43) | top 5% | 15 | 3565 | 52.1 | 0.0009 | 2238 | 58.4 | 0.0111 | 0.0046 | -0.0352 | -0.2773 | -0.0227 |
| boosting | sign | all features (43) | top 2% | 15 | 1995 | 50.9 | -0.0001 | 781 | 58.3 | 0.012 | 0.0022 | -0.0343 | -0.27 | -0.0215 |
| boosting | sign | helpful features (28) | top 10% | 15 | 6578 | 52.4 | 0.0044 | 4558 | 56.6 | 0.0095 | 0.0048 | -0.0368 | -0.2822 | -0.0244 |
| boosting | sign | helpful features (28) | top 5% | 15 | 3839 | 52.6 | 0.0057 | 2182 | 57.8 | 0.0112 | 0.0049 | -0.0353 | -0.2846 | -0.0229 |
| boosting | sign | helpful features (28) | top 2% | 15 | 2047 | 53.0 | 0.0073 | 800 | 59.6 | 0.007 | -0.0018 | -0.0396 | -0.2901 | -0.0272 |
| boosting | sign | all features (43) | top 10% | 30 | 3668 | 51.1 | 0.0059 | 2409 | 57.0 | 0.0158 | 0.0067 | -0.03 | -0.2764 | -0.0198 |
| boosting | sign | all features (43) | top 5% | 30 | 2158 | 50.5 | 0.0057 | 1194 | 55.8 | 0.0232 | 0.0081 | -0.0226 | -0.2706 | -0.0125 |
| boosting | sign | all features (43) | top 2% | 30 | 1194 | 49.8 | 0.0088 | 464 | 55.8 | 0.0276 | 0.0003 | -0.0184 | -0.265 | -0.008 |
| boosting | sign | helpful features (22) | top 10% | 30 | 4015 | 51.9 | 0.0115 | 2649 | 56.4 | 0.0178 | 0.0092 | -0.0281 | -0.2652 | -0.0174 |
| boosting | sign | helpful features (22) | top 5% | 30 | 2555 | 52.0 | 0.0093 | 1289 | 58.4 | 0.0229 | 0.0118 | -0.023 | -0.2615 | -0.0124 |
| boosting | sign | helpful features (22) | top 2% | 30 | 1252 | 50.2 | 0.0076 | 454 | 63.0 | 0.0429 | 0.0232 | -0.0032 | -0.2506 | 0.0072 |
| boosting | sign | all features (43) | top 10% | 60 | 1868 | 49.4 | -0.0005 | 817 | 55.4 | 0.0191 | -0.0076 | -0.0273 | -0.2811 | -0.0219 |
| boosting | sign | all features (43) | top 5% | 60 | 1230 | 47.7 | -0.0136 | 419 | 54.2 | 0.0115 | -0.0238 | -0.035 | -0.2829 | -0.0292 |
| boosting | sign | all features (43) | top 2% | 60 | 632 | 46.8 | -0.0112 | 147 | 55.1 | 0.039 | -0.018 | -0.0071 | -0.2476 | -0.0012 |
| boosting | sign | helpful features (22) | top 10% | 60 | 2046 | 53.0 | 0.0045 | 845 | 56.4 | 0.0394 | 0.0087 | -0.0069 | -0.2718 | -0.0022 |
| boosting | sign | helpful features (22) | top 5% | 60 | 1393 | 51.5 | 0.0001 | 451 | 54.5 | 0.0345 | 0.0018 | -0.0121 | -0.2683 | -0.0066 |
| boosting | sign | helpful features (22) | top 2% | 60 | 729 | 49.0 | -0.008 | 181 | 55.8 | 0.0275 | -0.0213 | -0.0191 | -0.2379 | -0.0115 |
| boosting | big move | all features (43) | top 10% | 15 | 3804 | 50.5 | 0.0055 | 4608 | 51.3 | 0.0071 | 0.0018 | -0.0388 | -0.2129 | -0.0237 |
| boosting | big move | all features (43) | top 5% | 15 | 2032 | 49.9 | 0.0038 | 2420 | 52.6 | 0.0062 | -0.0014 | -0.0394 | -0.1982 | -0.024 |
| boosting | big move | all features (43) | top 2% | 15 | 863 | 52.1 | 0.013 | 960 | 53.8 | 0.0197 | 0.0079 | -0.0257 | -0.1668 | -0.0097 |
| boosting | big move | helpful features (31) | top 10% | 15 | 3815 | 49.8 | 0.003 | 4639 | 51.6 | 0.0069 | 0.0018 | -0.039 | -0.2176 | -0.0242 |
| boosting | big move | helpful features (31) | top 5% | 15 | 2054 | 50.3 | 0.0035 | 2499 | 52.9 | 0.0095 | 0.0022 | -0.0361 | -0.202 | -0.021 |
| boosting | big move | helpful features (31) | top 2% | 15 | 892 | 51.5 | 0.0065 | 1044 | 53.3 | 0.0136 | 0.0018 | -0.0318 | -0.1808 | -0.0162 |
| boosting | big move | all features (43) | top 10% | 30 | 2634 | 51.3 | 0.0085 | 2619 | 51.5 | 0.0109 | 0.0006 | -0.0352 | -0.2231 | -0.0222 |
| boosting | big move | all features (43) | top 5% | 30 | 1415 | 52.6 | 0.0137 | 1245 | 52.8 | 0.0212 | 0.0068 | -0.0246 | -0.1972 | -0.0112 |
| boosting | big move | all features (43) | top 2% | 30 | 631 | 48.7 | 0.0017 | 424 | 52.6 | 0.0158 | -0.0082 | -0.0294 | -0.1537 | -0.0143 |
| boosting | big move | helpful features (35) | top 10% | 30 | 2592 | 51.4 | 0.0069 | 2638 | 51.6 | 0.0118 | 0.0021 | -0.0343 | -0.2258 | -0.0213 |
| boosting | big move | helpful features (35) | top 5% | 30 | 1429 | 54.4 | 0.0103 | 1269 | 52.4 | 0.0184 | 0.0046 | -0.0274 | -0.2003 | -0.0139 |
| boosting | big move | helpful features (35) | top 2% | 30 | 674 | 51.2 | 0.0057 | 427 | 55.5 | 0.035 | 0.0058 | -0.0102 | -0.141 | 0.0046 |
| boosting | big move | all features (43) | top 10% | 60 | 1630 | 51.8 | 0.0112 | 1355 | 51.7 | 0.0154 | -0.0076 | -0.0304 | -0.2238 | -0.0221 |
| boosting | big move | all features (43) | top 5% | 60 | 914 | 51.1 | 0.0087 | 666 | 55.0 | 0.0448 | 0.0167 | -0.0008 | -0.1804 | 0.008 |
| boosting | big move | all features (43) | top 2% | 60 | 436 | 54.4 | 0.0257 | 209 | 56.5 | 0.056 | 0.0131 | 0.011 | -0.1452 | 0.0205 |
| boosting | big move | helpful features (30) | top 10% | 60 | 1513 | 51.0 | 0.0045 | 1345 | 54.0 | 0.0211 | 0.0025 | -0.0249 | -0.2129 | -0.0162 |
| boosting | big move | helpful features (30) | top 5% | 60 | 892 | 49.9 | 0.0073 | 642 | 54.8 | 0.0321 | 0.003 | -0.0136 | -0.1988 | -0.0051 |
| boosting | big move | helpful features (30) | top 2% | 60 | 392 | 53.6 | 0.0248 | 204 | 60.3 | 0.0681 | 0.0265 | 0.0225 | -0.1338 | 0.0326 |

## Chosen candidate: out-of-sample breakdown

All splits after the rule or window was learned (train-split rows of a rule are in sample).

### By index

| index | signals | hit_rate | avg_move_pct | net_futures | beats_futures_pct | net_atm | beats_atm_pct | net_spread | beats_spread_pct |
|---|---|---|---|---|---|---|---|---|---|
| BANKNIFTY | 129 | 55.0 | 0.0105 | -0.0387 | 47.3 | -0.0465 | 48.1 | -0.5803 | 5.4 |
| NIFTY | 405 | 51.6 | 0.0146 | -0.0318 | 45.2 | -0.0151 | 47.4 | -0.0872 | 35.6 |
| SENSEX | 253 | 53.8 | 0.036 | -0.013 | 48.6 | 0.0032 | 49.8 | -0.1111 | 32.0 |

### By year

| year | signals | hit_rate | avg_move_pct | net_futures | beats_futures_pct | net_atm | beats_atm_pct | net_spread | beats_spread_pct |
|---|---|---|---|---|---|---|---|---|---|
| 2022 | 157 | 59.9 | 0.0436 | 0.0016 | 51.0 | 0.0099 | 51.6 | -0.1291 | 32.5 |
| 2023 | 208 | 46.2 | 0.0015 | -0.0405 | 41.3 | -0.0325 | 42.3 | -0.1743 | 24.5 |
| 2024 | 171 | 63.2 | 0.0732 | 0.0296 | 56.7 | 0.0379 | 59.1 | -0.1249 | 38.0 |
| 2025 | 78 | 55.1 | 0.0387 | -0.0108 | 51.3 | 0.0034 | 51.3 | -0.1597 | 34.6 |
| 2026 | 173 | 43.4 | -0.0365 | -0.0995 | 37.0 | -0.0741 | 40.5 | -0.2771 | 22.0 |

### By time of day

| time of day | signals | hit_rate | avg_move_pct | net_futures | beats_futures_pct | net_atm | beats_atm_pct | net_spread | beats_spread_pct |
|---|---|---|---|---|---|---|---|---|---|
| 09:15-10:00 | 159 | 57.2 | 0.0478 | 0.0014 | 50.3 | 0.0132 | 50.9 | -0.1379 | 36.5 |
| 10:00-12:00 | 203 | 52.2 | 0.0192 | -0.0287 | 42.9 | -0.0166 | 45.3 | -0.1891 | 25.1 |
| 12:00-14:00 | 304 | 52.3 | 0.0357 | -0.0122 | 48.0 | 0.0006 | 49.3 | -0.1602 | 30.3 |
| 14:00-15:30 | 121 | 49.6 | -0.0494 | -0.0979 | 44.6 | -0.0843 | 47.1 | -0.2418 | 25.6 |

## Feature importance (permutation, validation months; gain in log loss, above zero helps)

| feature | avg_gain | kept_in_settings |
|---|---|---|
| atr_rank | 0.020748 | 12 |
| rvol_opt | 0.007865 | 8 |
| dist_prev_close | 0.006049 | 7 |
| or15_mid_dist | 0.004674 | 7 |
| atr_pct | 0.004416 | 6 |
| or30_mid_dist | 0.004406 | 10 |
| gap | 0.003371 | 11 |
| atm_iv | 0.002983 | 10 |
| vwap_dist | 0.002387 | 6 |
| pcr | 0.001553 | 12 |
| vix | 0.001413 | 12 |
| ma20_dist | 0.001368 | 12 |
| since_day_high | 0.001142 | 9 |
| pe_oi_chg | 0.001069 | 8 |
| iv_chg_day | 0.000986 | 11 |
| dist_prev_low | 0.000948 | 4 |
| ce_oi_chg | 0.000946 | 10 |
| ma50_dist | 0.000938 | 9 |
| dist_prev_high | 0.000748 | 11 |
| adv_dec | 0.000673 | 10 |
| return_6bar | 0.000497 | 12 |
| expiry_day | 0.000409 | 8 |
| or30_position | 0.000302 | 6 |
| ma5_dist | 0.000235 | 11 |
| or15_position | 0.000234 | 7 |
| since_day_low | 0.000232 | 12 |
| above_vwap_share | 0.000227 | 9 |
| oi_tilt_chg | 0.000191 | 12 |
| updown_cons | 0.000159 | 9 |
| iv_chg_30m | 0.000156 | 8 |
| swing_5m | 0.000152 | 8 |
| rvol_cons | 0.000132 | 6 |
| pcr_chg | 0.000132 | 9 |
| vix_chg_day | 0.000125 | 6 |
| swing_15m | 0.000119 | 7 |
| updown_opt | 0.00011 | 9 |
| above_vwap_share_chg | 0.000107 | 12 |
| return_1bar | 9.6e-05 | 7 |
| rvol_trend_cons | 5.9e-05 | 9 |
| rvol_trend_opt | 4.1e-05 | 7 |
| weekday | -7e-06 | 6 |
| vwap_slope | -0.00013 | 6 |
| minutes | -0.000504 | 4 |

## Calibration of the direction models (walk-forward, up to validation end)

| model | features | horizon | decile | rows | predicted_up_pct | actual_up_pct |
|---|---|---|---|---|---|---|
| logistic | all features | 15 | 1 | 17172 | 44.5 | 48.1 |
| logistic | all features | 15 | 2 | 17172 | 47.9 | 48.8 |
| logistic | all features | 15 | 3 | 17171 | 49.2 | 49.7 |
| logistic | all features | 15 | 4 | 17172 | 50.1 | 50.8 |
| logistic | all features | 15 | 5 | 17171 | 51.0 | 51.5 |
| logistic | all features | 15 | 6 | 17172 | 51.8 | 50.9 |
| logistic | all features | 15 | 7 | 17171 | 52.7 | 52.3 |
| logistic | all features | 15 | 8 | 17172 | 53.7 | 53.0 |
| logistic | all features | 15 | 9 | 17171 | 55.0 | 52.7 |
| logistic | all features | 15 | 10 | 17172 | 58.2 | 53.6 |
| logistic | all features | 30 | 1 | 16454 | 42.8 | 48.9 |
| logistic | all features | 30 | 2 | 16453 | 47.3 | 48.6 |
| logistic | all features | 30 | 3 | 16453 | 49.0 | 50.7 |
| logistic | all features | 30 | 4 | 16453 | 50.2 | 50.7 |
| logistic | all features | 30 | 5 | 16453 | 51.3 | 51.2 |
| logistic | all features | 30 | 6 | 16453 | 52.3 | 51.9 |
| logistic | all features | 30 | 7 | 16454 | 53.4 | 52.7 |
| logistic | all features | 30 | 8 | 16452 | 54.6 | 51.8 |
| logistic | all features | 30 | 9 | 16453 | 56.3 | 53.2 |
| logistic | all features | 30 | 10 | 16454 | 60.7 | 53.5 |
| logistic | all features | 60 | 1 | 15018 | 40.2 | 48.4 |
| logistic | all features | 60 | 2 | 15017 | 45.8 | 48.9 |
| logistic | all features | 60 | 3 | 15018 | 48.0 | 49.4 |
| logistic | all features | 60 | 4 | 15016 | 49.6 | 49.9 |
| logistic | all features | 60 | 5 | 15017 | 51.1 | 51.3 |
| logistic | all features | 60 | 6 | 15017 | 52.4 | 51.3 |
| logistic | all features | 60 | 7 | 15017 | 53.8 | 52.1 |
| logistic | all features | 60 | 8 | 15017 | 55.4 | 52.4 |
| logistic | all features | 60 | 9 | 15017 | 57.4 | 54.2 |
| logistic | all features | 60 | 10 | 15017 | 62.5 | 53.9 |
| boosting | all features | 15 | 1 | 17172 | 45.2 | 47.9 |
| boosting | all features | 15 | 2 | 17172 | 47.8 | 49.2 |
| boosting | all features | 15 | 3 | 17171 | 49.1 | 49.4 |
| boosting | all features | 15 | 4 | 17172 | 50.1 | 50.9 |
| boosting | all features | 15 | 5 | 17172 | 51.1 | 50.9 |
| boosting | all features | 15 | 6 | 17174 | 51.9 | 51.4 |
| boosting | all features | 15 | 7 | 17168 | 52.8 | 52.0 |
| boosting | all features | 15 | 8 | 17173 | 53.7 | 53.2 |
| boosting | all features | 15 | 9 | 17170 | 55.0 | 52.2 |
| boosting | all features | 15 | 10 | 17172 | 57.8 | 54.1 |
| boosting | all features | 30 | 1 | 16454 | 43.6 | 48.8 |
| boosting | all features | 30 | 2 | 16454 | 47.5 | 50.3 |
| boosting | all features | 30 | 3 | 16452 | 49.1 | 49.7 |
| boosting | all features | 30 | 4 | 16453 | 50.3 | 50.8 |
| boosting | all features | 30 | 5 | 16453 | 51.3 | 50.2 |
| boosting | all features | 30 | 6 | 16453 | 52.2 | 51.5 |
| boosting | all features | 30 | 7 | 16453 | 53.2 | 53.1 |
| boosting | all features | 30 | 8 | 16454 | 54.3 | 52.7 |
| boosting | all features | 30 | 9 | 16452 | 55.9 | 53.2 |
| boosting | all features | 30 | 10 | 16454 | 60.2 | 52.9 |
| boosting | all features | 60 | 1 | 15019 | 40.7 | 50.8 |
| boosting | all features | 60 | 2 | 15018 | 46.3 | 47.9 |
| boosting | all features | 60 | 3 | 15015 | 48.3 | 48.8 |
| boosting | all features | 60 | 4 | 15017 | 49.7 | 50.2 |
| boosting | all features | 60 | 5 | 15017 | 50.9 | 49.3 |
| boosting | all features | 60 | 6 | 15017 | 52.1 | 51.0 |
| boosting | all features | 60 | 7 | 15017 | 53.3 | 53.2 |
| boosting | all features | 60 | 8 | 15017 | 54.7 | 54.7 |
| boosting | all features | 60 | 9 | 15017 | 56.8 | 54.0 |
| boosting | all features | 60 | 10 | 15017 | 62.7 | 51.7 |

### By index, year and time of day (all features)

| model | features | horizon | by | slice | rows | predicted_up_pct | actual_up_pct | hit_rate_pct |
|---|---|---|---|---|---|---|---|---|
| logistic | all features | 15 | index | BANKNIFTY | 57228 | 51.4 | 50.6 | 51.3 |
| logistic | all features | 15 | index | NIFTY | 57221 | 51.4 | 51.5 | 52.2 |
| logistic | all features | 15 | index | SENSEX | 57267 | 51.4 | 51.3 | 51.8 |
| logistic | all features | 15 | year | 2022 | 39639 | 51.5 | 51.1 | 51.6 |
| logistic | all features | 15 | year | 2023 | 52837 | 51.3 | 51.5 | 51.8 |
| logistic | all features | 15 | year | 2024 | 53104 | 51.8 | 50.8 | 51.6 |
| logistic | all features | 15 | year | 2025 | 26136 | 50.7 | 51.2 | 52.5 |
| logistic | all features | 15 | time of day | 09:15-10:00 | 21478 | 50.6 | 50.2 | 51.3 |
| logistic | all features | 15 | time of day | 10:00-12:00 | 57334 | 51.2 | 51.3 | 51.7 |
| logistic | all features | 15 | time of day | 12:00-14:00 | 57281 | 51.7 | 51.5 | 52.1 |
| logistic | all features | 15 | time of day | 14:00-15:30 | 35623 | 51.8 | 50.9 | 51.6 |
| logistic | all features | 30 | index | BANKNIFTY | 54834 | 51.8 | 50.8 | 50.9 |
| logistic | all features | 30 | index | NIFTY | 54827 | 51.8 | 51.8 | 52.4 |
| logistic | all features | 30 | index | SENSEX | 54871 | 51.8 | 51.4 | 52.0 |
| logistic | all features | 30 | year | 2022 | 37961 | 52.0 | 51.4 | 52.2 |
| logistic | all features | 30 | year | 2023 | 50632 | 51.4 | 51.6 | 51.3 |
| logistic | all features | 30 | year | 2024 | 50892 | 52.4 | 50.9 | 51.6 |
| logistic | all features | 30 | year | 2025 | 25047 | 50.9 | 51.3 | 52.4 |
| logistic | all features | 30 | time of day | 09:15-10:00 | 21478 | 50.9 | 50.7 | 52.2 |
| logistic | all features | 30 | time of day | 10:00-12:00 | 57334 | 51.7 | 51.6 | 51.9 |
| logistic | all features | 30 | time of day | 12:00-14:00 | 57280 | 52.0 | 51.3 | 51.7 |
| logistic | all features | 30 | time of day | 14:00-15:30 | 28440 | 52.3 | 51.2 | 51.1 |
| logistic | all features | 60 | index | BANKNIFTY | 50047 | 51.8 | 50.7 | 51.0 |
| logistic | all features | 60 | index | NIFTY | 50040 | 51.5 | 51.6 | 53.0 |
| logistic | all features | 60 | index | SENSEX | 50084 | 51.5 | 51.1 | 51.6 |
| logistic | all features | 60 | year | 2022 | 34611 | 52.3 | 51.5 | 53.0 |
| logistic | all features | 60 | year | 2023 | 46222 | 50.8 | 51.5 | 51.4 |
| logistic | all features | 60 | year | 2024 | 46469 | 52.3 | 50.6 | 51.6 |
| logistic | all features | 60 | year | 2025 | 22869 | 50.6 | 51.1 | 51.6 |
| logistic | all features | 60 | time of day | 09:15-10:00 | 21482 | 50.7 | 50.5 | 51.8 |
| logistic | all features | 60 | time of day | 10:00-12:00 | 57330 | 51.6 | 51.2 | 52.4 |
| logistic | all features | 60 | time of day | 12:00-14:00 | 57282 | 51.9 | 51.5 | 51.7 |
| logistic | all features | 60 | time of day | 14:00-15:30 | 14077 | 52.3 | 50.8 | 50.7 |
| boosting | all features | 15 | index | BANKNIFTY | 57228 | 51.3 | 50.6 | 51.5 |
| boosting | all features | 15 | index | NIFTY | 57221 | 51.5 | 51.5 | 51.9 |
| boosting | all features | 15 | index | SENSEX | 57267 | 51.5 | 51.3 | 51.9 |
| boosting | all features | 15 | year | 2022 | 39639 | 51.0 | 51.1 | 50.8 |
| boosting | all features | 15 | year | 2023 | 52837 | 51.9 | 51.5 | 51.4 |
| boosting | all features | 15 | year | 2024 | 53104 | 51.4 | 50.8 | 52.8 |
| boosting | all features | 15 | year | 2025 | 26136 | 51.1 | 51.2 | 52.1 |
| boosting | all features | 15 | time of day | 09:15-10:00 | 21478 | 50.5 | 50.2 | 51.5 |
| boosting | all features | 15 | time of day | 10:00-12:00 | 57334 | 51.4 | 51.3 | 52.0 |
| boosting | all features | 15 | time of day | 12:00-14:00 | 57281 | 51.8 | 51.5 | 52.0 |
| boosting | all features | 15 | time of day | 14:00-15:30 | 35623 | 51.7 | 50.9 | 51.3 |
| boosting | all features | 30 | index | BANKNIFTY | 54834 | 51.6 | 50.8 | 51.3 |
| boosting | all features | 30 | index | NIFTY | 54827 | 51.8 | 51.8 | 51.7 |
| boosting | all features | 30 | index | SENSEX | 54871 | 51.9 | 51.4 | 51.6 |
| boosting | all features | 30 | year | 2022 | 37961 | 51.1 | 51.4 | 50.9 |
| boosting | all features | 30 | year | 2023 | 50632 | 52.4 | 51.6 | 50.8 |
| boosting | all features | 30 | year | 2024 | 50892 | 51.9 | 50.9 | 52.5 |
| boosting | all features | 30 | year | 2025 | 25047 | 51.3 | 51.3 | 52.1 |
| boosting | all features | 30 | time of day | 09:15-10:00 | 21478 | 50.6 | 50.7 | 51.2 |
| boosting | all features | 30 | time of day | 10:00-12:00 | 57334 | 51.7 | 51.6 | 51.7 |
| boosting | all features | 30 | time of day | 12:00-14:00 | 57280 | 52.0 | 51.3 | 51.5 |
| boosting | all features | 30 | time of day | 14:00-15:30 | 28440 | 52.2 | 51.2 | 51.6 |
| boosting | all features | 60 | index | BANKNIFTY | 50047 | 51.4 | 50.7 | 51.3 |
| boosting | all features | 60 | index | NIFTY | 50040 | 51.6 | 51.6 | 52.3 |
| boosting | all features | 60 | index | SENSEX | 50084 | 51.6 | 51.1 | 51.3 |
| boosting | all features | 60 | year | 2022 | 34611 | 51.6 | 51.5 | 51.3 |
| boosting | all features | 60 | year | 2023 | 46222 | 52.0 | 51.5 | 51.3 |
| boosting | all features | 60 | year | 2024 | 46469 | 51.3 | 50.6 | 52.2 |
| boosting | all features | 60 | year | 2025 | 22869 | 51.1 | 51.1 | 51.6 |
| boosting | all features | 60 | time of day | 09:15-10:00 | 21482 | 50.3 | 50.5 | 51.2 |
| boosting | all features | 60 | time of day | 10:00-12:00 | 57330 | 51.5 | 51.2 | 51.8 |
| boosting | all features | 60 | time of day | 12:00-14:00 | 57282 | 51.9 | 51.5 | 51.7 |
| boosting | all features | 60 | time of day | 14:00-15:30 | 14077 | 52.0 | 50.8 | 51.3 |

## Every rule and model tried

- `reports/delta_runs/spot_rules.csv` (every rule cell, including those with too few trades)
- `reports/delta_runs/spot_models.csv`
- `reports/delta_runs/spot_importance.csv`, `reports/delta_runs/spot_calibration.csv`, `reports/delta_runs/spot_calibration_slices.csv`

Scripts: `backend/app/equity_lab/spot_fetch.py`, `spot_features.py`, `spot_study.py`.
