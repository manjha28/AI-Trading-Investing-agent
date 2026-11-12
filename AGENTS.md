# AGENTS.md — Trading Copilot

## Product
A private, portfolio-aware research analyst for one user. It reports daily, with checkable
evidence: what to put on radar, what to trim or book profit in, what to hold and why, and what
unintended risk is being carried. It also provides a signals and strategy workbench (scanning,
backtesting, forward monitoring) for Dhan (NSE/BSE) and Delta Exchange India. Live order
placement is a future goal; see "Execution policy".

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
Current state: no live order placement exists and none may be built now. Brokers are read-only
in practice.

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

Agents MUST NOT build:
- any code path that transmits an order (place, modify or cancel) to a broker;
- any execution credential storage (read-only credentials only);
- any live adapter, or any adapter method that can reach a broker's order endpoints.

Requirements that apply to the permitted architecture, and bind any future activation:
- Live mode is off by default. It is never inferred from stored credentials, broker
  connectivity, a strategy signal, or application startup. Activation needs an explicit,
  auditable owner action, and is a separate milestone approved by the owner.
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
