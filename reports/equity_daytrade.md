# Classic intraday day-trading on NSE stocks (25 Nov 2026)

*Hypothetical backtest on completed 5-minute bars; kept apart from real P&L. Decision support, not investment advice.*

- **Data:** Dhan 1-minute history kept as 5-minute bars: 382 Market watch stocks, 252 sessions (Nov 2025 to Nov 2026).
- **Universe:** each day, the 150 most traded stocks by their prior 20-day median turnover.
- **Trading:** flat by 15:15 IST. Costs are Dhan intraday charges at a Rs 50,000 order (about 10.6 bps a round trip) plus 2 bps slippage per side, about 14.6 bps in total.
- **Split:** train to 2026-05-25, validation to 2026-08-24, holdout after.
- **Variants:** 37 added. Rules are in `backend/app/equity_lab/daytrade.py`.

| Family | Best on validation | Holdout net bps/trade (trades, 90% range) | Longs / shorts |
|---|---|---|---|
| Opening range 30 min (ORB30) | stop at the other side, 1R target, narrow-range days | **-8.8** (5,180; -11.1 to -6.4) | -18.6 / **-0.7** |
| NR7 + ORB30 (Crabel) | stop other side, 2R | **-5.7** (1,295; -11.5 to +0.2) | -15.1 / **+1.6** |
| Previous-day high/low break | stop at the opening-range middle, exit 15:15 | -11.6 (5,986) | -15.0 / -9.2 |
| Opening range 15 min | stop other side, 1R, narrow days | -13.3 (1,101) | -20.7 / -6.4 |
| Gap and go (gap >= 2%) | — | -11.4 (54 only) | — |
| VWAP pullback with the trend | 2R | -17.1 (7,602) | -19.5 / -14.8 |
| Camarilla H3/L3 fade | — | -19.4 (5,974) | -19.9 / -18.7 |
| Gap fade | — | about -19 to -23 on validation | — |

## Reading
- **Every classic intraday rule loses after costs.** Before costs most of them are slightly positive (ORB30 is about +6 bps gross), but about 14.6 bps of costs per round trip is more than the edge.
- **One consistent detail: the SHORT side of the opening-range breakouts is about break-even after costs** (ORB30 -0.7, NR7 +1.6 bps), while the longs lose 15-21 bps. In this year (a weak market for these stocks) breakdowns followed through and breakouts failed.
- **Nothing to trade.** If anything is worth a second look, it is "ORB30 / NR7, shorts only, narrow opening range", as a paper idea, with the caveat that it is one market phase.
- **Data note:** this download stored bar times in thousands of seconds (a pandas slip, now fixed). Times were rebuilt exactly on full 75-bar days, but a bar can be up to 15 minutes early on days with gaps.
