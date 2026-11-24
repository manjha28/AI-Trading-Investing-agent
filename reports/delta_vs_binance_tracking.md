# Delta India vs Binance USD-M: 1-minute close tracking

Level difference = Delta close / Binance close − 1 on matching 1-minute bars (bps). Binance prices are USDT-quoted, Delta's are USD: part of the gap is the USDT/USD basis, which is not a trading edge.

| Symbol | Overlap | 1-min bars | Median |Δ| (bps) | p99 |Δ| (bps) | Median signed Δ (bps) | Median |return diff| per minute (bps) | Return correlation |
|---|---|---|---|---|---|---|---|
| BTCUSD | 2023-12-29 to 2026-11-22 | 1,495,371 | 1.01 | 42.99 | 0.96 | 0.13 | 0.9521 |
| ETHUSD | 2024-02-06 to 2026-11-22 | 1,465,682 | 1.87 | 21.75 | 1.68 | 0.55 | 0.9382 |

The median level gap is at or below 3 bps, so Binance prices are a reasonable stand-in for Delta's on 1-minute bars (the minute-to-minute return difference above is the more relevant number for fills).
