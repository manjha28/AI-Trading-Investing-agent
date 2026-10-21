# Trading Copilot

A mobile-first foundation for an integrated AI trading and investing copilot. It is currently read-only: no broker credentials, live market feed, or order placement is configured.

## Prerequisites
- Python 3.11 or newer
- Node.js 20 or newer and npm

## 1. Start the FastAPI backend

```bash
cd backend
python -m venv .venv
```

Activate the environment:
- macOS/Linux: `source .venv/bin/activate`
- Windows PowerShell: `.venv\\Scripts\\Activate.ps1`

Then run:

```bash
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Start PostgreSQL before the migration. For local development, the Codespaces Compose setup provides PostgreSQL; outside Codespaces, run `docker compose -f .devcontainer/docker-compose.yml up -d postgres` from the repository root. The default local connection is `copilot:copilot` on `localhost:5432`; override `DATABASE_URL` for a separately managed database.

Check liveness at `http://127.0.0.1:8000/api/v1/health`, database readiness at `http://127.0.0.1:8000/api/v1/ready`, and the API at `http://127.0.0.1:8000/docs`.

## 2. Start the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the local URL printed by Vite (usually `http://localhost:5173`).

### Open the frontend in GitHub Codespaces

The Vite server must listen on `0.0.0.0`; the `dev` script already does this. In the Codespaces VS Code window:

1. Open the **Ports** tab in the bottom panel.
2. Find port **5173** (labelled **Vite**) and choose **Open in Browser**. If it is not listed, use **Forward a Port** and enter `5173`.
3. Keep the port private unless you intentionally need to share the app. Do not open port `8000` expecting the React UI; that is the API.
4. If the app was installed as a PWA, first open the live forwarded Vite port in a browser tab. A previously installed app can show an old/offline shell.

Port `5173` is configured with `onAutoForward: openBrowser` in `.devcontainer/devcontainer.json`. The actual forwarded URL is Codespaces-specific; `localhost:5173` inside the dev container is not necessarily the same as `localhost` on your personal computer.

### Frontend troubleshooting

Run these checks in the Codespaces terminal from the repository root:

```bash
ps -eo pid,stat,args | grep -E '[v]ite|[n]pm run dev'
curl -I http://127.0.0.1:5173/
curl -i -H "Host: ${CODESPACE_NAME}-5173.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}" http://127.0.0.1:5173/
curl -i http://127.0.0.1:5173/api/v1/health
curl -i http://127.0.0.1:5173/api/v1/ready
```

- If the Vite process exists and the first request returns `HTTP 200`, the frontend server is running inside the container. Use the forwarded **5173** entry in the Ports tab; a successful container-local `curl` alone does not prove that browser port forwarding is available.
- If no Vite process exists, start one in a terminal and leave that terminal running:

  ```bash
  cd frontend
  npm run dev
  ```

- If Vite reports that port `5173` is already in use, inspect the process before acting. Reuse the running service if it is this project; do not start duplicate servers or terminate an unknown process. Stop a server from the terminal where it was started with `Ctrl+C`.
- If port `5173` returns `200` but the browser still does not show the page, reopen it from the Ports tab, verify that the forwarded port is available, and inspect the browser developer console for JavaScript errors. A hard reload of the forwarded page can resolve stale browser assets.
- This repo's Vite config allows only the exact Codespaces hostname `${CODESPACE_NAME}-5173.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}` while retaining Vite's host protection. If the forwarded route fails, compare its host to these environment values and verify that a direct request using that `Host` header is not rejected. Do not set Vite `allowedHosts: true`; that disables host validation and can expose the dev server to DNS-rebinding attacks.
- If `/api/v1/health` fails, the backend is stopped or its API proxy target is unavailable. Start the backend in a separate terminal:

  ```bash
  cd backend
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  ```

- `GET /api/v1/auth/me` returning `401` in a signed-out browser is expected; the frontend should show the sign-in screen. Database readiness `200` only proves a connection/`SELECT 1`, not that migrations are applied.

## Current scope
- Responsive React + TypeScript dashboard shell
- FastAPI application with CORS configuration
- Liveness and database-readiness endpoints
- PostgreSQL connection, Alembic migrations, user registration/login/logout, and session-backed identity
- Owner-scoped portfolio create/list/read endpoints with ISO currency validation
- Password hashing, HttpOnly session cookies, CSRF checks on authenticated mutations, and user-isolation tests
- Portfolio transaction ledger for deposits, withdrawals, dividends, and long-only cash-equity buys/sells
- Decimal-based weighted-average cost, fees, cash controls, realized P&L, and derived open positions
- Portfolio transaction CRUD and CSV export/bounded import with preview, row validation, ledger checks, and duplicate skipping
- Responsive portfolio UI with account creation, balances, position cost basis, manual entry, activity view, CSV transfer, and transaction editing/deletion
- User-owned journal trades for equities, futures, options, and crypto; long/short net P&L, recorded fees, contract multipliers, option details, tags, filters, editing, and deletion
- Explicit USD/INR journal accounting; analytics are currency-filtered and no FX conversion is implied
- Journal summary and strategy breakdown analytics for closed trades, including net P&L, win/loss counts, expectancy, and profit factor
- Venue-qualified instrument master with stable internal IDs, multiple provider identifier mappings, derivative metadata, and a deterministic mock provider
- Authenticated instrument search/details, mock or optional provider quotes, bounded historical OHLCV sync/retrieval, idempotent storage, cached quote freshness, provider status, and provenance-aware history freshness
- Installable PWA manifest and service worker caching only the static app shell/assets; API responses are excluded
- Codespaces devcontainer with PostgreSQL service
- Portfolio market values remain unavailable. The default mock stores only explicitly synthetic fixtures; an optional Twelve Data adapter is available but has no configured credentials or verified live coverage. Journal CSV import, corporate actions, research, backtesting, live orders, and AI are not connected.

Authentication routes are under `/api/v1/auth`: `POST /register`, `POST /login`, `GET /me`, and `POST /logout`. Sessions are opaque server-side records; password and session tokens are stored only as hashes. Portfolio writes require the `csrf_token` cookie value in the `X-CSRF-Token` header. Production cookies are Secure; configure HTTPS and an exact `FRONTEND_ORIGIN` before deployment. Authentication rate limiting, email verification, account recovery, corporate actions, and FX conversion remain unfinished.

Portfolio transaction CSV uses exactly `transaction_type,symbol,quantity,unit_price,amount,fee,occurred_at`; trade rows leave `amount` blank, and cash/dividend rows leave `quantity` and `unit_price` blank. Timestamps must include a timezone. The importer accepts up to 1 MB and 1,000 rows, shows a preview, rejects invalid ledger changes as a batch, and skips rows matching existing transactions or earlier rows in the same file. Export is portfolio-scoped and marked `no-store`.

## Market data (Phase 4 foundation)

The configured provider defaults to `mock`, a deterministic synthetic fixture provider. Its `DEMO_*` instruments and OHLCV values are not exchange-listed securities or real prices. Search results explicitly report `is_synthetic=true` and provider mappings as unverified. An optional Twelve Data adapter supports [symbol search](https://twelvedata.com/docs/discovery/symbol-search), [quotes](https://twelvedata.com/docs/market-data/quote), and [time series](https://twelvedata.com/docs/market-data/time-series) for common stocks, ETFs, indices, and cryptocurrencies in its supported country mappings. It is not enabled by default; live connectivity, venue coverage, entitlements, and licensing have not been verified. Unsupported provider asset types and countries are omitted from search results. Do not use fixture data for investing decisions or portfolio valuations.

Authenticated API endpoints:
- `GET /api/v1/market-data/provider` — provider health and capabilities.
- `GET /api/v1/market-data/instruments?query=...&limit=...` — provider search and canonical instrument persistence; same symbols at different venues remain distinct.
- `GET /api/v1/market-data/instruments/{id}` — instrument metadata and provider identifiers.
- `GET /api/v1/market-data/instruments/{id}/quote` — latest quote through the configured adapter, cached per provider for `MARKET_DATA_QUOTE_FRESHNESS_SECONDS` (default 60). A stale cached quote is returned with an explicit stale status if the refresh fails; no cached quote returns a service-unavailable error.
- `POST /api/v1/market-data/instruments/{id}/history/sync?interval=1d&start=...&end=...&limit=...` — fetch, validate, and idempotently store bounded OHLCV history.
- `GET /api/v1/market-data/instruments/{id}/history?interval=...&start=...&end=...&offset=...&limit=...` — retrieve stored bars and freshness/provenance metadata.

Intervals are `1d`, `1h`, and `15m`; range caps are 366, 31, and 7 days respectively, and each request is limited to at most 500 bars. Dates must be timezone-aware; stored timestamps are normalized to UTC. Conflicting duplicate bars are ignored rather than overwritten. History freshness reports `fresh`, `stale`, `unavailable`, or `never_fetched`; it is based on the last successful fetch, while also exposing the latest bar time. Quote freshness uses the last successful quote retrieval and its configured cache window. Closed-market detection and scheduled refresh are not implemented. Configure `MARKET_DATA_PROVIDER`, `MARKET_DATA_QUOTE_FRESHNESS_SECONDS`, `MARKET_DATA_DAILY_FRESHNESS_SECONDS`, and `MARKET_DATA_INTRADAY_FRESHNESS_SECONDS` in the backend environment. To select Twelve Data, set `MARKET_DATA_PROVIDER=twelve_data` and provide `TWELVE_DATA_API_KEY` in the backend environment only; never put provider credentials in frontend settings. The adapter applies bounded timeouts, response sizes, bar counts, and one retry for transient transport/server errors. It does not retry rate-limit responses. Check the provider's current plan, exchange entitlements, and licensing before use. Provider status reports configured credentials, not verified live connectivity; the first successful upstream request is still required to verify access.

Run backend tests with `cd backend && python -m pytest -q`. Frontend type-check/build with `cd frontend && npm run build`.

## Build checklist
- [x] Phase 1: backend/frontend baseline, PostgreSQL connectivity, migrations, health/readiness, Codespaces setup
- [x] Phase 2: authentication, normalized user/session/portfolio models and user isolation
- [x] Phase 3: portfolio CSV import/export and duplicate detection; portfolio/journal CRUD, accounting, and currency-isolated journal analytics
- [ ] Phase 4: instrument master, provider adapters, history and freshness (mock and optional Twelve Data adapter implemented; live credentials and coverage not verified)
- [ ] Phase 5: sourced research, valuation and scenarios
- [ ] Phase 6: indicators, backtesting, risk controls and paper trading
- [ ] Phase 7: persisted alerts, scheduled reports and AI tools
- [ ] Phase 8: mobile/PWA polish, security, deployment and end-to-end verification

Phases 1-3 are complete for the currently scoped behaviors. Phase 4 is in progress: the normalized instrument master, provider abstraction, mock fixture, optional Twelve Data adapter, historical storage, quote cache, freshness API, and market-data UI are implemented. Real credentials, provider connectivity, and verified market coverage are not configured; research, backtesting, live orders, and AI remain incomplete. Migrations `0001` through `0008` are additive and preserve existing application data. The PWA shell can load offline after installation, but authentication and private data require a network connection; no guaranteed background refresh or iOS push behavior is claimed.

The accounting methods are intentionally limited: portfolio equity positions use long-only weighted-average cost, fees are capitalized on buys and deducted from sale proceeds, cash is rounded to cents with half-even rounding, and oversells/negative cash are rejected. Journal net P&L is `(exit - entry) × quantity × multiplier - fees` for long trades and the reverse price delta for shorts. Open-trade P&L and portfolio market value are unavailable without verified prices. Journal summary totals include only the requested currency; there is no currency conversion.

Never put API keys in frontend code or commit `.env` files. Market data licences, exchange permissions and broker rules must be checked before production use.

## Continuation checkpoint (2026-10-21)

This section records the last inspected workspace state to make future work easier to resume. Re-check Git, services, and the database before relying on a dated status.

- Git branch is `main`, tracking `origin/main`; the inspected local branch was even with its upstream. The working tree contains a large, uncommitted implementation spanning the backend foundation, frontend, Codespaces setup, migrations `0001`-`0008`, and tests. Preserve all modified and untracked work; do not reset, clean, stage broadly, amend, or push this work as one batch.
- The frontend dev server was listening on `0.0.0.0:5173`; local requests to `/`, source modules, and proxied API health/readiness returned `200`, and `/api/v1/auth/me` returned the expected signed-out `401`. These local checks alone did not diagnose the browser failure.
- Diagnosis of the reported browser `502`: the Codespaces hostname matched this workspace, but a request carrying that forwarded `Host` header was rejected by Vite with `403 Blocked request. This host ... is not allowed.` The exact-host configuration in `frontend/vite.config.ts` now permits the generated port-5173 Codespaces hostname derived from `CODESPACE_NAME` and `GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`; after configuration reload, both the forwarded-host root request and proxied health check returned `200`. Keep the port private and do not allow all hosts.
- Backend test command: `cd backend && python -m pytest -q`. Last recorded result after the market-data quote cache work: **45 passed**, with one Starlette/httpx deprecation warning.
- Frontend build command: `cd frontend && npm run build`. Last recorded result: **passed**; Vite reported non-fatal `lucide-react` `"use client"` directive notices.
- Alembic offline SQL generation through `0008_market_quote_cache` succeeded. The reachable PostgreSQL database `trading_copilot` had no public tables or `alembic_version` row at last inspection. No migration was applied. Do not infer migration readiness from `/api/v1/ready`; it checks connectivity only.
- Market-data provider defaults to `mock`. Twelve Data support is optional and credentials are not configured; no live connection, venue coverage, or entitlement has been verified. Synthetic fixtures are never suitable for portfolio valuation.
- Phase 4 currently includes the normalized instrument/provider mapping schema, bounded/idempotent history sync and retrieval, history freshness/provenance, optional Twelve Data search/quote/history adapter, persistent quote cache with stale fallback, and a market-data UI. Phase 4 remains unverified against live provider access and a migrated PostgreSQL deployment.
- Phases 5-8 remain outstanding: sourced research and scenario models; indicators/backtesting/risk/paper trading; persisted alerts/reports/AI tools; and final mobile/security/deployment/end-to-end verification. Live order execution must remain disabled.
- Before each future milestone, inspect the full worktree and diff, select only the milestone's related files, run focused tests/build/migration checks, and review staged contents for secrets and generated files. Commit/push only a coherent passing milestone to the configured upstream; never force-push or overwrite unrelated changes.
