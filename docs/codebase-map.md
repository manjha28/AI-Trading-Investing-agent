# Codebase Map

> Agent navigation guide for `manjha28/AI-Trading-Investing-agent`.
>
> Read this file before walking the repository. It maps the code as it exists, including
> uncommitted work present when this map was generated. Use targeted file reads after
> identifying the relevant subsystem; do not start by reading the whole backend.

## 1. Product and governing constraints

This repository is a private, portfolio-aware trading research application. It is a
decision-support system, not an order-management system. It currently covers:

- portfolio accounting, journal entries, valuation, and risk
- read-only broker synchronization
- market data, quotes, OHLCV, depth, watchlists, and freshness/provenance
- company fundamentals, valuation, research, news, and sentiment
- deterministic strategy authoring, scanning, backtesting, and signal monitoring
- derivatives, options, intraday signals, scalp/hold, and VAR research

The binding project policy is in `AGENTS.md`. In particular:

- no live order placement or code that can call a broker order endpoint
- no synthetic or mock market data in runtime paths
- all money and quantity boundaries use `Decimal`; convert floats through strings
- models do not emit prices or monetary levels; Python computes evidence and levels
- completed historical bars only; no look-ahead in backtests or forward monitoring
- missing, stale, incomplete, synthetic, or currency-incompatible data is unknown or
  suppresses action-implying output
- no implicit FX conversion
- all user-facing output is decision support, not investment advice
- every route is authenticated and email-verified, mutations use CSRF, and all data is
  owner-scoped
- `AI_ENABLED=false` preserves all core capability
- migrations are additive, sequential, and have a single head
- every model module must be imported by `backend/app/models/__init__.py`
- `backend/app/api/market_data.py` and `backend/app/api/brokers.py` are intentionally
  large; read only targeted functions and ranges

The Signals & Strategy Intelligence Workbench blueprint is the product spec of record.
If implementation or this map conflicts with project policy, stop and report the conflict.

## 2. Repository tree

```text
.
├── AGENTS.md                         # Binding repository policy
├── README.md                         # High-level setup and product overview
├── Makefile                          # dev and lint shortcuts
├── CHANGELOG.md                      # Milestone history
├── ANGEL_ONE_CODESPACES_SETUP.md     # Angel One Codespaces/provider setup
├── .github/workflows/ci.yml          # CI: Postgres, backend tests, lint, frontend build
├── .devcontainer/                    # Codespaces container and Compose configuration
├── backend/
│   ├── app/                          # FastAPI application and domain runtime
│   ├── migrations/                   # Alembic environment and linear revisions
│   ├── scalp_hold/                   # Standalone/integrated research engine
│   ├── scripts/                      # Provider, validation, phase, and maintenance scripts
│   ├── tests/                        # Backend tests and explicit test fixtures
│   ├── pyproject.toml                # Python/tool configuration
│   ├── requirements*.txt             # Runtime and development dependencies
│   └── alembic.ini                   # Alembic configuration
├── frontend/
│   ├── src/                          # React/TypeScript application
│   ├── tests/                        # Non-Vitest .mjs checks
│   ├── public/                       # PWA icon, manifest, and service worker
│   ├── package.json                  # dev/build/test scripts
│   └── vite.config.ts                # Vite, proxy, and test configuration
├── deploy/compose/                   # Postgres, Kafka, and Valkey deployment
├── docs/                             # Architecture, runtime, methodology, workbench docs
├── diagnostics/                      # Reports and captured evidence; not runtime input
└── scripts/                          # Cross-repository provider/feed probes
```

## 3. Fast-path navigation

| Task | Start here | Then inspect |
|---|---|---|
| Add or change an API route | `backend/app/main.py` | relevant `backend/app/api/*.py`, called services, models, frontend caller |
| Change authentication or sessions | `backend/app/api/auth.py` | `security.py`, `two_factor.py`, `email_verification.py`, auth models, frontend `Auth.tsx` |
| Change portfolio math | `backend/app/api/portfolios.py` or `portfolio_transactions.py` | `accounting.py`, transaction/valuation models, frontend Overview/workspace state |
| Change market-data behavior | route call site in `api/market_data.py` | `market_data.py`, `provider_adapters.py`, schedulers, relevant model and frontend hooks |
| Change Dhan/broker behavior | targeted route in `api/brokers.py` | `broker_adapters.py`, broker models, `BrokersPage.tsx` |
| Change live-stream latest state | `api/market_stream.py` | `app/market_stream/*`, `hooks/useSnapshotFallback.ts`, `useWatchlistStream.ts` |
| Add a strategy | `services/starter_strategies.py` | `strategy_contract.py`, `strategy_engine.py`, `engine_strategies.py`, DSL, Signals UI |
| Change signal lifecycle | `services/signal_lifecycle.py` | `api/signals_lab.py`, model events, `signals/LifecycleChip.tsx` and timeline |
| Change a backtest | `services/backtest_engine.py` or `intraday_backtest.py` | strategy engine, indicators, costs, matching API/UI and focused tests |
| Change VAR research | `api/var_strategy.py` | `services/var/*`, `scalp_hold/funnel.py`, VAR models, `Var*.tsx` |
| Change scalp/hold | `scalp_hold/app_adapter.py` | `scalp_hold/*`, `services/scalp_hold_runner.py`, Signals API/UI |
| Change research/valuation | `services/research_pipeline.py` | scoring, fundamentals, valuation, `api/research.py`, `Research.tsx` |
| Change corporate actions | `api/corporate_actions.py` | `corporate_action_service.py`, models, `CorporateActions.tsx` |
| Change news/sentiment | `api/news.py` or `news_ingestion.py` | `services/news_sentiment.py`, alert/research consumers, `Research.tsx` |
| Add a table/model | existing model family plus next migration | import in `models/__init__.py`, verify with `alembic check` |
| Add a frontend page | `frontend/src/App.tsx` | `useWorkspace.ts`, `src/pages/`, styles, tests |
| Debug build/test/deploy | `Makefile`, package/requirements files | CI, Compose files, relevant docs |

## 4. Backend bootstrap and shared boundaries

### Application entry points

- `backend/app/main.py`
  - constructs the FastAPI application and CORS middleware
  - mounts routers under `/api/v1`
  - exposes application root behavior
  - starts and stops in-process schedulers/workers
  - conditionally starts market streaming, job/scan workers, market-data refresh,
    portfolio snapshots, Dhan capture, alerts, news sentiment, fundamentals,
    derivatives, and Delta research
  - suppresses most loops under pytest
  - filters/redacts broker callback request data from access logs
- `backend/app/config.py`
  - typed environment settings and feature flags
  - provider, market, broker, email, security, scheduling, Kafka, and Valkey config
  - CORS origin normalization and trusted-proxy behavior
  - NSE freshness calendar; comments identify it as non-authoritative
- `backend/app/db.py`
  - SQLAlchemy declarative base, engine, and `get_db()`
  - engine uses `pool_pre_ping=True`
- `backend/app/models/__init__.py`
  - required import registry for all model modules
  - read this first when determining tables/entities or adding a model

### Security and identity

- `api/auth.py`: register, login, logout, current user, email verification/resend,
  and TOTP setup/status/verification
- `security.py`: scrypt password hashing, SHA-256 token hashing, constant-time checks,
  and random token generation
- `two_factor.py`: TOTP behavior
- `fernet_keys.py`: Fernet key normalization and credential encryption key handling
- `email_verification.py`: verification delivery and token workflow

Cookie-authenticated mutations depend on CSRF helpers. Route protection is implemented at
the route level and is not safe to infer from the presence of a router prefix; inspect the
dependencies of each changed endpoint.

### External integration boundaries

- `broker_adapters.py`: broker-specific adapters, normalization, credential and mirror
  interactions; no live execution endpoint may be added
- `provider_adapters.py`: provider clients and market-data normalization
- `market_data.py`: shared market-data types and domain helpers
- `twelve_data.py`: Twelve Data integration
- `news_ingestion.py`: news provider ingestion
- `ws_diagnostics.py`: WebSocket diagnostics
- `api/brokers.py`: large broker route/control surface
- `api/market_data.py`: large market route/control surface

Sensitive values must remain backend-only. Preserve redaction in callback logging and
provider/broker adapters.

## 5. Backend model and data ownership map

The model registry currently spans:

```text
backend/app/models/
├── __init__.py
├── alerts.py
├── derivatives.py
├── jobs.py
├── provider_coverage.py
├── strategies.py
└── var_strategy.py
```

Major persisted domains include:

| Domain | Typical records |
|---|---|
| Identity/security | users, sessions, email verification, 2FA state |
| Portfolio/accounting | portfolios, transactions, positions, valuation history, journal trades |
| Instruments/markets | instruments, venues, quote cache, OHLCV bars, watchlists, sync state |
| Brokers | encrypted read-only credentials, connection state, Dhan account/transaction mirrors |
| Corporate actions | actions, application/reversal state, adjusted market/portfolio data |
| Research/fundamentals | sources, notes, theses, research runs, fundamentals, gaps, AI analyses |
| News | source articles, source health, sentiment, sentiment failures |
| Alerts | alert rules, events, evaluation state |
| Strategies/signals | strategy versions, compiled definitions, backtests, signals, lifecycle events |
| Jobs/scans | durable jobs, leases, progress, retries, scanner schedules/history |
| Derivatives/options | contracts, candles, chains, coverage, ingestion jobs |
| VAR | VAR configuration/versioning, run status, backtest and result data |

Always derive the current shape from the relevant SQLAlchemy model and migration rather than
from the frontend type alone. When a new model module is added, `models/__init__.py` must
import it so Alembic metadata and tests are complete.

## 6. Backend HTTP and WebSocket map

All routers are prefixed with `/api/v1` from `app/main.py`.

| Module | Route family and responsibility |
|---|---|
| `api/auth.py` | `/auth/*`: identity, sessions, email verification, 2FA |
| `api/health.py` | `/health`, `/ready`: process, database, and migration readiness |
| `api/portfolios.py` | `/portfolios/*`: portfolio CRUD, detail, and setup |
| `api/portfolio_transactions.py` | `/portfolios/*` transaction CRUD/import/export, valuations, risk, instrument links |
| `api/trades.py` | `/trades/*`: journal, annotations, notes, analytics, fills |
| `api/market_data.py` | `/market-data/*`: providers, settings, instruments, quotes, history, watchlists, coverage, backfill, depth/history WebSockets |
| `api/market_stream.py` | market-stream latest/poll APIs backed by event transport and state |
| `api/brokers.py` | `/brokers/*`: read-only connection, callbacks, sync, mirrors, import/reconciliation |
| `api/corporate_actions.py` | `/corporate-actions/*`: create/list, provider ingest, apply/reverse |
| `api/news.py` | `/news/*`: articles, source health, market/instrument/sector sentiment and context |
| `api/research.py` | `/research/*`: sources/notes, research runs, AI analysis, fundamentals/screener coverage |
| `api/position_theses.py` | `/position-theses/*`: thesis CRUD, revisions, coverage |
| `api/delta_research.py` | `/delta-research/*`: Delta market/research status, events, strategies, evaluations, stream |
| `api/strategies.py` | `/strategies/*`: legacy/daily strategy CRUD, catalog, evaluation, backtests, signals |
| `api/signals_lab.py` | `/signals-lab/*`: workbench strategy/scalp/scan/backtest/signal/job APIs |
| `api/var_strategy.py` | `/var/*`: VAR config, versioning, capabilities, jobs/results, charges/preflight |
| `api/costs.py` | `/costs/*`: execution cost and tax estimates |
| `api/derivatives.py` | `/derivatives/*`: universe, contracts, coverage, chains, ingestion jobs |

WebSocket state is split between Dhan/live-feed behavior in `api/market_data.py` and the
optional event-oriented implementation in `app/market_stream/`.

## 7. Service-layer map

`backend/app/services/` is the main domain layer used by routes, workers, and scripts.

### Market data, quality, and availability

| File(s) | Responsibility |
|---|---|
| `bar_quality.py` | bar validation and quality checks |
| `intraday_backfill.py` | historical intraday acquisition/backfill |
| `price_freshness.py`, `price_states.py` | staleness, basis, and user-facing availability state |
| `provider_coverage.py`, `source_registry.py` | provider coverage and authoritative source selection |
| `timeframes.py`, `trading_calendar.py` | candle timeframes and market calendars |
| `lot_sizes.py` | instrument lot/size information |
| `dhan_company_info.py` | Dhan company information normalization/coverage |
| `dhan_derivatives.py`, `derivatives_ingest.py` | derivative master/contracts/chains ingestion |
| `options_data.py`, `intraday_options.py` | option data and intraday option helpers |

### Indicators, strategies, scans, signals, and backtesting

| File(s) | Responsibility |
|---|---|
| `indicator_library.py`, `indicators.py`, `intraday_indicators.py` | reusable indicator definitions and transforms |
| `strategy_contract.py` | persisted/compiled strategy boundary and contracts |
| `rule_dsl.py` | condition-tree and operand DSL |
| `strategy_engine.py`, `engine_strategies.py` | strategy compilation/evaluation and built-in engine behavior |
| `starter_strategies.py` | bundled starter definitions |
| `pine_import.py` | Pine import diagnosis/import processing |
| `backtest_engine.py`, `intraday_backtest.py`, `intraday_runner.py` | completed-bar replay and result production |
| `intraday_strategy.py`, `intraday_python.py`, `intraday_metrics.py`, `intraday_validation.py` | intraday definitions, Python strategy support, metrics, and validation |
| `scan_service.py`, `scan_scheduler.py` | scanning and persisted scan schedules |
| `signal_lifecycle.py` | deterministic signal state/event behavior |
| `scalp_hold_runner.py` | bridge from Signals API to the `scalp_hold` engine |
| `barrier_study.py`, `filter_value.py`, `trade_metrics.py`, `trade_stats.py` | barrier/filter/trade research analysis |
| `option_selector.py`, `options_signal_engine.py`, `options_strategy.py` | option eligibility and signal strategy support |

The scanner and backtester must use the same compiled condition tree and indicator library.
Price-like output is tick-quantized only at display/storage boundaries.

### Costs and tax

- `transaction_costs.py`: generic transaction-cost schedules and estimates
- `indian_charges.py`: Indian equity/derivative charge calculations
- `indian_tax.py`: Indian tax estimates
- `cost_recalculation.py`: recalculates existing backtest costs/results

Cost assumptions must remain explicit in backtest output. Hypothetical P&L must not be
combined with real portfolio totals.

### Research, valuation, and fundamentals

- `research_pipeline.py`: research-run orchestration
- `research_scoring.py`: deterministic research scoring
- `ai_analyst.py`: optional AI narration around an evidence pack; not numerical authority
- `valuation.py`, `valuation_inputs.py`, `valuation_scoring.py`: valuation inputs and scoring
- `fundamentals.py`, `fundamentals_feed.py`: fundamentals state and provider feed
- `peer_universe.py`: peer selection
- `screener_import.py`: Screener page parsing/import
- `news_sentiment.py`: sentiment analysis, failure and coverage behavior

### VAR research subsystem

```text
backend/app/services/var/
├── config.py       # typed VAR configuration
├── vtypes.py       # shared typed records
├── loader.py       # historical data loading
├── universe.py     # instrument universe resolution
├── profile.py      # profile features
├── structure.py    # structure features
├── tpo.py          # time-price-opportunity logic
├── orderflow.py    # order-flow-derived features
├── strategy.py     # deterministic strategy evaluation
├── risk.py         # risk checks
├── execution.py    # research fill simulation
├── charges.py      # execution charge integration
├── metrics.py      # performance and risk metrics
├── funnel.py       # funnel/attribution reporting
├── backtest.py     # completed-bar replay
└── service.py      # public orchestration used by jobs/API
```

VAR execution is a research/backtest fill model only. It is not a broker adapter.

### Durable jobs and schedulers

- `job_queue.py`: job persistence, claiming, lease/progress/failure updates
- `job_handlers.py`: maps job types to domain handlers
- `jobs_worker.py`: claims and executes jobs, heartbeats, retries, and runs scheduler ticks
- `scan_scheduler.py`: scanner schedule evaluation
- `derivatives_scheduler.py`: derivatives master and option-chain polling
- `market_data_scheduler.py`: market-data refresh/snapshot behavior

The API starts several in-process loops. Before deploying multiple replicas, verify
idempotency and whether each scheduler needs single-instance coordination.

## 8. Market-stream subsystem

```text
backend/app/market_stream/
├── __init__.py
├── events.py       # event shape and identity
├── poller.py       # retrieves completed Dhan bars
├── transport.py    # in-memory/Kafka publish-consume abstraction
├── worker.py       # consumes and dispatches events
├── persistence.py  # DB/latest-state persistence
└── state.py        # in-memory/Valkey latest-state implementations
```

Runtime flow:

1. `poller.py` obtains completed provider bars.
2. `events.py` normalizes the event.
3. `transport.py` publishes to in-memory transport or Kafka.
4. `worker.py` consumes events.
5. `persistence.py` updates the database and latest-state store.
6. `state.py` backs fast latest-state reads through Valkey or memory.
7. `api/market_stream.py` exposes latest state and polling control.
8. The frontend uses `/market-stream/latest` as a fallback only when live watchlist
   streaming is unavailable.

The subsystem is feature-gated and disabled unless configured.

## 9. Scalp/hold research engine

```text
backend/scalp_hold/
├── __init__.py
├── __main__.py
├── cli.py
├── data.py          # data/session loading, gaps, resampling
├── venues.py        # venue profiles, tick/lot/fill/cost assumptions
├── features.py      # VWAP/ATR/profile/time/volatility features
├── signals.py       # entry/invalidation signal logic
├── barriers.py      # stops, horizons, targets, triple-barrier labels
├── sim.py           # fill/trade simulation, costs, excursions
├── evaluate.py      # walk-forward/grid/control/bootstrap evaluation
├── funnel.py        # trade funnel, cost, and sizing diagnostics
├── causality.py     # feature causality guard/assertion
└── app_adapter.py   # integration with the FastAPI service
```

Use the CLI for standalone research. The application enters through `app_adapter.py` and
`services/scalp_hold_runner.py`. Inputs/results are research data and must not be
represented as positions, orders, or money at risk.

## 10. Backend operational and research scripts

Run environment-dependent scripts from `backend/` so `.env` loading and imports behave as
expected. Scripts are not FastAPI startup modules.

| Script group | Files |
|---|---|
| Dhan/company-info capture | `dhan_company_info_capture.py`, `dhan_company_info_analyse.py` |
| Derivatives operations | `derivatives_backfill.py`, `dhan_derivatives_probe.py` |
| Equity backfill | `fetch_equity_1m.py` |
| Phase 0 research | `phase0_common.py`, `phase0_base_rates.py`, `phase0_cluster_check.py`, `phase0_components.py`, `phase0_direction_split.py`, `phase0_filter_value.py`, `phase0_gap_fill_check.py` |
| Phase 1 research | `phase1_scalp.py`, `phase1_var.py`, `phase1_compare.py`, `phase1_coverage.py` |
| Valuation maintenance | `refresh_valuation_inputs.py`, `valuation_coverage.py`, `valuation_readiness.py` |
| Cost maintenance | `recalculate_backtest_costs.py` |
| Validation | `var_data_validation.py`, `validate_screener_page.py` |

Root-level `scripts/` are separate provider/feed probes:

- `scripts/verify_providers.py`
- `scripts/test_dhan_feed.py`
- `scripts/test_dhan_depth.py`

Provider scripts may require real credentials and network access. They are not unit tests.

## 11. Frontend architecture

### Entry and shell

- `frontend/src/main.tsx`: React `StrictMode` mount, token/workspace styles, service-worker registration
- `frontend/src/App.tsx`: authenticated application shell, global navigation, portfolio
  selection, onboarding, banners, broker status/sync, and view selection
- `frontend/src/api.ts`: shared `/api/v1` request helper, credentials, CSRF mutation
  header, in-flight deduplication, typed request errors, CSV downloads
- `frontend/src/components/useWorkspace.ts`: central cross-feature state and action boundary
- `frontend/vite.config.ts`: proxy, test environment, React plugin, Codespaces host/HMR

Navigation is state-based, not route-based. `App.tsx` selects one of:

```text
overview
journal
market-data
brokers
alerts
corporate-actions
research
signals
```

### Pages

| File | Responsibility |
|---|---|
| `pages/Overview.tsx` | portfolio summary, holdings/watchlist, valuation, risk, alerts/history, theses |
| `pages/PortfolioSetup.tsx` | initial portfolio creation |
| `pages/Journal.tsx` | trade journal, filters/calendar, annotations, broker history, CSV/backfill |
| `pages/MarketData.tsx` | quote/history/chart, indicators, watchlists, providers, coverage, fundamentals/options |
| `pages/BrokersPage.tsx` | read-only broker connection, account views, sync/import |
| `pages/Alerts.tsx` | alert CRUD, event state, manual evaluation |
| `pages/CorporateActions.tsx` | action entry/apply/reverse |
| `pages/Research.tsx` | research runs, sources, notes, news, sentiment |
| `pages/ResearchReport.tsx` | research report and optional AI presentation |
| `pages/DeltaResearch.tsx` | Delta research/market/event presentation; not a primary `App.tsx` view |
| `pages/Strategies.tsx` | legacy strategy manager/backtests, used from Signals legacy tab |
| `pages/Activity.tsx` | broker activity/mirror view; not a primary shell view |
| `pages/Auth.tsx` | login/register and verification state |
| `pages/researchTypes.ts` | shared research types |
| `pages/watchlistCloseDisposition.ts` | WebSocket close-state classifier |

### Shared components, hooks, and library code

- `components/useWorkspace.ts`
  - owner/session auth state
  - portfolio selection
  - transactions and journal state
  - valuation/risk and history
  - watchlists, market search, quote/history/indicators
  - CSV flows
  - email-verification and 2FA state
- `components/TransactionEntry.tsx`: transaction form
- `components/ThesisEditor.tsx`: thesis revisions and investment-plan form
- `components/CandleChart.tsx`: candle/line charts and indicator/session display
- `components/ui/index.tsx`: shared sortable tables and UI primitives
- `components/market/DataFreshnessIndicator.tsx`: freshness/provenance state
- `components/market/DhanCompanyInfo.tsx`: Dhan company metric presentation
- `hooks/useWatchlistStream.ts`: watchlist WebSocket, quote batching, reconnect/backoff,
  auth/market-close/error states
- `hooks/useSnapshotFallback.ts`: `/market-stream/latest` polling only when live state is
  unavailable; prevents older snapshots replacing newer live updates
- `lib/priceStates.ts`: availability/freshness basis state
- `lib/formatMoney.ts`, `lib/formatChange.ts`: presentation formatting
- `lib/depthStatus.ts`: derivative depth status

### Frontend API integration

All normal JSON calls use `apiRequest` from `src/api.ts`; direct WebSockets are implemented
by the watchlist hook. Main endpoint families are:

- `/auth/*`
- `/portfolios/*`, `/trades/*`
- `/market-data/*`, `/market-stream/latest`
- `/brokers/*`
- `/research/*`, `/news/*`
- `/alerts/*`, `/corporate-actions/*`
- `/strategies/*`, `/signals-lab/*`, `/derivatives/*`, `/var/*`

## 12. Signals & Strategy frontend workbench

`frontend/src/signals/SignalsWorkspace.tsx` owns nested tab composition for the Signals
workspace.

### Workbench files

| Area | Files |
|---|---|
| Workspace and tabs | `SignalsWorkspace.tsx`, `Dashboard.tsx` |
| Strategy building | `Builder.tsx`, `ConditionEditor.tsx`, `EngineParamsPanel.tsx`, `OptionsPanel.tsx` |
| Strategy library | `Library.tsx` |
| Backtesting | `Backtesting.tsx`, `charts.tsx` |
| Signals/lifecycle | `LiveSignals.tsx`, `SignalTimeline.tsx`, `LifecycleChip.tsx`, `RejectionsPanel.tsx` |
| Jobs/scans | `JobsScanner.tsx` |
| Indicators | `IndicatorLibrary.tsx` |
| Import/diagnostics | `ImportPanel.tsx`, `PineDiagnosisView.tsx` |
| Derivatives | `DerivativesData.tsx` |
| Data preparation | `BackfillPanel.tsx`, `InstrumentPicker.tsx` |
| Scalp/hold | `ScalpHoldView.tsx` |
| VAR | `VarIntraday.tsx`, `VarPerformance.tsx`, `VarFunnel.tsx`, `VarLedger.tsx`, `VarCharges.tsx`, `VarDataAudit.tsx` |
| Shared UI/data | `shared.tsx`, `StyledSelect.tsx`, `types.ts`, `lab.ts`, `lifecycle.ts`, `preflight.ts`, `engineGeometry.ts`, `varFormat.ts` |
| Styles | `signals.css`, `derivatives-data.css` |

### Important frontend contracts

- `signals/types.ts`: strategy definition, condition and operand types, indicator metadata,
  options configuration, lifecycle, backtest and market-data readiness contracts
- `signals/lab.ts`: `/signals-lab/*` request functions
- `signals/lifecycle.ts`: lifecycle/status helpers
- `signals/preflight.ts`: readiness and validation representation
- `signals/engineGeometry.ts`: strategy geometry helpers
- `signals/varFormat.ts`: VAR display formatting

Keep backend response shapes and frontend contracts synchronized. For type/shape changes,
trace model/serialization → route schema → API wrapper → component/test.

## 13. End-to-end flows

### Authentication

1. `frontend/src/pages/Auth.tsx` calls `/auth/*` through `api.ts`.
2. `api/auth.py` validates input and manages session/security state.
3. `security.py`, `two_factor.py`, and `email_verification.py` provide cryptographic flows.
4. `useWorkspace.ts` hydrates `/auth/me` and authentication state.
5. `App.tsx` gates verified setup and portfolio/research surfaces.

### Portfolio valuation

1. Transactions enter through `Overview.tsx` / `TransactionEntry.tsx`.
2. API calls reach `api/portfolio_transactions.py`.
3. `accounting.py`, models, quote data, and corporate actions calculate deterministic
   valuation inputs and ledger state.
4. Historical/current valuation and risk routes return frontend-ready records.
5. `Overview.tsx` displays missing/stale states rather than favorable defaults.

### Market quote and watchlist

1. Provider/broker data is normalized by `provider_adapters.py` and `market_data.py`.
2. API/scheduler paths persist instruments, quotes, bars, coverage, and freshness state.
3. `useWatchlistStream.ts` maintains the direct authenticated watchlist WebSocket.
4. `useSnapshotFallback.ts` uses market-stream latest state only when the live path is
   unavailable.
5. `DataFreshnessIndicator.tsx` and `priceStates.ts` expose provenance/freshness to users.

### Durable backtest or scan

1. A Signals route validates the request and enqueues a durable job.
2. `job_queue.py` stores the job and lease metadata.
3. `jobs_worker.py` claims it.
4. `job_handlers.py` dispatches to a strategy/scanner/backtest handler.
5. The handler loads completed bars, compiles/uses the shared strategy definition and
   indicators, computes costs and metrics, and persists results.
6. The frontend polls job status/results and renders them in the appropriate Signals tab.

### VAR study

1. `VarIntraday.tsx` sends typed configuration to `/var/*`.
2. `api/var_strategy.py` persists/version-checks config and enqueues a run.
3. `services/var/service.py` coordinates loader/universe/profile/structure/orderflow.
4. Strategy, risk, execution, charges, and metrics run over stored completed data.
5. Persisted result sections are rendered by VAR performance/funnel/ledger/charges/audit
   components.

### Research and optional AI narration

1. A research route accepts instrument/context input.
2. `research_pipeline.py` gathers stored/provider data, fundamentals, valuation inputs,
   news, and other deterministic evidence.
3. Scoring is computed in Python.
4. `ai_analyst.py` is optional and receives a structured evidence pack for narration only.
5. `Research.tsx` / `ResearchReport.tsx` render source-backed output.

## 14. Migrations and schema lifecycle

- Migration environment: `backend/migrations/env.py`
- Template: `backend/migrations/script.py.mako`
- Revisions: `backend/migrations/versions/`
- Current repository chain: `0001_initial_schema.py` through `0056_var_run_statuses.py`
- Headers form one numbered linear chain
- Next migration, if needed, must be the next sequential number after the actual highest
  revision, not a stale number from prose documentation

Schema chronology:

| Range | Major additions |
|---|---|
| `0001`–`0005` | base schema, identity, sessions, portfolios, transactions, journal/currency |
| `0006`–`0013` | instruments/market data, quote cache, read-only brokers, email/2FA |
| `0014`–`0025` | corporate actions, research, theses, alerts, fundamentals, provider controls |
| `0026`–`0040` | broker mirrors, watchlists, strategy signals, uniqueness, valuation, stream diagnostics |
| `0041`–`0048` | news, sentiment, research orchestration, AI analyses, valuation gap fill |
| `0049`–`0056` | intraday lab, options/derivatives, events, provider-unserved, jobs, VAR |

Migration `0035_instrument_identity_uniq.py` is a documented approved non-additive
exception. Do not generalize that exception. Import every model in `models/__init__.py`.
After migration changes, `alembic check` must be clean.

> Documentation caveat: `AGENTS.md` still mentions migration 0051 as the head, but the
> working tree contains revisions through 0056. Always inspect the migration directory.

## 15. Tests and fixtures

### Backend

Backend tests are under `backend/tests/` and are broadly grouped by:

- auth and portfolio/accounting: `test_auth_portfolios.py`, `test_indian_tax.py`, costs
- brokers/providers: `test_brokers.py`, Angel/Dhan/provider tests
- market data/streaming: market-data, backfill, freshness, latest-candle and stream tests
- corporate actions, research, fundamentals, valuation and news
- strategies/backtests/options/intraday/indicator behavior
- durable jobs and migrations
- scalp/hold and VAR tests

Shared fixtures:

- `tests/market_data_fixtures.py`: deterministic recorded provider support
- `tests/var_fixtures.py`: explicit test-only VAR data
- `tests/fixtures/dhan_company_info/*.json`
- `tests/fixtures/golden_crypto_500.csv`, `golden_india_500.csv`
- `tests/fixtures/news_sentiment_validation.json`
- `tests/fixtures/screener_testco_*.html`

No `conftest.py` is present; fixtures are generally local/shared modules.

Run only the test file named for a milestone. From `backend/`:

```bash
python -m pytest tests/<focused_test>.py -q
```

If a task requires the full baseline, CI uses:

```bash
python -m pytest -q
```

`test_market_stream_integration.py` is service-dependent. It may skip when Kafka/Valkey
and environment variables are absent; a skip is not proof of live integration.

### Frontend

Vitest is configured for colocated `src/**/*.test.{ts,tsx}` and uses jsdom. Coverage includes:

- candle date/time behavior
- price/freshness/change formatting
- snapshot fallback behavior
- Dhan company information
- research reports
- Signals builder/import/backtest/live flows
- backfill/instrument picking
- VAR panels and scanner/lifecycle UI

Additional `.mjs` tests:

- `frontend/tests/research-render.test.mjs`
- `frontend/tests/watchlist-close-disposition.test.mjs`

Those `.mjs` files are outside the configured Vitest include pattern and must be run by
their own configured command if changed.

## 16. Build, lint, and run

### Backend

From `backend/`:

```bash
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Run from `backend/` so `.env` is loaded by project configuration.

### Frontend

From `frontend/`:

```bash
npm install
npm run dev
npm test
npm run build
```

Vite proxies `/api`, `/health`, and `/ready` to `127.0.0.1:8000`.

### Root shortcuts

```bash
make dev-codespaces
make lint
```

`make dev-codespaces` starts backend and frontend together. `make lint` performs Ruff
undefined-name and warning checks. Run lint before reporting code work complete, and run
`npm run build` once at the end of a frontend milestone.

### CI

`.github/workflows/ci.yml` uses:

- PostgreSQL 16
- Python 3.12
- Node 22
- backend dependency install and migration
- `make lint`
- mypy (`continue-on-error`)
- full backend pytest
- frontend dependency install and build

Read the workflow before changing dependency, migration, or test commands.

## 17. Infrastructure and deployment

```text
deploy/compose/
├── docker-compose.yml
├── docker-compose.dev.yml
├── docker-compose.prod.yml
└── .env.example
```

The Compose stack includes PostgreSQL 16, Kafka 3.9 in KRaft mode, and Valkey 8.
The base file publishes no ports; dev publishes loopback ports; prod keeps services
internal and configures restart/log limits.

This Codespace may provide only Compose v1. Check whether `docker compose` or
`docker-compose` exists before running either. Documented commands use
`docker-compose` 1.29.2.

Relevant documentation:

- `docs/infrastructure.md`: Compose services, topic setup, stream tests, recovery
- `docs/dhan-runtime-verification.md`: readiness and live Dhan verification procedure

Documentation describing verification does not prove that live services are currently
reachable. Confirm connectivity explicitly before claiming runtime verification.

## 18. Environment and secrets

Primary template: `backend/.env.example`.

Important groups include:

- `DATABASE_URL`
- authentication/session/cookie/email/2FA settings
- broker/provider credentials and enablement
- `AI_ENABLED`
- market freshness, backfill, and scheduler settings
- market-stream enablement, Kafka, and Valkey settings
- instrumentation and provider debugging controls

Infrastructure variables are documented in `deploy/compose/.env.example`.
Angel One-specific setup names are documented in `ANGEL_ONE_CODESPACES_SETUP.md`.

Never commit values for credentials, tokens, PINs, OTPs, callback signing material,
encryption keys, or broker secrets. Missing sensitive configuration should fail clearly
rather than silently enabling fallback behavior.

## 19. Diagnostics and documentation

`diagnostics/` contains investigation evidence and milestone reports, not application
inputs. Examples cover:

- Dhan derivative probes and company-info captures
- equity and derivative backtest reports
- phase 0/1 scalp/VAR/cost analysis
- valuation coverage and acceptance
- VAR data validation
- corporate-action and news/filings coverage
- cost recalculation

Do not import diagnostics data into runtime paths or commit synthetic substitutes. The
project policy says milestone diagnostics replace prior milestone artifacts rather than
accumulate numbered duplicates, and diagnostics are generally gitignored.

Product/methodology documentation under `docs/`:

- `infrastructure.md`: data-plane Compose infrastructure
- `dhan-runtime-verification.md`: Dhan verification procedure
- `signals-intraday-workspace.md`: Signals/intraday workbench notes
- `valuation-scoring-methodology.md`: valuation methodology
- `intraday-lab/`: detailed intraday-lab design/material

## 20. Change-impact checklist

Use this checklist after identifying the target subsystem:

1. Re-read `AGENTS.md` and the relevant governing spec.
2. Inspect `git status`; preserve unrelated tracked and untracked work.
3. Trace one concrete route/component flow from caller to persistence and back.
4. For database changes, confirm PostgreSQL is reachable before interpreting records.
5. Check owner scoping, email verification, and CSRF on every affected route.
6. Confirm no new path can reach a broker order endpoint.
7. Use `Decimal` at money/quantity boundaries and explicit provenance/as-of state.
8. Keep scanner/backtester on shared compilation/indicators.
9. Keep hypothetical and real P&L completely separate.
10. Add focused tests only for introduced/fixed behavior.
11. Add/update related documentation only when required.
12. Run the named test file(s), lint, and the applicable build.
13. For migrations, run `alembic check` and ensure one sequential head.
14. Report files changed, environment variables, and manual verification still required.

## 21. Known hotspots and caveats

- `backend/app/api/market_data.py` and `backend/app/api/brokers.py` are large and
  integration-heavy; never full-read them in one pass.
- Route auth is local rather than centrally uniform. Inspect route dependencies.
- API startup runs several loops in-process; multi-replica operation needs coordination
  review.
- Kafka/Valkey, provider APIs, Dhan feeds, email, AI, and fundamentals feeds are external
  runtime dependencies.
- The Signals subsystem spans a large frontend tab tree, two backend route families,
  shared services, models, jobs, and migrations. Trace the whole contract before editing.
- The codebase currently contains uncommitted work for VAR strategy/run APIs, services,
  migrations, scripts, tests, and frontend panels. Treat those files as real working-tree
  inputs but do not overwrite unrelated edits.
- Current repository migrations extend beyond prose that still names 0051 as the head.
- Research and analysis scripts may call real providers and should not be mistaken for
  offline tests.
- A skipped service integration test or written verification guide is not evidence that
  live Dhan/Kafka/Valkey/PostgreSQL integration is working.
