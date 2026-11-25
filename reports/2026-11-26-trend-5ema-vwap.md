# Trend filter, 5 EMA entries and VWAP bands (Delta India data only), 26 Nov 2026

*Hypothetical backtest on completed bars; kept apart from real P&L. Decision support, not investment advice. **Nothing live was changed.** No order was placed.*

## Short answer

- **No version beats costs in a way I would trust.** 446 variants were tried in this run (about 749 in the running total). The validation-chosen best of each part has a final-check 90% interval that includes zero (see below).
- **Some limit-entry versions do look positive on the final check, and their interval excludes zero when all 10 coins are pooled, but the gain comes only from the five coins that Delta listed in the last 14 months (EVAA, AKE, BLESS, ARIA, AIOT).** On BTC, ETH, SOL, XRP and LISTA the same versions lose 4-5 bps per trade. Those five coins are in the list because they are in today's top 10 by turnover (a selection effect), they are thin (1 bp slippage and a fill at every touch are generous there), and they appear only in the final window. I treat this as a lead to test on paper, not as an edge.
- **The trend filter would have blocked today's long.** All 18 filter combinations block it (details below). On history the filter does **not** improve results: averaged over the 144 paired Part B versions it moves validation by about +0.4 bps and the final check by about -1.7 bps per trade, i.e. nothing.
- **Least bad for live: stop live 5 EMA and move it to paper.** If you keep it live, the single pullback limit (halfway up the alert candle) is the least bad entry, not the current close entry. Nothing was applied.

## Method, and what to distrust

- **Data:** Delta India 1-minute last-price candles only (public API), BTC, ETH, SOL, LISTA plus Delta's top-10 crypto perps by today's 24h turnover (EVAA, AIOT, BLESS, AKE, ARIA, XRP). Each coin starts when Delta listed it: BTC Dec 2023, ETH Feb 2024, SOL/XRP Apr 2024, LISTA Jul 2024, BLESS and EVAA Oct 2025, AIOT and ARIA Apr 2026, AKE Jul 2026. **The train period is therefore BTC/ETH/SOL/XRP/LISTA only**, and the top-10 list is picked from today's turnover (a selection bias).
- **Costs:** taker 0.05% and maker 0.02% per side, plus 18% GST, plus 1 bp slippage on every taker fill. A resting limit that fills is a maker fill with no slippage. **This is optimistic for limit orders:** a fill is assumed whenever price touches the limit; a real queue often does not fill at a touch, and the trades you miss are usually the winners.
- **Split (fixed before looking):** train < 2025-07-01, validation 2025-07-01 to 2026-03-31, final check from 2026-04-01 to now, by entry time. Versions were chosen on validation only (at least 300 trades). **The table shows the final check for every version for information; the headline uses only the validation-chosen one.** The final window overlaps the period used in today's earlier reports (they used Binance data for the same months), so it is not fresh data.
- **Definitions I had to choose** (the brief left room): B1 uses 2-bar fractal swing points on 1h bars, the last two highs and lows inside 24h; B2 is the 1h close vs EMA(50) with the EMA falling/rising (it is almost never neutral, so its two neutral options are identical); B3 uses the 4h EMA(20); B4 uses a rolling 24h VWAP of typical price. R1 = the latest 1h close beyond the last swing high (low); R2 = a higher low (lower high), then a 1h close beyond EMA(20). A 1h or 4h value is used only after that bar closed. The VWAP script's source was not supplied, so Part C uses the generic session VWAP, reset 00:00 UTC, with volume-weighted standard-deviation bands, and nothing is traded in a session's first 2 hours.
- **Other choices:** the 'nearest 1h support' target uses 1h swing lows of the last 7 days and the trade is skipped when none is 2R away; the trail moves behind the latest confirmed swing high (low) of the trading timeframe once +1R was reached; trades end after 48 candles; one position per coin; E2's daily reset means trades are closed at 00:00 UTC and the alert and break candles must be on the same UTC day; min-stop uses 3 x the trade's own round-trip cost (about 41 bps taker both ways).

## Part A. Trend filter

### Today's long (BTCUSD, 25 Nov)

- **Which source?** The numbers match a **5-minute** breakout candle: it closed at **11:50 UTC (17:20 IST)** at 58,731.0 with the alert candle's low at 58,431.5 (your stop); the target 59,629.5 is exactly 3 x the distance from 58,731 to the stop, i.e. measured from the signal close, not from your fill at 58,762. A 15m scanner is ruled out: its only long that hour (12:00 UTC) had its stop at 58,427. The live 5m test and a 5m scanner run the same rule, so **I cannot tell which of the two sent it**: the local database has no orders and the production query I tried was blocked, so I did not look further. Please check the live ledger's `forward_test_id` / `scanner_id` for that order.
- **The fill was 31 points (5.3 bps) worse than the signal close** (58,762 vs 58,731). That moved reward:risk from 3.0 to 2.6. My model assumes 1 bp slippage; this one fill was five times that. One sample, but worth watching.
- **Every bias said bearish** at that moment: B1 lower highs and lower lows, B2 price under a falling 1h EMA(50), B3 4h close under EMA(20), B4 price (58,613) 1.7% under the 24h VWAP (59,605). No reversal had happened (no 1h close above the last swing high at 59,448.5, no higher low with a close above the 1h EMA(20)). **All 18 filter combinations block this long.** The 5m short at 12:00 UTC was allowed by all of them.

### Each filter on the live rule (enter at the close, 3R from the signal close, no other checks). Net bps per trade (trades)

| Filter (bias \| reversal rule \| neutral) | 5m validation | 5m final | 15m validation | 15m final |
|---|---|---|---|---|
| none | -14.8 (30950) | -19.2 (38618) | -13.7 (10461) | -22.7 (13511) |
| B1 · R1 · neutral=both | -14.4 (28555) | -19.7 (35562) | -14.4 (9523) | -23.1 (12355) |
| B1 · R1 · neutral=none **(chosen on validation)** | -12.9 (14284) | -17.6 (16533) | -16.7 (4835) | -23.7 (5836) |
| B1 · R2 · neutral=both | -14.7 (28286) | -19.8 (35211) | -14.8 (9507) | -22.7 (12333) |
| B1 · R2 · neutral=none | -13.4 (13916) | -17.6 (16015) | -17.5 (4785) | -23.6 (5759) |
| B1 · R3 · neutral=both | -14.7 (28286) | -19.8 (35211) | -14.8 (9507) | -22.7 (12333) |
| B1 · R3 · neutral=none | -13.4 (13916) | -17.6 (16015) | -17.5 (4785) | -23.6 (5759) |
| B2 · R1 · neutral=both | -15.7 (24889) | -17.4 (30257) | -14.5 (7204) | -19.0 (9080) |
| B2 · R1 · neutral=none | -15.7 (24889) | -17.4 (30257) | -14.5 (7204) | -19.0 (9080) |
| B2 · R2 · neutral=both | -15.8 (25192) | -17.1 (30576) | -15.9 (7387) | -19.7 (9267) |
| B2 · R2 · neutral=none | -15.8 (25192) | -17.1 (30576) | -15.9 (7387) | -19.7 (9267) |
| B2 · R3 · neutral=both | -16.0 (24601) | -17.2 (29925) | -15.2 (7133) | -19.2 (8980) |
| B2 · R3 · neutral=none | -16.0 (24601) | -17.2 (29925) | -15.2 (7133) | -19.2 (8980) |
| B3 · R1 · neutral=both | -15.6 (25753) | -16.8 (31306) | -13.8 (8270) | -19.8 (10399) |
| B3 · R2 · neutral=both | -15.8 (26134) | -16.8 (31701) | -15.1 (8406) | -20.6 (10538) |
| B3 · R3 · neutral=both | -16.1 (25288) | -16.8 (30778) | -14.1 (8233) | -20.3 (10360) |
| B4 · R1 · neutral=both | -15.2 (23096) | -19.0 (28593) | -13.4 (6803) | -21.1 (8664) |
| B4 · R2 · neutral=both | -15.2 (23837) | -18.7 (29525) | -13.9 (6949) | -21.2 (8852) |
| B4 · R3 · neutral=both | -15.4 (22706) | -19.1 (28222) | -13.6 (6766) | -21.1 (8590) |

**How much the filter changes the live rule** (5m, validation / final, net bps per trade; longs and shorts separately):

| | all | longs | shorts |
|---|---|---|---|
| validation, no filter | -14.8 (30950) | -14.7 (15588) | -14.8 (15362) |
| validation, filter B1 · R1 · neutral=none | -12.9 (14284) | -12.3 (6937) | -13.5 (7347) |
| final, no filter | -19.2 (38618) | -17.6 (19558) | -20.8 (19060) |
| final, filter B1 · R1 · neutral=none | -17.6 (16533) | -17.4 (8157) | -17.9 (8376) |

R3 (no counter-trend trades at all) sits within about a bp of R1 and R2 in the table: confirmed reversals are too rare to matter. 'No trade when neutral' halves the trade count and moves the result about 2 bps, better on 5m validation and worse on 15m, which is noise at this size.

## Part B. 5 EMA entries and exits

Rows are the unfiltered, no-min-stop versions (fill check on), net bps per trade (trades), pooled over all coins. The full list of every version, with the filter and min-stop switches, is in the appendix.

| Version | 5m val | 5m final | 15m val | 15m final |
|---|---|---|---|---|
| E1 today's live rule, no checks | -14.8 (30950) | -19.2 (38618) | -13.7 (10461) | -22.7 (13511) |
| E1 T2 | -15.1 (35693) | -18.2 (43912) | -14.9 (12059) | -20.9 (15570) |
| E1 T3 | -14.8 (30159) | -19.4 (37582) | -13.0 (10305) | -22.0 (13370) |
| E1 T3ref | -14.9 (29150) | -19.7 (36141) | -13.3 (10196) | -22.4 (13063) |
| E1 SR | -15.9 (27296) | -19.9 (34711) | -16.8 (9974) | -21.6 (13064) |
| E1 TR | -8.9 (57365) | -8.8 (74158) | -7.5 (18980) | -5.3 (24958) |
| E2 algotest | -12.5 (21453) | -14.2 (23192) | -11.8 (7148) | -19.8 (8486) |
| E3 · broken · w3 T2 | -8.1 (18438) | -8.3 (21168) | -6.6 (6327) | -6.0 (7617) |
| E3 · broken · w3 T3 | -8.0 (16978) | -8.4 (19713) | -6.7 (5831) | -7.3 (7115) |
| E3 · broken · w3 T3ref | -7.4 (15429) | -8.0 (17884) | -5.5 (5385) | -9.1 (6550) |
| E3 · broken · w3 SR | -8.6 (15198) | -7.0 (17710) | -7.3 (5581) | -8.1 (6811) |
| E3 · broken · w3 TR | -5.2 (19327) | -2.5 (22182) | -3.7 (6835) | 0.2 (8116) |
| E3 · broken · w6 T2 | -8.0 (19134) | -7.9 (22001) | -5.8 (6581) | -5.8 (7968) |
| E3 · broken · w6 T3 | -8.0 (17592) | -7.7 (20390) | -6.1 (6029) | -6.7 (7419) |
| E3 · broken · w6 T3ref | -8.0 (15860) | -7.7 (18370) | -6.4 (5510) | -6.0 (6736) |
| E3 · broken · w6 SR | -8.5 (15727) | -6.8 (18273) | -7.4 (5769) | -6.5 (7086) |
| E3 · broken · w6 TR | -5.0 (20238) | -2.4 (23163) | -3.5 (7164) | 0.0 (8548) |
| E3 · half · w3 T2 | -6.6 (19225) | -4.1 (20832) | -5.1 (7322) | -5.9 (8468) |
| E3 · half · w3 T3 | -6.9 (18791) | -5.2 (20356) | -5.8 (7130) | -7.4 (8227) |
| E3 · half · w3 T3ref | -7.3 (16935) | -5.2 (18356) | -8.1 (6351) | -9.6 (7439) |
| E3 · half · w3 SR | -7.8 (16515) | -4.8 (17996) | -7.7 (6638) | -9.3 (7669) |
| E3 · half · w3 TR | -5.8 (18143) | -3.9 (19746) | -6.0 (7017) | -4.9 (8110) |
| E3 · half · w6 T2 | -6.5 (20519) | -3.7 (22193) | -5.4 (7851) | -5.2 (9105) |
| E3 · half · w6 T3 | -6.8 (19947) | -4.9 (21567) | -5.8 (7580) | -7.1 (8837) |
| E3 · half · w6 T3ref | -7.4 (17625) | -5.2 (19068) | -8.0 (6658) | -9.3 (7827) |
| E3 · half · w6 SR | -7.6 (17375) | -4.4 (18936) | -7.5 (7032) | -9.1 (8219) |
| E3 · half · w6 TR | -5.7 (19346) | -4.0 (21025) | -5.6 (7495) | -4.9 (8737) |
| E3 · ema · w3 T2 | -7.2 (16746) | -7.3 (20758) | -3.4 (5873) | -5.5 (7331) |
| E3 · ema · w3 T3 | -6.2 (15288) | -7.0 (19073) | -2.3 (5390) | -6.6 (6812) |
| E3 · ema · w3 T3ref | -7.6 (14239) | -9.1 (17662) | -2.1 (5035) | -5.3 (6335) |
| E3 · ema · w3 SR | -8.7 (14051) | -8.2 (17637) | -3.0 (5198) | -6.6 (6579) |
| E3 · ema · w3 TR | -4.5 (18955) | -4.3 (23378) | -2.1 (6663) | 1.2 (8261) |
| E3 · ema · w6 T2 | -7.1 (17370) | -6.9 (21552) | -2.8 (6081) | -4.9 (7641) |
| E3 · ema · w6 T3 | -6.4 (15843) | -7.3 (19710) | -1.3 (5542) | -6.1 (7054) |
| E3 · ema · w6 T3ref | -7.7 (14675) | -10.0 (18127) | -3.0 (5129) | -5.3 (6525) |
| E3 · ema · w6 SR | -8.6 (14554) | -8.3 (18231) | -2.9 (5360) | -6.0 (6829) |
| E3 · ema · w6 TR | -4.5 (19862) | -4.3 (24551) | -2.7 (6975) | 0.8 (8701) |

- **Positive on the final check (at least 200 trades): 39 of 290 Part B versions; positive on both validation and final: 16.** 38 of the 39 are limit-entry (E3) versions and 35 have the min-stop rule on (it keeps only the wider-stop signals). All rest on the assumption that every touched limit fills at no slippage.

### Where the positive results come from

Three representative versions (5m, no trend filter, min-stop on), final check, net bps per trade with 90% bootstrap interval:

| Version | all 10 coins | only BTC, ETH, SOL, XRP, LISTA | only the 5 young coins |
|---|---|---|---|
| E3 pullback to the broken alert low, wait 3, break-even + trail | 5.2 [0.6, 10.9] (8150) | -4.8 [-6.2, -3.2] (2218) | 8.9 [2.9, 16.6] (5932) |
| E3 limit halfway up the alert candle, wait 6, 2R target | 8.1 [4.6, 11.6] (4655) | -3.9 [-9.6, 2.1] (436) | 9.3 [5.6, 13.2] (4219) |
| E1 close entry, break-even + trail | -5.1 [-6.4, -3.9] (33531) | -7.6 [-8.6, -6.6] (6664) | -4.5 [-6.1, -2.9] (26867) |

The young coins were listed from Oct 2025 (BLESS, EVAA) to Jul 2026 (AKE), so most of their history sits in the final window.

- **Entry matters most.** On 5m validation: close entry (today's rule) about -15, AlgoTest exact about -12.5, pullback limits about -6.5 to -8. On the final check the close entry is worse (about -19) while the limits stay around -4 to -8.
- **Exit matters less, except the break-even trail**, which is the best exit in every row (close entry -15 becomes -9; pullbacks -7 become -4.5 to -6). Fixed 2R, 3R and 'nearest support' are within about 1 bp of each other. The trail's gain is partly that more trades end near break-even and so lose about the cost instead of a full stop.

## Part C. VWAP bands

Net bps per trade (trades), pooled, best of the filter / hours switches on validation shown in the appendix; here are the unfiltered, all-hours rows:

| Version | 1m val | 1m final | 5m val | 5m final | 15m val | 15m final |
|---|---|---|---|---|---|---|
| fade k=2 stop=next target=vwap | -16.6 (9682) | -15.5 (12292) | -21.2 (6728) | -17.8 (8733) | -17.9 (4734) | -12.8 (6176) |
| fade k=2 stop=next target=1s | -15.2 (9601) | -14.3 (11948) | -16.4 (6526) | -15.7 (8275) | -12.4 (4456) | -10.7 (5563) |
| fade k=2 stop=half target=vwap | -16.0 (11686) | -16.8 (14790) | -21.0 (7991) | -17.9 (10177) | -17.9 (5334) | -15.3 (6870) |
| fade k=2 stop=half target=1s | -14.8 (11496) | -15.8 (14251) | -16.4 (7558) | -15.3 (9436) | -13.5 (4876) | -13.5 (6034) |
| fade k=3 stop=next target=vwap | -18.3 (1783) | -13.4 (2779) | -19.7 (1355) | -12.2 (2142) | -18.5 (985) | -10.4 (1599) |
| fade k=3 stop=next target=1s | -15.1 (1715) | -11.7 (2634) | -12.8 (1267) | -9.8 (1980) | -15.6 (896) | -7.5 (1449) |
| fade k=3 stop=half target=vwap | -16.0 (1978) | -14.3 (3079) | -15.6 (1441) | -12.7 (2288) | -18.0 (1033) | -15.2 (1682) |
| fade k=3 stop=half target=1s | -13.7 (1905) | -13.1 (2923) | -10.7 (1346) | -7.3 (2113) | -15.2 (935) | -12.8 (1515) |
| pullback target=1sigma | -6.7 (13445) | -3.1 (17938) | -6.7 (10328) | 0.2 (13881) | -7.3 (9559) | 0.1 (12540) |
| pullback target=2sigma | -7.9 (12700) | -6.1 (16642) | -7.9 (8345) | -3.8 (11086) | -6.8 (7330) | -2.6 (9579) |

- **1m, plainly: fees are bigger than the 1m move.** A taker round trip costs about 13.8 bps (fees, GST, slippage) and a maker entry plus taker exit about 9.3. The best 1m version on validation (at least 300 trades) made -5.3 bps per trade.
- **Fade:** every fade version with enough trades lost on train (best -8.1) and on validation (best -8.4). Its final-check gains (up to about +19 bps with the filter and 08-20 UTC hours) come from versions that were negative on validation; that is what noise looks like.
- **Pullback:** the closest family to its costs: validation -8.2 to -4.5 bps across versions, helped by maker entries; final check about +1 for the validation-chosen one, with longs and shorts of opposite sign.

## Best version per part (chosen on validation, final check shown once)

Deflated Sharpe uses N = 749 trials and the variance of per-trade Sharpe across this run's variants (0.00209).

### Part A (trend filter on the live rule): `5m|E1 live rule|filter=B1|R1|neutral=none`

| segment | trades | net bps | win rate | profit factor | worst drawdown (bps; per-trade results added up across all coins, one contract each) | longs net (n) | shorts net (n) |
|---|---|---|---|---|---|---|---|
| train | 19965 | -12.9 | 26% | 0.60 | 258459 | -12.6 (10042) | -13.2 (9923) |
| validation | 14284 | -12.9 | 26% | 0.68 | 184542 | -12.3 (6937) | -13.5 (7347) |
| final | 16533 | -17.6 | 26% | 0.67 | 292222 | -17.4 (8157) | -17.9 (8376) |

- **Final check bootstrap 90% interval for the mean: -19.8 to -15.4 bps.** Deflated Sharpe (probability the edge is real after 749 trials): **0.000**.
- Final-check by coin: AIOTUSD -36.5 (1417); AKEUSD -10.5 (1123); ARIAUSD -24.5 (951); BLESSUSD -17.9 (2018); BTCUSD -13.7 (2270); ETHUSD -14.1 (2088); EVAAUSD -22.3 (1910); LISTAUSD -33.3 (294); SOLUSD -12.6 (2202); XRPUSD -12.5 (2260)
- By quarter: 2024Q1 -12.6 (1054); 2024Q2 -14.1 (3301); 2024Q3 -13.1 (3887); 2024Q4 -10.1 (3867); 2025Q1 -13.2 (3766); 2025Q2 -14.4 (4090); 2025Q3 -13.0 (4246); 2025Q4 -11.7 (5505); 2026Q1 -14.2 (4533); 2026Q2 -19.6 (5718); 2026Q3 -17.4 (6372); 2026Q4 -15.5 (4443)

### Part B (5 EMA entry/exit): `5m|E3|half|w3|T3ref|filter=best|minstop=1`

| segment | trades | net bps | win rate | profit factor | worst drawdown (bps; per-trade results added up across all coins, one contract each) | longs net (n) | shorts net (n) |
|---|---|---|---|---|---|---|---|
| train | 474 | -3.2 | 17% | 0.93 | 2861 | -6.8 (240) | 0.6 (234) |
| validation | 551 | 10.7 | 19% | 1.20 | 2347 | 0.7 (262) | 19.8 (289) |
| final | 1145 | 6.0 | 18% | 1.08 | 5890 | 7.1 (591) | 4.9 (554) |

- **Final check bootstrap 90% interval for the mean: -7.1 to 20.4 bps.** Deflated Sharpe (probability the edge is real after 749 trials): **0.000**.
- Final-check by coin: AIOTUSD 0.2 (134); AKEUSD -2.5 (324); ARIAUSD 18.0 (156); BLESSUSD -9.0 (203); BTCUSD -8.1 (11); ETHUSD 10.3 (21); EVAAUSD 29.3 (227); LISTAUSD 29.7 (13); SOLUSD 4.1 (25); XRPUSD -19.7 (31)
- By quarter: 2024Q1 -23.6 (12); 2024Q2 -0.4 (19); 2024Q3 -16.9 (52); 2024Q4 12.3 (125); 2025Q1 -4.0 (170); 2025Q2 -12.5 (96); 2025Q3 22.3 (43); 2025Q4 8.1 (370); 2026Q1 14.2 (138); 2026Q2 9.9 (250); 2026Q3 2.4 (433); 2026Q4 7.2 (462)

### Part C fade: `15m|filter=best|hours=all|fade k=2 stop=next target=vwap`

| segment | trades | net bps | win rate | profit factor | worst drawdown (bps; per-trade results added up across all coins, one contract each) | longs net (n) | shorts net (n) |
|---|---|---|---|---|---|---|---|
| train | 1309 | -17.5 | 44% | 0.70 | 23574 | -17.8 (703) | -17.0 (606) |
| validation | 1056 | -8.4 | 47% | 0.88 | 9308 | -13.2 (561) | -3.0 (495) |
| final | 1214 | 2.3 | 49% | 1.03 | 8717 | -1.9 (597) | 6.3 (617) |

- **Final check bootstrap 90% interval for the mean: -12.0 to 16.7 bps.** Deflated Sharpe (probability the edge is real after 749 trials): **0.000**.
- Final-check by coin: AIOTUSD 97.8 (124); AKEUSD 4.2 (64); ARIAUSD 23.4 (79); BLESSUSD 0.9 (156); BTCUSD -18.7 (148); ETHUSD -13.2 (132); EVAAUSD -12.8 (162); LISTAUSD 17.0 (52); SOLUSD -17.4 (145); XRPUSD -22.4 (152)
- By quarter: 2024Q1 -5.3 (71); 2024Q2 -20.8 (227); 2024Q3 -18.9 (255); 2024Q4 -6.4 (254); 2025Q1 -30.1 (239); 2025Q2 -15.7 (263); 2025Q3 -11.7 (291); 2025Q4 -3.7 (395); 2026Q1 -10.9 (370); 2026Q2 -5.2 (444); 2026Q3 15.6 (440); 2026Q4 -5.5 (330)

### Part C pullback: `5m|filter=none|hours=08-20|pullback target=2sigma`

| segment | trades | net bps | win rate | profit factor | worst drawdown (bps; per-trade results added up across all coins, one contract each) | longs net (n) | shorts net (n) |
|---|---|---|---|---|---|---|---|
| train | 5397 | -7.2 | 37% | 0.85 | 39764 | -8.4 (2707) | -5.9 (2690) |
| validation | 4154 | -4.5 | 36% | 0.92 | 24070 | -7.3 (2088) | -1.7 (2066) |
| final | 5313 | 1.1 | 38% | 1.01 | 15683 | 7.5 (2794) | -6.0 (2519) |

- **Final check bootstrap 90% interval for the mean: -5.6 to 7.9 bps.** Deflated Sharpe (probability the edge is real after 749 trials): **0.000**.
- Final-check by coin: AIOTUSD 3.5 (656); AKEUSD -2.4 (310); ARIAUSD 8.9 (495); BLESSUSD 22.9 (573); BTCUSD -11.5 (619); ETHUSD -10.7 (686); EVAAUSD 25.3 (458); LISTAUSD -11.2 (302); SOLUSD -5.0 (607); XRPUSD -6.6 (607)
- By quarter: 2024Q1 -9.8 (226); 2024Q2 -7.6 (893); 2024Q3 -9.4 (1050); 2024Q4 -15.9 (1014); 2025Q1 -1.3 (1103); 2025Q2 -2.1 (1111); 2025Q3 -6.7 (1101); 2025Q4 -3.1 (1596); 2026Q1 -4.5 (1457); 2026Q2 -1.9 (1960); 2026Q3 2.0 (2061); 2026Q4 4.2 (1292)

## Recommendation (nothing applied)

1. **Stop live 5 EMA and run it as paper**, with the checks below, until a version holds up on data it was not chosen on. Nothing here shows an edge that survives costs, selection across ~750 variants, a 90% interval that excludes zero, and the coins that existed from the start.
2. If you want it live regardless, the **least bad** of your three options is the **single pullback limit** (halfway up the alert candle, wait 3 candles), with the fill check and the min-stop rule: about -5 to -8 bps on 5m validation (and the final check) versus -15 and -19 for today's close entry. Its optimism is the maker-fill assumption, and a limit that is never touched makes no trade, so it trades less.
3. Add the **fill check** (skip when reward:risk from the actual fill is under 1:2, using the real fill) and the **min-stop rule** to whatever stays on; they cost nothing and remove the 1:1 and 1:12 style trades. The trend filter is optional: it blocked today's long but does not improve results on history.

## What you need to decide

- Pause the 5 EMA live test and 5m scanner Live switch (keep paper)? Or keep live and switch the entry to the single pullback limit?
- Whether the trend filter should be added anyway as a safety rule (it blocked today's long, and costs nothing measurable), recognising it is not shown to add edge.
- Whether to spend time on measuring real slippage and real limit fill rates from your own ledger (today's fill was 5.3 bps worse than the signal): that is the biggest unknown behind every limit-entry result here.

## Appendix: every version (net bps per trade, trades). Filter 'best' = B1|R1|neutral=none

| Version | val | final | final longs | final shorts | val PF | final PF | final win |
|---|---|---|---|---|---|---|---|
| 5m · E1 · T2 · filter=none · minstop=0 | -15.1 (35693) | -18.2 (43912) | -16.4 (22179) | -20.1 (21733) | 0.64 | 0.68 | 31% |
| 5m · E1 · T3 · filter=none · minstop=0 | -14.8 (30159) | -19.4 (37582) | -17.3 (18979) | -21.6 (18603) | 0.67 | 0.68 | 26% |
| 5m · E1 · T3ref · filter=none · minstop=0 | -14.9 (29150) | -19.7 (36141) | -18.1 (18307) | -21.3 (17834) | 0.67 | 0.69 | 27% |
| 5m · E1 · SR · filter=none · minstop=0 | -15.9 (27296) | -19.9 (34711) | -17.4 (17822) | -22.6 (16889) | 0.66 | 0.68 | 24% |
| 5m · E1 · TR · filter=none · minstop=0 | -8.9 (57365) | -8.8 (74158) | -8.5 (37465) | -9.0 (36693) | 0.49 | 0.56 | 8% |
| 5m · E3 · broken · w3 · T2 · filter=none · minstop=0 | -8.1 (18438) | -8.3 (21168) | -7.1 (10606) | -9.6 (10562) | 0.69 | 0.76 | 33% |
| 5m · E3 · broken · w3 · T3 · filter=none · minstop=0 | -8.0 (16978) | -8.4 (19713) | -7.2 (9885) | -9.8 (9828) | 0.73 | 0.79 | 27% |
| 5m · E3 · broken · w3 · T3ref · filter=none · minstop=0 | -7.4 (15429) | -8.0 (17884) | -6.4 (8952) | -9.6 (8932) | 0.77 | 0.82 | 20% |
| 5m · E3 · broken · w3 · SR · filter=none · minstop=0 | -8.6 (15198) | -7.0 (17710) | -3.5 (8903) | -10.5 (8807) | 0.72 | 0.83 | 22% |
| 5m · E3 · broken · w3 · TR · filter=none · minstop=0 | -5.2 (19327) | -2.5 (22182) | -2.4 (11103) | -2.5 (11079) | 0.58 | 0.83 | 9% |
| 5m · E3 · broken · w6 · T2 · filter=none · minstop=0 | -8.0 (19134) | -7.9 (22001) | -6.7 (11074) | -9.2 (10927) | 0.69 | 0.77 | 34% |
| 5m · E3 · broken · w6 · T3 · filter=none · minstop=0 | -8.0 (17592) | -7.7 (20390) | -6.3 (10253) | -9.2 (10137) | 0.73 | 0.80 | 27% |
| 5m · E3 · broken · w6 · T3ref · filter=none · minstop=0 | -8.0 (15860) | -7.7 (18370) | -5.7 (9212) | -9.7 (9158) | 0.75 | 0.82 | 19% |
| 5m · E3 · broken · w6 · SR · filter=none · minstop=0 | -8.5 (15727) | -6.8 (18273) | -3.8 (9206) | -9.8 (9067) | 0.73 | 0.84 | 22% |
| 5m · E3 · broken · w6 · TR · filter=none · minstop=0 | -5.0 (20238) | -2.4 (23163) | -2.7 (11657) | -2.1 (11506) | 0.60 | 0.84 | 9% |
| 5m · E3 · half · w3 · T2 · filter=none · minstop=0 | -6.6 (19225) | -4.1 (20832) | -3.9 (10338) | -4.3 (10494) | 0.57 | 0.80 | 33% |
| 5m · E3 · half · w3 · T3 · filter=none · minstop=0 | -6.9 (18791) | -5.2 (20356) | -5.1 (10098) | -5.4 (10258) | 0.61 | 0.78 | 28% |
| 5m · E3 · half · w3 · T3ref · filter=none · minstop=0 | -7.3 (16935) | -5.2 (18356) | -4.9 (9098) | -5.5 (9258) | 0.66 | 0.82 | 15% |
| 5m · E3 · half · w3 · SR · filter=none · minstop=0 | -7.8 (16515) | -4.8 (17996) | -4.0 (8926) | -5.5 (9070) | 0.63 | 0.83 | 18% |
| 5m · E3 · half · w3 · TR · filter=none · minstop=0 | -5.8 (18143) | -3.9 (19746) | -5.0 (9772) | -2.9 (9974) | 0.55 | 0.74 | 9% |
| 5m · E3 · half · w6 · T2 · filter=none · minstop=0 | -6.5 (20519) | -3.7 (22193) | -3.5 (11085) | -3.8 (11108) | 0.58 | 0.82 | 34% |
| 5m · E3 · half · w6 · T3 · filter=none · minstop=0 | -6.8 (19947) | -4.9 (21567) | -4.5 (10760) | -5.2 (10807) | 0.62 | 0.80 | 29% |
| 5m · E3 · half · w6 · T3ref · filter=none · minstop=0 | -7.4 (17625) | -5.2 (19068) | -3.9 (9549) | -6.4 (9519) | 0.66 | 0.82 | 15% |
| 5m · E3 · half · w6 · SR · filter=none · minstop=0 | -7.6 (17375) | -4.4 (18936) | -3.3 (9468) | -5.5 (9468) | 0.64 | 0.84 | 18% |
| 5m · E3 · half · w6 · TR · filter=none · minstop=0 | -5.7 (19346) | -4.0 (21025) | -4.8 (10468) | -3.3 (10557) | 0.55 | 0.73 | 9% |
| 5m · E3 · ema · w3 · T2 · filter=none · minstop=0 | -7.2 (16746) | -7.3 (20758) | -7.5 (10557) | -7.0 (10201) | 0.77 | 0.82 | 35% |
| 5m · E3 · ema · w3 · T3 · filter=none · minstop=0 | -6.2 (15288) | -7.0 (19073) | -6.8 (9703) | -7.2 (9370) | 0.82 | 0.85 | 29% |
| 5m · E3 · ema · w3 · T3ref · filter=none · minstop=0 | -7.6 (14239) | -9.1 (17662) | -8.3 (8955) | -10.0 (8707) | 0.80 | 0.82 | 22% |
| 5m · E3 · ema · w3 · SR · filter=none · minstop=0 | -8.7 (14051) | -8.2 (17637) | -7.3 (8999) | -9.2 (8638) | 0.77 | 0.82 | 24% |
| 5m · E3 · ema · w3 · TR · filter=none · minstop=0 | -4.5 (18955) | -4.3 (23378) | -4.1 (11942) | -4.4 (11436) | 0.63 | 0.70 | 8% |
| 5m · E3 · ema · w6 · T2 · filter=none · minstop=0 | -7.1 (17370) | -6.9 (21552) | -7.0 (11021) | -6.9 (10531) | 0.78 | 0.83 | 36% |
| 5m · E3 · ema · w6 · T3 · filter=none · minstop=0 | -6.4 (15843) | -7.3 (19710) | -6.8 (10060) | -7.9 (9650) | 0.82 | 0.84 | 29% |
| 5m · E3 · ema · w6 · T3ref · filter=none · minstop=0 | -7.7 (14675) | -10.0 (18127) | -8.3 (9230) | -11.7 (8897) | 0.80 | 0.80 | 22% |
| 5m · E3 · ema · w6 · SR · filter=none · minstop=0 | -8.6 (14554) | -8.3 (18231) | -6.8 (9329) | -9.9 (8902) | 0.77 | 0.83 | 24% |
| 5m · E3 · ema · w6 · TR · filter=none · minstop=0 | -4.5 (19862) | -4.3 (24551) | -4.4 (12599) | -4.2 (11952) | 0.64 | 0.70 | 8% |
| 5m · E2 · algotest · filter=none · minstop=0 | -12.5 (21453) | -14.2 (23192) | — (0) | -14.2 (23192) | 0.62 | 0.70 | 27% |
| 5m · E1 · T2 · filter=none · minstop=1 | -17.8 (13233) | -23.7 (20071) | -19.6 (10043) | -27.7 (10028) | 0.76 | 0.75 | 33% |
| 5m · E1 · T3 · filter=none · minstop=1 | -16.8 (11738) | -25.6 (17874) | -20.8 (8938) | -30.4 (8936) | 0.79 | 0.75 | 28% |
| 5m · E1 · T3ref · filter=none · minstop=1 | -16.9 (11506) | -25.8 (17415) | -22.4 (8764) | -29.1 (8651) | 0.78 | 0.75 | 28% |
| 5m · E1 · SR · filter=none · minstop=1 | -19.1 (10953) | -28.1 (16810) | -23.1 (8561) | -33.4 (8249) | 0.76 | 0.72 | 27% |
| 5m · E1 · TR · filter=none · minstop=1 | -4.8 (20817) | -5.2 (33531) | -4.6 (16805) | -5.7 (16726) | 0.77 | 0.79 | 8% |
| 5m · E3 · broken · w3 · T2 · filter=none · minstop=1 | -6.9 (5773) | -7.7 (7812) | -3.8 (3927) | -11.7 (3885) | 0.86 | 0.89 | 36% |
| 5m · E3 · broken · w3 · T3 · filter=none · minstop=1 | -6.1 (5488) | -8.2 (7420) | -3.9 (3724) | -12.5 (3696) | 0.89 | 0.89 | 28% |
| 5m · E3 · broken · w3 · T3ref · filter=none · minstop=1 | -5.4 (5158) | -7.1 (6969) | -1.8 (3504) | -12.4 (3465) | 0.90 | 0.91 | 22% |
| 5m · E3 · broken · w3 · SR · filter=none · minstop=1 | -7.5 (4970) | -6.0 (6746) | 4.1 (3379) | -16.2 (3367) | 0.86 | 0.92 | 24% |
| 5m · E3 · broken · w3 · TR · filter=none · minstop=1 | -0.3 (5936) | 5.2 (8150) | 5.7 (4113) | 4.7 (4037) | 0.98 | 1.25 | 10% |
| 5m · E3 · broken · w6 · T2 · filter=none · minstop=1 | -6.7 (6092) | -6.8 (8271) | -3.1 (4142) | -10.5 (4129) | 0.86 | 0.90 | 36% |
| 5m · E3 · broken · w6 · T3 · filter=none · minstop=1 | -6.6 (5790) | -6.1 (7817) | -1.7 (3907) | -10.4 (3910) | 0.88 | 0.92 | 29% |
| 5m · E3 · broken · w6 · T3ref · filter=none · minstop=1 | -7.2 (5426) | -5.6 (7266) | 0.6 (3629) | -11.8 (3637) | 0.87 | 0.93 | 22% |
| 5m · E3 · broken · w6 · SR · filter=none · minstop=1 | -7.3 (5266) | -4.8 (7073) | 3.9 (3530) | -13.5 (3543) | 0.87 | 0.94 | 24% |
| 5m · E3 · broken · w6 · TR · filter=none · minstop=1 | 0.1 (6315) | 4.9 (8643) | 4.3 (4343) | 5.5 (4300) | 1.00 | 1.24 | 10% |
| 5m · E3 · half · w3 · T2 · filter=none · minstop=1 | -2.4 (2031) | 7.3 (4283) | 10.2 (2009) | 4.7 (2274) | 0.94 | 1.14 | 42% |
| 5m · E3 · half · w3 · T3 · filter=none · minstop=1 | -3.3 (2001) | 1.6 (4208) | 4.7 (1969) | -1.1 (2239) | 0.93 | 1.03 | 31% |
| 5m · E3 · half · w3 · T3ref · filter=none · minstop=1 | -10.3 (1916) | 1.9 (3949) | 6.9 (1856) | -2.6 (2093) | 0.83 | 1.03 | 17% |
| 5m · E3 · half · w3 · SR · filter=none · minstop=1 | -11.2 (1834) | 2.5 (3742) | 7.1 (1720) | -1.4 (2022) | 0.80 | 1.04 | 19% |
| 5m · E3 · half · w3 · TR · filter=none · minstop=1 | -1.8 (1991) | 6.0 (4155) | 2.5 (1959) | 9.1 (2196) | 0.93 | 1.21 | 12% |
| 5m · E3 · half · w6 · T2 · filter=none · minstop=1 | -1.4 (2261) | 8.1 (4655) | 10.9 (2199) | 5.5 (2456) | 0.97 | 1.16 | 42% |
| 5m · E3 · half · w6 · T3 · filter=none · minstop=1 | -2.7 (2225) | 2.2 (4558) | 6.2 (2154) | -1.3 (2404) | 0.94 | 1.04 | 31% |
| 5m · E3 · half · w6 · T3ref · filter=none · minstop=1 | -9.1 (2097) | 1.3 (4221) | 9.3 (2011) | -5.9 (2210) | 0.84 | 1.02 | 17% |
| 5m · E3 · half · w6 · SR · filter=none · minstop=1 | -10.3 (2033) | 3.7 (4046) | 10.4 (1884) | -2.2 (2162) | 0.82 | 1.05 | 20% |
| 5m · E3 · half · w6 · TR · filter=none · minstop=1 | 0.3 (2212) | 5.2 (4518) | 3.1 (2151) | 7.2 (2367) | 1.01 | 1.19 | 12% |
| 5m · E3 · ema · w3 · T2 · filter=none · minstop=1 | -6.1 (7220) | -6.5 (9585) | -6.5 (4923) | -6.6 (4662) | 0.88 | 0.91 | 36% |
| 5m · E3 · ema · w3 · T3 · filter=none · minstop=1 | -4.0 (6751) | -6.0 (8975) | -4.8 (4618) | -7.3 (4357) | 0.93 | 0.92 | 30% |
| 5m · E3 · ema · w3 · T3ref · filter=none · minstop=1 | -6.6 (6369) | -9.0 (8416) | -6.3 (4343) | -11.8 (4073) | 0.89 | 0.89 | 24% |
| 5m · E3 · ema · w3 · SR · filter=none · minstop=1 | -9.6 (6207) | -7.8 (8314) | -5.5 (4294) | -10.3 (4020) | 0.83 | 0.90 | 26% |
| 5m · E3 · ema · w3 · TR · filter=none · minstop=1 | -0.9 (7930) | -0.5 (10538) | -0.7 (5432) | -0.3 (5106) | 0.93 | 0.97 | 10% |
| 5m · E3 · ema · w6 · T2 · filter=none · minstop=1 | -5.8 (7614) | -5.4 (10126) | -5.0 (5232) | -5.9 (4894) | 0.89 | 0.92 | 37% |
| 5m · E3 · ema · w6 · T3 · filter=none · minstop=1 | -4.3 (7107) | -6.5 (9435) | -4.4 (4876) | -8.7 (4559) | 0.92 | 0.91 | 30% |
| 5m · E3 · ema · w6 · T3ref · filter=none · minstop=1 | -7.5 (6681) | -10.2 (8798) | -6.2 (4559) | -14.6 (4239) | 0.87 | 0.87 | 24% |
| 5m · E3 · ema · w6 · SR · filter=none · minstop=1 | -9.6 (6566) | -8.0 (8758) | -4.4 (4538) | -11.8 (4220) | 0.83 | 0.90 | 27% |
| 5m · E3 · ema · w6 · TR · filter=none · minstop=1 | -0.8 (8412) | -0.4 (11259) | -1.0 (5821) | 0.1 (5438) | 0.94 | 0.97 | 10% |
| 5m · E2 · algotest · filter=none · minstop=1 | -12.7 (4093) | -17.6 (7472) | — (0) | -17.6 (7472) | 0.84 | 0.83 | 29% |
| 5m · E1 · T2 · filter=best · minstop=0 | -13.6 (15220) | -17.2 (17619) | -17.4 (8712) | -17.1 (8907) | 0.64 | 0.66 | 31% |
| 5m · E1 · T3 · filter=best · minstop=0 | -13.2 (14100) | -17.5 (16238) | -17.3 (8039) | -17.6 (8199) | 0.68 | 0.68 | 26% |
| 5m · E1 · T3ref · filter=best · minstop=0 | -12.7 (13242) | -17.9 (15139) | -18.0 (7473) | -17.8 (7666) | 0.69 | 0.68 | 26% |
| 5m · E1 · SR · filter=best · minstop=0 | -14.4 (12845) | -16.9 (15104) | -14.9 (7543) | -19.0 (7561) | 0.65 | 0.68 | 23% |
| 5m · E1 · TR · filter=best · minstop=0 | -8.8 (19215) | -7.9 (22700) | -7.4 (11206) | -8.5 (11494) | 0.49 | 0.59 | 8% |
| 5m · E3 · broken · w3 · T2 · filter=best · minstop=0 | -8.4 (6283) | -8.0 (6939) | -5.3 (3445) | -10.6 (3494) | 0.66 | 0.76 | 33% |
| 5m · E3 · broken · w3 · T3 · filter=best · minstop=0 | -7.2 (6093) | -8.8 (6741) | -7.2 (3351) | -10.4 (3390) | 0.74 | 0.76 | 26% |
| 5m · E3 · broken · w3 · T3ref · filter=best · minstop=0 | -6.7 (5890) | -8.0 (6481) | -7.5 (3221) | -8.5 (3260) | 0.78 | 0.80 | 19% |
| 5m · E3 · broken · w3 · SR · filter=best · minstop=0 | -8.4 (5630) | -6.9 (6275) | -2.7 (3149) | -11.1 (3126) | 0.70 | 0.82 | 22% |
| 5m · E3 · broken · w3 · TR · filter=best · minstop=0 | -5.4 (6564) | -3.0 (7226) | -3.6 (3572) | -2.4 (3654) | 0.57 | 0.79 | 9% |
| 5m · E3 · broken · w6 · T2 · filter=best · minstop=0 | -8.2 (6960) | -7.4 (7585) | -4.8 (3767) | -10.0 (3818) | 0.67 | 0.78 | 33% |
| 5m · E3 · broken · w6 · T3 · filter=best · minstop=0 | -7.5 (6726) | -7.8 (7350) | -6.2 (3650) | -9.2 (3700) | 0.73 | 0.79 | 27% |
| 5m · E3 · broken · w6 · T3ref · filter=best · minstop=0 | -7.1 (6478) | -6.7 (7020) | -6.7 (3474) | -6.7 (3546) | 0.76 | 0.83 | 19% |
| 5m · E3 · broken · w6 · SR · filter=best · minstop=0 | -8.7 (6217) | -6.6 (6841) | -3.9 (3426) | -9.3 (3415) | 0.69 | 0.83 | 22% |
| 5m · E3 · broken · w6 · TR · filter=best · minstop=0 | -5.4 (7307) | -3.4 (7960) | -4.2 (3936) | -2.7 (4024) | 0.57 | 0.76 | 9% |
| 5m · E3 · half · w3 · T2 · filter=best · minstop=0 | -6.6 (6397) | -4.2 (6515) | -4.4 (3211) | -3.9 (3304) | 0.54 | 0.79 | 32% |
| 5m · E3 · half · w3 · T3 · filter=best · minstop=0 | -6.7 (6293) | -5.1 (6407) | -4.4 (3154) | -5.8 (3253) | 0.60 | 0.78 | 28% |
| 5m · E3 · half · w3 · T3ref · filter=best · minstop=0 | -5.3 (6058) | -5.2 (6144) | -5.3 (3019) | -5.2 (3125) | 0.73 | 0.81 | 15% |
| 5m · E3 · half · w3 · SR · filter=best · minstop=0 | -7.1 (5869) | -5.4 (5957) | -4.8 (2946) | -6.1 (3011) | 0.63 | 0.79 | 19% |
| 5m · E3 · half · w3 · TR · filter=best · minstop=0 | -6.8 (6345) | -3.6 (6467) | -4.5 (3192) | -2.6 (3275) | 0.46 | 0.76 | 9% |
| 5m · E3 · half · w6 · T2 · filter=best · minstop=0 | -6.8 (7418) | -4.2 (7505) | -4.5 (3724) | -4.0 (3781) | 0.54 | 0.78 | 32% |
| 5m · E3 · half · w6 · T3 · filter=best · minstop=0 | -7.0 (7276) | -5.2 (7359) | -4.5 (3650) | -5.9 (3709) | 0.59 | 0.77 | 29% |
| 5m · E3 · half · w6 · T3ref · filter=best · minstop=0 | -5.9 (6940) | -4.9 (6989) | -4.5 (3465) | -5.3 (3524) | 0.71 | 0.82 | 15% |
| 5m · E3 · half · w6 · SR · filter=best · minstop=0 | -7.2 (6746) | -5.2 (6819) | -4.5 (3395) | -6.0 (3424) | 0.63 | 0.80 | 20% |
| 5m · E3 · half · w6 · TR · filter=best · minstop=0 | -6.3 (7346) | -4.1 (7452) | -4.5 (3704) | -3.7 (3748) | 0.50 | 0.72 | 9% |
| 5m · E3 · ema · w3 · T2 · filter=best · minstop=0 | -7.0 (5867) | -7.1 (6902) | -8.0 (3448) | -6.1 (3454) | 0.77 | 0.81 | 35% |
| 5m · E3 · ema · w3 · T3 · filter=best · minstop=0 | -5.8 (5679) | -6.8 (6690) | -8.9 (3328) | -4.6 (3362) | 0.82 | 0.84 | 29% |
| 5m · E3 · ema · w3 · T3ref · filter=best · minstop=0 | -6.6 (5506) | -8.3 (6482) | -9.4 (3216) | -7.2 (3266) | 0.81 | 0.82 | 22% |
| 5m · E3 · ema · w3 · SR · filter=best · minstop=0 | -8.7 (5217) | -7.2 (6222) | -7.8 (3127) | -6.5 (3095) | 0.74 | 0.83 | 24% |
| 5m · E3 · ema · w3 · TR · filter=best · minstop=0 | -5.0 (6288) | -4.2 (7358) | -4.1 (3672) | -4.3 (3686) | 0.60 | 0.70 | 8% |
| 5m · E3 · ema · w6 · T2 · filter=best · minstop=0 | -6.9 (6395) | -7.0 (7511) | -8.0 (3747) | -6.0 (3764) | 0.77 | 0.82 | 35% |
| 5m · E3 · ema · w6 · T3 · filter=best · minstop=0 | -5.7 (6173) | -6.8 (7241) | -9.1 (3592) | -4.6 (3649) | 0.83 | 0.84 | 29% |
| 5m · E3 · ema · w6 · T3ref · filter=best · minstop=0 | -6.8 (5958) | -9.0 (6968) | -9.9 (3446) | -8.2 (3522) | 0.81 | 0.80 | 22% |
| 5m · E3 · ema · w6 · SR · filter=best · minstop=0 | -8.6 (5659) | -7.4 (6731) | -8.6 (3371) | -6.2 (3360) | 0.75 | 0.83 | 25% |
| 5m · E3 · ema · w6 · TR · filter=best · minstop=0 | -5.0 (6903) | -4.2 (8084) | -4.5 (4029) | -3.8 (4055) | 0.60 | 0.71 | 8% |
| 5m · E2 · algotest · filter=best · minstop=0 | -10.7 (6943) | -12.9 (7056) | — (0) | -12.9 (7056) | 0.65 | 0.69 | 28% |
| 5m · E1 · T2 · filter=best · minstop=1 | -12.6 (4693) | -23.6 (6823) | -24.0 (3375) | -23.3 (3448) | 0.83 | 0.75 | 33% |
| 5m · E1 · T3 · filter=best · minstop=1 | -11.5 (4427) | -24.2 (6381) | -23.6 (3168) | -24.9 (3213) | 0.85 | 0.76 | 28% |
| 5m · E1 · T3ref · filter=best · minstop=1 | -10.6 (4272) | -24.7 (6078) | -25.4 (3019) | -24.0 (3059) | 0.86 | 0.76 | 28% |
| 5m · E1 · SR · filter=best · minstop=1 | -14.6 (3922) | -24.0 (5824) | -19.6 (2900) | -28.4 (2924) | 0.81 | 0.76 | 27% |
| 5m · E1 · TR · filter=best · minstop=1 | -2.8 (5838) | -2.1 (8669) | -1.4 (4302) | -2.9 (4367) | 0.86 | 0.92 | 10% |
| 5m · E3 · broken · w3 · T2 · filter=best · minstop=1 | -6.4 (1781) | -6.2 (2248) | 2.2 (1133) | -14.8 (1115) | 0.86 | 0.91 | 36% |
| 5m · E3 · broken · w3 · T3 · filter=best · minstop=1 | -1.8 (1751) | -8.8 (2209) | -3.4 (1116) | -14.2 (1093) | 0.96 | 0.89 | 28% |
| 5m · E3 · broken · w3 · T3ref · filter=best · minstop=1 | -0.3 (1712) | -7.2 (2143) | -5.8 (1085) | -8.6 (1058) | 0.99 | 0.91 | 22% |
| 5m · E3 · broken · w3 · SR · filter=best · minstop=1 | -6.0 (1563) | -5.3 (2017) | 5.5 (1025) | -16.5 (992) | 0.89 | 0.93 | 25% |
| 5m · E3 · broken · w3 · TR · filter=best · minstop=1 | 0.6 (1826) | 4.8 (2320) | 3.1 (1159) | 6.6 (1161) | 1.04 | 1.23 | 11% |
| 5m · E3 · broken · w6 · T2 · filter=best · minstop=1 | -5.9 (1955) | -5.1 (2479) | 3.0 (1250) | -13.2 (1229) | 0.88 | 0.93 | 36% |
| 5m · E3 · broken · w6 · T3 · filter=best · minstop=1 | -3.0 (1921) | -6.2 (2433) | -1.7 (1229) | -10.8 (1204) | 0.94 | 0.92 | 28% |
| 5m · E3 · broken · w6 · T3ref · filter=best · minstop=1 | -2.2 (1878) | -3.3 (2349) | -3.6 (1188) | -3.0 (1161) | 0.96 | 0.96 | 22% |
| 5m · E3 · broken · w6 · SR · filter=best · minstop=1 | -6.7 (1719) | -4.4 (2217) | 1.9 (1119) | -10.8 (1098) | 0.88 | 0.94 | 25% |
| 5m · E3 · broken · w6 · TR · filter=best · minstop=1 | 0.8 (2009) | 3.9 (2584) | 1.3 (1292) | 6.5 (1292) | 1.05 | 1.18 | 11% |
| 5m · E3 · half · w3 · T2 · filter=best · minstop=1 | 2.0 (569) | 10.7 (1190) | 9.6 (610) | 11.8 (580) | 1.05 | 1.20 | 43% |
| 5m · E3 · half · w3 · T3 · filter=best · minstop=1 | 3.6 (560) | 5.2 (1178) | 8.5 (604) | 1.7 (574) | 1.08 | 1.08 | 32% |
| 5m · E3 · half · w3 · T3ref · filter=best · minstop=1 | 10.7 (551) | 6.0 (1145) | 7.1 (591) | 4.9 (554) | 1.20 | 1.08 | 18% |
| 5m · E3 · half · w3 · SR · filter=best · minstop=1 | 5.0 (509) | 2.7 (1068) | 5.4 (538) | 0.1 (530) | 1.10 | 1.04 | 22% |
| 5m · E3 · half · w3 · TR · filter=best · minstop=1 | -2.8 (566) | 8.6 (1196) | 2.0 (619) | 15.6 (577) | 0.88 | 1.31 | 12% |
| 5m · E3 · half · w6 · T2 · filter=best · minstop=1 | 1.9 (694) | 9.1 (1380) | 7.3 (718) | 11.0 (662) | 1.05 | 1.17 | 43% |
| 5m · E3 · half · w6 · T3 · filter=best · minstop=1 | 2.2 (683) | 3.4 (1359) | 5.8 (711) | 0.8 (648) | 1.05 | 1.05 | 32% |
| 5m · E3 · half · w6 · T3ref · filter=best · minstop=1 | 7.8 (666) | 5.7 (1313) | 8.2 (690) | 3.0 (623) | 1.14 | 1.08 | 18% |
| 5m · E3 · half · w6 · SR · filter=best · minstop=1 | 3.0 (621) | 2.1 (1231) | 4.5 (631) | -0.3 (600) | 1.06 | 1.03 | 22% |
| 5m · E3 · half · w6 · TR · filter=best · minstop=1 | 2.7 (691) | 5.3 (1384) | 1.6 (722) | 9.3 (662) | 1.12 | 1.19 | 12% |
| 5m · E3 · ema · w3 · T2 · filter=best · minstop=1 | -3.7 (2332) | -5.8 (2811) | -7.3 (1430) | -4.2 (1381) | 0.92 | 0.92 | 36% |
| 5m · E3 · ema · w3 · T3 · filter=best · minstop=1 | -1.6 (2273) | -6.3 (2739) | -10.6 (1396) | -1.9 (1343) | 0.97 | 0.92 | 29% |
| 5m · E3 · ema · w3 · T3ref · filter=best · minstop=1 | -2.7 (2232) | -9.3 (2670) | -11.4 (1364) | -7.0 (1306) | 0.95 | 0.88 | 23% |
| 5m · E3 · ema · w3 · SR · filter=best · minstop=1 | -7.2 (2035) | -7.0 (2498) | -8.4 (1285) | -5.6 (1213) | 0.87 | 0.91 | 27% |
| 5m · E3 · ema · w3 · TR · filter=best · minstop=1 | -1.0 (2454) | 0.1 (2969) | -0.8 (1506) | 1.0 (1463) | 0.93 | 1.00 | 9% |
| 5m · E3 · ema · w6 · T2 · filter=best · minstop=1 | -3.2 (2581) | -5.5 (3100) | -7.1 (1575) | -3.9 (1525) | 0.93 | 0.92 | 36% |
| 5m · E3 · ema · w6 · T3 · filter=best · minstop=1 | -1.1 (2508) | -6.5 (3012) | -10.8 (1530) | -2.0 (1482) | 0.98 | 0.91 | 29% |
| 5m · E3 · ema · w6 · T3ref · filter=best · minstop=1 | -3.3 (2448) | -10.4 (2926) | -11.8 (1488) | -9.0 (1438) | 0.94 | 0.87 | 23% |
| 5m · E3 · ema · w6 · SR · filter=best · minstop=1 | -6.8 (2251) | -7.8 (2743) | -9.9 (1400) | -5.6 (1343) | 0.87 | 0.90 | 27% |
| 5m · E3 · ema · w6 · TR · filter=best · minstop=1 | -0.8 (2720) | 0.2 (3303) | -1.4 (1681) | 1.9 (1622) | 0.94 | 1.01 | 9% |
| 5m · E2 · algotest · filter=best · minstop=1 | -0.3 (1222) | -14.2 (1890) | — (0) | -14.2 (1890) | 0.99 | 0.86 | 29% |
| 5m · E1 · asis_live_no_checks · filter=none · minstop=0 | -14.8 (30950) | -19.2 (38618) | -17.6 (19558) | -20.8 (19060) | 0.67 | 0.68 | 27% |
| 15m · E1 · T2 · filter=none · minstop=0 | -14.9 (12059) | -20.9 (15570) | -17.2 (7936) | -24.8 (7634) | 0.77 | 0.76 | 32% |
| 15m · E1 · T3 · filter=none · minstop=0 | -13.0 (10305) | -22.0 (13370) | -15.7 (6819) | -28.5 (6551) | 0.81 | 0.77 | 27% |
| 15m · E1 · T3ref · filter=none · minstop=0 | -13.3 (10196) | -22.4 (13063) | -16.4 (6592) | -28.5 (6471) | 0.81 | 0.77 | 27% |
| 15m · E1 · SR · filter=none · minstop=0 | -16.8 (9974) | -21.6 (13064) | -13.7 (6790) | -30.2 (6274) | 0.76 | 0.76 | 24% |
| 15m · E1 · TR · filter=none · minstop=0 | -7.5 (18980) | -5.3 (24958) | -4.5 (12539) | -6.2 (12419) | 0.66 | 0.80 | 9% |
| 15m · E3 · broken · w3 · T2 · filter=none · minstop=0 | -6.6 (6327) | -6.0 (7617) | -4.1 (3837) | -7.8 (3780) | 0.85 | 0.90 | 36% |
| 15m · E3 · broken · w3 · T3 · filter=none · minstop=0 | -6.7 (5831) | -7.3 (7115) | -3.4 (3577) | -11.2 (3538) | 0.86 | 0.89 | 28% |
| 15m · E3 · broken · w3 · T3ref · filter=none · minstop=0 | -5.5 (5385) | -9.1 (6550) | -3.9 (3268) | -14.2 (3282) | 0.89 | 0.87 | 20% |
| 15m · E3 · broken · w3 · SR · filter=none · minstop=0 | -7.3 (5581) | -8.1 (6811) | -5.1 (3416) | -11.0 (3395) | 0.85 | 0.88 | 24% |
| 15m · E3 · broken · w3 · TR · filter=none · minstop=0 | -3.7 (6835) | 0.2 (8116) | 2.3 (4073) | -1.9 (4043) | 0.77 | 1.01 | 9% |
| 15m · E3 · broken · w6 · T2 · filter=none · minstop=0 | -5.8 (6581) | -5.8 (7968) | -2.2 (4036) | -9.5 (3932) | 0.86 | 0.90 | 36% |
| 15m · E3 · broken · w6 · T3 · filter=none · minstop=0 | -6.1 (6029) | -6.7 (7419) | -2.0 (3758) | -11.4 (3661) | 0.87 | 0.90 | 28% |
| 15m · E3 · broken · w6 · T3ref · filter=none · minstop=0 | -6.4 (5510) | -6.0 (6736) | 1.4 (3394) | -13.5 (3342) | 0.88 | 0.92 | 20% |
| 15m · E3 · broken · w6 · SR · filter=none · minstop=0 | -7.4 (5769) | -6.5 (7086) | -1.8 (3608) | -11.4 (3478) | 0.85 | 0.90 | 25% |
| 15m · E3 · broken · w6 · TR · filter=none · minstop=0 | -3.5 (7164) | 0.0 (8548) | 2.1 (4321) | -2.1 (4227) | 0.78 | 1.00 | 9% |
| 15m · E3 · half · w3 · T2 · filter=none · minstop=0 | -5.1 (7322) | -5.9 (8468) | -6.3 (4259) | -5.5 (4209) | 0.79 | 0.82 | 35% |
| 15m · E3 · half · w3 · T3 · filter=none · minstop=0 | -5.8 (7130) | -7.4 (8227) | -7.8 (4143) | -7.0 (4084) | 0.79 | 0.81 | 28% |
| 15m · E3 · half · w3 · T3ref · filter=none · minstop=0 | -8.1 (6351) | -9.6 (7439) | -8.4 (3723) | -10.9 (3716) | 0.76 | 0.79 | 14% |
| 15m · E3 · half · w3 · SR · filter=none · minstop=0 | -7.7 (6638) | -9.3 (7669) | -8.8 (3841) | -9.8 (3828) | 0.75 | 0.78 | 19% |
| 15m · E3 · half · w3 · TR · filter=none · minstop=0 | -6.0 (7017) | -4.9 (8110) | -7.9 (4067) | -1.8 (4043) | 0.66 | 0.78 | 9% |
| 15m · E3 · half · w6 · T2 · filter=none · minstop=0 | -5.4 (7851) | -5.2 (9105) | -5.8 (4597) | -4.6 (4508) | 0.78 | 0.84 | 36% |
| 15m · E3 · half · w6 · T3 · filter=none · minstop=0 | -5.8 (7580) | -7.1 (8837) | -7.2 (4475) | -7.0 (4362) | 0.80 | 0.82 | 28% |
| 15m · E3 · half · w6 · T3ref · filter=none · minstop=0 | -8.0 (6658) | -9.3 (7827) | -6.9 (3946) | -11.7 (3881) | 0.77 | 0.80 | 14% |
| 15m · E3 · half · w6 · SR · filter=none · minstop=0 | -7.5 (7032) | -9.1 (8219) | -8.2 (4150) | -9.9 (4069) | 0.77 | 0.79 | 19% |
| 15m · E3 · half · w6 · TR · filter=none · minstop=0 | -5.6 (7495) | -4.9 (8737) | -7.9 (4416) | -1.8 (4321) | 0.69 | 0.78 | 9% |
| 15m · E3 · ema · w3 · T2 · filter=none · minstop=0 | -3.4 (5873) | -5.5 (7331) | -1.6 (3692) | -9.4 (3639) | 0.93 | 0.92 | 36% |
| 15m · E3 · ema · w3 · T3 · filter=none · minstop=0 | -2.3 (5390) | -6.6 (6812) | -0.9 (3403) | -12.3 (3409) | 0.96 | 0.91 | 29% |
| 15m · E3 · ema · w3 · T3ref · filter=none · minstop=0 | -2.1 (5035) | -5.3 (6335) | 1.5 (3144) | -12.1 (3191) | 0.96 | 0.93 | 22% |
| 15m · E3 · ema · w3 · SR · filter=none · minstop=0 | -3.0 (5198) | -6.6 (6579) | -2.6 (3300) | -10.6 (3279) | 0.95 | 0.91 | 27% |
| 15m · E3 · ema · w3 · TR · filter=none · minstop=0 | -2.1 (6663) | 1.2 (8261) | 3.1 (4139) | -0.8 (4122) | 0.87 | 1.06 | 9% |
| 15m · E3 · ema · w6 · T2 · filter=none · minstop=0 | -2.8 (6081) | -4.9 (7641) | -0.1 (3881) | -9.8 (3760) | 0.94 | 0.93 | 37% |
| 15m · E3 · ema · w6 · T3 · filter=none · minstop=0 | -1.3 (5542) | -6.1 (7054) | 1.0 (3548) | -13.2 (3506) | 0.98 | 0.92 | 29% |
| 15m · E3 · ema · w6 · T3ref · filter=none · minstop=0 | -3.0 (5129) | -5.3 (6525) | 4.3 (3271) | -15.0 (3254) | 0.95 | 0.93 | 22% |
| 15m · E3 · ema · w6 · SR · filter=none · minstop=0 | -2.9 (5360) | -6.0 (6829) | 0.3 (3449) | -12.3 (3380) | 0.95 | 0.92 | 27% |
| 15m · E3 · ema · w6 · TR · filter=none · minstop=0 | -2.7 (6975) | 0.8 (8701) | 3.1 (4409) | -1.6 (4292) | 0.83 | 1.04 | 9% |
| 15m · E2 · algotest · filter=none · minstop=0 | -11.8 (7148) | -19.8 (8486) | — (0) | -19.8 (8486) | 0.77 | 0.74 | 29% |
| 15m · E1 · T2 · filter=none · minstop=1 | -15.9 (7407) | -25.3 (9964) | -19.5 (5156) | -31.5 (4808) | 0.82 | 0.79 | 32% |
| 15m · E1 · T3 · filter=none · minstop=1 | -12.9 (6504) | -26.3 (8777) | -16.5 (4543) | -36.8 (4234) | 0.86 | 0.80 | 27% |
| 15m · E1 · T3ref · filter=none · minstop=1 | -13.2 (6474) | -26.6 (8671) | -17.6 (4463) | -36.2 (4208) | 0.86 | 0.79 | 27% |
| 15m · E1 · SR · filter=none · minstop=1 | -18.6 (6277) | -25.0 (8620) | -12.6 (4555) | -38.9 (4065) | 0.80 | 0.80 | 26% |
| 15m · E1 · TR · filter=none · minstop=1 | -4.2 (11252) | -1.1 (15619) | 0.5 (8068) | -2.8 (7551) | 0.83 | 0.96 | 9% |
| 15m · E3 · broken · w3 · T2 · filter=none · minstop=1 | -5.8 (4020) | -4.9 (4716) | -2.4 (2492) | -7.7 (2224) | 0.90 | 0.94 | 36% |
| 15m · E3 · broken · w3 · T3 · filter=none · minstop=1 | -5.9 (3795) | -6.9 (4481) | -1.1 (2362) | -13.4 (2119) | 0.91 | 0.92 | 28% |
| 15m · E3 · broken · w3 · T3ref · filter=none · minstop=1 | -3.0 (3550) | -9.9 (4210) | -1.7 (2213) | -18.9 (1997) | 0.95 | 0.90 | 21% |
| 15m · E3 · broken · w3 · SR · filter=none · minstop=1 | -6.0 (3621) | -7.9 (4291) | -3.4 (2263) | -12.8 (2028) | 0.90 | 0.91 | 25% |
| 15m · E3 · broken · w3 · TR · filter=none · minstop=1 | -0.2 (4244) | 5.1 (4979) | 7.4 (2635) | 2.4 (2344) | 0.99 | 1.20 | 10% |
| 15m · E3 · broken · w6 · T2 · filter=none · minstop=1 | -5.0 (4240) | -5.2 (4979) | 0.0 (2628) | -11.0 (2351) | 0.91 | 0.94 | 36% |
| 15m · E3 · broken · w6 · T3 · filter=none · minstop=1 | -5.0 (3979) | -6.6 (4725) | 0.4 (2488) | -14.4 (2237) | 0.92 | 0.93 | 28% |
| 15m · E3 · broken · w6 · T3ref · filter=none · minstop=1 | -4.3 (3693) | -5.3 (4384) | 5.9 (2309) | -17.8 (2075) | 0.94 | 0.94 | 21% |
| 15m · E3 · broken · w6 · SR · filter=none · minstop=1 | -6.4 (3809) | -5.7 (4525) | 1.2 (2401) | -13.4 (2124) | 0.90 | 0.94 | 26% |
| 15m · E3 · broken · w6 · TR · filter=none · minstop=1 | -0.4 (4496) | 4.8 (5301) | 7.2 (2799) | 2.0 (2502) | 0.98 | 1.19 | 10% |
| 15m · E3 · half · w3 · T2 · filter=none · minstop=1 | 1.8 (2111) | -3.0 (3005) | -3.4 (1593) | -2.5 (1412) | 1.04 | 0.95 | 37% |
| 15m · E3 · half · w3 · T3 · filter=none · minstop=1 | -0.3 (2086) | -6.8 (2935) | -7.2 (1555) | -6.2 (1380) | 0.99 | 0.91 | 28% |
| 15m · E3 · half · w3 · T3ref · filter=none · minstop=1 | -5.1 (1932) | -11.2 (2757) | -4.3 (1452) | -18.8 (1305) | 0.92 | 0.87 | 15% |
| 15m · E3 · half · w3 · SR · filter=none · minstop=1 | -2.6 (1953) | -12.3 (2771) | -8.4 (1449) | -16.6 (1322) | 0.95 | 0.85 | 19% |
| 15m · E3 · half · w3 · TR · filter=none · minstop=1 | -1.6 (2043) | 0.5 (2914) | -6.1 (1534) | 7.7 (1380) | 0.94 | 1.01 | 10% |
| 15m · E3 · half · w6 · T2 · filter=none · minstop=1 | 0.5 (2377) | -1.7 (3322) | -2.8 (1750) | -0.5 (1572) | 1.01 | 0.97 | 37% |
| 15m · E3 · half · w6 · T3 · filter=none · minstop=1 | -0.1 (2342) | -6.5 (3242) | -6.2 (1712) | -6.9 (1530) | 1.00 | 0.91 | 28% |
| 15m · E3 · half · w6 · T3ref · filter=none · minstop=1 | -4.0 (2152) | -10.1 (2999) | -1.5 (1580) | -19.6 (1419) | 0.94 | 0.88 | 15% |
| 15m · E3 · half · w6 · SR · filter=none · minstop=1 | -2.2 (2196) | -11.7 (3041) | -6.9 (1592) | -16.9 (1449) | 0.96 | 0.86 | 19% |
| 15m · E3 · half · w6 · TR · filter=none · minstop=1 | 0.0 (2292) | 0.4 (3215) | -6.9 (1693) | 8.5 (1522) | 1.00 | 1.01 | 10% |
| 15m · E3 · ema · w3 · T2 · filter=none · minstop=1 | -1.8 (4221) | -4.6 (5284) | 0.5 (2741) | -10.1 (2543) | 0.97 | 0.95 | 36% |
| 15m · E3 · ema · w3 · T3 · filter=none · minstop=1 | -0.7 (3917) | -6.4 (4958) | 1.3 (2550) | -14.4 (2408) | 0.99 | 0.93 | 29% |
| 15m · E3 · ema · w3 · T3ref · filter=none · minstop=1 | 0.6 (3685) | -3.8 (4663) | 4.7 (2388) | -12.8 (2275) | 1.01 | 0.96 | 23% |
| 15m · E3 · ema · w3 · SR · filter=none · minstop=1 | -0.7 (3774) | -5.6 (4792) | -0.0 (2474) | -11.6 (2318) | 0.99 | 0.94 | 28% |
| 15m · E3 · ema · w3 · TR · filter=none · minstop=1 | 0.1 (4687) | 5.4 (5827) | 7.7 (3032) | 2.9 (2795) | 1.00 | 1.24 | 9% |
| 15m · E3 · ema · w6 · T2 · filter=none · minstop=1 | -1.4 (4423) | -3.7 (5563) | 2.9 (2895) | -10.9 (2668) | 0.98 | 0.96 | 37% |
| 15m · E3 · ema · w6 · T3 · filter=none · minstop=1 | 0.5 (4080) | -5.6 (5187) | 4.2 (2666) | -15.9 (2521) | 1.01 | 0.94 | 29% |
| 15m · E3 · ema · w6 · T3ref · filter=none · minstop=1 | -0.8 (3807) | -3.8 (4855) | 8.5 (2493) | -16.8 (2362) | 0.99 | 0.96 | 23% |
| 15m · E3 · ema · w6 · SR · filter=none · minstop=1 | -1.1 (3958) | -5.1 (5037) | 3.5 (2607) | -14.3 (2430) | 0.98 | 0.94 | 28% |
| 15m · E3 · ema · w6 · TR · filter=none · minstop=1 | -0.8 (4955) | 5.0 (6208) | 7.8 (3252) | 2.0 (2956) | 0.95 | 1.22 | 9% |
| 15m · E2 · algotest · filter=none · minstop=1 | -12.4 (3309) | -28.1 (4180) | — (0) | -28.1 (4180) | 0.85 | 0.78 | 30% |
| 15m · E1 · T2 · filter=best · minstop=0 | -17.0 (5057) | -20.7 (6141) | -18.6 (3006) | -22.8 (3135) | 0.71 | 0.73 | 32% |
| 15m · E1 · T3 · filter=best · minstop=0 | -16.2 (4810) | -24.6 (5819) | -22.0 (2869) | -27.1 (2950) | 0.74 | 0.70 | 26% |
| 15m · E1 · T3ref · filter=best · minstop=0 | -16.7 (4659) | -23.2 (5561) | -20.1 (2729) | -26.1 (2832) | 0.74 | 0.72 | 26% |
| 15m · E1 · SR · filter=best · minstop=0 | -16.1 (4526) | -22.1 (5615) | -18.8 (2778) | -25.4 (2837) | 0.73 | 0.72 | 24% |
| 15m · E1 · TR · filter=best · minstop=0 | -8.5 (5865) | -4.8 (7100) | -2.6 (3506) | -6.9 (3594) | 0.61 | 0.82 | 10% |
| 15m · E3 · broken · w3 · T2 · filter=best · minstop=0 | -8.3 (2073) | -11.2 (2372) | -10.3 (1182) | -12.1 (1190) | 0.79 | 0.79 | 34% |
| 15m · E3 · broken · w3 · T3 · filter=best · minstop=0 | -9.1 (2038) | -12.2 (2337) | -13.1 (1167) | -11.4 (1170) | 0.80 | 0.80 | 27% |
| 15m · E3 · broken · w3 · T3ref · filter=best · minstop=0 | -9.1 (2002) | -16.2 (2281) | -18.4 (1138) | -14.1 (1143) | 0.81 | 0.75 | 19% |
| 15m · E3 · broken · w3 · SR · filter=best · minstop=0 | -9.3 (1962) | -13.6 (2258) | -16.5 (1124) | -10.6 (1134) | 0.79 | 0.77 | 24% |
| 15m · E3 · broken · w3 · TR · filter=best · minstop=0 | -3.6 (2152) | -4.8 (2439) | -5.3 (1219) | -4.3 (1220) | 0.78 | 0.77 | 9% |
| 15m · E3 · broken · w6 · T2 · filter=best · minstop=0 | -8.3 (2261) | -10.0 (2634) | -7.1 (1327) | -12.9 (1307) | 0.79 | 0.81 | 34% |
| 15m · E3 · broken · w6 · T3 · filter=best · minstop=0 | -9.1 (2212) | -11.2 (2590) | -9.8 (1310) | -12.7 (1280) | 0.80 | 0.81 | 27% |
| 15m · E3 · broken · w6 · T3ref · filter=best · minstop=0 | -10.1 (2157) | -11.5 (2512) | -9.1 (1269) | -13.9 (1243) | 0.79 | 0.82 | 20% |
| 15m · E3 · broken · w6 · SR · filter=best · minstop=0 | -10.2 (2128) | -12.0 (2498) | -10.8 (1264) | -13.2 (1234) | 0.78 | 0.80 | 24% |
| 15m · E3 · broken · w6 · TR · filter=best · minstop=0 | -4.3 (2360) | -3.6 (2723) | -3.0 (1377) | -4.2 (1346) | 0.74 | 0.82 | 9% |
| 15m · E3 · half · w3 · T2 · filter=best · minstop=0 | -5.7 (2276) | -6.3 (2574) | -7.0 (1288) | -5.7 (1286) | 0.76 | 0.81 | 34% |
| 15m · E3 · half · w3 · T3 · filter=best · minstop=0 | -5.0 (2249) | -8.4 (2539) | -10.5 (1271) | -6.4 (1268) | 0.81 | 0.78 | 26% |
| 15m · E3 · half · w3 · T3ref · filter=best · minstop=0 | -7.2 (2204) | -11.4 (2475) | -10.6 (1241) | -12.2 (1234) | 0.78 | 0.74 | 14% |
| 15m · E3 · half · w3 · SR · filter=best · minstop=0 | -6.5 (2197) | -12.0 (2476) | -9.8 (1238) | -14.2 (1238) | 0.77 | 0.70 | 19% |
| 15m · E3 · half · w3 · TR · filter=best · minstop=0 | -3.5 (2289) | -6.3 (2578) | -8.2 (1287) | -4.5 (1291) | 0.80 | 0.71 | 8% |
| 15m · E3 · half · w6 · T2 · filter=best · minstop=0 | -5.4 (2619) | -5.7 (2986) | -7.0 (1495) | -4.5 (1491) | 0.77 | 0.82 | 34% |
| 15m · E3 · half · w6 · T3 · filter=best · minstop=0 | -5.3 (2569) | -8.9 (2945) | -10.7 (1474) | -7.1 (1471) | 0.80 | 0.77 | 26% |
| 15m · E3 · half · w6 · T3ref · filter=best · minstop=0 | -7.6 (2490) | -10.8 (2854) | -8.5 (1429) | -13.2 (1425) | 0.76 | 0.75 | 13% |
| 15m · E3 · half · w6 · SR · filter=best · minstop=0 | -6.8 (2507) | -12.0 (2864) | -9.6 (1431) | -14.3 (1433) | 0.77 | 0.71 | 19% |
| 15m · E3 · half · w6 · TR · filter=best · minstop=0 | -4.4 (2632) | -6.4 (3004) | -7.4 (1505) | -5.4 (1499) | 0.75 | 0.70 | 8% |
| 15m · E3 · ema · w3 · T2 · filter=best · minstop=0 | -7.1 (1974) | -10.2 (2282) | -12.2 (1133) | -8.2 (1149) | 0.85 | 0.84 | 36% |
| 15m · E3 · ema · w3 · T3 · filter=best · minstop=0 | -6.2 (1932) | -12.6 (2242) | -14.3 (1114) | -11.0 (1128) | 0.88 | 0.82 | 28% |
| 15m · E3 · ema · w3 · T3ref · filter=best · minstop=0 | -3.1 (1892) | -14.3 (2183) | -19.9 (1085) | -8.9 (1098) | 0.94 | 0.81 | 21% |
| 15m · E3 · ema · w3 · SR · filter=best · minstop=0 | -5.3 (1822) | -13.2 (2152) | -15.3 (1063) | -11.1 (1089) | 0.90 | 0.81 | 26% |
| 15m · E3 · ema · w3 · TR · filter=best · minstop=0 | -5.4 (2062) | -4.0 (2378) | -1.1 (1183) | -6.8 (1195) | 0.67 | 0.81 | 9% |
| 15m · E3 · ema · w6 · T2 · filter=best · minstop=0 | -7.7 (2126) | -8.1 (2480) | -7.5 (1235) | -8.6 (1245) | 0.84 | 0.87 | 36% |
| 15m · E3 · ema · w6 · T3 · filter=best · minstop=0 | -7.7 (2075) | -9.2 (2434) | -7.1 (1213) | -11.4 (1221) | 0.86 | 0.87 | 28% |
| 15m · E3 · ema · w6 · T3ref · filter=best · minstop=0 | -5.6 (2018) | -10.0 (2359) | -9.0 (1178) | -11.0 (1181) | 0.90 | 0.87 | 21% |
| 15m · E3 · ema · w6 · SR · filter=best · minstop=0 | -7.3 (1952) | -11.4 (2335) | -9.4 (1156) | -13.4 (1179) | 0.86 | 0.83 | 26% |
| 15m · E3 · ema · w6 · TR · filter=best · minstop=0 | -6.3 (2231) | -3.2 (2596) | 0.6 (1295) | -7.0 (1301) | 0.62 | 0.85 | 9% |
| 15m · E2 · algotest · filter=best · minstop=0 | -16.1 (2310) | -17.9 (2580) | — (0) | -17.9 (2580) | 0.67 | 0.72 | 29% |
| 15m · E1 · T2 · filter=best · minstop=1 | -17.6 (2710) | -27.0 (3438) | -22.5 (1735) | -31.5 (1703) | 0.79 | 0.77 | 32% |
| 15m · E1 · T3 · filter=best · minstop=1 | -16.9 (2584) | -33.5 (3282) | -27.6 (1661) | -39.6 (1621) | 0.82 | 0.73 | 26% |
| 15m · E1 · T3ref · filter=best · minstop=1 | -17.8 (2540) | -31.0 (3189) | -24.6 (1618) | -37.6 (1571) | 0.81 | 0.75 | 26% |
| 15m · E1 · SR · filter=best · minstop=1 | -17.0 (2370) | -30.4 (3066) | -22.9 (1567) | -38.2 (1499) | 0.81 | 0.75 | 25% |
| 15m · E1 · TR · filter=best · minstop=1 | -3.9 (3125) | 0.2 (3964) | 4.7 (2009) | -4.3 (1955) | 0.84 | 1.01 | 10% |
| 15m · E3 · broken · w3 · T2 · filter=best · minstop=1 | -9.0 (1230) | -12.9 (1363) | -11.9 (714) | -14.1 (649) | 0.84 | 0.84 | 34% |
| 15m · E3 · broken · w3 · T3 · filter=best · minstop=1 | -10.0 (1214) | -14.4 (1346) | -16.2 (705) | -12.5 (641) | 0.84 | 0.84 | 26% |
| 15m · E3 · broken · w3 · T3ref · filter=best · minstop=1 | -8.1 (1199) | -20.4 (1324) | -24.4 (694) | -16.0 (630) | 0.87 | 0.79 | 20% |
| 15m · E3 · broken · w3 · SR · filter=best · minstop=1 | -10.0 (1154) | -16.3 (1293) | -22.2 (673) | -10.0 (620) | 0.84 | 0.81 | 25% |
| 15m · E3 · broken · w3 · TR · filter=best · minstop=1 | -0.1 (1265) | -2.3 (1398) | -3.4 (737) | -1.1 (661) | 0.99 | 0.91 | 10% |
| 15m · E3 · broken · w6 · T2 · filter=best · minstop=1 | -9.6 (1358) | -11.5 (1504) | -7.5 (788) | -16.0 (716) | 0.83 | 0.86 | 34% |
| 15m · E3 · broken · w6 · T3 · filter=best · minstop=1 | -10.4 (1337) | -13.6 (1483) | -11.6 (779) | -15.7 (704) | 0.83 | 0.85 | 26% |
| 15m · E3 · broken · w6 · T3ref · filter=best · minstop=1 | -10.7 (1313) | -14.0 (1452) | -11.0 (762) | -17.3 (690) | 0.84 | 0.85 | 20% |
| 15m · E3 · broken · w6 · SR · filter=best · minstop=1 | -11.9 (1273) | -14.7 (1420) | -14.4 (744) | -15.0 (676) | 0.81 | 0.83 | 25% |
| 15m · E3 · broken · w6 · TR · filter=best · minstop=1 | -1.6 (1407) | -1.1 (1547) | -1.2 (818) | -1.1 (729) | 0.91 | 0.96 | 10% |
| 15m · E3 · half · w3 · T2 · filter=best · minstop=1 | 1.7 (565) | -0.8 (808) | -2.4 (442) | 1.1 (366) | 1.04 | 0.99 | 34% |
| 15m · E3 · half · w3 · T3 · filter=best · minstop=1 | 4.4 (563) | -6.3 (799) | -12.8 (436) | 1.4 (363) | 1.08 | 0.92 | 26% |
| 15m · E3 · half · w3 · T3ref · filter=best · minstop=1 | -1.6 (561) | -15.4 (781) | -12.4 (426) | -19.1 (355) | 0.97 | 0.84 | 14% |
| 15m · E3 · half · w3 · SR · filter=best · minstop=1 | 0.1 (548) | -18.4 (769) | -12.5 (416) | -25.4 (353) | 1.00 | 0.79 | 19% |
| 15m · E3 · half · w3 · TR · filter=best · minstop=1 | 9.8 (567) | -1.9 (806) | -7.2 (435) | 4.4 (371) | 1.38 | 0.95 | 10% |
| 15m · E3 · half · w6 · T2 · filter=best · minstop=1 | 1.5 (673) | 0.9 (937) | -2.4 (508) | 4.8 (429) | 1.03 | 1.01 | 35% |
| 15m · E3 · half · w6 · T3 · filter=best · minstop=1 | 3.1 (667) | -7.6 (927) | -13.0 (502) | -1.3 (425) | 1.06 | 0.91 | 25% |
| 15m · E3 · half · w6 · T3ref · filter=best · minstop=1 | -4.6 (660) | -14.2 (902) | -6.5 (488) | -23.3 (414) | 0.93 | 0.85 | 13% |
| 15m · E3 · half · w6 · SR · filter=best · minstop=1 | -2.4 (649) | -17.4 (892) | -10.8 (480) | -25.2 (412) | 0.96 | 0.80 | 18% |
| 15m · E3 · half · w6 · TR · filter=best · minstop=1 | 6.6 (675) | -1.6 (941) | -4.3 (508) | 1.4 (433) | 1.25 | 0.95 | 10% |
| 15m · E3 · ema · w3 · T2 · filter=best · minstop=1 | -6.8 (1339) | -11.2 (1546) | -14.1 (790) | -8.1 (756) | 0.89 | 0.87 | 35% |
| 15m · E3 · ema · w3 · T3 · filter=best · minstop=1 | -5.2 (1316) | -14.7 (1521) | -16.8 (777) | -12.4 (744) | 0.92 | 0.84 | 27% |
| 15m · E3 · ema · w3 · T3ref · filter=best · minstop=1 | 0.2 (1296) | -16.1 (1484) | -24.8 (756) | -7.2 (728) | 1.00 | 0.84 | 21% |
| 15m · E3 · ema · w3 · SR · filter=best · minstop=1 | -3.8 (1222) | -14.7 (1441) | -17.9 (730) | -11.4 (711) | 0.94 | 0.84 | 26% |
| 15m · E3 · ema · w3 · TR · filter=best · minstop=1 | -4.8 (1388) | -0.6 (1602) | 3.2 (820) | -4.5 (782) | 0.72 | 0.98 | 9% |
| 15m · E3 · ema · w6 · T2 · filter=best · minstop=1 | -8.1 (1455) | -7.9 (1693) | -6.7 (867) | -9.2 (826) | 0.87 | 0.90 | 36% |
| 15m · E3 · ema · w6 · T3 · filter=best · minstop=1 | -8.1 (1425) | -9.4 (1663) | -5.8 (851) | -13.2 (812) | 0.88 | 0.90 | 28% |
| 15m · E3 · ema · w6 · T3ref · filter=best · minstop=1 | -3.8 (1400) | -9.5 (1617) | -8.9 (828) | -10.2 (789) | 0.95 | 0.90 | 22% |
| 15m · E3 · ema · w6 · SR · filter=best · minstop=1 | -7.2 (1324) | -12.0 (1576) | -9.6 (799) | -14.5 (777) | 0.89 | 0.86 | 27% |
| 15m · E3 · ema · w6 · TR · filter=best · minstop=1 | -5.9 (1516) | 0.6 (1761) | 5.7 (901) | -4.7 (860) | 0.67 | 1.02 | 9% |
| 15m · E2 · algotest · filter=best · minstop=1 | -20.2 (991) | -26.6 (1136) | — (0) | -26.6 (1136) | 0.74 | 0.77 | 30% |
| 15m · E1 · asis_live_no_checks · filter=none · minstop=0 | -13.7 (10461) | -22.7 (13511) | -16.8 (6857) | -28.8 (6654) | 0.80 | 0.76 | 27% |
| C · 1m · filter=none · hours=all · fade k=2 stop=next target=vwap | -16.6 (9682) | -15.5 (12292) | -16.4 (6035) | -14.7 (6257) | 0.64 | 0.75 | 40% |
| C · 1m · filter=none · hours=all · fade k=2 stop=next target=1s | -15.2 (9601) | -14.3 (11948) | -15.2 (5830) | -13.5 (6118) | 0.63 | 0.73 | 46% |
| C · 1m · filter=none · hours=all · fade k=2 stop=half target=vwap | -16.0 (11686) | -16.8 (14790) | -17.6 (7201) | -16.1 (7589) | 0.61 | 0.70 | 34% |
| C · 1m · filter=none · hours=all · fade k=2 stop=half target=1s | -14.8 (11496) | -15.8 (14251) | -16.4 (6904) | -15.3 (7347) | 0.60 | 0.68 | 39% |
| C · 1m · filter=none · hours=all · fade k=3 stop=next target=vwap | -18.3 (1783) | -13.4 (2779) | -16.0 (1277) | -11.3 (1502) | 0.69 | 0.82 | 41% |
| C · 1m · filter=none · hours=all · fade k=3 stop=next target=1s | -15.1 (1715) | -11.7 (2634) | -12.0 (1219) | -11.5 (1415) | 0.73 | 0.84 | 43% |
| C · 1m · filter=none · hours=all · fade k=3 stop=half target=vwap | -16.0 (1978) | -14.3 (3079) | -19.5 (1408) | -10.0 (1671) | 0.70 | 0.79 | 35% |
| C · 1m · filter=none · hours=all · fade k=3 stop=half target=1s | -13.7 (1905) | -13.1 (2923) | -15.5 (1341) | -11.1 (1582) | 0.73 | 0.80 | 37% |
| C · 1m · filter=none · hours=all · pullback target=1sigma | -6.7 (13445) | -3.1 (17938) | -0.3 (9070) | -6.0 (8868) | 0.79 | 0.93 | 45% |
| C · 1m · filter=none · hours=all · pullback target=2sigma | -7.9 (12700) | -6.1 (16642) | -3.2 (8400) | -9.0 (8242) | 0.77 | 0.88 | 39% |
| C · 1m · filter=none · hours=08-20 · fade k=2 stop=next target=vwap | -19.4 (5159) | -18.0 (6251) | -17.0 (2946) | -18.9 (3305) | 0.60 | 0.72 | 39% |
| C · 1m · filter=none · hours=08-20 · fade k=2 stop=next target=1s | -17.7 (5174) | -16.2 (6171) | -13.4 (2884) | -18.6 (3287) | 0.60 | 0.72 | 46% |
| C · 1m · filter=none · hours=08-20 · fade k=2 stop=half target=vwap | -17.7 (6313) | -19.2 (7630) | -19.4 (3539) | -18.9 (4091) | 0.59 | 0.67 | 34% |
| C · 1m · filter=none · hours=08-20 · fade k=2 stop=half target=1s | -16.1 (6297) | -17.3 (7460) | -16.6 (3435) | -17.9 (4025) | 0.60 | 0.67 | 39% |
| C · 1m · filter=none · hours=08-20 · fade k=3 stop=next target=vwap | -20.4 (943) | -17.6 (1395) | -21.4 (600) | -14.7 (795) | 0.67 | 0.78 | 39% |
| C · 1m · filter=none · hours=08-20 · fade k=3 stop=next target=1s | -18.8 (930) | -15.4 (1354) | -12.4 (585) | -17.7 (769) | 0.70 | 0.80 | 41% |
| C · 1m · filter=none · hours=08-20 · fade k=3 stop=half target=vwap | -19.1 (1068) | -18.6 (1561) | -23.4 (662) | -15.0 (899) | 0.67 | 0.75 | 33% |
| C · 1m · filter=none · hours=08-20 · fade k=3 stop=half target=1s | -18.5 (1054) | -15.9 (1515) | -15.2 (644) | -16.5 (871) | 0.67 | 0.78 | 35% |
| C · 1m · filter=none · hours=08-20 · pullback target=1sigma | -5.3 (6697) | -0.7 (8735) | 2.2 (4515) | -3.8 (4220) | 0.84 | 0.99 | 46% |
| C · 1m · filter=none · hours=08-20 · pullback target=2sigma | -5.7 (6398) | -4.3 (8285) | -2.1 (4272) | -6.7 (4013) | 0.84 | 0.92 | 42% |
| C · 1m · filter=best · hours=all · fade k=2 stop=next target=vwap | -15.5 (2023) | -10.8 (2507) | -9.0 (1198) | -12.4 (1309) | 0.65 | 0.81 | 41% |
| C · 1m · filter=best · hours=all · fade k=2 stop=next target=1s | -16.0 (2032) | -9.9 (2507) | -8.9 (1196) | -10.8 (1311) | 0.60 | 0.80 | 47% |
| C · 1m · filter=best · hours=all · fade k=2 stop=half target=vwap | -15.6 (2484) | -12.9 (3039) | -11.6 (1446) | -14.1 (1593) | 0.61 | 0.76 | 35% |
| C · 1m · filter=best · hours=all · fade k=2 stop=half target=1s | -16.6 (2474) | -10.9 (2990) | -9.0 (1421) | -12.7 (1569) | 0.55 | 0.76 | 40% |
| C · 1m · filter=best · hours=all · fade k=3 stop=next target=vwap | -22.9 (335) | 4.0 (524) | 19.5 (231) | -8.3 (293) | 0.66 | 1.05 | 44% |
| C · 1m · filter=best · hours=all · fade k=3 stop=next target=1s | -19.0 (327) | -2.5 (505) | 9.2 (224) | -11.9 (281) | 0.71 | 0.97 | 46% |
| C · 1m · filter=best · hours=all · fade k=3 stop=half target=vwap | -17.9 (382) | 1.3 (583) | 1.9 (254) | 0.9 (329) | 0.69 | 1.02 | 38% |
| C · 1m · filter=best · hours=all · fade k=3 stop=half target=1s | -14.7 (374) | -4.0 (563) | -7.2 (247) | -1.5 (316) | 0.74 | 0.94 | 39% |
| C · 1m · filter=best · hours=all · pullback target=1sigma | -6.8 (6064) | -6.8 (7526) | -5.5 (3913) | -8.2 (3613) | 0.79 | 0.85 | 44% |
| C · 1m · filter=best · hours=all · pullback target=2sigma | -7.2 (5751) | -8.6 (7075) | -7.5 (3680) | -9.8 (3395) | 0.79 | 0.82 | 39% |
| C · 1m · filter=best · hours=08-20 · fade k=2 stop=next target=vwap | -14.0 (1060) | -5.6 (1209) | 2.2 (560) | -12.4 (649) | 0.69 | 0.90 | 42% |
| C · 1m · filter=best · hours=08-20 · fade k=2 stop=next target=1s | -16.4 (1077) | -7.8 (1225) | -0.3 (565) | -14.2 (660) | 0.61 | 0.84 | 48% |
| C · 1m · filter=best · hours=08-20 · fade k=2 stop=half target=vwap | -13.4 (1317) | -10.7 (1492) | -6.3 (695) | -14.6 (797) | 0.66 | 0.80 | 36% |
| C · 1m · filter=best · hours=08-20 · fade k=2 stop=half target=1s | -16.1 (1329) | -11.0 (1487) | -7.0 (689) | -14.5 (798) | 0.57 | 0.76 | 41% |
| C · 1m · filter=best · hours=08-20 · fade k=3 stop=next target=vwap | -24.8 (179) | 12.6 (252) | 9.3 (103) | 14.8 (149) | 0.65 | 1.15 | 46% |
| C · 1m · filter=best · hours=08-20 · fade k=3 stop=next target=1s | -23.9 (179) | 1.9 (251) | 11.9 (103) | -5.0 (148) | 0.66 | 1.02 | 47% |
| C · 1m · filter=best · hours=08-20 · fade k=3 stop=half target=vwap | -18.9 (212) | 10.3 (286) | -6.1 (117) | 21.7 (169) | 0.69 | 1.14 | 39% |
| C · 1m · filter=best · hours=08-20 · fade k=3 stop=half target=1s | -18.6 (212) | 2.7 (284) | -5.5 (117) | 8.4 (167) | 0.69 | 1.04 | 40% |
| C · 1m · filter=best · hours=08-20 · pullback target=1sigma | -5.8 (3173) | -3.9 (3724) | -3.3 (1981) | -4.7 (1743) | 0.83 | 0.92 | 45% |
| C · 1m · filter=best · hours=08-20 · pullback target=2sigma | -6.9 (3031) | -5.2 (3572) | -5.7 (1898) | -4.7 (1674) | 0.81 | 0.90 | 41% |
| C · 5m · filter=none · hours=all · fade k=2 stop=next target=vwap | -21.2 (6728) | -17.8 (8733) | -14.2 (4297) | -21.2 (4436) | 0.69 | 0.80 | 45% |
| C · 5m · filter=none · hours=all · fade k=2 stop=next target=1s | -16.4 (6526) | -15.7 (8275) | -13.6 (4073) | -17.7 (4202) | 0.68 | 0.76 | 54% |
| C · 5m · filter=none · hours=all · fade k=2 stop=half target=vwap | -21.0 (7991) | -17.9 (10177) | -15.8 (5023) | -20.0 (5154) | 0.65 | 0.76 | 36% |
| C · 5m · filter=none · hours=all · fade k=2 stop=half target=1s | -16.4 (7558) | -15.3 (9436) | -13.5 (4672) | -17.1 (4764) | 0.64 | 0.73 | 45% |
| C · 5m · filter=none · hours=all · fade k=3 stop=next target=vwap | -19.7 (1355) | -12.2 (2142) | -15.9 (1020) | -8.8 (1122) | 0.76 | 0.88 | 45% |
| C · 5m · filter=none · hours=all · fade k=3 stop=next target=1s | -12.8 (1267) | -9.8 (1980) | -14.3 (942) | -5.8 (1038) | 0.82 | 0.89 | 50% |
| C · 5m · filter=none · hours=all · fade k=3 stop=half target=vwap | -15.6 (1441) | -12.7 (2288) | -14.8 (1100) | -10.8 (1188) | 0.78 | 0.86 | 39% |
| C · 5m · filter=none · hours=all · fade k=3 stop=half target=1s | -10.7 (1346) | -7.3 (2113) | -11.5 (1012) | -3.5 (1101) | 0.83 | 0.90 | 45% |
| C · 5m · filter=none · hours=all · pullback target=1sigma | -6.7 (10328) | 0.2 (13881) | 5.1 (7100) | -4.8 (6781) | 0.84 | 1.00 | 48% |
| C · 5m · filter=none · hours=all · pullback target=2sigma | -7.9 (8345) | -3.8 (11086) | 3.5 (5646) | -11.5 (5440) | 0.84 | 0.95 | 36% |
| C · 5m · filter=none · hours=08-20 · fade k=2 stop=next target=vwap | -28.7 (3631) | -23.0 (4420) | -16.8 (2070) | -28.4 (2350) | 0.63 | 0.76 | 43% |
| C · 5m · filter=none · hours=08-20 · fade k=2 stop=next target=1s | -20.9 (3640) | -19.6 (4355) | -16.0 (2044) | -22.8 (2311) | 0.64 | 0.73 | 54% |
| C · 5m · filter=none · hours=08-20 · fade k=2 stop=half target=vwap | -25.2 (4436) | -23.1 (5337) | -20.8 (2530) | -25.1 (2807) | 0.61 | 0.71 | 34% |
| C · 5m · filter=none · hours=08-20 · fade k=2 stop=half target=1s | -19.1 (4304) | -19.0 (5139) | -17.4 (2446) | -20.4 (2693) | 0.62 | 0.70 | 45% |
| C · 5m · filter=none · hours=08-20 · fade k=3 stop=next target=vwap | -32.6 (640) | -19.8 (1016) | -30.7 (458) | -10.8 (558) | 0.67 | 0.82 | 41% |
| C · 5m · filter=none · hours=08-20 · fade k=3 stop=next target=1s | -24.8 (624) | -16.2 (972) | -22.6 (434) | -11.0 (538) | 0.71 | 0.84 | 47% |
| C · 5m · filter=none · hours=08-20 · fade k=3 stop=half target=vwap | -25.1 (686) | -17.2 (1080) | -26.1 (491) | -9.7 (589) | 0.69 | 0.83 | 36% |
| C · 5m · filter=none · hours=08-20 · fade k=3 stop=half target=1s | -18.9 (664) | -10.6 (1032) | -16.5 (464) | -5.7 (568) | 0.74 | 0.88 | 42% |
| C · 5m · filter=none · hours=08-20 · pullback target=1sigma | -5.3 (5070) | 5.0 (6504) | 9.1 (3425) | 0.5 (3079) | 0.88 | 1.08 | 50% |
| C · 5m · filter=none · hours=08-20 · pullback target=2sigma | -4.5 (4154) | 1.1 (5313) | 7.5 (2794) | -6.0 (2519) | 0.92 | 1.01 | 38% |
| C · 5m · filter=best · hours=all · fade k=2 stop=next target=vwap | -15.1 (1441) | -7.0 (1726) | -4.2 (826) | -9.5 (900) | 0.75 | 0.91 | 45% |
| C · 5m · filter=best · hours=all · fade k=2 stop=next target=1s | -15.6 (1422) | -6.8 (1645) | -1.0 (792) | -12.2 (853) | 0.67 | 0.88 | 54% |
| C · 5m · filter=best · hours=all · fade k=2 stop=half target=vwap | -17.3 (1700) | -8.6 (2008) | -5.3 (972) | -11.8 (1036) | 0.67 | 0.87 | 36% |
| C · 5m · filter=best · hours=all · fade k=2 stop=half target=1s | -16.6 (1635) | -7.0 (1870) | -1.7 (905) | -12.0 (965) | 0.61 | 0.86 | 45% |
| C · 5m · filter=best · hours=all · fade k=3 stop=next target=vwap | -14.9 (226) | 14.8 (364) | 38.3 (168) | -5.3 (196) | 0.83 | 1.16 | 48% |
| C · 5m · filter=best · hours=all · fade k=3 stop=next target=1s | -7.2 (217) | 14.1 (336) | 20.6 (154) | 8.5 (182) | 0.90 | 1.16 | 51% |
| C · 5m · filter=best · hours=all · fade k=3 stop=half target=vwap | -12.4 (245) | 9.0 (389) | 33.5 (180) | -12.0 (209) | 0.83 | 1.10 | 41% |
| C · 5m · filter=best · hours=all · fade k=3 stop=half target=1s | -10.3 (236) | 14.8 (359) | 19.4 (165) | 10.9 (194) | 0.84 | 1.20 | 45% |
| C · 5m · filter=best · hours=all · pullback target=1sigma | -7.7 (4503) | -3.2 (5719) | -0.6 (2989) | -6.2 (2730) | 0.82 | 0.94 | 48% |
| C · 5m · filter=best · hours=all · pullback target=2sigma | -8.2 (3803) | -7.6 (4773) | -3.7 (2468) | -11.8 (2305) | 0.84 | 0.90 | 36% |
| C · 5m · filter=best · hours=08-20 · fade k=2 stop=next target=vwap | -14.4 (761) | 0.5 (823) | 10.8 (396) | -9.1 (427) | 0.77 | 1.01 | 43% |
| C · 5m · filter=best · hours=08-20 · fade k=2 stop=next target=1s | -15.9 (764) | -6.6 (829) | 0.5 (399) | -13.2 (430) | 0.69 | 0.89 | 56% |
| C · 5m · filter=best · hours=08-20 · fade k=2 stop=half target=vwap | -15.6 (911) | -4.1 (1001) | 3.3 (490) | -11.2 (511) | 0.72 | 0.94 | 34% |
| C · 5m · filter=best · hours=08-20 · fade k=2 stop=half target=1s | -18.0 (896) | -8.9 (987) | -2.0 (479) | -15.4 (508) | 0.60 | 0.83 | 46% |
| C · 5m · filter=best · hours=08-20 · fade k=3 stop=next target=vwap | -9.2 (105) | 25.0 (166) | 21.1 (70) | 27.8 (96) | 0.90 | 1.23 | 47% |
| C · 5m · filter=best · hours=08-20 · fade k=3 stop=next target=1s | -11.4 (104) | 10.1 (163) | 4.4 (68) | 14.1 (95) | 0.86 | 1.10 | 52% |
| C · 5m · filter=best · hours=08-20 · fade k=3 stop=half target=vwap | -2.7 (114) | 29.7 (176) | 34.1 (75) | 26.5 (101) | 0.96 | 1.30 | 42% |
| C · 5m · filter=best · hours=08-20 · fade k=3 stop=half target=1s | -8.0 (113) | 18.7 (171) | 14.8 (72) | 21.5 (99) | 0.88 | 1.21 | 46% |
| C · 5m · filter=best · hours=08-20 · pullback target=1sigma | -7.0 (2387) | 1.2 (2800) | 2.6 (1494) | -0.4 (1306) | 0.85 | 1.02 | 49% |
| C · 5m · filter=best · hours=08-20 · pullback target=2sigma | -8.1 (2015) | -2.7 (2387) | 0.1 (1263) | -5.9 (1124) | 0.86 | 0.97 | 38% |
| C · 15m · filter=none · hours=all · fade k=2 stop=next target=vwap | -17.9 (4734) | -12.8 (6176) | -6.7 (3072) | -18.9 (3104) | 0.78 | 0.87 | 48% |
| C · 15m · filter=none · hours=all · fade k=2 stop=next target=1s | -12.4 (4456) | -10.7 (5563) | -8.7 (2825) | -12.9 (2738) | 0.77 | 0.84 | 56% |
| C · 15m · filter=none · hours=all · fade k=2 stop=half target=vwap | -17.9 (5334) | -15.3 (6870) | -11.6 (3425) | -19.0 (3445) | 0.74 | 0.82 | 39% |
| C · 15m · filter=none · hours=all · fade k=2 stop=half target=1s | -13.5 (4876) | -13.5 (6034) | -11.2 (3070) | -16.0 (2964) | 0.72 | 0.78 | 47% |
| C · 15m · filter=none · hours=all · fade k=3 stop=next target=vwap | -18.5 (985) | -10.4 (1599) | -14.6 (791) | -6.2 (808) | 0.81 | 0.90 | 48% |
| C · 15m · filter=none · hours=all · fade k=3 stop=next target=1s | -15.6 (896) | -7.5 (1449) | -7.3 (732) | -7.7 (717) | 0.81 | 0.91 | 55% |
| C · 15m · filter=none · hours=all · fade k=3 stop=half target=vwap | -18.0 (1033) | -15.2 (1682) | -17.2 (846) | -13.1 (836) | 0.80 | 0.85 | 42% |
| C · 15m · filter=none · hours=all · fade k=3 stop=half target=1s | -15.2 (935) | -12.8 (1515) | -13.3 (774) | -12.2 (741) | 0.80 | 0.84 | 48% |
| C · 15m · filter=none · hours=all · pullback target=1sigma | -7.3 (9559) | 0.1 (12540) | 5.7 (6477) | -5.9 (6063) | 0.83 | 1.00 | 47% |
| C · 15m · filter=none · hours=all · pullback target=2sigma | -6.8 (7330) | -2.6 (9579) | 4.7 (4912) | -10.3 (4667) | 0.88 | 0.97 | 34% |
| C · 15m · filter=none · hours=08-20 · fade k=2 stop=next target=vwap | -22.0 (2800) | -13.8 (3424) | -5.2 (1630) | -21.6 (1794) | 0.75 | 0.87 | 47% |
| C · 15m · filter=none · hours=08-20 · fade k=2 stop=next target=1s | -14.7 (2621) | -10.2 (3163) | -7.1 (1516) | -13.0 (1647) | 0.75 | 0.86 | 58% |
| C · 15m · filter=none · hours=08-20 · fade k=2 stop=half target=vwap | -21.4 (3121) | -19.1 (3790) | -14.2 (1810) | -23.7 (1980) | 0.71 | 0.79 | 37% |
| C · 15m · filter=none · hours=08-20 · fade k=2 stop=half target=1s | -16.5 (2861) | -15.3 (3425) | -13.0 (1651) | -17.5 (1774) | 0.68 | 0.76 | 48% |
| C · 15m · filter=none · hours=08-20 · fade k=3 stop=next target=vwap | -28.6 (468) | -10.2 (745) | -22.4 (368) | 1.6 (377) | 0.76 | 0.91 | 45% |
| C · 15m · filter=none · hours=08-20 · fade k=3 stop=next target=1s | -27.3 (433) | -3.5 (686) | 0.4 (343) | -7.5 (343) | 0.72 | 0.96 | 55% |
| C · 15m · filter=none · hours=08-20 · fade k=3 stop=half target=vwap | -27.3 (496) | -17.4 (781) | -27.1 (392) | -7.6 (389) | 0.74 | 0.84 | 39% |
| C · 15m · filter=none · hours=08-20 · fade k=3 stop=half target=1s | -23.1 (457) | -10.6 (715) | -8.0 (362) | -13.3 (353) | 0.73 | 0.88 | 48% |
| C · 15m · filter=none · hours=08-20 · pullback target=1sigma | -7.6 (4963) | 4.5 (6268) | 8.3 (3315) | 0.2 (2953) | 0.84 | 1.07 | 50% |
| C · 15m · filter=none · hours=08-20 · pullback target=2sigma | -5.8 (3968) | 0.8 (5005) | 4.1 (2619) | -2.9 (2386) | 0.91 | 1.01 | 35% |
| C · 15m · filter=best · hours=all · fade k=2 stop=next target=vwap | -8.4 (1056) | 2.3 (1214) | -1.9 (597) | 6.3 (617) | 0.88 | 1.03 | 49% |
| C · 15m · filter=best · hours=all · fade k=2 stop=next target=1s | -10.8 (954) | -6.0 (1072) | -0.8 (526) | -11.0 (546) | 0.77 | 0.89 | 55% |
| C · 15m · filter=best · hours=all · fade k=2 stop=half target=vwap | -13.2 (1130) | 0.5 (1307) | -2.3 (639) | 3.2 (668) | 0.78 | 1.01 | 40% |
| C · 15m · filter=best · hours=all · fade k=2 stop=half target=1s | -11.7 (1010) | -5.0 (1132) | -0.1 (556) | -9.8 (576) | 0.72 | 0.90 | 47% |
| C · 15m · filter=best · hours=all · fade k=3 stop=next target=vwap | -10.0 (163) | -8.5 (259) | 11.4 (127) | -27.6 (132) | 0.91 | 0.93 | 47% |
| C · 15m · filter=best · hours=all · fade k=3 stop=next target=1s | -5.7 (155) | -2.0 (235) | 11.2 (117) | -15.1 (118) | 0.93 | 0.98 | 54% |
| C · 15m · filter=best · hours=all · fade k=3 stop=half target=vwap | -5.0 (169) | -17.4 (267) | -3.5 (132) | -31.0 (135) | 0.94 | 0.84 | 41% |
| C · 15m · filter=best · hours=all · fade k=3 stop=half target=1s | -0.2 (160) | -11.8 (239) | -9.7 (120) | -13.9 (119) | 1.00 | 0.86 | 47% |
| C · 15m · filter=best · hours=all · pullback target=1sigma | -6.6 (3971) | -4.8 (4849) | -3.4 (2559) | -6.3 (2290) | 0.85 | 0.93 | 47% |
| C · 15m · filter=best · hours=all · pullback target=2sigma | -6.7 (3259) | -7.2 (4012) | -5.5 (2084) | -8.9 (1928) | 0.88 | 0.91 | 34% |
| C · 15m · filter=best · hours=08-20 · fade k=2 stop=next target=vwap | -13.0 (541) | 19.0 (632) | 22.8 (300) | 15.6 (332) | 0.83 | 1.22 | 48% |
| C · 15m · filter=best · hours=08-20 · fade k=2 stop=next target=1s | -13.9 (516) | 3.1 (589) | 11.7 (279) | -4.6 (310) | 0.74 | 1.05 | 59% |
| C · 15m · filter=best · hours=08-20 · fade k=2 stop=half target=vwap | -16.3 (590) | 15.2 (690) | 20.5 (328) | 10.4 (362) | 0.74 | 1.20 | 39% |
| C · 15m · filter=best · hours=08-20 · fade k=2 stop=half target=1s | -14.5 (556) | 1.9 (632) | 11.4 (300) | -6.7 (332) | 0.69 | 1.04 | 50% |
| C · 15m · filter=best · hours=08-20 · fade k=3 stop=next target=vwap | -18.8 (70) | -3.2 (106) | -10.2 (54) | 4.0 (52) | 0.84 | 0.98 | 48% |
| C · 15m · filter=best · hours=08-20 · fade k=3 stop=next target=1s | -8.7 (68) | 3.6 (102) | 22.7 (52) | -16.3 (50) | 0.91 | 1.04 | 60% |
| C · 15m · filter=best · hours=08-20 · fade k=3 stop=half target=vwap | -7.0 (75) | -28.8 (109) | -52.3 (56) | -4.0 (53) | 0.92 | 0.79 | 39% |
| C · 15m · filter=best · hours=08-20 · fade k=3 stop=half target=1s | 0.5 (73) | -27.6 (103) | -33.4 (53) | -21.5 (50) | 1.01 | 0.74 | 48% |
| C · 15m · filter=best · hours=08-20 · pullback target=1sigma | -6.4 (2189) | -0.1 (2531) | -4.2 (1356) | 4.7 (1175) | 0.87 | 1.00 | 49% |
| C · 15m · filter=best · hours=08-20 · pullback target=2sigma | -5.1 (1818) | -2.7 (2157) | -7.9 (1149) | 3.3 (1008) | 0.92 | 0.97 | 36% |

Code: `backend/app/delta_lab/trend_core.py`, `trend_5ema.py`, `trend_vwap.py`, `trend_report.py`, `trend_dl.py`; numbers: `reports/trend_5ema.json`, `reports/trend_vwap.json`. 446 variants added to the running total.