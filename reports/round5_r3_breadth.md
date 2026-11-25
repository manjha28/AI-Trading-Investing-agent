# Round 5: frozen R3 on 20 more perpetuals (25 Nov 2026)

*Hypothetical backtest on completed historical 1-minute bars; kept apart from real P&L. Decision support, not investment advice.*

**Rule:** R3 exactly as frozen (hash `eaea623c741f`), not tuned: 1h bars, the 24-bar range in the lowest 10% of its last 1440 bars, stop entries both sides valid 60 min, stop at the other side of the range, 3R target, 48 h time stop. Risk gate: 10x isolated, 0.5% risk. **Costs** are the frozen Delta model: taker 0.05% / maker 0.02%, 18% GST, 1 bp slippage, funding at the real Binance rate.
**Data:** Binance USD-M 1-minute klines from data.binance.vision (checksum-verified), Jan 2023 (or the listing date) to 24 Nov 2026. The Delta contract spec is used where Delta lists the coin; GUA uses a Delta-cost spec. The mark price is taken as the last price, which only affects the liquidation check.
**Universe** (from today's 24 h turnover, so it is biased towards coins that are liquid NOW): Delta's top 10 USD perpetuals and Binance's top 20 USDT perpetuals, crypto only (gold, silver and stock contracts excluded). BTC and ETH are left out of the pool because R3 was chosen on them; they are shown on their own holdout below.
**Reproduction check:** the BTC and ETH holdout below reproduces round 2 exactly: 101 trades at 38.94 bps and 94 at 51.71.

| Symbol | From | Trades | Net bps/trade | PF | Long trades / bps | Short trades / bps | Quarters positive |
|---|---|---|---|---|---|---|---|
| SOLUSD | 2023-01-01 | 199 | 48.7 | 1.4 | 114 / 28.9 | 85 / 75.3 | 11/16 |
| EVAAUSD | 2025-10-03 | 17 | 39.3 | 1.2 | 7 / -211.3 | 10 / 214.8 | 1/5 |
| AIOTUSD | 2025-04-30 | 15 | 202.6 | 2.4 | 8 / 112.0 | 7 / 306.1 | 2/3 |
| BLESSUSD | 2025-09-23 | 5 | 230.2 | 1.9 | 1 / 1074.8 | 4 / 19.1 | 1/2 |
| LISTAUSD | 2024-06-20 | 74 | 25.4 | 1.1 | 39 / 15.2 | 35 / 36.9 | 6/10 |
| ARIAUSD | 2025-09-03 | 13 | 164.5 | 2.2 | 3 / 383.8 | 10 / 98.8 | 3/5 |
| XRPUSD | 2023-01-01 | 236 | 30.0 | 1.1 | 111 / 24.1 | 125 / 35.2 | 9/16 |
| AKEUSD | 2025-09-26 | 11 | 26.2 | 1.1 | 5 / 209.0 | 6 / -126.2 | 2/4 |
| ZECUSD | 2023-01-01 | 157 | 69.7 | 1.5 | 82 / 76.7 | 75 / 62.1 | 11/16 |
| HYPEUSD | 2025-05-30 | 57 | -6.7 | 0.9 | 29 / -70.2 | 28 / 59.2 | 2/6 |
| NEARUSD | 2023-01-01 | 155 | -49.6 | 0.8 | 80 / -57.3 | 75 / -41.3 | 6/16 |
| BNBUSD | 2023-01-01 | 216 | 20.4 | 1.3 | 118 / 2.9 | 98 / 41.4 | 12/16 |
| DOGEUSD | 2023-01-01 | 266 | 40.0 | 1.2 | 124 / 22.1 | 142 / 55.6 | 9/16 |
| 1000PEPEUSD | 2023-05-05 | 123 | 33.3 | 1.3 | 63 / 36.6 | 60 / 29.8 | 6/13 |
| MMTUSD | 2025-11-04 | 33 | 85.3 | 1.6 | 14 / -31.2 | 19 / 171.2 | 3/4 |
| ENAUSD | 2024-04-02 | 42 | 136.2 | 1.8 | 17 / 40.2 | 25 / 201.5 | 5/9 |
| ADAUSD | 2023-01-01 | 250 | -16.2 | 0.8 | 126 / -30.5 | 124 / -1.8 | 6/16 |
| GUAUSD | 2025-12-21 | 34 | -105.9 | 0.6 | 15 / -194.7 | 19 / -35.8 | 1/4 |
| SUIUSD | 2023-05-03 | 148 | -9.4 | 1.0 | 74 / -16.8 | 74 / -2.0 | 7/14 |
| UNIUSD | 2023-01-01 | 242 | -0.7 | 1.0 | 122 / -14.6 | 120 / 13.4 | 8/16 |

**Pooled (20 symbols):** 2293 trades, **20.6 net bps per trade**, PF 1.13, bootstrap 90% CI **7.2 to 34.4 bps** (trades resampled independently: this ignores trades clustered in the same market moves, so the true interval is wider).
- Long: 1152 trades, 4.1 bps (90% CI -14.8 to 23.1): **not distinguishable from zero**.
- Short: 1141 trades, 37.2 bps (90% CI 17.9 to 56.9).

**Quarterly, pooled (net bps per trade):** 2023-Q1 179 (40), 2023-Q2 -52 (171), 2023-Q3 -8 (186), 2023-Q4 55 (64), 2024-Q1 16 (90), 2024-Q2 -20 (165), 2024-Q3 30 (139), 2024-Q4 -34 (48), 2025-Q1 99 (136), 2025-Q2 52 (130), 2025-Q3 52 (151), 2025-Q4 21 (181), 2026-Q1 97 (228), 2026-Q2 -29 (205), 2026-Q3 -10 (237), 2026-Q4 6 (122). 10/16 quarters positive. No quarter was used to choose anything: the rule is frozen, so every quarter is out of sample for these symbols.

**BTC/ETH holdout (from 2025-03-03), long vs short:**

| Symbol | Trades | Net bps | PF | Long trades / bps | Short trades / bps |
|---|---|---|---|---|---|
| BTCUSD | 101 | 38.9 | 1.5 | 60 / 37.6 | 41 / 40.9 |
| ETHUSD | 94 | 51.7 | 1.5 | 61 / 32.8 | 33 / 86.6 |

## Does it hold?
**Partly. It is weaker than on BTC/ETH and fading recently, so I would not call it confirmed.**
- Pooled it stays positive (20.6 bps, CI above zero), but at about half the BTC/ETH holdout (39-52 bps).
- The 10 symbols with full history since 2023 split 6 positive and 4 negative: SOL, ZEC, DOGE, PEPE, XRP and BNB win; NEAR, ADA, SUI and UNI lose.
- The big numbers come from young listings with 5-42 trades (AIOT, BLESS, ARIA, ENA, MMT). Those samples are too small to count.
- **The edge is almost all on the short side** (37 bps, CI 18 to 57). Longs are about zero (4 bps, CI -15 to 23). On BTC/ETH both sides were positive.
- **The last three quarters are weak:** 2026-Q2 -29, 2026-Q3 -10, 2026-Q4 +6 bps. The positive pooled figure leans on 2025 and 2026-Q1.
- Bias: the universe was picked by today's liquidity (survivorship). Delisted coins are not in it.
- **Implication for the paper test:** keep R3 on BTC/ETH as pre-registered. These results give no reason to widen it to more coins, or to trust the long side on alts. The 50-trade go/no-go on BTC/ETH stays the decision point.

Code: `backend/app/delta_lab/round5_r3_breadth.py`; numbers: `reports/round5_r3_breadth.json`.
