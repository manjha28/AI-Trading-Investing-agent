from collections.abc import Callable, Iterable
from typing import Protocol

from app.market_stream.events import MarketBarEvent, event_from_json_bytes

TOPIC = "market.bars.v1"
CONSUMER_GROUP = "market-bars-persister"


class MarketStreamError(RuntimeError):
    pass


class EventPublisher(Protocol):
    def publish_many(self, items: Iterable[tuple[str, bytes]]) -> int: ...


class InMemoryEventPublisher:
    """Test double; records published (key, value) pairs in order."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []

    def publish_many(self, items: Iterable[tuple[str, bytes]]) -> int:
        batch = list(items)
        self.messages.extend(batch)
        return len(batch)


class KafkaEventPublisher:
    def __init__(self, bootstrap_servers: str, topic: str = TOPIC) -> None:
        from confluent_kafka import Producer

        self.topic = topic
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                "enable.idempotence": True,
                "acks": "all",
                "client.id": "trading-copilot-market-stream",
                "socket.timeout.ms": 10_000,
            }
        )

    def publish_many(self, items: Iterable[tuple[str, bytes]]) -> int:
        failures: list[str] = []

        def on_delivery(err, _msg) -> None:
            if err is not None:
                failures.append(str(err))

        count = 0
        for key, value in items:
            self._producer.produce(self.topic, key=key.encode(), value=value, on_delivery=on_delivery)
            count += 1
        remaining = self._producer.flush(timeout=15)
        if failures or remaining:
            raise MarketStreamError("kafka_publish_failed")
        return count


def consume_batch(
    consumer,
    handler: Callable[[MarketBarEvent], None],
    *,
    max_messages: int = 100,
    timeout_seconds: float = 1.0,
) -> int:
    """Handle up to max_messages, committing offsets only after each successful handle."""
    processed = 0
    for message in consumer.consume(num_messages=max_messages, timeout=timeout_seconds):
        if message.error():
            raise MarketStreamError("kafka_consume_failed")
        event = event_from_json_bytes(message.value())
        handler(event)
        consumer.commit(message=message, asynchronous=False)
        processed += 1
    return processed


def make_consumer(bootstrap_servers: str, group_id: str = CONSUMER_GROUP, topic: str = TOPIC):
    from confluent_kafka import Consumer

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([topic])
    return consumer
