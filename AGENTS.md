# AGENTS.md — FinByte Nexus

## Product
A private, portfolio-aware research analyst for one user. It reports daily, with checkable
evidence: what to put on radar, what to trim or book profit in, what to hold and why, and what
unintended risk is being carried. It also provides a signals and strategy workbench (scanning,
backtesting, forward monitoring) for Dhan (NSE/BSE) and Delta Exchange India. Live order
placement exists only as the owner-approved, tightly scoped Dhan exception in "Execution policy".

## Spec of record
The "Signals & Strategy Intelligence Workbench" blueprint is the governing product specification
for signals, strategies, backtesting and monitoring. Where this file and the blueprint conflict,
the blueprint wins and this file MUST be updated to match. Report the conflict; do not silently
pick one.

## Signals, levels and strategies (in scope)
- Signals carry an entry zone, invalidation, initial stop and a target ladder. Every level is
  computed deterministically in Python from market data and shows its derivation (inputs,
  rule, parameters). A model never emits a price or level.
- Backtests replay completed historical bars only. Forward monitoring evaluates completed
  candles only. Neither may look ahead.
- A backtest states its data window, bar count, source, and cost assumptions. Results are stored
  separately from real portfolio data, are labelled hypothetical, and MUST NEVER appear in the
  same figure, total or table as real portfolio P&L.
- Hypothetical results never represent a current position, an order, or money at risk.
- Indicator computation (moving averages, RSI, ATR, VWAP, volume profile) MAY use float and
  pandas internally; these are statistical transforms. Quantise a price-like indicator value to
  the instrument tick only when displaying it. Any price, stop, target, P&L or other displayed
  or stored monetary figure MUST be Decimal at that boundary. Decimal(float) is forbidden:
  convert via string. No float may be written to a money column or rendered as a currency figure.
- Indicator and strategy logic ported from del_trading_bot lives HERE. The del_trading_bot copy is
  frozen reference and must not be edited or re-synced. There is exactly one home for this code.
- Scanner and backtester consume the same compiled condition tree and indicator library.

## Execution policy
Current state: Dhan and Delta Exchange each have ONE owner-approved live-execution exception (below);
every other broker, segment and strategy stays read-only.

### Owner-approved exception: Dhan live execution (approved by the owner, 2026-11-21)
The owner runs this system on their own account and approved building live order execution for
Dhan only, within hard limits. Agents MAY build and maintain exactly this, and nothing wider:
- NSE equity, INTRADAY product only. No options, futures, currency or commodity orders, no CNC.
- Maximum order quantity 1 share. The cap is enforced in the intent, the guard-rails and the
  adapter, and cannot be raised by configuration; raising it needs a new owner approval here.
- Only strategies on a code-level allowlist (currently `five_ema_alert` and `mfi_dip_30m`).
- The Dhan access token already stored for read-only use is reused; no new credential storage.
  Execution scope is a separate, explicit owner arming step, never implied by the token.
- Off by default in three independent layers, all required: deployment flag
  (`DHAN_LIVE_EXECUTION_ENABLED`), per-user arming by the owner (verified email, 2FA-verified
  session, CSRF, a warning dialog the owner confirms with one click (the typed phrase was removed by the owner on 2026-11-24), auditable), and per-test `execute_live` on that forward test.
- Guard-rails fail closed: stale/missing price, outside market hours, no stop or target, daily
  loss cap, daily trade cap, open-position cap, notional cap, duplicate signal, kill switch.
- A kill switch disarms instantly and cancels this app's open orders.
- Tests, CI and development NEVER reach a Dhan order endpoint (the adapter refuses under pytest/CI
  and is exercised only against a mocked transport). Agents must not place a real order.
- Every intent, decision, order request, broker response and fill is written to the append-only
  audit trail with the real broker reply; tokens are never logged.

### Owner-approved exception: Delta Exchange India live execution (approved by the owner, 2026-11-23)
The owner approved building live order execution for Delta Exchange India, within hard limits. Agents MAY
build and maintain exactly this, and nothing wider:
- USD-quoted perpetual futures only. No options, no spot, no non-USD contracts.
- Maximum order size 1 contract (one lot, whatever the lot size is). Enforced in the intent, the guard-rails
  and the client; configuration cannot raise it.
- Leverage is set to the LOWER of 25x and the maximum the contract's initial margin allows, and the order
  is refused if that leverage cannot be confirmed.
- Only strategies on a code-level allowlist (currently `five_ema_alert`, from the Delta paper forward test).
- The API key and secret are read from backend environment secrets only (`DELTA_API_KEY`, `DELTA_API_SECRET`),
  never stored in the database, never logged, never returned.
- Off by default in three independent layers, all required: deployment flag (`DELTA_LIVE_EXECUTION_ENABLED`),
  per-user arming by the owner (verified email, 2FA-verified session, CSRF, a warning dialog confirmed with one click (typed phrase removed by the owner on 2026-11-24), auditable),
  and per-test `execute_live` on that Delta forward test.
- Guard-rails fail closed (stale price, missing stop or target, daily loss cap, daily trade cap, open-position
  cap, notional cap, duplicate signal, one open order per contract, kill switch). The kill switch disarms
  instantly and cancels this app's unfilled orders.
- Owner decisions, 2026-11-25: effective leverage cap (order value / wallet) is 25x, configurable
  downward only; there is no pre-send balance gate (Delta rejects an order the wallet cannot cover, and
  Delta's rejection reason is shown in the ledger); signal freshness is one candle of the strategy's
  timeframe (5 minutes for 5m), and an unknown timeframe fails closed.
- Tests, CI and development NEVER reach a Delta order endpoint (the client refuses under pytest/CI and is
  exercised only against a mocked transport). Agents must not place a real order.
- Every intent, decision, order request, broker response and fill is written to the append-only audit trail
  with the real broker reply; credentials are never logged.

Agents MAY design and build the architecture that lets live execution be added later without
rework:
- a structured `OrderIntent` object (instrument, side, quantity, order type, limit, stop, TIF,
  validity, strategy/signal/version reference);
- an adapter seam between signal and fill, so a backtest fill model and a future broker adapter
  are interchangeable behind one interface;
- credential scoping that separates read-only from execution scope;
- a system-wide mode flag (research / monitor / live) that defaults to research;
- deterministic client order IDs;
- guard-rail limits (risk checks, size and exposure caps) that fail closed;
- an append-only audit trail recording intent, decision and fill identically for backtest and live.

Agents MUST NOT build (outside the Dhan and Delta exceptions above):
- any code path that transmits an order (place, modify or cancel) to a broker;
- any execution credential storage (read-only credentials only);
- any live adapter, or any adapter method that can reach a broker's order endpoints.

Requirements that apply to the permitted architecture, and bind any future activation:
- Live mode is off by default. It is never inferred from stored credentials, broker
  connectivity, a strategy signal, or application startup. Activation needs an explicit,
  auditable owner action. (The Dhan milestone is approved above; anything else is not.)
- Development, automated tests and CI must never reach a broker order endpoint; fail closed
  if any live path is reached in those environments.
- Every order intent and lifecycle transition is auditable, owner-scoped, and correlated with its
  strategy decision and risk checks. Never log credentials, tokens, PINs, OTPs or signing material.
- Risk checks fail closed on missing, stale, synthetic, unverifiable or currency-incompatible
  inputs. Never convert FX implicitly.
- A signal or advisory output never implies an order.
- Existing authentication, email verification, CSRF and broker security controls must not be
  weakened.

## Hard constraints (binding)
- No synthetic or mock market data, ever. Tests use fixtures clearly marked as test data and
  never flow into runtime paths.
- The LLM never computes numbers. All figures come from Python over the ledger, stored OHLCV,
  corporate actions and journal, serialized into a deterministic evidence pack. The model sees
  only that pack.
- Decimal for all money and quantity math, matching existing ledger rounding.
- No silent FX: never convert currencies implicitly.
- Missing data renders as unknown ("—" or an explicit unknown state), never as zero, "fresh", or
  any favourable default. Age and staleness are derived from a stored as-of timestamp, by one
  backend implementation.
- Stale, synthetic, unavailable, incomplete or currency-mismatched data suppresses or downgrades
  action-implying output (enter/exit/trim/book profit and signal levels) with a stated reason,
  rather than manufacturing a result.
- All user-facing output is labelled "decision support, not investment advice".
- Every route: authenticated + email-verified; CSRF on mutations; every query and mutation
  owner-scoped.
- Secrets are backend-only. Never log credentials, tokens, PINs or OTPs.
- Everything works with AI_ENABLED=false. AI adds narration, never capability.
- Migrations: additive only, single head, next sequential number. Never drop a populated column.
  Alembic revision IDs are <= 32 characters, format `NNNN_short_slug`.
- Every model module is imported in app/models/__init__.py so SQLAlchemy metadata is complete.
  After any migration, `alembic check` must report no pending changes.

## Efficiency rules (important)
- Implement exactly what the prompt asks. No refactors, renames, new abstractions or
  "while I was here" fixes.
- Touch only the files listed in the prompt. Ask before creating or moving a file not listed.
- If the prompt references earlier work that does not exist in the repo, STOP and report it.
  Do not silently write a substitute implementation.
- Never full-read a file over 50KB. In particular never full-read `backend/app/api/market_data.py`
  (384KB) or `backend/app/api/brokers.py` (184KB). Grep with line numbers and read narrow ranges.
- Do not read the whole repo. Follow imports only as far as needed.
- Before any database work, confirm Postgres is reachable. Report a connection failure as a
  connection failure, never as "no records found".
- This Codespace's Docker CLI may lack Compose v2. Check which of `docker compose` or
  `docker-compose` exists before using either (currently only `docker-compose` 1.29.2).
- Run only the test file named in the prompt. Do not run the full suite unless asked.
- Lint must pass before any task is reported complete; include lint output in the report.
- Run `npm run build` once, at the end of the milestone, not after each edit.
- Do not update README.md unless the prompt says so. Append one line to CHANGELOG.md instead.
- Do not paste full file contents or long diffs in your reply. Summarize in under 10 lines.
- Stop after the named milestone. Never start the next one.

## Stack facts
Backend: FastAPI + SQLAlchemy + Alembic + PostgreSQL, `backend/app/`, tests in `backend/tests/`.
Frontend: React + TypeScript + Vite, `frontend/src/`.
Migrations: current head is the highest-numbered file in `backend/migrations/versions/`
(0051_derivatives_platform at time of writing); the next migration is the following number.
Migration 0035 has a non-additive key change, approved as a documented exception; its downgrade
and pre-check are in place.

## Diagnostics artifacts
Milestone diagnostics live in `diagnostics/`, which is gitignored. Each milestone REPLACES its
artifact rather than adding a new numbered one. Do not commit diagnostics output.
Run the backend from `backend/` so `.env` loads. `/api/v1/ready` fails when migrations are pending.

## Definition of done
Named tests pass, frontend builds, `alembic check` clean, CHANGELOG line added, reply under
10 lines listing files changed, new env vars, and anything a human must verify manually.
