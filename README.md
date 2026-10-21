# Trading Copilot

A mobile-first foundation for an integrated AI trading and investing copilot. Broker integrations are read-only and disabled by default; there is no broker order placement.

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

Start PostgreSQL before the migration. For local development, the Codespaces Compose setup provides PostgreSQL; outside Codespaces, run `docker compose -f .devcontainer/docker-compose.yml up -d postgres` from the repository root. The default connection uses the Compose hostname `postgres` and the `copilot` database user; when running the backend outside the Compose network, override `DATABASE_URL` to use `localhost` or your managed database host.

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
- Portfolio valuation is available for instrument-linked holdings and explicitly marks missing, stale, and synthetic prices as unreliable. Journal CSV import, corporate actions, research, backtesting, live orders, and AI are not connected.

Authentication routes are under `/api/v1/auth`: `POST /register`, `POST /login`, `GET /me`, `POST /verify-email`, `POST /verification/resend`, and `POST /logout`. Registration returns the same generic acknowledgement for new and existing email addresses to prevent enumeration; sign in after registration. Accounts remain unverified until a single-use, hashed, 24-hour token is confirmed. The verification email is sent through Resend; delivery failure does not mark the account verified, and a signed-in user can request a rate-limited resend. Sessions are opaque server-side records; password and session tokens are stored only as hashes. Portfolio writes require the `csrf_token` cookie value in the `X-CSRF-Token` header. Production cookies are Secure; configure HTTPS and an exact `FRONTEND_ORIGIN` before deployment. Account recovery, broad authentication rate limiting, corporate actions, and FX conversion remain unfinished.

For email verification, create a Resend account, add and verify a domain you control, then create an API key with email-send access. Set these backend-only values:

```dotenv
RESEND_API_KEY=
RESEND_FROM_EMAIL=Trading Copilot <accounts@mail.example.com>
EMAIL_VERIFICATION_URL=https://app.example.com/
```

The `RESEND_FROM_EMAIL` domain must be verified in Resend. `EMAIL_VERIFICATION_URL` must be the public frontend root; links use a URL fragment (`/#verify-email/<single-use-token>`) so the token is not sent in the frontend HTTP request. In Codespaces, set it to the forwarded port-5173 URL. Do not use `FINNHUB_API_KEY` for mail. Apply migration `0012_email_verification` with `cd backend && alembic upgrade head`.

Portfolio transaction CSV uses exactly `transaction_type,symbol,quantity,unit_price,amount,fee,occurred_at`; trade rows leave `amount` blank, and cash/dividend rows leave `quantity` and `unit_price` blank. Timestamps must include a timezone. The importer accepts up to 1 MB and 1,000 rows, shows a preview, rejects invalid ledger changes as a batch, and skips rows matching existing transactions or earlier rows in the same file. Export is portfolio-scoped and marked `no-store`.

## Market data (Phase 4 foundation)

The ordered provider chain defaults to `delta_exchange,binance,yfinance,upstox,mock`. It starts with unauthenticated free sources, so real market data is available with a completely empty `.env`: Delta Exchange and Binance provide public crypto market data, and yfinance provides equities without an API key. Upstox is an opt-in NSE source and stays inactive until an authenticated user account is connected (M3). Providers that fail or do not cover a symbol are skipped in order. Mock data is restricted to two `ALPHA` demo instruments (US and India); these are not exchange-listed securities or real prices. Real instruments never fall back to synthetic quotes or bars: when all live providers fail, prices are unavailable, and previously stored synthetic data is hidden. Portfolio valuation explicitly marks synthetic, stale, and unavailable prices unreliable. Search persists provider identifiers against the canonical instrument so resolved symbols are reused. Yahoo Finance maps NSE to `.NS`, BSE to `.BO`, and US symbols without a suffix; its 1m/15m/1h history windows are limited to 7/60/730 days respectively. Delta Exchange and Binance candles paginate at their upstream caps. Twelve Data and Finnhub remain optional, disabled-by-default US-market fallbacks that require their own API keys; Twelve Data's free tier excludes NSE/BSE and Finnhub free does not provide equity candles. Provider coverage, live connectivity, entitlements, and licensing must still be verified. Do not use synthetic fallback data for investing decisions.

Authenticated API endpoints:
- `GET /api/v1/market-data/provider` — provider health and capabilities.
- `GET /api/v1/market-data/instruments?query=...&limit=...` — provider search and canonical instrument persistence; same symbols at different venues remain distinct.
- `GET /api/v1/market-data/instruments/{id}` — instrument metadata and provider identifiers.
- `POST /api/v1/market-data/instruments/cleanup-synthetic` — remove unlinked demo instruments, preserve linked portfolio instruments, flag them for relinking, and report real replacement matches when providers resolve them.
- `GET /api/v1/market-data/instruments/{id}/quote` — latest quote through the provider chain, cached per provider for `MARKET_DATA_QUOTE_FRESHNESS_SECONDS` (default 60). Add `?refresh=true` to bypass a fresh cache. Synthetic quotes are available only for the two `ALPHA` demos; real instruments return unavailable when live providers fail.
- `POST /api/v1/market-data/instruments/{id}/history/sync?interval=1d&start=...&end=...&limit=...` — fetch, validate, and idempotently store bounded OHLCV history.
- `GET /api/v1/market-data/instruments/{id}/history?interval=...&start=...&end=...&offset=...&limit=...` — retrieve stored bars and freshness/provenance metadata.
- `POST /api/v1/portfolios/{id}/positions/{symbol}/instrument` — link a searched instrument to all existing buy/sell transactions for an open holding. Existing transactions remain valid and unlinked until attached.
- `GET /api/v1/portfolios/{id}/valuation` — return per-position values and portfolio totals with price freshness, synthetic flags, confidence, and reasons. Foreign-currency holdings are not converted and remain unavailable in a differently denominated portfolio.
- `POST` / `DELETE /api/v1/market-data/watchlist/{instrument_id}` — include or remove an instrument from the authenticated user's background quote-refresh watchlist.

Intervals are `1d`, `1h`, `15m`, and `1m`; yfinance range caps are unlimited for daily bars, 730, 60, and 7 days respectively for the other intervals, and each retrieval request is limited to at most 500 bars. Dates must be timezone-aware; stored timestamps are normalized to UTC. Conflicting duplicate bars are ignored rather than overwritten, and stored ranges are reused rather than fetched again. History freshness reports `fresh`, `stale`, `unavailable`, or `never_fetched`; it is based on the last successful fetch, while also exposing the latest bar time. Quote freshness uses the last successful quote retrieval and its configured cache window. The optional background quote scheduler is disabled by default; enable it with `MARKET_DATA_SCHEDULER_ENABLED=true` and set `MARKET_DATA_REFRESH_SECONDS` (default 300). It refreshes instruments linked to holdings and the authenticated watchlist. The scheduler does not start during tests.

Set these backend `.env` values:

```dotenv
MARKET_DATA_PROVIDERS=delta_exchange,binance,yfinance,upstox,mock
TWELVE_DATA_API_KEY=
FINNHUB_API_KEY=
MARKET_DATA_QUOTE_FRESHNESS_SECONDS=60
MARKET_DATA_DAILY_FRESHNESS_SECONDS=172800
MARKET_DATA_INTRADAY_FRESHNESS_SECONDS=3600
MARKET_DATA_REFRESH_SECONDS=300
MARKET_DATA_SCHEDULER_ENABLED=false
```

Provider keys belong in the backend environment only. The default chain does not read Twelve Data or Finnhub credentials; add either provider explicitly to `MARKET_DATA_PROVIDERS` to opt in. yfinance requires no API key. The detail-page history uses lightweight-charts for native candlesticks and volume, with a line toggle and crosshair OHLC readout. Values remain full precision in the API and CSV; the history table displays prices to two decimal places.

## Read-only broker connections (M3)

Broker endpoints only read profile, holdings, positions, order history, and funds. There is deliberately no broker order placement, modification, or cancellation method or endpoint. Broker holdings and positions are stored in a separate user-scoped mirror and do not modify the manual portfolio ledger. Reconciliation imports create ordinary manual buy/sell ledger entries only after the user confirms each difference. The `mock_broker` is always available but returns fixed, clearly synthetic demo values.

Broker API routes are under `/api/v1/brokers`:

- `GET /api/v1/brokers` — supported brokers and the current user's connection status.
- `POST /api/v1/brokers/{slug}/connect` — starts the broker's official redirect/consent flow. Angel One is explicitly pending; Trading Copilot never collects broker passwords, PINs, or OTPs.
- `GET /api/v1/brokers/{slug}/callback` — handles broker callback and stores encrypted tokens.
- `POST /api/v1/brokers/{slug}/sync` — refreshes the read-only holdings/positions mirror idempotently.
- `GET /api/v1/brokers/{slug}/mirror?kind=holding|position` — reads the current user's mirror.
- `GET /api/v1/brokers/{slug}/account` — reads profile, funds, and order history from the connected broker; never returns credentials.
- `GET /api/v1/brokers/{slug}/reconciliation?portfolio_id=...` — compares broker holdings with the selected manual portfolio.
- `POST /api/v1/brokers/{slug}/reconciliation/import` — creates explicitly confirmed manual transactions for selected differences.
- `DELETE /api/v1/brokers/{slug}` — hard-deletes that user's encrypted broker credential and pending OAuth state.

Every broker route requires an authenticated session with a verified email and a completed 2FA step, and all mutating requests require CSRF validation. Email verification is implemented; session 2FA enrollment and completion are still pending, so real broker access remains blocked until a real 2FA flow is implemented. Do not manually mark accounts or sessions verified to bypass the gate. The mock broker is clearly synthetic and is not a real broker connection.

Set `BROKER_ENCRYPTION_KEY` in the backend environment before connecting any broker. Generate a Fernet key with:

```bash
cd backend
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the key stable and private: losing it makes stored broker tokens unreadable. Never commit it or expose broker secrets to the frontend. Each real adapter is opt-in and fails closed unless its enable flag is true and every required credential and callback setting is present:

| Broker | Required backend environment | Login callback URL / flow |
| --- | --- | --- |
| Zerodha Kite Connect | `BROKER_ZERODHA_ENABLED=true`, `ZERODHA_API_KEY`, `ZERODHA_API_SECRET`, `ZERODHA_REDIRECT_URL` | Register the exact URL `http://localhost:5173/api/v1/brokers/zerodha/callback` locally, or `https://<codespace>-5173.app.github.dev/api/v1/brokers/zerodha/callback` in Codespaces, in the Kite developer console. Kite redirects with a short-lived `request_token`; ordinary access tokens cannot be refreshed, so reconnect after expiry. |
| Upstox | `BROKER_UPSTOX_ENABLED=true`, `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, `UPSTOX_REDIRECT_URI` | Register the exact URL `http://localhost:5173/api/v1/brokers/upstox/callback` locally, or the corresponding forwarded Codespaces URL. Upstox returns a single-use authorization code. Standard OAuth access tokens do not provide a documented refresh-token grant; reconnect when expired. |
| Angel One SmartAPI | Not enabled | Pending. The documented SmartAPI login requires collecting the user's client PIN/TOTP in a way this application intentionally does not support. No credentials are requested or accepted. |
| Dhan | `BROKER_DHAN_ENABLED=true`, `DHAN_API_KEY`, `DHAN_API_SECRET`, `DHAN_REDIRECT_URI` | Register `http://localhost:5173/api/v1/brokers/dhan/callback` locally, or the corresponding forwarded Codespaces URL. Dhan uses its consent login and returns a `tokenId` to this callback; it is exchanged server-side. |

For Codespaces, set each broker redirect variable to the exact HTTPS forwarded **frontend** URL shown in the Ports panel (port 5173), and register the same URL with that broker. The callback path must match exactly. Do not forward the backend API port publicly for broker callbacks. Zerodha documents the request-token exchange and next-day token expiry in [Kite Connect User API](https://kite.trade/docs/connect/v3/user/); Upstox documents its official OAuth authorization-code flow in [Upstox Authentication](https://upstox.com/developer/api-documentation/authentication/); Dhan documents individual API-key consent in [DhanHQ Authentication](https://dhanhq.co/docs/v2/authentication/). Each requires the user's developer app/API access and exact redirect registration; verify current access eligibility and any commercial/approval terms in each broker's developer console before enabling it. No live credentials or broker account were available here, so live authentication and permissions have not been verified. No additional broker is currently supported beyond the listed adapters.

After configuring the backend environment, apply all additive migrations through `0012_email_verification` with `cd backend && alembic upgrade head`.

Run backend tests with `cd backend && python -m pytest -q`. Frontend type-check/build with `cd frontend && npm run build`.

## Build checklist
- [x] Phase 1: backend/frontend baseline, PostgreSQL connectivity, migrations, health/readiness, Codespaces setup
- [x] Phase 2: authentication, normalized user/session/portfolio models and user isolation
- [x] Phase 3: portfolio CSV import/export and duplicate detection; portfolio/journal CRUD, accounting, and currency-isolated journal analytics
- [ ] Phase 4: instrument master, provider chain, history, portfolio valuation, and freshness (code complete; live credentials and coverage not verified)
- [ ] Phase 5: sourced research, valuation and scenarios
- [ ] Phase 6: indicators, backtesting, risk controls and paper trading
- [ ] Phase 7: persisted alerts, scheduled reports and AI tools
- [ ] Phase 8: mobile/PWA polish, security, deployment and end-to-end verification

Phases 1-3 are complete for the currently scoped behaviors. Phase 4 includes a free-first Delta Exchange/Binance/yfinance market-data chain, two explicit synthetic `ALPHA` demos, persisted symbol mappings, nullable instrument links on transactions, portfolio valuation, optional background quote refresh, and live/synthetic/unavailable UI states. Upstox and the paid providers remain opt-in. Migrations `0001` through `0010` are additive and preserve existing application data. The PWA shell can load offline after installation, but authentication and private data require a network connection; the optional quote scheduler is disabled by default.

The accounting methods are intentionally limited: portfolio equity positions use long-only weighted-average cost, fees are capitalized on buys and deducted from sale proceeds, cash is rounded to cents with half-even rounding, and oversells/negative cash are rejected. Journal net P&L is `(exit - entry) × quantity × multiplier - fees` for long trades and the reverse price delta for shorts. Portfolio market value is reliable only when every position is linked and all quotes are fresh, non-synthetic, and denominated in the portfolio currency. Journal summary totals and portfolio valuation do not convert FX.

Never put API keys in frontend code or commit `.env` files. Market data licences, exchange permissions and broker rules must be checked before production use.

## Continuation checkpoint (2026-10-21)

The Phase 4 baseline was committed and pushed to `origin/main`; the `phase4-baseline` tag identifies that baseline. All current schema tables are applied, and the database contains a real user row. The Codespaces 502 was resolved by using VS Code Desktop: browser port forwarding was the failure, not the application. Recheck the live worktree and database before relying on this checkpoint; current M1.5 changes may be uncommitted.

- Phases 5-8 remain outstanding: sourced research and scenario models; indicators/backtesting/risk/paper trading; persisted alerts/reports/AI tools; and final mobile/security/deployment/end-to-end verification. Live order execution must remain disabled.
The market-data default is free-first (`delta_exchange,binance,yfinance,upstox,mock`) and requires no provider credentials for public crypto/equity data. Only two `ALPHA` demos are synthetic; real instruments show unavailable when all live providers fail. Apply migration `0010_free_market_data_and_relink` after the Phase 4 migrations. Phases 5-8 remain outstanding: sourced research and scenario models; indicators/backtesting/risk/paper trading; persisted alerts/reports/AI tools; and final mobile/security/deployment/end-to-end verification. Live order execution must remain disabled.
