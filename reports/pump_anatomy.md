# Pump anatomy: illiquid coins up 100-200% in 3-4 days (25 Nov 2026)

*Hypothetical research on historical prices; descriptive only, no strategy and no tuning. Decision support, not investment advice.*

**Data:** Binance USDT-M daily candles and funding (920 contracts, delisted included), Jan 2022 to Oct 2026, the round 4 D3 download. Code: `backend/app/delta_lab/pump_anatomy.py`, rows in `reports/pump_anatomy.json`.
**Event (fixed before looking):** the day-0 close is +100% to +200% above the lower of the closes 3 and 4 days earlier. Illiquid means a median daily quote volume under 5 M USDT over the 60 days ending 5 days before day 0. One event per coin per 30 days. Everything is measured from the day-0 close, the first moment the event is known.
**Sample:** 139 events on 118 coins. 6 more were excluded because volume went to zero within 7 days, which means trading was halted or settled and the price is frozen: MEMEFI, TANSSI, YALA, A2Z, DAM, HFT. **Almost all events are in 2025 (34) and 2026 (103).** Only 2 come from 2022-2024, so this is mostly a description of the last ~18 months.

## What happens after day 0 (percent from the day-0 close)

| Horizon | Median | Mean | Share below day 0 | p10 / p90 |
|---|---|---|---|---|
| +1 day | -11.2 | -4.0 | 64% | -32 / +35 |
| +3 days | -17.3 | -5.9 | 68% | -44 / +38 |
| +7 days | -24.7 | -9.5 | 74% | -50 / +44 |
| +14 days | -28.6 | -14.3 | 79% | -55 / +39 |
| +30 days | -34.9 | -16.7 | 79% | -66 / +42 |

**The squeeze side** is the highest high after day 0, measured against the day-0 close:

| Window | Median | p75 | p90 |
|---|---|---|---|
| next 1 day | +14.6% | +39% | +57% |
| next 3 days | +22.4% | +49% | +109% |
| next 7 days | +28.5% | +74% | +146% |

- Over the next 3 days, **24% of events went at least 50% higher and 11% went at least 100% higher.**
- **The peak is usually at or just after day 0.** The median peak close is 1 day later; it is day 0 itself in at least 25% of events, and 4+ days later in 25%.
- **Most events give back half the pump.** In the next 30 days, 81% fell to the halfway point between the base and day 0, with a median of 2 days to get there. 28% fell all the way back to the pre-pump base.
- **But most squeeze first.** 59% made a high above the day-0 high before reaching that halfway point.
- **Volume fades fast:** day 0 trades a median 192x the base volume, and by day 7 volume is down to a median 6% of day 0.
- **Funding gives no signal on day 0.** The median is about 0 and it is skewed negative (p10 -4.4%/day): shorts are often the ones paying.

## Reading
- The direction is clear: illiquid 2-3x pumps mostly deflate (74% are lower 7 days later, median -25%).
- **The path is the problem for a short.** The average move is much smaller than the median because of a fat right tail. 1 in 4 squeezes another 50% within 3 days, and most make a new high before deflating.
- A short entered at the day-0 close with a stop anywhere near the day-0 high would usually be stopped out first. That fits round 4 D3: its pump-short variants did not pass, and its parabolic short had 20 squeeze stop-outs in 56 trades.
- Any further test needs a rule written before looking at results, for example waiting for the first lower daily high after the peak, and fresh data.

**Not tested:** intraday timing (daily candles only), borrow and short availability on Delta for these coins (most are not listed on Delta India), open interest (no history).
