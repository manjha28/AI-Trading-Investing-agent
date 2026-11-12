# 01 — Intraday infrastructure audit (verified against code and the dev database, 2026-11-05)

Method: read the relevant code and queried the dev Postgres. "Measured" = queried; "Code" = read in source; nothing was assumed.

## Capability matrix

| Capability | Status | Source (file) | Limits / notes | Reuse |
|---|---|---|---|---|
| Stored intervals | Schema allows `1d,1h,15m,5m,1m` (CHECK `ck_market_bars_interval`); unique `(instrument, interval, timestamp, source)` | `models/__init__.py` MarketBar | Real bars only (`is_synthetic=false` CHECK); OHLC and volume CHECKs | Yes |
| Syncable intervals | **Only `1d` and `1m`** are written by the Dhan sync; other intervals are derived on read | `broker_adapters.py:1355`, `api/market_data.py:1162,8913` | Stored 5m/15m/1h rows (yfinance/dhan/delta) exist but are legacy/partial | 1m is the base |
| 1m history provider | Dhan `/v2/charts/intraday`, ≤90 days per request, up to ~5y; backfill cap 250k bars | `broker_adapters.py:1359`, `api/market_data.py:126` | Angel One/yfinance adapters list 1m but their stored bars are absent | Yes (Dhan only) |
| Provider timeframe tables | angel_one 1d/1h/15m/5m/1m; dhan 1d/1m/5m/15m/25m/1h; yfinance 1d/1h/15m/1m (7-day lookback); delta 1d/1h/15m/5m/1m | `api/market_data.py:2131` | Priority order unchanged (Angel One → Dhan → Yahoo) | Yes |
| Timeframe aggregation | `_aggregate_intraday`: venue-session-aligned buckets from 1m; marks `is_complete`; **omits** buckets with missing 1m rows; reports gaps and coverage | `api/market_data.py:3050` | Server cache of 8; endpoint-bound (not a library function) | Extract into a service for the engine (see gaps) |
| Session calendar | Weekends and `NSE_NON_TRADING_DATES` skipped; session open/length per venue | `config.py:182-212`, `api/market_data.py:3203` | **Holiday calendar only covers 2026** (`NSE_CALENDAR_YEARS={2026}`); special sessions in `NSE_SPECIAL_SESSION_DATES` | Extend before backtesting 2024–25 |
| Live stream | Dhan live-feed hub → `/instruments/{id}/stream`, `/watchlists/{id}/stream`, depth; reconnect + stale handling; poller fallback | `api/market_data.py:6719,7278`, `market_stream/*` | Live path persists to ledger; not needed for backtests | Display only |
| Gap tracking and repair | `MarketDataGap` opened on disconnect, closed on reconnect, then 1m `_sync_dhan_history` back-fills | `api/market_data.py:840-880` | Measured: 551 gaps, 0 open, 537 back-filled | Yes |
| Backfill API | `POST /backfill/intraday/watchlist`, `/instruments/{id}/history/backfill`, job status endpoint | `api/market_data.py:9320,9852,9308` | Dhan-only | Yes |
| Indicators | `indicator_library`: SMA, EMA, RMA, WMA, VWMA, TEMA, DEMA, TMA, ALMA, HMA, LSMA, ZLEMA, RSI (Wilder), ATR, session VWAP, volume expansion; `minimum_bars_for`; Decimal output | `services/indicator_library.py` | **No ADX, Bollinger Bands, MACD, Stochastic, SuperTrend, OBV, opening range.** `session_vwap` must be fed one session at a time | Reuse; add missing ones |
| Strategy contract | `StrategySpecification`, `StrategyContext(as_of, frames[tf])`, `completed_frame(tf)` returns only completed bars; timeframes 1m…1d | `services/strategy_contract.py` | Daily-only adapters registered (SMA crossover, SMA+RSI) | Yes — completed-bar discipline already enforced |
| Strategy persistence | `Strategy`, versions, activate/suspend/retire, signals, evaluate, backtest endpoints; `/catalog`, `/data-availability` | `api/strategies.py`, `Strategies.tsx` | Params are a fixed Pydantic model (`StrategyParams`), not a rule schema | Reuse versioning/lifecycle, replace the param model |
| Backtester | Daily, next-open entry, ATR stop / R-multiple target, bps costs and slippage, stop-first on stop+target same bar, gap fills, ambiguity counter, synthetic-bar rejection | `services/backtest_engine.py` | Daily only, single position, long+short flag, no sessions/EOD/partial fills/taxes | Reuse conventions; engine is rewritten event-driven |
| Trade analytics | Net P&L, win rate, profit factor, expectancy, max drawdown (Decimal) | `services/trade_metrics.py` | No exposure, holding time, Sharpe/Sortino, daily/monthly returns | Extend |
| Charts | `CandleChart.tsx` (lightweight-charts style), venue time-zone handling per interval | `components/CandleChart.tsx` | Display; reuse for trade markers | Yes |
| Watchlists | CRUD + stream | `api/market_data.py:5765-5960` | Owner-scoped | Yes (instrument selection) |

## Measured data state (dev DB)

* 1m bars: **1,241,388** rows, Dhan only, **9 NSE equities** (SBIN, RELIANCE, RPOWER, RIIL, PREMIERPOL, PCJEWELLER, UJJIVANSFB, GROWW, JPL), 2024-11-04 → 2026-11-05. Only 4–6 of them have ~2 years; UJJIVANSFB/GROWW ~1 year; JPL 8 days. No crypto/Delta 1m, no US 1m.
* Session shape: first minute 09:15, last 15:29 IST (375 per full day). Zero-volume minutes: 0.00% (SBIN).
* **Incompleteness (SBIN, 497 days):** 183 full days (370–375 bars), 312 days with 300–369 bars, 2 with <300. About 63% of days have missing minutes. Aggregation drops incomplete buckets rather than interpolating — correct, but a backtest on this data must treat gaps explicitly.
* Duplicates: **0** (unique constraint). Weekend rows: 5,026, all on 2025-02-01 and 2026-02-01 (Union-Budget special sessions) — legitimate, so a naive "no weekends" filter would wrongly discard them.
* Open gaps: 0.
* Other stored intervals are sparse (5m 5k rows, 15m ~5k, 1h ~2.7k) and not a basis for backtests.

## Gaps for the strategy lab (ranked)

1. **Holiday/special-session calendar for 2024–2025** (only 2026 exists). Without it, expected-bar logic mislabels holidays as gaps.
2. **Aggregation as a service** (currently inside the API module and cached by request) so the engine and API share one implementation.
3. **Indicators:** ADX, Bollinger Bands, MACD, SuperTrend, opening-range high/low, previous-day high/low/close, session volume profile.
4. **Gap policy**: ~63% of days have missing minutes; decide per strategy whether to skip the day, skip the bucket, or abort (default: skip bucket, record, never interpolate).
5. **Universe breadth**: 9 instruments with long 1m history. Backfilling a larger NSE universe needs Dhan quota and time (one watchlist job exists).
6. **No tick/depth history** — in-candle ordering can only be resolved by 1m bars (see doc 03).
7. **Corporate-action adjustment** of intraday history is not verified (the `corporate_action_service` exists; adjustment of stored 1m bars was not checked and must be before long-window tests).
8. **Costs model**: only flat bps today; no STT/GST/stamp/SEBI/DP charges.

## Not verified
UI controls for backfill and for timeframes in `MarketData.tsx` were not exercised in a browser; endpoints were read, not called.
