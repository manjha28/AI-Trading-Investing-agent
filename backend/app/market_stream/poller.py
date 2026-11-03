from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.market_stream.events import MarketBarEvent, event_identity
from app.market_stream.transport import EventPublisher, MarketStreamError
from app.provider_adapters import DhanV2Adapter

PROVIDER = "dhan"
DHAN_SEGMENTS = frozenset({"NSE_EQ", "BSE_EQ"})


def parse_dhan_identifier(provider_instrument_id: str) -> tuple[str, str]:
    security_id, separator, segment = provider_instrument_id.partition("|")
    if not separator or segment not in DHAN_SEGMENTS or not security_id.isdigit():
        raise MarketStreamError("invalid_provider_instrument_id")
    return security_id, segment


@dataclass(frozen=True)
class PollResult:
    fetched: int
    published: int
    rejected: int


class DhanBarPoller:
    def __init__(
        self,
        publisher: EventPublisher,
        *,
        adapter_factory: Callable[[dict[str, str]], Any] = DhanV2Adapter,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._publisher = publisher
        self._adapter_factory = adapter_factory
        self._clock = clock

    def poll(
        self,
        *,
        instrument_id: UUID,
        provider_instrument_id: str,
        tokens: dict[str, str],
        interval: str,
        start: datetime,
        end: datetime,
    ) -> PollResult:
        security_id, segment = parse_dhan_identifier(provider_instrument_id)
        adapter = self._adapter_factory(tokens)
        bars = adapter.get_historical_bars(segment, security_id, interval, start, end)
        ingested_at = self._clock()
        items: list[tuple[str, bytes]] = []
        rejected = 0
        for bar in sorted(bars, key=lambda item: item.timestamp):
            try:
                event = MarketBarEvent(
                    event_id=event_identity(PROVIDER, instrument_id, interval, bar.timestamp),
                    provider=PROVIDER,
                    instrument_id=instrument_id,
                    provider_instrument_id=provider_instrument_id,
                    interval=interval,
                    bar_timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    ingested_at=ingested_at,
                )
            except (ValueError, ValidationError):
                rejected += 1
                continue
            items.append((str(instrument_id), event.to_json_bytes()))
        published = self._publisher.publish_many(items) if items else 0
        return PollResult(fetched=len(bars), published=published, rejected=rejected)
