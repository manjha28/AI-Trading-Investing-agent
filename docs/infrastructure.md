# Infrastructure: Kafka, Valkey, PostgreSQL

Decision support, not investment advice. This platform never places, modifies, or cancels orders.

## Status

Compose files are written and validated for YAML syntax. They have **not** been started or
runtime-verified in this devcontainer, which blocks Docker. Kafka, Valkey, and PostgreSQL
integration must be verified on a Docker-capable host with the integration tests below.

## Layout

- `deploy/compose/docker-compose.yml`: base services, pinned images, named volumes, health checks, restart policy, resource limits. No host ports, no credentials.
- `deploy/compose/docker-compose.dev.yml`: publishes ports on `127.0.0.1` only.
- `deploy/compose/docker-compose.prod.yml`: no host ports, `restart: always`, log rotation.
- `deploy/compose/.env.example`: placeholders only. Copy to `deploy/compose/.env`. That file is git-ignored.

## Pinned images and licenses

| Service | Image | License |
|---|---|---|
| Kafka (KRaft, single node) | `apache/kafka:3.9.0` | Apache 2.0 |
| Valkey | `valkey/valkey:8.0-alpine` | BSD 3-Clause |
| PostgreSQL | `postgres:16-alpine` | PostgreSQL License |

Redpanda (BSL 1.1) and Redis (RSALv2/SSPL) were rejected to keep licensing OSI-open.
Python clients: `confluent-kafka` (Apache 2.0), `redis` (MIT). No commercial or subscription
dependencies are required. Hosting cost is the only expected cost.

## Resource estimate (single node)

| Service | RAM limit | CPU limit | Disk |
|---|---|---|---|
| Kafka | 1280 MB (heap 512–768 MB) | 1.5 | Grows with retention (168 h default) |
| Valkey | 384 MB (maxmemory 256 MB) | 0.5 | AOF, small |
| PostgreSQL | 1024 MB | 1.0 | Grows with `market_bars` |

Total: about 2.7 GB RAM and 3 CPUs at peak. Network is low: one Dhan REST poll per instrument
per interval. Back up the PostgreSQL volume (`pg_dump`), since it is the source of truth.
Kafka and Valkey can be rebuilt from the database and from new polls.

## Start, stop, inspect, recover

Run from `deploy/compose/` on a Docker-capable host.

```bash
cp .env.example .env              # then replace every placeholder
# Generate a KAFKA_CLUSTER_ID and put it in .env:
docker run --rm apache/kafka:3.9.0 /opt/kafka/bin/kafka-storage.sh random-uuid

# Development
docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file .env up -d
# Production
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d

docker compose --env-file .env ps                                  # status and health
docker compose --env-file .env logs -f kafka                       # logs (also valkey, postgres)
docker compose --env-file .env exec valkey valkey-cli ping         # auth via REDISCLI_AUTH
docker compose --env-file .env exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list

# Create the market topic (auto-create is disabled)
docker compose --env-file .env exec kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 --create --if-not-exists --topic market.bars.v1 --partitions 3 --replication-factor 1

docker compose --env-file .env stop                                # stop, keep data
docker compose --env-file .env down                                # remove containers, keep volumes
docker compose --env-file .env down -v                             # DESTROYS all data (volumes)
```

Recovery:

- A crashed container restarts automatically (`restart` policy). If it stays unhealthy, read `logs`, then `docker compose ... restart <service>`.
- Kafka data loss: recreate the topic. Bars are re-polled from Dhan, and `market_bars` upserts are idempotent.
- Valkey loss: latest state is rebuilt from new bars. Reads report `missing_instrument_ids` until then.
- PostgreSQL: restore from `pg_dump`, then run `alembic upgrade head` from `backend/`.

## Application configuration

Set these in the backend environment (`backend/.env`, never committed):

| Variable | Default | Purpose |
|---|---|---|
| `MARKET_STREAM_ENABLED` | `false` | Turns the market-stream routes on |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka bootstrap address |
| `VALKEY_URL` | `redis://localhost:6379/0` | Valkey URL. Include the password, e.g. `redis://:PASSWORD@host:6379/0` |
| `MARKET_STREAM_POLL_LOOKBACK_DAYS` | `5` | Poll window, 1–30 days |

Run the worker from `backend/`: `python -m app.market_stream.worker`.

## Tests

```bash
# No live services needed
cd backend && python -m pytest tests/test_market_stream.py -q

# Requires the running Compose stack and these variables
export MARKET_STREAM_KAFKA_BOOTSTRAP=localhost:9092
export MARKET_STREAM_VALKEY_URL="redis://:PASSWORD@localhost:6379/0"
cd backend && python -m pytest tests/test_market_stream_integration.py -q
```

Without those variables the integration tests skip. A skip is not runtime verification.

## Portability

Application code depends only on the configured URLs and the `EventPublisher` and
`LatestStateStore` interfaces. Moving to a VPS or dedicated server means running the same
Compose files there and changing the URLs. No application redesign is needed.
