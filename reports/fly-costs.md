# Fly.io cost review (Step 1 done, nothing changed)

Written 25 Nov 2026. No machine, volume, IP, secret or database was changed. Prices below are **estimates**: I could not read Fly's official price list from this environment, so the per-item figures come from your 18-24 Nov usage lines and third-party summaries (Managed Postgres Basic about $38/month plus storage, static egress IP about $3.60/month, volumes about $0.15 per GB/month, shared-cpu-1x 256 MB about $2.2/month). Check the billing page before relying on any number.

## Inventory (account `personal`, region `sin` for everything)
| App | Machine | Size | State | What runs | Volume | IPs |
|---|---|---|---|---|---|---|
| finbyte-nexus-api | `e82dd02db14718` (leader) | shared-cpu-4x, 4 GB | started | FastAPI + ALL scheduled jobs (leader lock), Delta research collector, forward tests incl. the live Delta test | none | dedicated egress IPv4 209.71.68.242 + IPv6 egress, public ingress |
| finbyte-nexus-api | `82dd029b77e668` (follower) | shared-cpu-4x, 4 GB | started | FastAPI only (no jobs: not the leader) | none | same app |
| finbyte-nexus-web | `2870e37b393498` | shared-cpu-1x, 256 MB | started | nginx + built frontend | none | shared ingress |
| finbyte-delta-lab | `7812734f223058` | shared-cpu-1x, 1 GB | started | order-book recorder + frozen R3 paper test | `delta_lab` 3 GB | none |
| Managed Postgres (not an app) | cluster | Basic plan | running | the only database | its own storage | private |
There are **no `iad` machines any more** (the ones you saw billed were already gone). Total machines: 4. The Managed Postgres Basic plan (about $38/month) is the biggest single item, then the two 4 GB / 4-CPU API machines.

## Measurements (what I could and could not get)
- **Database:** 841 MB, PostgreSQL 17.12. Largest tables: `market_bars` 417 MB, `option_contracts` 79 MB, `dhan_market_ticks` 49 MB, `instrument_research_briefs` 45 MB, `news_items` 43 MB. `pg_stat_statements` is not installed on the managed database, so no slow-query list.
- **Memory right now (snapshots, 3 minutes after the deploy, so NOT peaks):** leader's uvicorn 851 MB RSS, follower's 490 MB, lab's python 120 MB of its 1 GB. Load average 0.04 to 0.83 on 4 CPUs. Peak memory over the last days could not be read (Fly's metrics endpoint refused my token, and the machines have no cgroup peak file).
- **Speed baseline: NOT recorded.** Every main endpoint (daily report, market watch, journal, multibagger, crypto markets, Delta watch) needs a logged-in 2FA session, which I do not have overnight. Only unauthenticated numbers exist (healthz about 0.1-0.3 s). Without a baseline the speed gate in Steps 2 and 4 cannot be evaluated.
- Daily job and nightly ingest durations: not measured (same reason, plus no job-duration log table to read).

## Decision: Steps 2 to 4 skipped, with reasons
Your rule: if a step cannot meet all three conditions, skip it. The database move (Step 2) needs the speed gate (no baseline), a peak-based sizing (no peak data), a cutover window with the Delta live test idle, and a tested rollback, and it carries the real risk of data loss on the one database. Doing it unattended and unmeasured would break condition 1 and 2. Right-sizing (Step 4) also needs peak memory. Step 3 (one region) is already true: everything is in `sin`.

## Cheapest SAFE setup I recommend, and what each step saves
1. **Remove the follower API machine** (`82dd029b77e668`). It runs no jobs, the leader holds the lock and the Delta store, and a single user's load is tiny. Saves about one of the two largest compute items (roughly $10-15/month on your numbers). Trade-off: no second machine to take over during a crash or deploy (a deploy then has about a minute of downtime). Rollback: `fly machine clone e82dd02db14718 -a finbyte-nexus-api` or `fly scale count 2 -a finbyte-nexus-api`. **I tried to do this and the tool permission layer blocked a machine destroy, so it is NOT done; run it yourself if you agree:** `~/.fly/bin/flyctl machine destroy 82dd029b77e668 -a finbyte-nexus-api --force`.
2. **Then right-size the leader** only after you give me a logged-in session (or a week of memory data): its RSS is about 0.9 GB, so 2 GB would likely fit, but the research daily job and the multibagger run may spike; do not shrink on a snapshot.
3. **Keep the static egress IP** until you confirm in the Dhan and Delta dashboards whether order APIs need a whitelisted IP (Delta live is on, so I did not release it). Saves about $3.60/month if it turns out to be unnecessary.
4. **Managed Postgres to self-run Postgres (about $38 to roughly $8-10):** worth doing, but as its own supervised session: baseline first with a login, dump-restore rehearsal with row counts, speed gate, one-command rollback script, Tigris backups. It is the only step that gets the bill near $20. I did not start it.

## Estimated totals
- Now (full month): about $65-70, as you computed (Postgres about $38, two API machines about $20-25 together, lab about $3-4, web about $2, IP about $4).
- After step 1 only: about $55 to $58.
- After steps 1 and 4 plus right-sizing: about $20-25. Under $20 is not reachable without the database move and a smaller API machine; under $10 would also need the lab merged into the API machine, which would stop the order-book recorder or the frozen R3 paper test during API restarts, so I recommend against it.
Nothing from Steps 5 to 7 was applied.

## What is left for you
- Decide on step 1 (destroy the follower) and run the command above if yes.
- Give me a logged-in session or 10 minutes at your keyboard so the speed baseline can be recorded, then I can do the Postgres move properly.
- Confirm whether Dhan or Delta whitelists your egress IP.
- Do not delete the Managed Postgres cluster until a self-run one has been live for 7 days.
