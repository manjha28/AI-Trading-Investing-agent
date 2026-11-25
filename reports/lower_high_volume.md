# Your marked shorts, with volume (25 Nov 2026)

*Hypothetical backtest on completed historical bars; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

## What your 4 marked shorts have in common (Delta 5m candles, times UTC-6 as on your chart)

| # | Rally into | Volume on the rally bar | Rejection candle | Your entry | Your exit |
|---|---|---|---|---|---|
| 1 | 21:20, high 59,640, a lower high under 59,729 | 1.5x the 20-bar average (21:10 was 1.8x) | 21:25 red, close 59,425 | about 59,500 | about 59,290 |
| 2 | 22:15, high 59,377, lower high | **0.6x, a quiet rally** | 22:20 red, close 59,245 | about 59,235 | about 59,090 |
| 3 | 00:10, high 59,328, lower high | 2.0x | 00:15 red, close 59,229 | about 59,294 | about 59,235 |
| 4 | 01:05, high 59,392 | 2.1x | 01:10 red, close 59,221 | about 59,184 | still riding (58,700-58,800); 01:35 breakdown candle on **6.4x** volume |

In all four the price rallied into the last swing high and failed below it. In three of the four, the rally candle carried 1.5-2x normal volume: buyers spent themselves at resistance, and the next red candle shut it down. Your exits were near the last swing low, except #4, where you want to hold through the high-volume breakdown with the stop in profit.

## The rule I tested (written in `backend/app/delta_lab/lower_high_volume.py`)
1. **Trend:** 5m swings (3 bars each side) show falling highs and falling lows, so shorts only. The mirror for longs is a variant.
2. **Setup:** a rally candle reaches the last swing high (or pokes up to 0.5 ATR above it) and doesn't close above it. **Volume variant:** that candle trades at least 1.5x its 20-bar average volume.
3. **Trigger:** the same or next candle closes red and below the rally candle's midpoint. Short at the next minute's open.
4. **Stop:** the rally high + 0.1 ATR. The stop must be at least one round trip of costs (about 14 bps) away.
5. **Exit:** target at the last swing low, at least 1R away.
   - **Volume-hold variant:** if a red candle on 3x+ volume closes through the target, don't take profit; move the stop to that candle's high and trail lower highs.
   - **Adverse-volume variant:** get out on a green 2x-volume candle that closes above the entry.

**Data:** BTC and ETH, 5m, Binance 1-minute data from Jan 2023 to Nov 2026, Delta fees + GST + slippage. Split by time 50/25/25, chosen on validation. **16 variants added to the running total.** These years were already used today, so the holdout isn't fresh. Your 25 Nov trades are after the end of this data, so they aren't in it.

| Variant | Train net bps (trades) | Validation net bps (trades) | Validation PF |
|---|---|---|---|
| volsetup=off|volhold=off|adverse=off|longs=off | -12.6 (1670) | -14.8 (872) | 0.54 |
| volsetup=off|volhold=off|adverse=off|longs=on | -12.2 (3302) | -14.1 (1622) | 0.55 |
| volsetup=off|volhold=off|adverse=on|longs=off | -12.5 (1674) | -14.6 (878) | 0.53 |
| volsetup=off|volhold=off|adverse=on|longs=on | -11.7 (3322) | -13.9 (1636) | 0.54 |
| volsetup=off|volhold=on|adverse=off|longs=off | -12.1 (1657) | -13.4 (868) | 0.58 |
| volsetup=off|volhold=on|adverse=off|longs=on | -11.2 (3275) | -13.5 (1616) | 0.57 |
| volsetup=off|volhold=on|adverse=on|longs=off | -12.1 (1660) | -13.2 (875) | 0.58 |
| volsetup=off|volhold=on|adverse=on|longs=on | -10.9 (3294) | -13.2 (1631) | 0.57 |
| volsetup=on|volhold=off|adverse=off|longs=off | -12.3 (299) | -8.0 (155) | 0.75 |
| volsetup=on|volhold=off|adverse=off|longs=on | -13.5 (677) | -11.0 (312) | 0.68 |
| volsetup=on|volhold=off|adverse=on|longs=off | -11.6 (299) | -9.4 (157) | 0.69 |
| volsetup=on|volhold=off|adverse=on|longs=on | -12.1 (680) | -11.2 (316) | 0.65 |
| volsetup=on|volhold=on|adverse=off|longs=off | -9.4 (298) | -11.2 (155) | 0.65 |
| volsetup=on|volhold=on|adverse=off|longs=on | -9.2 (676) | -10.2 (312) | 0.70 |
| volsetup=on|volhold=on|adverse=on|longs=off | -10.6 (298) | -12.6 (157) | 0.58 |
| volsetup=on|volhold=on|adverse=on|longs=on | -8.7 (679) | -10.3 (316) | 0.68 |

**Holdout (volsetup=on|volhold=off|adverse=off|longs=off):** 161 trades, **-13.2 net bps per trade**, PF 0.59, win rate 35%, bootstrap 90% -19.8 to -5.9. BTC -10.8 (61), ETH -14.6 (100). Exits: {'stop': 105, 'target': 56}. Average -0.07R before costs; median stop 29 bps.

## Reading
- **The volume filter is the one piece that helps.** It cuts trades by about 5x and lifts validation from about -14 to about -8 to -11 bps. That still loses.
- **Holding through high-volume breakdowns doesn't help on average.** Big breakdowns like your 01:35 candle are rare, and most "through the target" moves bounce back.
- **No mechanical version beats Delta's costs.** Fees and slippage are about 14 bps per round trip, and the stops on these setups are only 20-35 bps, so a winner has to run far just to break even.
- **What would change the answer:** your own calls tracked forward with real costs, or the same setups on 15m/1h, where stops are wider than the fees. Both are research only; neither touches live trading.

Numbers: `reports/lower_high_volume.json`.
