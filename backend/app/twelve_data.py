import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
    ProviderHealth,
)

API_BASE_URL = "https://api.twelvedata.com"
MAX_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT = httpx.Timeout(connect=2.0, read=8.0, write=2.0, pool=2.0)
INTERVALS = {"1d": "1day", "1h": "1h", "15m": "15min"}
COUNTRY_CODES = {
    "australia": "AU",
    "canada": "CA",
    "china": "CN",
    "france": "FR",
    "germany": "DE",
    "hong kong": "HK",
    "india": "IN",
    "japan": "JP",
    "netherlands": "NL",
    "singapore": "SG",
    "switzerland": "CH",
    "united kingdom": "GB",
    "united states": "US",
}
ASSET_CLASSES = {
    "common stock": "equity",
    "depositary receipt": "equity",
    "equity": "equity",
    "etf": "etf",
    "exchange-traded fund": "etf",
    "index": "index",
    "cryptocurrency": "crypto",
    "digital currency": "crypto",
}
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class TwelveDataProvider:
    name = "twelve_data"
    _base_url = API_BASE_URL
    is_mock = False

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Twelve Data API key is required")
        self._api_key = api_key.strip()
        self._transport = transport

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=False,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote"),
            status="credentials_configured_connectivity_unverified",
        )

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not query.strip():
            raise MarketDataProviderError("search_query_required")
        if not 1 <= limit <= 50:
            raise MarketDataProviderError("invalid_search_limit")

        payload = self._request(
            "symbol_search",
            {"symbol": query.strip(), "outputsize": limit},
        )
        records = payload.get("data")
        if not isinstance(records, list):
            raise MarketDataProviderError("invalid_provider_response")

        candidates: list[InstrumentCandidate] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                candidate = self._candidate(record)
            except (KeyError, TypeError, ValueError, ValidationError):
                continue
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) == limit:
                break
        return candidates

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        symbol, _, _ = self._decode_instrument_id(provider_instrument_id)
        payload = self._request("symbol_search", {"symbol": symbol, "outputsize": 50})
        records = payload.get("data")
        if not isinstance(records, list):
            raise MarketDataProviderError("invalid_provider_response")
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                candidate = self._candidate(record)
            except (KeyError, TypeError, ValueError, ValidationError):
                continue
            if candidate is not None and candidate.provider_instrument_id == provider_instrument_id:
                return candidate
        raise MarketDataProviderError("instrument_not_found")

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        if interval not in INTERVALS:
            raise MarketDataProviderError("unsupported_interval")
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise MarketDataProviderError("invalid_time_range")
        if not 1 <= limit <= 500:
            raise MarketDataProviderError("invalid_bar_limit")

        symbol, venue, venue_kind = self._decode_instrument_id(
            instrument.provider_instrument_id
        )
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": INTERVALS[interval],
            "start_date": self._format_utc(start),
            "end_date": self._format_utc(end),
            "outputsize": limit,
            "order": "ASC",
            "timezone": "UTC",
        }
        params[venue_kind] = venue
        payload = self._request("time_series", params)
        records = payload.get("values")
        if not isinstance(records, list):
            raise MarketDataProviderError("invalid_provider_response")

        bars: list[HistoricalBar] = []
        try:
            for record in records:
                if not isinstance(record, dict):
                    raise MarketDataProviderError("invalid_provider_response")
                bar = self._bar(record)
                if start.astimezone(timezone.utc) <= bar.timestamp < end.astimezone(timezone.utc):
                    bars.append(bar)
        except (KeyError, TypeError, ValueError, InvalidOperation, ValidationError) as error:
            raise MarketDataProviderError("invalid_provider_response") from error
        return sorted(bars, key=lambda bar: bar.timestamp)[:limit]

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        symbol, venue, venue_kind = self._decode_instrument_id(
            instrument.provider_instrument_id
        )
        params: dict[str, Any] = {"symbol": symbol, "timezone": "UTC"}
        params[venue_kind] = venue
        payload = self._request("quote", params)
        try:
            return self._bar(payload, timestamp_key="datetime")
        except (KeyError, TypeError, ValueError, InvalidOperation, ValidationError) as error:
            raise MarketDataProviderError("invalid_provider_response") from error

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        request_params = {**params, "apikey": self._api_key, "format": "JSON"}
        for attempt in range(2):
            try:
                with httpx.Client(
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    with client.stream(
                        "GET",
                        f"{API_BASE_URL}/{endpoint}",
                        params=request_params,
                    ) as response:
                        status_code = response.status_code
                        if status_code in RETRYABLE_STATUS_CODES and attempt == 0:
                            time.sleep(0.2)
                            continue
                        if status_code == 429:
                            raise MarketDataProviderError("provider_rate_limit_exceeded")
                        if status_code in {401, 403}:
                            raise MarketDataProviderError("provider_authentication_failed")
                        if status_code >= 500:
                            raise MarketDataProviderError("provider_server_error")
                        if status_code >= 400:
                            raise MarketDataProviderError("provider_request_failed")

                        content = bytearray()
                        for chunk in response.iter_bytes():
                            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise MarketDataProviderError("provider_response_too_large")
                            content.extend(chunk)
            except httpx.TransportError:
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                raise MarketDataProviderError("provider_network_error") from None

            try:
                payload = json.loads(content)
            except ValueError as error:
                raise MarketDataProviderError("invalid_provider_response") from error
            if not isinstance(payload, dict):
                raise MarketDataProviderError("invalid_provider_response")
            if str(payload.get("status", "")).casefold() == "error":
                code = str(payload.get("code", ""))
                message = str(payload.get("message", "")).casefold()
                if code == "429" or "rate limit" in message or "credits" in message:
                    raise MarketDataProviderError("provider_rate_limit_exceeded")
                if code in {"401", "403"}:
                    raise MarketDataProviderError("provider_authentication_failed")
                raise MarketDataProviderError("provider_request_failed")
            return payload
        raise MarketDataProviderError("provider_server_error")

    @classmethod
    def _candidate(cls, record: dict[str, Any]) -> InstrumentCandidate | None:
        symbol = cls._required_text(record, "symbol").upper()
        exchange = cls._required_text(record, "exchange")
        mic_code = str(record.get("mic_code") or "").strip().upper()
        venue = mic_code or exchange.upper()
        if "|" in symbol or "|" in venue:
            return None

        raw_country = cls._required_text(record, "country")
        country = cls._country_code(raw_country)
        asset_class = ASSET_CLASSES.get(
            cls._required_text(record, "instrument_type").casefold()
        )
        if country is None or asset_class is None:
            return None
        timezone_name = cls._required_text(record, "exchange_timezone")
        ZoneInfo(timezone_name)
        currency = cls._required_text(record, "currency").upper()
        venue_kind = "mic_code" if mic_code else "exchange"
        provider_instrument_id = f"{symbol}|{venue_kind}:{venue}"
        return InstrumentCandidate(
            provider="twelve_data",
            provider_instrument_id=provider_instrument_id,
            provider_symbol=f"{symbol}:{venue}",
            symbol=symbol,
            display_name=cls._required_text(record, "instrument_name"),
            asset_class=asset_class,
            venue=venue,
            currency=currency,
            country=country,
            timezone=timezone_name,
            is_synthetic=False,
            is_verified=False,
        )

    @staticmethod
    def _country_code(value: str) -> str | None:
        normalized = value.strip()
        if len(normalized) == 2 and normalized.isalpha():
            return normalized.upper()
        return COUNTRY_CODES.get(normalized.casefold())

    @staticmethod
    def _required_text(record: dict[str, Any], key: str) -> str:
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("required provider field is missing")
        return value.strip()

    @staticmethod
    def _decode_instrument_id(provider_instrument_id: str) -> tuple[str, str, str]:
        parts = provider_instrument_id.split("|", 1)
        if len(parts) != 2 or not parts[0]:
            raise MarketDataProviderError("invalid_instrument_mapping")
        venue_parts = parts[1].split(":", 1)
        if len(venue_parts) != 2 or venue_parts[0] not in {"mic_code", "exchange"}:
            raise MarketDataProviderError("invalid_instrument_mapping")
        symbol, venue = parts[0], venue_parts[1]
        if not venue:
            raise MarketDataProviderError("invalid_instrument_mapping")
        return symbol, venue, venue_parts[0]

    @staticmethod
    def _format_utc(value: datetime) -> str:
        if value.utcoffset() is None:
            raise MarketDataProviderError("invalid_time_range")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _bar(
        record: dict[str, Any], *, timestamp_key: str = "datetime"
    ) -> HistoricalBar:
        timestamp = datetime.fromisoformat(str(record[timestamp_key]))
        if timestamp.utcoffset() is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return HistoricalBar(
            timestamp=timestamp,
            open=Decimal(str(record["open"])),
            high=Decimal(str(record["high"])),
            low=Decimal(str(record["low"])),
            close=Decimal(str(record["close"])),
            volume=Decimal(str(record["volume"])),
        )
