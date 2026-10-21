import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
    ProviderHealth,
)

REQUEST_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)
MAX_RESPONSE_BYTES = 2_000_000


def canonical_provider_symbol(symbol: str, venue: str, country: str) -> str:
    normalized = symbol.strip().upper()
    if country == "IN":
        if venue.upper() in {"BSE", "XBOM", "BOM", "BSE_EQ"}:
            return f"{normalized.removesuffix('.BO')}.BO"
        if venue.upper() in {"NSE", "XNSE", "NSI", "NSE_EQ"}:
            return f"{normalized.removesuffix('.NS')}.NS"
    return normalized


def _candidate(
    provider: str,
    provider_id: str,
    provider_symbol: str,
    symbol: str,
    display_name: str,
    venue: str,
    currency: str,
    country: str,
    timezone_name: str,
) -> InstrumentCandidate:
    try:
        return InstrumentCandidate(
            provider=provider,
            provider_instrument_id=provider_id[:128],
            provider_symbol=provider_symbol[:96],
            symbol=symbol.upper()[:64],
            display_name=display_name[:160],
            asset_class="equity",
            venue=(venue or "UNKNOWN").upper()[:32],
            currency=currency.upper()[:3],
            country=country.upper()[:2],
            timezone=timezone_name,
            is_synthetic=False,
            is_verified=False,
        )
    except (ValueError, ValidationError) as error:
        raise MarketDataProviderError("invalid_provider_instrument") from error


class _HttpProvider:
    name = ""
    is_mock = False

    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self._transport = transport

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                with client.stream("GET", url, params=params) as response:
                    if response.status_code == 429:
                        raise MarketDataProviderError("provider_rate_limit_exceeded")
                    if response.status_code in {401, 403}:
                        raise MarketDataProviderError("provider_authentication_failed")
                    if response.status_code >= 500:
                        raise MarketDataProviderError("provider_server_error")
                    if response.status_code >= 400:
                        raise MarketDataProviderError("provider_request_failed")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise MarketDataProviderError("provider_response_too_large")
                        content.extend(chunk)
        except httpx.TimeoutException as error:
            raise MarketDataProviderError("provider_timeout") from error
        except httpx.TransportError as error:
            raise MarketDataProviderError("provider_network_error") from error
        try:
            payload = json.loads(content)
        except ValueError as error:
            raise MarketDataProviderError("invalid_provider_response") from error
        if not isinstance(payload, dict):
            raise MarketDataProviderError("invalid_provider_response")
        return payload

    @staticmethod
    def _bar(
        timestamp: int | float | datetime,
        opening: Any,
        high: Any,
        low: Any,
        close: Any,
        volume: Any,
    ) -> HistoricalBar:
        try:
            if isinstance(timestamp, datetime):
                stamp = timestamp
            else:
                stamp = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            return HistoricalBar(
                timestamp=stamp,
                open=Decimal(str(opening)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(volume or 0)),
            )
        except (ValueError, TypeError, InvalidOperation, OverflowError, ValidationError) as error:
            raise MarketDataProviderError("invalid_provider_response") from error

    @staticmethod
    def _unsupported_interval(interval: str) -> None:
        if interval not in {"1d", "1h", "15m"}:
            raise MarketDataProviderError("unsupported_interval")
        if interval == "15m":
            return

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=False,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote"),
            status="credentials_configured_connectivity_unverified",
        )


class FinnhubProvider(_HttpProvider):
    name = "finnhub"
    _base_url = "https://finnhub.io/api/v1"
    _exchange_details = {
        "NSE": ("INR", "IN", "Asia/Kolkata"),
        "BSE": ("INR", "IN", "Asia/Kolkata"),
        "NASDAQ": ("USD", "US", "America/New_York"),
        "NYSE": ("USD", "US", "America/New_York"),
        "AMEX": ("USD", "US", "America/New_York"),
    }

    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None):
        super().__init__(transport=transport)
        if not api_key.strip():
            raise ValueError("Finnhub API key is required")
        self._api_key = api_key.strip()

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._get_json(
            f"{self._base_url}/{endpoint}",
            {**params, "token": self._api_key},
        )
        if payload.get("error"):
            message = str(payload["error"]).casefold()
            code = "provider_rate_limit_exceeded" if "limit" in message else "provider_request_failed"
            raise MarketDataProviderError(code)
        return payload

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not query.strip():
            raise MarketDataProviderError("search_query_required")
        payload = self._request("search", {"q": query.strip()})
        records = payload.get("result")
        if not isinstance(records, list):
            raise MarketDataProviderError("invalid_provider_response")
        results = []
        for item in records[:limit]:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            symbol = str(item["symbol"]).upper()
            exchange = str(item.get("exchange") or "UNKNOWN").upper()
            details = self._exchange_details.get(exchange, ("USD", "US", "America/New_York"))
            try:
                results.append(
                    _candidate(
                        self.name,
                        f"{symbol}|{exchange}",
                        symbol,
                        symbol.split(":")[-1],
                        str(item.get("description") or symbol),
                        exchange,
                        *details,
                    )
                )
            except MarketDataProviderError:
                continue
        return results

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        symbol, separator, exchange = provider_instrument_id.partition("|")
        if not separator or not symbol or not exchange:
            raise MarketDataProviderError("invalid_instrument_mapping")
        details = self._exchange_details.get(exchange, ("USD", "US", "America/New_York"))
        return _candidate(
            self.name,
            provider_instrument_id,
            symbol,
            symbol.split(":")[-1],
            symbol,
            exchange,
            *details,
        )

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        payload = self._request("quote", {"symbol": instrument.provider_symbol})
        if not payload.get("t") or not payload.get("c"):
            raise MarketDataProviderError("provider_quote_unavailable")
        previous = payload.get("pc") or payload["c"]
        return self._bar(
            payload["t"],
            payload.get("o") or previous,
            payload.get("h") or payload["c"],
            payload.get("l") or payload["c"],
            payload["c"],
            0,
        )

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        self._unsupported_interval(interval)
        resolution = {"1d": "D", "1h": "60", "15m": "15"}[interval]
        payload = self._request(
            "stock/candle",
            {
                "symbol": instrument.provider_symbol,
                "resolution": resolution,
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        if payload.get("s") == "no_data":
            return []
        keys = ("t", "o", "h", "l", "c", "v")
        if payload.get("s") != "ok" or any(not isinstance(payload.get(key), list) for key in keys):
            raise MarketDataProviderError("invalid_provider_response")
        try:
            bars = [
                self._bar(*values)
                for values in zip(*(payload[key] for key in keys), strict=True)
            ]
        except (TypeError, ValueError) as error:
            raise MarketDataProviderError("invalid_provider_response") from error
        return [bar for bar in bars if start <= bar.timestamp < end][:limit]


class YahooFinanceProvider(_HttpProvider):
    name = "yfinance"
    _base_url = "https://query1.finance.yahoo.com"
    _exchange_details = {
        "NSE": ("INR", "IN", "Asia/Kolkata"),
        "NSI": ("INR", "IN", "Asia/Kolkata"),
        "BSE": ("INR", "IN", "Asia/Kolkata"),
        "BOM": ("INR", "IN", "Asia/Kolkata"),
        "NASDAQ": ("USD", "US", "America/New_York"),
        "NYSE": ("USD", "US", "America/New_York"),
    }

    @staticmethod
    def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
        error = payload.get("chart", {}).get("error")
        if error:
            raise MarketDataProviderError("provider_symbol_unavailable")
        results = payload.get("chart", {}).get("result")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise MarketDataProviderError("provider_quote_unavailable")
        return results[0]

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not query.strip():
            raise MarketDataProviderError("search_query_required")
        payload = self._get_json(
            f"{self._base_url}/v1/finance/search",
            {"q": query.strip(), "quotesCount": limit, "newsCount": 0},
        )
        records = payload.get("quotes")
        if not isinstance(records, list):
            raise MarketDataProviderError("invalid_provider_response")
        results = []
        for item in records[:limit]:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            ticker = str(item["symbol"]).upper()
            exchange = str(item.get("exchange") or "UNKNOWN").upper()
            details = self._exchange_details.get(exchange, ("USD", "US", "America/New_York"))
            country = "IN" if ticker.endswith((".NS", ".BO")) else details[1]
            currency = "INR" if country == "IN" else details[0]
            timezone_name = "Asia/Kolkata" if country == "IN" else details[2]
            symbol = ticker.removesuffix(".NS").removesuffix(".BO")
            venue = "NSE" if ticker.endswith(".NS") else "BSE" if ticker.endswith(".BO") else exchange
            try:
                results.append(
                    _candidate(
                        self.name,
                        f"{ticker}|{venue}",
                        ticker,
                        symbol,
                        str(item.get("shortname") or item.get("longname") or ticker),
                        venue,
                        currency,
                        country,
                        timezone_name,
                    )
                )
            except MarketDataProviderError:
                continue
        return results

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        ticker, separator, venue = provider_instrument_id.partition("|")
        if not separator or not ticker or not venue:
            raise MarketDataProviderError("invalid_instrument_mapping")
        details = self._exchange_details.get(venue, ("USD", "US", "America/New_York"))
        country = "IN" if ticker.endswith((".NS", ".BO")) else details[1]
        return _candidate(
            self.name,
            provider_instrument_id,
            ticker,
            ticker.removesuffix(".NS").removesuffix(".BO"),
            ticker,
            venue,
            "INR" if country == "IN" else details[0],
            country,
            "Asia/Kolkata" if country == "IN" else details[2],
        )

    def _chart(
        self,
        ticker: str,
        *,
        interval: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"interval": interval, "events": "div,splits"}
        if start is not None and end is not None:
            params.update(period1=int(start.timestamp()), period2=int(end.timestamp()))
        else:
            params["range"] = "5d"
        return self._unwrap(
            self._get_json(f"{self._base_url}/v8/finance/chart/{quote(ticker, safe='')}", params)
        )

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        payload = self._chart(instrument.provider_symbol, interval="1d")
        try:
            timestamps = payload.get("timestamp") or []
            quote = payload["indicators"]["quote"][0]
            if not timestamps:
                raise MarketDataProviderError("provider_quote_unavailable")
            last = len(timestamps) - 1
            opening = quote["open"][last]
            high = quote["high"][last]
            low = quote["low"][last]
            close = payload.get("meta", {}).get("regularMarketPrice") or quote["close"][last]
            timestamp = payload.get("meta", {}).get("regularMarketTime") or timestamps[last]
            return self._bar(
                timestamp,
                opening or close,
                high or close,
                low or close,
                close,
                quote.get("volume", [0] * len(timestamps))[last],
            )
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise MarketDataProviderError("invalid_provider_response") from error

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        self._unsupported_interval(interval)
        yf_interval = {"1d": "1d", "1h": "60m", "15m": "15m"}[interval]
        payload = self._chart(
            instrument.provider_symbol,
            interval=yf_interval,
            start=start,
            end=end,
        )
        try:
            timestamps = payload.get("timestamp") or []
            quote = payload["indicators"]["quote"][0]
            bars = []
            for index, timestamp in enumerate(timestamps):
                values = [quote[key][index] for key in ("open", "high", "low", "close")]
                if any(value is None for value in values):
                    continue
                bars.append(
                    self._bar(
                        timestamp,
                        *values,
                        (quote.get("volume") or [0] * len(timestamps))[index],
                    )
                )
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise MarketDataProviderError("invalid_provider_response") from error
        return [bar for bar in bars if start <= bar.timestamp < end][:limit]


def candidate_for_instrument(
    provider_name: str,
    provider_id: str,
    provider_symbol: str,
    *,
    symbol: str,
    display_name: str,
    asset_class: str,
    venue: str,
    currency: str,
    country: str,
    timezone_name: str,
    is_synthetic: bool = False,
) -> InstrumentCandidate:
    if asset_class not in {"equity", "etf", "index", "crypto"}:
        raise MarketDataProviderError("unsupported_provider_asset_class")
    return InstrumentCandidate(
        provider=provider_name,
        provider_instrument_id=provider_id,
        provider_symbol=provider_symbol,
        symbol=symbol,
        display_name=display_name,
        asset_class=asset_class,
        venue=venue or "UNKNOWN",
        currency=currency,
        country=country,
        timezone=timezone_name if timezone_name else str(ZoneInfo("UTC")),
        is_synthetic=is_synthetic,
    )
