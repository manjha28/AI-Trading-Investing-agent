# Overnight run 24-25 Nov 2026

_Decision support, not investment advice._ Updated after every item.

## Morning summary (2 minutes)

| Item | Status | Evidence | You decide |
|---|---|---|---|
| Live-state baseline | Done | see "Live baseline" below | – |
| A1 Delta watch outage | Partly done (code + tests; Fly deploy pending) | `CryptoWatch.test.tsx` "Crypto watch outage" | – |
| A2 to A6, B, C, D | Not started yet | – | – |

## Live baseline (24 Nov ~18:30 UTC, read-only query of the Fly DB; nothing changed)
- **Delta**: deployment flag `DELTA_LIVE_EXECUTION_ENABLED=true` on finbyte-nexus-api; per-user state ARMED (armed 17:59 UTC, caps $25/day loss, 6 trades/day, 2 open, $2000 notional). Live tests: ONE, `5 EMA BTCUSD 5m` (BTCUSD, 5m, 1 contract, `execute_live=true`). Wallet balance not queried (needs the Delta key; you said ~$3).
- **Dhan**: deployment flag `DHAN_LIVE_EXECUTION_ENABLED=true`; per-user state DISARMED (disarmed 08:50 UTC); no Dhan forward test has `execute_live`. 4 Dhan paper tests (2 active, 2 paused).
- Fly API runs TWO machines (82dd029b77e668, e82dd02db14718), both version 34.
- Delta live orders overnight: listed at the end of the run.

## A1. "Delta research store is unavailable"
**Real cause (not the lab app).** The research store is a SQLite file at `/tmp/delta_research.sqlite3`, local to one machine. `app/main.py` starts the collector only on the scheduler leader (Postgres advisory lock). The API app runs two machines; the non-leader (82dd029b77e668) has no file, so `_read_store` raises 503. Fly alternates requests between machines, so every other poll failed (logs: 503/200 alternating on `/delta-research/markets`). The 1-second poll in `CryptoWatch` turned that into a flickering alert.
**Fix.** Backend: the collector records its machine id in `app_flags`; a machine without the file answers 503 with `fly-replay: instance=<holder>` so Fly re-sends the request to the machine that has the data (no loop: only a non-holder replays). Frontend: one inline banner ("Research data unavailable since HH:MM IST · showing last data from HH:MM IST · Retry"), cleared on recovery; backoff 2/5/15/60 s capped, reset on success; no polling while the tab is hidden. `CryptoMarkets` (15 s poll) got the same hidden-tab pause and a 60 s backoff.
**Other polling surfaces** (not changed, listed for you): ForwardTest.tsx, VarIntraday.tsx (3 s), JobsScanner.tsx (3 s), Overview.tsx (30 s x3), FeedToggle (15 s), AlertsBell, DerivativesData (15 s), DhanStatusRow (60 s), BackfillPanel, DailyReport. They show inline errors, not stacked toasts; the app has no toast calls in use (`useToast` has no callers).
