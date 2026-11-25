# Which pumps dump? Delta coins, 1h (25 Nov 2026)

*Hypothetical backtest on completed bars of Delta's own candles and funding; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

**Event:** a 1h close at least +15% above the close 3 hours earlier, on 10x normal volume. 706 events on 146 coins (183 Delta coins, listed today only).
**Costs:** fees + GST, 20 bps slippage per fill, and Delta funding (shorts receive a positive rate).
**Variants:** 6 added to the running total. Split by time 50/25/25.

## What follows a pump (train events, n = 353)
- 24h later, **71% are lower (median -7%)**. 72h later, 73% are lower (median -8.6%).
- **But first the squeeze:** the median highest price in the next 24h is **+8.7% above the pump close**. Almost every event goes higher before it dumps.
- **The best sign of a dump:** the **next hour closes below the pump bar's midpoint**. Then the median 24h move is -10% with 77% down, vs -5% and 68% down when it doesn't.
- Bigger jumps dump more (+25-50% jumps: median -11.7%). Upper wick, volume fade, funding and the prior 7-day run-up made little difference.

## The short rule (entry after a failure, never into the pump)
- **Confirm:** none / the first 1h close below the pump bar's midpoint within 6h / the first lower-high red hour within 12h.
- **Stop:** 0.5% above the post-pump peak.
- **Exit:** at the pre-pump price (the close 3h before), or trail above the last 4h high; 3-day time stop.

| Variant | Train mean net (trades) | Validation mean net (trades) | Validation PF |
|---|---|---|---|
| none / base | +0.3% (353) | +0.2% (175) | 1.06 |
| none / trail | -0.2% (353) | -0.0% (175) | 1.00 |
| **close below mid / base** | **+1.8% (209)** | **+2.3% (105)** | **1.55** |
| close below mid / trail | +1.5% (209) | +0.5% (105) | 1.14 |
| lower high / base | +2.8% (352) | +1.0% (175) | 1.19 |
| lower high / trail | +1.3% (352) | -0.4% (175) | 0.88 |

**Holdout (close below mid / base, touched once):** 99 trades, **mean -0.45% per trade, median +0.6%**, PF 0.91, 51% winners. Bootstrap 90% -2.3% to +1.3%. Exits: 46 at target, 42 at the stop, 11 on time; median hold 12h.

## Reading
- **The direction is real:** pumps on Delta coins mostly give back a good part within a day.
- **The edge is too thin to trade on this evidence.** The best rule was positive in train and validation (+1.8%, +2.3%) but slightly negative on the holdout (-0.45%). Squeezes of +9% or more are routine, so the stop above the peak gets hit about 4 times in 10.
- **Worth keeping as a paper candidate:** short only after the first hourly close below the pump bar's midpoint, stop above the peak, target the pre-pump price. Track it forward on paper before any money.

Code: `backend/app/delta_lab/dump_study.py`; numbers: `reports/dump_study.json`.
