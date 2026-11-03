# AGENTS.md — Trading Copilot

## Product
A private, portfolio-aware research analyst for one user. It reports daily, with checkable
evidence: what to put on radar, what to trim or book profit in, what to hold and why, and what
unintended risk is being carried. The approved product direction also includes a complete
automated trading platform for Dhan and Delta Exchange India.

## Authorized trading scope
- Brokers are read-only by design. Order placement, order modification, order cancellation,
  live execution, execution adapters, strategy-driven entries and exits, stop-losses, targets,
  trailing stops, paper trading, and simulated fills are PERMANENTLY OUT OF SCOPE. They are
  excluded by design, not deferred. Do not implement, scaffold, stub, or plan them.
- Any request for execution must be refused and reported to the owner rather than built.

## Historical evaluation and indicator scope
- Historical evaluation (backtesting) is IN SCOPE. A backtest replays strategy logic over stored historical bars to produce statistics: trades, win rate, profit factor, expectancy, drawdown. It must use completed bars only, must never look ahead, and must state the data window, bar count, source and any cost assumptions used. Paper trading remains OUT OF SCOPE: no simulated positions tracked against live markets, no simulated order book, no pretend portfolio, no live-fill emulation. A backtest produces statistics about the past. It never represents a current position, an order, or money at risk.
- Indicator computation (moving averages, RSI, ATR, VWAP, volume profile) MAY use float and pandas internally. These are statistical transforms, not monetary values. Indicator outputs keep full precision for comparison and strategy logic. Quantise a price-like indicator value to the instrument tick ONLY when displaying it. Any price, stop, target, P&L or other displayed or stored monetary figure MUST be converted to Decimal at that boundary. Decimal(float) is forbidden - convert via string. No float may be written to a money column or rendered as a currency figure.
- Indicator and strategy logic ported from the del_trading_bot repository lives HERE. The del_trading_bot copy is frozen reference only and must not be edited or re-synced. There is exactly one home for this code.

## Hard constraints
- Live execution must be disabled by default and unavailable until an owner explicitly activates
  it through a deliberate, auditable control. Do not infer activation from stored credentials,
  broker connectivity, strategy signals, or application startup.
- Development, automated tests, and CI must never submit real broker orders. Use isolated test
  adapters/fixtures and fail closed if a live execution path is reached in those environments.
- Every live order intent and lifecycle transition must be auditable, owner-scoped, and correlated
  with its strategy decision and risk checks. Never log credentials, tokens, PINs, OTPs, or
  signing material.
- Live submissions must pass independent configured risk checks and fail closed on missing,
  stale, synthetic, unverifiable, or currency-incompatible inputs. Never convert FX implicitly.
- Preserve existing authentication and email-verification requirements; mutations require CSRF,
  and every query or mutation must be owner-scoped. Execution controls must not weaken these
  requirements or existing broker security controls.
- Live trading is never implied by an advisory or strategy signal. No order may be submitted
  without explicit owner activation and a successful, recorded risk decision.
- The LLM never computes numbers. All figures come from Python over the ledger, stored OHLCV,
  corporate actions, and journal, serialized into an evidence pack. The model sees only that pack.
- Action-implying output (trim/book profit/enter/exit) is suppressed or downgraded when the price
  is synthetic, stale, unavailable, or in a currency different from the portfolio. Never convert FX.
- Decimal for all money/quantity math, matching existing ledger rounding.
- Migrations: additive only, single head, next sequential number. Never drop a populated column.
- Alembic revision IDs must be <= 32 characters, format `NNNN_short_slug`.
- Every model module must be imported in app/models/__init__.py so SQLAlchemy metadata is
  complete. After any migration, `alembic check` must report no pending changes.
- Every route: authenticated + email-verified; CSRF on mutations; owner-scoped queries.
- Secrets are backend-only.
- Everything works with AI_ENABLED=false. AI adds narration, never capability.
- All user-facing output is labelled "decision support, not investment advice".

## Efficiency rules (important)
- Implement exactly what the prompt asks. No refactors, renames, new abstractions, or
  "while I was here" fixes.
- Touch only the files listed in the prompt. Ask before creating or moving a file not listed.
- If the prompt references earlier work that does not exist in the repo, STOP and report it.
  Do not silently write a substitute implementation.
- Do not read the whole repo. Follow imports only as far as needed.
- Run only the test file named in the prompt. Do not run the full suite unless asked.
- Lint must pass before any task is reported complete, and include lint output in the report.
- Run `npm run build` once, at the end of the milestone, not after each edit.
- Do not update README.md unless the prompt says so. Append one line to CHANGELOG.md instead.
- Do not paste full file contents or long diffs in your reply. Summarize in under 10 lines.
- Stop after the named milestone. Never start the next one.

## Stack facts
Backend: FastAPI + SQLAlchemy + Alembic + PostgreSQL, `backend/app/`, tests in `backend/tests/`.
Frontend: React + TypeScript + Vite, `frontend/src/`.
Current head: 0036_portfolio_valuation_history. Next migration: 0037.
Migration 0035 has a non-additive key change, approved as a documented exception; its downgrade and pre-check are in place.

## Diagnostics artifacts
Milestone diagnostics live in `diagnostics/`, which is gitignored. Each milestone REPLACES its artifact rather than adding a new numbered one. Do not commit diagnostics output.
Run backend from `backend/` so `.env` loads. `/api/v1/ready` fails when migrations are pending.

## Definition of done
Named tests pass, frontend builds, `alembic check` clean, CHANGELOG line added, reply under
10 lines listing files changed, new env vars, and anything a human must verify manually.
