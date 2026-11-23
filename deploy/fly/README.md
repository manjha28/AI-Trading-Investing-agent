# Hosting FinByte Nexus on Fly.io (forward testing)

Two apps: `APP_NAME-api` (FastAPI, one always-on machine, Mumbai) and `APP_NAME-web` (nginx + built frontend).
Everything below runs from the **repo root**. Nothing here arms live orders: `DHAN_LIVE_EXECUTION_ENABLED` and every research flag stay off.

1. Install and log in (your terminal): `curl -L https://fly.io/install.sh | sh`, then `fly auth login`.
2. Pick a unique name and replace `APP_NAME` in both `*.fly.toml` files.
3. Database: create a Managed Postgres cluster in the Fly dashboard (Managed Postgres), then
   `fly mpg attach <cluster id> -a APP_NAME-api` (sets `DATABASE_URL`; make sure it uses the `postgresql+psycopg://` form the app expects).
4. Create the apps (no deploy yet):
   `fly launch --no-deploy --copy-config -c deploy/fly/backend.fly.toml` and the same for `frontend.fly.toml`.
5. Secrets (values from your local `backend/.env`; do not paste them into chat or commit them):
   `fly secrets set -a APP_NAME-api BROKER_ENCRYPTION_KEY=... TWO_FACTOR_ENCRYPTION_KEY=... ALLOWED_ORIGINS=https://APP_NAME-web.fly.dev FRONTEND_ORIGIN=https://APP_NAME-web.fly.dev TRUSTED_PROXY_HOSTS=* RESEND_API_KEY=...`
   Use the SAME encryption keys as local, or the stored Dhan token cannot be decrypted and you simply reconnect Dhan.
6. Deploy: `fly deploy -c deploy/fly/backend.fly.toml` then `fly deploy -c deploy/fly/frontend.fly.toml`.
7. Optional: move your local data: `pg_dump` local, `fly mpg proxy` and `pg_restore` into the cluster (stop the app first).
8. Static IP for Dhan orders: allocate a dedicated egress IP for the API app (check current Fly docs for the command and price), register it in Dhan, and only then consider arming live execution.

## State as deployed (2026-11-23)
- Region `sin` (Managed Postgres is not offered in Mumbai). API: 1 always-on machine, shared-cpu-4x, 4 GB. Web: nginx, auto-stops when idle.
- Database: Managed Postgres Basic plan (the $4 Developer plan was too small: 256 MB). Schema via `alembic upgrade head`; data copied from local except `futures_candles_1m`, `options_candles_1m`, `rolling_options_candles_1m` and 1-minute `market_bars` older than 30 days.
- The web app reaches the API through its public `*.fly.dev` hostname (Fly's edge needs an IPv4 listener; `--host 0.0.0.0`).
- Secrets are set with `fly secrets`; `DHAN_LIVE_EXECUTION_ENABLED` is not set (off). Orders still need a static egress IP registered with Dhan.
