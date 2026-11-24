# Overnight run 24-25 Nov 2026

_Decision support, not investment advice._ Updated after every item.

## Morning summary (2 minutes)

| Item | Status | Evidence | You decide |
|---|---|---|---|
| Live-state baseline | Done | see "Live baseline" below | – |
| A1 Delta watch outage | Partly done (code + tests; Fly deploy pending) | `CryptoWatch.test.tsx` "Crypto watch outage" | – |
| A2 Journal est vs actual | Partly done (causes found + fixed + tests; 23/24 Nov reconcile NOT verifiable) | `test_dhan_charge_estimate.py` (3 new), `test_dhan_reconciliation.py::..without_portfolio_id` | Run a backfill on Fly after deploy; see A2 |
| A3 to A6, B, C, D | Not started yet | – | – |

## Live baseline (24 Nov ~18:30 UTC, read-only query of the Fly DB; nothing changed)
- **Delta**: deployment flag `DELTA_LIVE_EXECUTION_ENABLED=true` on finbyte-nexus-api; per-user state ARMED (armed 17:59 UTC, caps $25/day loss, 6 trades/day, 2 open, $2000 notional). Live tests: ONE, `5 EMA BTCUSD 5m` (BTCUSD, 5m, 1 contract, `execute_live=true`). Wallet balance not queried (needs the Delta key; you said ~$3).
- **Dhan**: deployment flag `DHAN_LIVE_EXECUTION_ENABLED=true`; per-user state DISARMED (disarmed 08:50 UTC); no Dhan forward test has `execute_live`. 4 Dhan paper tests (2 active, 2 paused).
- Fly API runs TWO machines (82dd029b77e668, e82dd02db14718), both version 34.
- Delta live orders overnight: listed at the end of the run.

## A1. "Delta research store is unavailable"
**Real cause (not the lab app).** The research store is a SQLite file at `/tmp/delta_research.sqlite3`, local to one machine. `app/main.py` starts the collector only on the scheduler leader (Postgres advisory lock). The API app runs two machines; the non-leader (82dd029b77e668) has no file, so `_read_store` raises 503. Fly alternates requests between machines, so every other poll failed (logs: 503/200 alternating on `/delta-research/markets`). The 1-second poll in `CryptoWatch` turned that into a flickering alert.
**Fix.** Backend: the collector records its machine id in `app_flags`; a machine without the file answers 503 with `fly-replay: instance=<holder>` so Fly re-sends the request to the machine that has the data (no loop: only a non-holder replays). Frontend: one inline banner ("Research data unavailable since HH:MM IST · showing last data from HH:MM IST · Retry"), cleared on recovery; backoff 2/5/15/60 s capped, reset on success; no polling while the tab is hidden. `CryptoMarkets` (15 s poll) got the same hidden-tab pause and a 60 s backoff.
**Other polling surfaces** (not changed, listed for you): ForwardTest.tsx, VarIntraday.tsx (3 s), JobsScanner.tsx (3 s), Overview.tsx (30 s x3), FeedToggle (15 s), AlertsBell, DerivativesData (15 s), DhanStatusRow (60 s), BackfillPanel, DailyReport. They show inline errors, not stacked toasts; the app has no toast calls in use (`useToast` has no callers).

## A2. Journal: estimated charges after actuals
**Findings (Fly DB, read-only).** 23 Nov: 340 Dhan fills, 153 with real charges, 187 with none; 153 orders have a MIX (some fills charged, some not). 24 Nov: no rows on Fly at all (your backfill ran against your local DB, which is down in this Codespace, so today's rows could not be inspected).
**Why "est" stayed (3 real defects, all fixed):**
1. Estimator charged brokerage again on the uncharged fills of an order that already had real charges on other fills (double count; those legs kept showing "est"). Now an order with any real charge is never estimated; its other fills stay unknown (`—`), not invented.
2. `_dhan_trade_rows` kept the live-API copy of a fill over the CSV copy even when only the CSV copy had real charges. Now the copy with real charges wins.
3. The upsert wrote `charges=None` over stored real charges when a later sync (day trade book, no charges) re-saw the fill. It now never erases real charges.
Estimates are never stored (computed on display copies at read time), so the backfill needs no recompute step; the Journal already refetches trades, analytics and breakdown after a backfill. Rule "actual replaces estimate everywhere" holds because rows, KPI, summaries and exports all read these same rows. "est." tooltip already says what is estimated.
**Reconciliation 422.** Cause: `brokers.router` registers `/{slug}/reconciliation` (requires `portfolio_id`) before the Dhan-specific `/brokers/dhan/reconciliation`, so the generic route shadowed it. Sending `portfolio_id` would have returned the wrong payload (a differences list), so I fixed the route order in `main.py` instead (deviation from your instruction, on purpose). Test added.
**23/24 Nov vs Dhan realised: NOT verified.** On Fly the build returns 0 matched trades and no Dhan snapshot for both days, and 24 Nov fills are not on Fly. After deploy, run Backfill + Dhan sync on Fly, open "Reconcile with Dhan" and read the gap.
