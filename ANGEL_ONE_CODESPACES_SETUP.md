# Angel One SmartAPI setup in GitHub Codespaces

This guide configures Angel One for **market data** in Trading Copilot. It does
not connect an Angel One broker account for holdings, and it cannot place
orders. All prices and analysis are decision support, not investment advice.

## 1. Get the SmartAPI API key

1. Sign in to the official [Angel One SmartAPI developer portal](https://smartapi.angelone.in/).
2. Open **My Apps** (or the app/API-key section shown in your portal account).
3. Open an existing application or create one for Trading Copilot. Copy its
   **API Key**. The API key is not your client code, PIN, or authenticator code.
4. Confirm the application and your Angel One account are permitted to use
   SmartAPI market-data endpoints. If the portal labels differ, use its API-key
   or application settings; do not substitute credentials from the Angel One
   trading app.

The application uses the API key plus your client code, PIN, and current
authenticator code at connection time. It does not require an API secret.

## 2. Find the network values for the Codespace

Run these commands in the Codespaces terminal where the **backend** will run.
Do not use the IP address shown by your browser or home router.

1. Get the current outbound public IPv4 address:

   ```bash
   curl -4 https://api.ipify.org
   ```

   Copy the result to `ANGEL_ONE_CLIENT_PUBLIC_IP`. This is the public address
   Angel One sees for requests leaving this Codespace. Codespaces outbound
   addresses are not guaranteed to be static. Recheck this value after
   recreating or moving the Codespace. If the address changes, update the
   portal allowlist and this setting. If SmartAPI requires a stable allowlist
   and the Codespace cannot provide one, use a backend environment with a
   supported static outbound IP; do not enter a guessed address.

2. Find the local address and network interface used for outbound requests:

   ```bash
   ip -4 route get 1.1.1.1
   ```

   In the output, copy the value after `src` to `ANGEL_ONE_CLIENT_LOCAL_IP` and
   note the interface after `dev` (often `eth0`).

3. Find that interface's MAC address:

   ```bash
   ip -o link show
   ```

   Find the row for the interface noted above. Copy the value after `link/ether`
   to `ANGEL_ONE_CLIENT_MAC_ADDRESS`. This is the Codespace interface's MAC,
   not your laptop's MAC. These container/network values can change when a
   Codespace is recreated; verify them again if authentication starts failing.

4. In the SmartAPI application settings, register the public IP and MAC if the
   portal asks for them. The exact IP/MAC allowlist UI is controlled by Angel
   One. Trading Copilot sends the local IP, public IP, and MAC in the SmartAPI
   request headers; it does not infer or replace them.

## 3. Set backend environment variables

Edit `backend/.env` in the Codespace. Add or update these lines without
replacing the rest of the file:

```dotenv
ANGEL_ONE_API_KEY=<API key copied from the SmartAPI application>
ANGEL_ONE_CLIENT_PUBLIC_IP=<current outbound public IPv4 from step 2>
ANGEL_ONE_CLIENT_LOCAL_IP=<local source IPv4 from step 2>
ANGEL_ONE_CLIENT_MAC_ADDRESS=<MAC for the outbound interface from step 2>
```

Keep the API key and all account credentials in the backend only. Do not put
them in `frontend/.env`, source code, screenshots, or chat messages.

The existing backend also needs its encryption keys:

```dotenv
BROKER_ENCRYPTION_KEY=<existing backend Fernet key>
TWO_FACTOR_ENCRYPTION_KEY=<existing backend Fernet key>
```

These keys protect stored session and two-factor data. If they are already set,
keep the existing values; changing them can make previously encrypted data
unreadable. Do not copy example keys into a working Codespace.

If a key is blank and this is a new environment with no broker credentials or
authenticator secrets already stored, generate a Fernet key from `backend/`:

```bash
cd /workspaces/AI-Trading-Investing-agent/backend
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run the command separately for each setting and keep each result private and
stable. If encrypted data already exists and a key is missing, stop and recover
the original key instead of generating a replacement.

`MARKET_DATA_PROVIDERS` already defaults to including `angel_one,dhan,...,yfinance`.
The application tries the manually selected provider first, then the automatic
Angel One → Dhan → Yahoo Finance fallback order. A connected provider is not
proof that a fresh quote or candle is available.

## 4. Callback and two-factor authentication

There is **no callback URL to register for this market-data connection**. The
application connects using SmartAPI's direct login endpoint from the backend;
the user enters their client code, PIN, and current six-digit TOTP in the
Market Data page. The generic broker callback integration is a separate flow,
and the repository's Angel One broker adapter is marked pending/disabled. Do
not register a guessed Codespaces callback URL for this market-data flow.

Before connecting:

1. Enable app-based two-factor authentication for the Angel One account using
   Angel One's current account-security instructions.
2. Set up the authenticator app for the Angel One account and make sure the
   device clock is correct. Use a newly generated six-digit code; never reuse
   an expired code.
3. In Trading Copilot, sign in with a verified email and complete the
   application's own app-based two-factor verification for this session.
   Angel One connection is blocked unless both conditions are met.

The authenticator code and PIN are submitted for the login request. The
resulting session tokens are stored encrypted by the backend. They are not
market prices or broker-order credentials.

## 5. Apply/check the database migration

The migration is additive and creates `user_market_data_settings`; it does not
reset user or market-data records. In a Codespaces terminal:

```bash
cd /workspaces/AI-Trading-Investing-agent/backend
alembic current
alembic upgrade head
python -c 'from sqlalchemy import inspect; from app.db import engine; print(inspect(engine).has_table("user_market_data_settings"))'
alembic check
```

The current head should be `0023_md_provider_settings`, the table check should
print `True`, and `alembic check` should report no new upgrade operations.
Never use `alembic stamp` or delete/recreate the database to resolve a migration
problem. If any command fails, stop and inspect that error before proceeding.

## 6. Restart the backend and frontend

In the backend terminal, stop the existing process with **Ctrl+C**. Start it
again from the backend directory so it reloads `backend/.env`:

```bash
cd /workspaces/AI-Trading-Investing-agent/backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

In a second Codespaces terminal, start or restart the frontend:

```bash
cd /workspaces/AI-Trading-Investing-agent/frontend
npm run dev
```

Use the forwarded port **5173** link in the Codespaces Ports tab. The backend
uses forwarded port **8000**. Check readiness from the Codespaces terminal:

```bash
curl -i http://127.0.0.1:8000/api/v1/ready
```

It should return HTTP `200` with `"status":"ready"`. A `503` means the database
or migrations are not ready; resolve that before connecting a provider.

## 7. Connect and test market data

1. Sign in to Trading Copilot using a verified account and complete its
   app-based two-factor verification.
2. Open **Market Data** and the market-data source/settings panel.
3. Enter your Angel One **client code**, account **PIN**, and current
   six-digit authenticator code, then choose **Connect Angel One**.
4. On success, the page reports: **“Angel One connected. Select it as the
   active data source to use its market data.”** Select **Angel One SmartAPI**
   in the source dropdown.
5. Search for an NSE equity and choose a result whose provider mapping is shown
   as **verified**. Do not test using a synthetic/demo instrument.
6. Press **Refresh instrument price**. A successful Angel One REST response
   should show Angel One as the source, a market timestamp and retrieval time,
   and the response's actual freshness/provenance. A REST result is on-demand
   REST data, not a WebSocket tick.
7. Only for an eligible NSE/BSE equity, use **Connect Angel One live feed** to
   test streaming. Treat it as live only when the UI says the feed is active
   and the quote is explicitly labelled **LIVE WEBSOCKET TICK**. If the feed
   is unavailable, rely on the displayed REST/cached status instead.
8. Load history for a supported interval. Every returned bar should identify
   its provider and retrieval time. If fallback occurs, the page should show
   the failed/skipped provider, fallback reason, and source that actually
   returned the data.

Do not treat a successful login or broker connection as proof that market data
is live. Do not place orders; the application is read-only.

## Troubleshooting

- **“API key and registered client public IP must be configured”**: check the
  four Angel One variables in `backend/.env`, save the file, and restart the
  backend.
- **Invalid client code, PIN, or authenticator code**: verify the Angel One
  client code and PIN, generate a fresh TOTP, and check the authenticator
  device clock. Do not paste credentials into logs or support messages.
- **Connection succeeds but Angel One is not selectable**: refresh the Market
  Data settings panel and confirm `ANGEL_ONE_API_KEY` is configured and the
  session remains valid.
- **Quote/history falls back to Dhan or Yahoo Finance**: inspect the displayed
  provider attempts and fallback reason. Confirm the instrument mapping,
  interval support, account permissions, current public-IP allowlist, and
  provider status. A Yahoo daily close is historical and must not be read as a
  live quote.
- **A quote is stale or unavailable**: use the displayed market timestamp and
  freshness status; refresh once after checking provider availability. Do not
  use a stale/synthetic price for action decisions.
- **`/api/v1/ready` returns 503**: run `alembic current`, then the additive
  `alembic upgrade head` and `alembic check` commands above. Do not stamp or
  recreate the database.

For current provider-side requirements and account entitlements, use the
[official SmartAPI portal](https://smartapi.angelone.in/) and its
[documentation](https://smartapi.angelone.in/docs). Trading Copilot does not
verify or guarantee a user's Angel One entitlements or Codespaces egress-IP
stability.
