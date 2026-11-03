import json
from collections.abc import Iterable
from typing import Any, Protocol
from uuid import UUID

from app.market_stream.events import MarketBarEvent, format_timestamp

LATEST_KEY_PREFIX = "market:latest:"

# Compare-and-set: only replace the stored latest bar when the incoming bar is strictly newer.
# Timestamps use a fixed-width UTC format, so string comparison is chronological.
_UPSERT_NEWER_LUA = """
local current = redis.call('GET', KEYS[1])
if current then
  local decoded = cjson.decode(current)
  if decoded['bar_timestamp'] >= ARGV[2] then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""


def latest_key(instrument_id: UUID | str) -> str:
    return f"{LATEST_KEY_PREFIX}{instrument_id}"


class LatestStateStore(Protocol):
    def upsert_if_newer(self, event: MarketBarEvent) -> bool: ...

    def get_many(self, instrument_ids: Iterable[UUID]) -> dict[str, dict[str, Any]]: ...


class ValkeyLatestStateStore:
    def __init__(self, client) -> None:
        self._client = client
        self._upsert = client.register_script(_UPSERT_NEWER_LUA)

    def upsert_if_newer(self, event: MarketBarEvent) -> bool:
        payload = event.to_json_bytes()
        result = self._upsert(
            keys=[latest_key(event.instrument_id)],
            args=[payload, format_timestamp(event.bar_timestamp)],
        )
        return bool(int(result))

    def get_many(self, instrument_ids: Iterable[UUID]) -> dict[str, dict[str, Any]]:
        ids = [str(item) for item in instrument_ids]
        if not ids:
            return {}
        raw_values = self._client.mget([latest_key(item) for item in ids])
        found: dict[str, dict[str, Any]] = {}
        for instrument_id, raw in zip(ids, raw_values, strict=True):
            if raw is not None:
                found[instrument_id] = json.loads(raw)
        return found


class InMemoryLatestStateStore:
    """Same newer-only semantics as ValkeyLatestStateStore, for tests without a server."""

    def __init__(self) -> None:
        self._values: dict[str, dict[str, Any]] = {}

    def upsert_if_newer(self, event: MarketBarEvent) -> bool:
        key = str(event.instrument_id)
        current = self._values.get(key)
        stamp = format_timestamp(event.bar_timestamp)
        if current is not None and current["bar_timestamp"] >= stamp:
            return False
        self._values[key] = event.to_dict()
        return True

    def get_many(self, instrument_ids: Iterable[UUID]) -> dict[str, dict[str, Any]]:
        return {
            str(item): self._values[str(item)]
            for item in instrument_ids
            if str(item) in self._values
        }
