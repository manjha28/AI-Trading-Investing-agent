import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

SCHEMA_VERSION = 1
ALLOWED_INTERVALS = frozenset({"1d", "1h", "15m", "5m", "1m"})
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class MarketBarEvent:
    event_id: UUID
    provider: str
    instrument_id: UUID
    provider_instrument_id: str
    interval: str
    bar_timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    ingested_at: datetime
    schema_version: int = SCHEMA_VERSION
    is_synthetic: bool = False

    def __post_init__(self) -> None:
        if self.interval not in ALLOWED_INTERVALS:
            raise ValueError("unsupported_interval")
        if self.bar_timestamp.utcoffset() is None or self.ingested_at.utcoffset() is None:
            raise ValueError("timestamps_must_be_timezone_aware")
        if not (self.open > 0 and self.high > 0 and self.low > 0 and self.close > 0):
            raise ValueError("prices_must_be_positive")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("invalid_ohlc_relationship")
        if self.volume < 0:
            raise ValueError("negative_volume")
        if self.is_synthetic:
            raise ValueError("synthetic_bars_not_allowed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": str(self.event_id),
            "provider": self.provider,
            "instrument_id": str(self.instrument_id),
            "provider_instrument_id": self.provider_instrument_id,
            "interval": self.interval,
            "bar_timestamp": format_timestamp(self.bar_timestamp),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "ingested_at": format_timestamp(self.ingested_at),
            "is_synthetic": self.is_synthetic,
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True).encode()


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def event_identity(provider: str, instrument_id: UUID, interval: str, bar_timestamp: datetime) -> UUID:
    """Deterministic ID so replayed polls produce the same event identity."""
    name = f"{provider}|{instrument_id}|{interval}|{format_timestamp(bar_timestamp)}"
    return uuid5(NAMESPACE_URL, name)


def event_from_dict(payload: dict[str, Any]) -> MarketBarEvent:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported_schema_version")
    return MarketBarEvent(
        event_id=UUID(payload["event_id"]),
        provider=payload["provider"],
        instrument_id=UUID(payload["instrument_id"]),
        provider_instrument_id=payload["provider_instrument_id"],
        interval=payload["interval"],
        bar_timestamp=parse_timestamp(payload["bar_timestamp"]),
        open=Decimal(payload["open"]),
        high=Decimal(payload["high"]),
        low=Decimal(payload["low"]),
        close=Decimal(payload["close"]),
        volume=Decimal(payload["volume"]),
        ingested_at=parse_timestamp(payload["ingested_at"]),
        is_synthetic=bool(payload["is_synthetic"]),
    )


def event_from_json_bytes(raw: bytes) -> MarketBarEvent:
    return event_from_dict(json.loads(raw.decode()))
