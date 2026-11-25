# All crypto strategies on Delta's own data: one table (25 Nov 2026)

*Hypothetical backtests on completed bars; kept apart from real P&L. Decision support, not investment advice. Live trading is OFF (paper only since 25 Nov).*

- **Costs:** Delta fees + GST plus slippage everywhere; funding where Delta's series exists.
- **Split:** each strategy is split by time into train / validation / final check (holdout). Variants are chosen on validation, and the holdout is used once.
- **Data:** the 1-minute set is 10 coins (BTC, ETH, SOL, LISTA, XRP, EVAA, AIOT, BLESS, ARIA, AKE, from their Delta listing; BTC/ETH from about Jan 2024). The 15-minute set is 183 coins.
- Full per-variant tables are in `reports/delta_runs/*.json` and the linked reports.

| Strategy (data) | Variants | Best on validation | Final check (holdout), net per trade | Verdict |
|---|---|---|---|---|
| **R3 1h quiet-range breakout, FROZEN** (1m, 10 coins) | 1 + 17 | n/a (pre-registered) | **+10.3 bps** (173 trades; longs +27.8, shorts -7.3); train +50.3, validation +57.8 | **Only consistent positive.** Keep the BTC/ETH paper test running. Funding not counted. |
| R3 variations (same data) | 17 | 24h range, quiet <=5%, 3R: validation +46.3 | ranges from -34 to +30 bps; most 24h/48h versions positive, every 12h version negative | 24h range is the stable choice; do not retune on this |
| 5 EMA alert candle, entries/exits/trend filters (1m, 10 coins) | about 750 in total | several | none whose 90% range excludes zero; live rule about -14 to -18 bps | **No edge**; keep paper only |
| 5 EMA pullback entry vs close | 24 | halfway pullback | about -9 vs -14 bps | better but still a loss |
| Owner's trend + rejection rule (1m, 10 coins) | 6 | 15m, trail 15m | **-9.4 bps** (2,871) | no edge |
| Owner's lower-high shorts + volume (1m, 10 coins) | 16 | volume filter + hold + adverse exit + longs | **-17.8 bps** (770) | no edge |
| VWAP band scalps, BTC 1m | many | fade +2σ | +2.3 bps, range -12 to +17 | too thin vs 12-14 bps costs |
| Pump ride, 15m/30m pump bars (15m, 183 coins) | 32 | 15m, 20x vol, +10% bar | -0.7% per trade | no edge |
| BTW absorption → breakout, trail fixed (15m, 183 coins) | 16 | 40% decline, 5x vol, 1-day-low trail | **-0.93% per trade** (258) | no edge |
| Dump short after the first failed hour (1h, 146 coins) | 6 | close below the pump candle's middle | -0.45% (train +1.8%, validation +2.3%) | direction real, edge not proven; paper candidate |
| Buy before the pump (1h, 183 coins) | 5 | — | -0.4% | not predictable |

**Running total of variants tried across today's crypto research: roughly 1,100.** With that many tries, a single positive holdout can be luck. R3 is the exception because it was frozen BEFORE today and has now been positive on Binance and on Delta, train through holdout.
