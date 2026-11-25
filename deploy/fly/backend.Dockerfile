# Backend image for Fly.io. Build from the repo root:  fly deploy -c deploy/fly/backend.fly.toml
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app/backend
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/ ./
# Fail the BUILD, not production, if any module the app imports is missing from the build context.
RUN python -c "import app.main"
# ONE worker: the live Dhan feed hub is per-process state, so more workers would open duplicate feeds.
# Migrations run first; they are additive and idempotent.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
