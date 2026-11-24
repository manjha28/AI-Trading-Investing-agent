**None of the 12 family-and-symbol combinations passed, and none came close on net P&L.** Every holdout result is negative before any stress is applied.

**Why: costs are larger than the edge at this timescale.** A taker round trip costs 0.10% in fees plus 18% GST on them, which is **11.8 bps**, before slippage (about 1 to 3 bps more) and funding. Even a perfect maker-in, maker-out round trip costs 4.7 bps. The gross edge the families found, before any cost, was:

| Family | Gross edge (bps/trade, holdout) | Cost paid (bps/trade) |
|---|---|---|
| F1 compression breakout | +5.6 BTC, +3.7 ETH | about 12.8 to 12.5 |
| F2 liquidation fade (1-min candles) | −3.3 BTC, −7.5 ETH | about 6 to 6.3 (maker entry) plus larger stop losses |
| F3 funding contrarian | +7.8 BTC, −0.9 ETH | about 9.2 BTC, 8.7 ETH |
| F4 time-series momentum | +3.0 BTC, +0.7 ETH | about 9.3 BTC, 9.3 ETH |
| F5 session effects | +1.2 BTC, +5.0 ETH | about 13.8 BTC, 13.8 ETH |
| F6 candle-direction control | −0.2 BTC, −0.3 ETH | about 13.8 BTC, 13.8 ETH |

- **The control behaves as designed.** F6 has no edge (gross ≈ 0) and loses almost exactly the cost per trade (−14 bps/trade, win rate 4 to 7%, profit factor 0.02 to 0.03). That is the cost model working: a zero-edge strategy pays the round-trip cost.
- **F3 is the nearest miss** (BTC −1.4 bps/trade, −$13 on 89 trades; it also fell below the 100-trade minimum). It is the one family with a gross edge (7.8 bps) close to its costs, because it trades rarely, enters with maker orders and holds for hours so fees are spread over larger moves. It still lost money, was positive in only 3 of 9 holdout months, and needs more months of data to say anything about it.
- **F1 and F4 have small positive gross edges that taker fees erase.** F1's 2R target works as a maker exit, but its entries are taker breakouts.
- **F2 loses even gross.** The one-minute candle cannot place a limit "0.3 to 0.6 sigma further" the way a 1-second book could: a 66% win rate is paired with losses 4 times the size of wins, and the strategy is almost certainly mis-specified at this resolution. A faithful test needs tick data.
- **F5's 00:00 UTC entry never trades.** Every one of those signals falls inside the 2-minute funding window and is rejected by design (260 rejections on BTC), so only the 13:30 UTC entry runs.
- **No liquidation or gap-through-stop losses occurred**, and the stop-before-liquidation rule rejected only a handful of orders (a few F2 and F3 orders). Funding was small (under $3 per family), because holding periods are short and rates sit near 0.01% per 8 h.

**What this does not prove.** Slippage is assumed (1 bp, never under a tick), not measured; this setup cannot test F2 at 1 second or F6 on true order flow; your own fee tier could not be read (no fills on the account). A single top-of-book snapshot taken during the paper-engine test showed a 0.5 USD spread on BTCUSD (0.08 bps), so the 1 bp assumption looks conservative at small size, but one snapshot is not a measurement. The tests cover 2.9 years for BTC and 2.8 for ETH, one regime sequence, 28 variants per symbol.

**What would change the answer, in order of cost.** (1) Record the top of the book and the trade tape for a few weeks (the paper engine already records top-of-book each minute, which is the missing input for a measured slippage model). (2) A new, separately pre-registered maker-only version of F1/F3/F4: it would be a new family with its own variant count, and I have not run it, so as not to tune on this holdout. (3) Longer holds or wider targets, which dilute fixed costs but need many more months of data to test.
