"""Live-service checks for Kafka and Valkey. Skipped unless the service URLs are set.

Run on a Docker-capable host after `docker compose up -d`:
  MARKET_STREAM_KAFKA_BOOTSTRAP=localhost:9092 MARKET_STREAM_VALKEY_URL=redis://localhost:6379/0 \
  python -m pytest tests/test_market_stream_integration.py -q
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.market_stream.events import MarketBarEvent, event_identity, event_from_json_bytes
from app.market_stream.state import ValkeyLatestStateStore
from app.market_stream.transport import KafkaEventPublisher, consume_batch, make_consumer

KAFKA = os.environ.get("MARKET_STREAM_KAFKA_BOOTSTRAP")
VALKEY = os.environ.get("MARKET_STREAM_VALKEY_URL")


def sample_event(instrument_id, minutes_ago: int, close: str = "101.5") -> MarketBarEvent:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now - timedelta(minutes=minutes_ago)
    return MarketBarEvent(
        event_id=event_identity("dhan", instrument_id, "5m", stamp),
        provider="dhan",
        instrument_id=instrument_id,
        provider_instrument_id="1333|NSE_EQ",
        interval="5m",
        bar_timestamp=stamp,
        open=Decimal("100.25"),
        high=Decimal("102"),
        low=Decimal("99.5"),
        close=Decimal(close),
        volume=Decimal("1200.5"),
        ingested_at=now,
    )


@pytest.mark.skipif(not KAFKA, reason="MARKET_STREAM_KAFKA_BOOTSTRAP not set")
def test_kafka_round_trip_preserves_keys_and_event_payload():
    instrument_id = uuid4()
    event = sample_event(instrument_id, minutes_ago=7)
    topic = f"market.bars.test.{uuid4().hex}"
    publisher = KafkaEventPublisher(KAFKA, topic=topic)
    assert publisher.publish_many([(str(instrument_id), event.to_json_bytes())]) == 1

    consumer = make_consumer(KAFKA, group_id=f"it-{uuid4().hex}", topic=topic)
    received: list[MarketBarEvent] = []
    try:
        for _ in range(20):
            if consume_batch(consumer, received.append, timeout_seconds=1.0):
                break
    finally:
        consumer.close()
    assert received == [event_from_json_bytes(event.to_json_bytes())]


@pytest.mark.skipif(not VALKEY, reason="MARKET_STREAM_VALKEY_URL not set")
def test_valkey_latest_state_is_newer_only_under_lua_compare_and_set():
    import redis

    client = redis.Redis.from_url(VALKEY, decode_responses=False)
    store = ValkeyLatestStateStore(client)
    instrument_id = uuid4()
    older = sample_event(instrument_id, minutes_ago=10)
    newer = sample_event(instrument_id, minutes_ago=5, close="101.9")
    try:
        assert store.upsert_if_newer(newer) is True
        assert store.upsert_if_newer(older) is False
        assert store.upsert_if_newer(newer) is False
        latest = store.get_many([instrument_id])[str(instrument_id)]
        assert latest["close"] == "101.9"
        assert latest["is_synthetic"] is False
    finally:
        client.delete(f"market:latest:{instrument_id}")
