# Riding pumps on illiquid coins (long, multi-day), 25 Nov 2026

*Hypothetical backtest on completed daily bars; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

**Look-ahead first:** we can't pick coins that we *know* went up 100-200%; that is only known afterwards. So the entry uses only what's visible at a daily close: the START of a volume-driven move.
- **Coins:** illiquid ones, with under 5 M USDT/day median volume over the prior 60 days.
- **Signal day:** volume at least V x normal (V = 5 or 10), a close at least X% up on the day (X = 20 or 40) in the top third of the day's range, and not yet up 100% from the 10-day low.
- **Entry:** buy the next day's open.
- **Exit:** stop at the signal day's low, trailed either under the last 2 days' lows or at the highest close minus 3 ATR. Time stop 7 or 14 days.
- **Data and costs:** 822 Binance perpetuals (delisted ones included), Jan 2022 to Oct 2026, daily bars. Delta fees + GST, 20 bps slippage per fill, funding paid while long.
- **Split:** by date: train to 2026-04-16, validation to 2026-07-28, holdout after. **16 variants added to the running total.**

| Variant | Train mean net % (trades) | Validation mean net % (trades) | Validation PF |
|---|---|---|---|
| vol>=5x|day>=+20%|trail=2d_low|max=7d | -0.5 (264) | -1.7 (144) | 0.86 |
| vol>=5x|day>=+20%|trail=2d_low|max=14d | -1.0 (264) | -0.8 (144) | 0.93 |
| vol>=5x|day>=+20%|trail=3atr|max=7d | 0.9 (264) | 1.8 (144) | 1.14 |
| vol>=5x|day>=+20%|trail=3atr|max=14d | -0.1 (264) | 7.6 (144) | 1.54 |
| vol>=5x|day>=+40%|trail=2d_low|max=7d | -7.1 (74) | -14.8 (36) | 0.14 |
| vol>=5x|day>=+40%|trail=2d_low|max=14d | -8.0 (74) | -14.8 (36) | 0.14 |
| vol>=5x|day>=+40%|trail=3atr|max=7d | -9.5 (74) | -11.0 (36) | 0.39 |
| vol>=5x|day>=+40%|trail=3atr|max=14d | -13.3 (74) | -12.3 (36) | 0.36 |
| vol>=10x|day>=+20%|trail=2d_low|max=7d | -2.2 (222) | -0.1 (106) | 0.99 |
| vol>=10x|day>=+20%|trail=2d_low|max=14d | -2.7 (222) | 1.0 (106) | 1.08 |
| vol>=10x|day>=+20%|trail=3atr|max=7d | -1.9 (222) | 4.4 (106) | 1.32 |
| vol>=10x|day>=+20%|trail=3atr|max=14d | -3.1 (222) | 10.6 (106) | 1.72 |
| vol>=10x|day>=+40%|trail=2d_low|max=7d | -7.5 (71) | -14.3 (35) | 0.15 |
| vol>=10x|day>=+40%|trail=2d_low|max=14d | -8.4 (71) | -14.3 (35) | 0.15 |
| vol>=10x|day>=+40%|trail=3atr|max=7d | -9.9 (71) | -10.7 (35) | 0.41 |
| vol>=10x|day>=+40%|trail=3atr|max=14d | -13.5 (71) | -12.5 (35) | 0.36 |

**Holdout (vol>=10x|day>=+20%|trail=3atr|max=14d):** 111 trades, mean **+4.2%** per trade, median **-10.8%**, PF 1.35, win rate 35%, worst -39%, best +331%. Bootstrap 90% range of the mean: -3.1% to +13.0%. Median hold 9 days. 32% of the entries went on to double their pre-signal price within 14 days.
- **Without the single best trade (TUT, +331%) the mean is +1.2%; without the top 3 it is -2.8%.**

## Reading
- **Starting late doesn't work.** Buying after a +40% day loses 11-15% per trade in every version.
- **Starting early (+20% day, very high volume) with a loose 3-ATR trail is the only promising corner.** It is a lottery: about 2 in 3 trades lose about 10%, and a few winners run 100-300%.
- **It is not proven.** The same version LOST 3.1% per trade in training, the holdout range includes zero, and the gain rests on 3-5 trades.
- **Practical limits:** many of these coins aren't listed on Delta India, Delta caps size at 1 contract, and slippage on a spike day can be far worse than 20 bps.
- **Suggestion:** if you want it, run it as an 'Unproven' paper scanner on Delta's own listed coins, with frozen rules, the same way as the funding-extreme candidate. No live trading on this evidence.

Code: `backend/app/delta_lab/pump_ride.py`; numbers: `reports/pump_ride.json`.
