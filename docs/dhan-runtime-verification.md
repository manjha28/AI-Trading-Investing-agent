# Dhan runtime verification (Docker-capable host)

This procedure is for live verification of Dhan REST, Dhan WebSocket, Kafka, Valkey, PostgreSQL and
frontend provenance. It is separate from the mocked unit tests. Record the output of each step. Do not
treat a passing unit test as evidence of connectivity.

No step in this document has been executed against live services. The current development environment
has no Docker daemon, no Dhan access token, and no running PostgreSQL, Valkey or Kafka.

## 0. Rules

- Credentials come only from environment variables or a secret manager. Never paste tokens, API secrets,
  client IDs or signed URLs into tickets, logs or chat.
- Redact `access-token`, `client-id` and `Authorization` values before saving any output.
- Dhan access tokens are per-user and come from OAuth consent. Complete the consent flow in the app
  (`/api/v1/brokers/dhan/...`) before running the REST or WebSocket checks.
- Use SBIN as the diagnostic instrument: `securityId=3045`, `exchangeSegment=NSE_EQ`,
  ISIN `INE062A01020`. Verify the mapping against the current scrip master. Do not hardcode it as a
  production mapping.
- Market-closed results are not failures. Record the session state and validate the last completed
  session instead of expecting live ticks.

## 1. Start infrastructure

From the repository root, with a populated `deploy/compose/.env` (copied from the example, values kept
out of git):

```bash
cd deploy/compose
docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file .env up -d
docker compose --env-file .env ps                     # kafka, valkey, postgres must be healthy
docker compose --env-file .env exec valkey valkey-cli ping          # expect PONG (auth via REDISCLI_AUTH)
docker compose --env-file .env exec kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 --list                             # expect market.bars.v1
```

Record: container health, the topic list, and the Valkey `PONG`.

## 2. Apply migrations and start the backend

```bash
cd backend
alembic upgrade head
alembic check                                          # expect: no new upgrade operations detected
MARKET_STREAM_ENABLED=true uvicorn app.main:app --port 8000
curl -fsS http://localhost:8000/api/v1/ready           # fails while migrations are pending
```

Record the `alembic check` output and the `/ready` response.

## 3. Dhan REST: authentication and quote

Use the backend authenticated session (log in, verify email, complete the Dhan connection). Then:

1. Check the connection status endpoint. Expect the connection to be active and the token not expired.
2. Request the SBIN quote through `GET /api/v1/market-data/instruments/{instrument_id}/quote` with the
   Dhan source selected.
3. Validate the response body, not only HTTP 200. Confirm the following fields:
   - `source` is `dhan`.
   - The instrument's verified Dhan identifier is `3045` on `NSE_EQ` (see `instrument_provider_identifiers`).
   - The source timestamp and retrieved-at timestamp are present and timezone-aware.
   - The data classification (live, provider snapshot or historical close) matches the market session.
   - No yfinance provider attempt appears in `provider_attempts`.

Record HTTP status, Dhan error code and message (if any), the timestamps and the classification.

## 4. Dhan REST: historical candles

1. Request daily candles for SBIN with `source=dhan` over a window that ends on the current date.
2. Confirm that the latest returned candle is the last completed session. `toDate` is non-inclusive, so a
   candle on the `toDate` day should not appear unless it is complete.
3. Confirm that the candle timestamps are UTC epoch values and that the IST session boundary is
   correct (09:15 to 15:30 IST for NSE_EQ).
4. Request a window that spans a weekend and a known exchange holiday. Confirm that no candle is invented
   for those days.
5. Request a window with no trading days. Expect an explicit empty result with a provider attempt, not
   a fallback provider's bars.
6. Compare the latest completed candle close with the Dhan web or app chart for the same session. Record
   the comparison result.

## 5. Dhan WebSocket

1. Confirm the feed endpoint and protocol version used by the adapter match the Dhan documentation at
   the time of the run.
2. Start the market-stream worker with the same environment (confirm its entrypoint in
   `backend/app/market_stream/` first; this procedure has not verified the command). Confirm the subscription log entry shows
   the request code the adapter sends (currently 17, the quote subscription, labelled "ticker" in the
   code) for `NSE_EQ` security `3045`, and that Dhan acknowledges the subscription.
3. During market hours, confirm that binary packets are received and parsed. Record the packet type,
   security ID, LTP, LTT (IST) and the ingestion timestamp.
4. Confirm a heartbeat or packet arrives within the expected interval. Force a disconnect (for example,
   stop the network for 30 seconds) and confirm reconnection and resubscription.
5. Outside market hours, record that no new live ticks are expected. Do not count an empty stream as a
   failure. Confirm the connection stays authenticated and idle.
6. Note any disconnect codes received. Code 805 means too many active connections.

## 6. Kafka

```bash
docker compose --env-file .env exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9092 --topic market.bars.v1 --from-beginning --max-messages 5
docker compose --env-file .env exec kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka:9092 --describe --group market-bars-persister
```

Record the topic name, that events are published, that the sample events carry the SBIN identifier and
source, and the consumer group lag for `market-bars-persister`. Confirm the lag is bounded and does not grow while the market is
open.

## 7. Valkey

```bash
docker compose --env-file .env exec valkey valkey-cli --scan --pattern 'market:latest:*' | head
docker compose --env-file .env exec valkey valkey-cli HGETALL market:latest:<sbin-instrument-uuid>
```

The key is `market:latest:<instrument UUID>`. Use the UUID from the instruments API for SBIN. Record the key
name, the stored source timestamp and the ingestion timestamp.
Confirm that the stored timestamp never moves backwards after an out-of-order event.

## 8. PostgreSQL

```sql
SELECT b.source, b.timestamp, b.is_synthetic
FROM market_bars b
JOIN instrument_provider_identifiers p ON p.instrument_id = b.instrument_id
WHERE p.provider = 'dhan' AND p.provider_instrument_id = '3045' AND b.source LIKE 'dhan%'
ORDER BY b.timestamp DESC LIMIT 5;
```

Confirm that the persisted rows match the Valkey latest state and the Dhan REST candles. Confirm that
`is_synthetic` is false for every Dhan row. Record the row
counts without exposing row contents beyond the diagnostic fields.

## 9. Frontend provenance

1. Start the frontend (`npm run dev` in `frontend/`) and log in.
2. Open the market-data page with the source selector set to Dhan.
3. Confirm that each displayed value shows its provider, timestamp, freshness status and whether it is
   historical or live.
4. Confirm that no value labelled yfinance appears while Dhan is selected.
5. Confirm that the market-closed state shows the last completed session date and does not label it a
   live tick.
6. Confirm the unavailable state appears when the backend returns no Dhan data, rather than substituted
   data.

Capture screenshots only with credentials and account identifiers masked.

## 10. Result recording

Keep a table with one row per step: step, command or request, HTTP status or Dhan error code, redacted
evidence, pass or fail, and the reason for any failure. Keep this record separate from the mocked
test results in `backend/tests`.

## 11. Teardown

```bash
docker compose --env-file .env stop     # keep data
```

Use `down -v` only when you intend to remove the volumes.
