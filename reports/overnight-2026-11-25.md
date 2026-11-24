# Overnight run 24-25 Nov 2026

_Decision support, not investment advice._ Updated after every item.

## Morning summary (2 minutes)

| Item | Status | Evidence | You decide |
|---|---|---|---|
| Live-state baseline | Done | see "Live baseline" below | – |
| A1 Delta watch outage | Partly done (code + tests; Fly deploy pending) | `CryptoWatch.test.tsx` "Crypto watch outage" | – |
| A2 Journal est vs actual | Partly done (causes found + fixed + tests; 23/24 Nov reconcile NOT verifiable) | `test_dhan_charge_estimate.py` (3 new), `test_dhan_reconciliation.py::..without_portfolio_id` | Run a backfill on Fly after deploy; see A2 |
| A3 MCX closed-market columns | Partly done (code + 3 tests; needs Fly deploy and a look at the live page; no MCX-open check possible overnight) | `test_mcx_calendar.py` (3 new) | – |
| A4 Confidence proof | Done | `diagnostics/nexus-v2/overnight_a4_confidence_proof.py` (+`.json`); `test_win_confidence.py` | Answer below: No ML in use, score does not predict outcomes |
| A5 Multibagger price filter | Done (live pass pending deploy) | `Multibagger.test.tsx` (3 new), `test_multibagger_api.py::..latest_real_daily_close..` | Universe widening (below) |
| A6, B, C, D | Not started yet | – | – |

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

## A3. MCX watch when closed
Closed-market ticks now carry last price (labelled close), change and % vs previous close, open, high, low, volume, open interest, time of the last stored tick, and a "Market closed · last close <date time IST>" stamp above the table. Source order per contract: (1) snapshot stored from live ticks (new table `mcx_last_snapshots`, migration 0077, throttled to one write per 30 s per contract, forced at session end); (2) Dhan quote endpoint + Dhan daily history (`/v2/charts/historical`, `oi: true`) for the last completed session; (3) unknown shown as "—", never zero. Yahoo is not used (Dhan had the data path). Contract roll: on a contract's expiry day, after the session closes, it is treated as expired and the next month is shown. Source is logged in the tick (`source`).
Tests: weekend, after 23:30 (US summer) and 23:55 (US winter) closed, 23:50 winter still open; Decimal change recompute and unknown-stays-unknown; snapshot save/throttle/force/load. NOT tested: the websocket end to end and the expiry-day roll against a DB (no Postgres in this Codespace; `alembic check` could not be run here, the migration is a plain create_table and `alembic heads` shows a single head 0077).

## A4. Is the confidence score real? (plain answer)
**Is any ML model used for confidence? No.** **Does the score predict outcomes? No** (on the latest holdout nothing beat the base rate in a way that holds up).
**What produces each number the app shows** (all hand-written or lookup, nothing trained at runtime):
1. *Win rate "N% · n past setups"* (`services/win_confidence.py`, called from `stock_verdict._win_confidence`): a **lookup of similar past setups** (tercile-binned setup/stack/rs60/dist52/stop%/regime/size, weakest feature dropped until >=30 matches) with beta-binomial shrinkage toward the base rate (k=50). Model file `win_confidence_model.json`, built from 1,915 replayed setups.
2. *Ring score / "factor rank"* (`services/research_confidence.py`, `universe_rank.py`): a **percentile of a hand-weighted five-factor rank** (or the older 0.40/0.25/0.20/0.15 formula). Not a probability.
3. *Setup grade A/B/C* (`setup_grade.py`): rules.
4. *Component confidence %* in the research report: data coverage x agreement, rules. *AI confidence %*: Gemini, separate, never mixed in.
**ML models in the repo:** only the LightGBM experiment scripts under `diagnostics/nexus-v2/` (`phase7b_lgbm.py`, and tonight's script). No weights are stored, nothing in `backend/app` imports lightgbm. Earlier result confirmed: positional holdout AUC **0.426** (match-rate 0.503), swing "too little data to run honestly" (112 holdout setups). The 0.557 is also confirmed: positional holdout AUC with the delivery-percentage feature added (Brier 0.2166 vs 0.2172 match-rate; top and bottom decile net R intervals overlap), so not qualified either.
**Tonight's proof** (script above; train earliest 50% / tune next 25% / holdout latest 25%, holdout touched once per family with the variant chosen on tune; 62 variants recorded in the JSON; 5 buckets, 90% bootstrap):
| Horizon | Holdout (n, dates, base win rate) | Best per family: AUC / Brier vs base | Qualifies |
|---|---|---|---|
| Swing | 112, 2025-01-28..2026-07-03, 18.8% | match-rate 0.553 / 0.1531 vs 0.1534; logistic 0.680 / 0.1816; LightGBM 0.695 / 0.1414; monotone LGBM 0.562 / 0.1622 | **none** (LightGBM beats base on Brier but top bucket net R is not above the bottom with separated intervals, and holds in 1 of 3 slices) |
| Positional | 311, 2024-12-16..2026-07-03, 29.9% | match-rate 0.497 / 0.2109 vs 0.2133; logistic 0.573 / 0.2371; LightGBM 0.526 / 0.2094; monotone 0.543 / 0.2082 | **none** (Brier gain is tiny, buckets overlap, 2 of 3 slices at best but no separation) |
| Long-term | not testable (4 decision dates, 12-month outcome) | – | – |
Isotonic/Platt were fitted on the tune window and then judged on the holdout; calibrated variants that win on tune are optimistic by construction, and they still did not qualify. Swing LightGBM is the one to watch (AUC 0.69 on 112 setups) but 112 setups is too few to trust; re-test when the forward record has data (first resolutions mid-December).
**Display rule applied.** No model qualified, so no flag was switched on. The match-rate number stays, but its label is now "Unproven · N similar setups" (was "High (not yet separating)"); High/Medium are never shown until a holdout or the live forward record shows separation. Changed `win_confidence.score`, `DailyReport.shownBand`, 3 tests.

## A5. Multibagger price filter
Presets Any price (default) / Below Rs 20 / 50 / 100 / 500 plus custom min and max; filters by the latest stored real daily close (new `last_close`, `last_close_date` per item from the API, `null` when no close is stored); a stock with no stored close is left out of any active price filter (unknown never passes). Works with class tabs, search, buy-zone, unknown and Q filters and with column sorting (new "Last close" column); shows "N of M stocks"; persists in the URL (`price_min`, `price_max`) and local storage (`mb.price`); the illiquidity note appears when a price filter is active and the tradability gates are unchanged. Empty result reads "No stocks below Rs 20 in the current universe."
**Not done:** LTP during market hours (would need a live quote for ~270 stocks; the stored close is used and labelled "Last close").
**Universe (decision for you).** The app's `instruments` table holds 414 NSE and 20 BSE equities, not Dhan's full equity master (about 2,000+ NSE EQ symbols; the master is not loaded into `instruments`, so I cannot give an exact excluded count tonight). The Multibagger screen assesses the same universe as the Daily report (295 rows in the latest run, 29 of them under Rs 20, 42 under Rs 50, 69 under Rs 100). Very low-price names outside that research universe are simply not in it. I did not widen it. Decision: do you want the universe widened to Dhan's full NSE EQ list (cost: more nightly work and many illiquid names that the gates will mark not_tradable)?
