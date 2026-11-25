# Slower 4h strategies: near-pump longs and squeeze-aware shorts with Smith VWAP bands (25 Nov 2026)

*Hypothetical backtests on completed 4-hour bars of Delta's own data (178 coins); kept apart from real P&L. Decision support, not investment advice.*

Rules are in `backend/app/delta_lab/slow_lab.py`: Smith VWAP deviation bands on 4h (length 30 or 60 bars), a "near pump" filter (24h volume >= 2x the 30-day average, 7-day return +5% to +40%), three long ideas, three short ideas after a +20% week, a squeeze guard (funding >= 0, volume no longer exploding, stop within 15%), two exits (band or mean target, or a 3-ATR trail) and a 5-day time stop. Delta fees + GST, slippage 5 bps (top-20 coins) / 20 bps (others), and funding are included. Split by time: train to 2026-04-06, validation to 2026-08-20, holdout after. **36 variants added.**

| Idea | Best on validation | Train / validation mean net | **Holdout** (trades, 90% range) |
|---|---|---|---|
| L1 dip to lower band (near pump) | too few trades (<30) | — | — |
| L2 break above upper band on 2x volume | L=30, trail | -1.0% / +2.8% | **-0.7%** (100; -2.6 to +1.4) |
| L3 reclaim of the mean after 3 closes below | L=60, exit at upper band | +0.6% / -1.1% | **+1.0%** (84; -1.3 to +3.3), 58% winners |
| S1 failed upper band (short) | L=60, trail, no guard | +1.9% / +4.0% | **-0.5%** (57; -4.1 to +3.0) |
| S2 lower high below the mean (short) | L=60, trail, no guard | +1.7% / +4.7% | **-1.6%** (66; -4.2 to +1.1) |
| S3 blow-off then breakdown (short) | L=30, trail, no guard | -1.4% / -2.9% | +0.8% (67; -2.1 to +4.0) |

## Squeezes
- Median adverse move before the exit: about 4-9%. The squeeze guard cut the short trades by 70-90%, and with so few trades left, it did not help in a way that can be measured.
- The shorts lose less to squeezes than the 1h dump short did, because the stops sit beyond the swing high + ATR. But the shorts that looked good in train and validation (S1, S2: +2-5%) turned negative on the holdout.

## Reading
- Nothing holds across all three periods with a range clear of zero.
- **L3 (buy the mean reclaim on a near-pump coin) is the only long that is positive on the holdout** (+1.0%, 58% winners), but it was negative on validation.
- Most coins only exist on Delta since 2025, so each period has only 50-250 trades. These are hypotheses, not edges.
