"""Consumes market bar events from Kafka, persisting each one and refreshing latest state.

Run from backend/ so .env loads: python -m app.market_stream.worker
"""

import logging
from functools import lru_cache

import redis
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market_stream.persistence import apply_event
from app.market_stream.state import LatestStateStore, ValkeyLatestStateStore
from app.market_stream.transport import consume_batch, make_consumer

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _latest_store() -> LatestStateStore:
    return ValkeyLatestStateStore(
        redis.Redis.from_url(settings.valkey_url, socket_timeout=5, socket_connect_timeout=5)
    )


def _handle(event, store: LatestStateStore) -> None:
    with Session(engine) as db:
        outcome = apply_event(db, store, event)
    logger.debug("market bar %s outcome %s", event.event_id, outcome)


def run_forever() -> None:
    consumer = make_consumer(settings.kafka_bootstrap_servers)
    store = _latest_store()
    try:
        while True:
            consume_batch(consumer, lambda event: _handle(event, store))
    finally:
        consumer.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
