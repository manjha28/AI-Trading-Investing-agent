import json
import random
import time
import csv
import gzip
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx
import yfinance as yf
from curl_cffi import requests as curl_requests
from pydantic import ValidationError

from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
    ProviderHealth,
)

REQUEST_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)
MAX_RESPONSE_BYTES = 32_000_000
_YFINANCE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="yfinance")


def canonical_provider_symbol(symbol: str, venue: str, country: str) -> str:
    normalized = symbol.strip().upper()
    if country == "IN":
        if venue.upper() in {"BSE", "XBOM", "BOM", "BSE_EQ"}:
            return f"{normalized.removesuffix('.BO')}.BO"
        if venue.upper() in {"NSE", "XNSE", "NSI", "NSE_EQ"}:
            return f"{normalized.removesuffix('.NS')}.NS"
    return normalized


def binance_provider_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized.endswith("USD") and not normalized.endswith("USDT"):
        return f"{normalized[:-3]}USDT"
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
    asset_class: str = "equity",
) -> InstrumentCandidate:
    try:
        return InstrumentCandidate(
            provider=provider,
            provider_instrument_id=provider_id[:128],
            provider_symbol=provider_symbol[:96],
            symbol=symbol.upper()[:64],
            display_name=display_name[:160],
            asset_class=asset_class,
            venue=(venue or "UNKNOWN").upper()[:32],
            currency=currency.upper()[:4],
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

    def _get_json_payload(self, url: str, params: dict[str, Any]) -> Any:
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "TradingCopilot/1.0 (market data)"},
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
        return payload

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._get_json_payload(url, params)
        if not isinstance(payload, dict):
            raise MarketDataProviderError("invalid_provider_response")
        return payload

    @staticmethod
    def _bar(
        timestamp: int | float | str | datetime,
        opening: Any,
        high: Any,
        low: Any,
        close: Any,
        volume: Any,
    ) -> HistoricalBar:
        try:
            if isinstance(timestamp, datetime):
                stamp = timestamp
            elif isinstance(timestamp, str):
                try:
                    numeric_timestamp = float(timestamp)
                    if numeric_timestamp > 10_000_000_000:
                        numeric_timestamp /= 1000
                    stamp = datetime.fromtimestamp(numeric_timestamp, tz=timezone.utc)
                except ValueError:
                    stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            else:
                stamp = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            return HistoricalBar(
                timestamp=stamp,
                open=Decimal(str(opening)).quantize(Decimal("0.00000001")),
                high=Decimal(str(high)).quantize(Decimal("0.00000001")),
                low=Decimal(str(low)).quantize(Decimal("0.00000001")),
                close=Decimal(str(close)).quantize(Decimal("0.00000001")),
                volume=Decimal(str(volume or 0)).quantize(Decimal("0.00000001")),
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


class DeltaExchangeProvider(_HttpProvider):
    name = "delta_exchange"
    _base_url = "https://api.india.delta.exchange"
    _product_cache: tuple[float, list[dict[str, Any]]] | None = None

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=False,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote"),
            status="public_provider_connectivity_unverified",
        )

    @classmethod
    def clear_product_cache(cls) -> None:
        cls._product_cache = None

    def _products(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = type(self)._product_cache
        if cached and now - cached[0] < 86400:
            return cached[1]
        payload = self._get_json(f"{self._base_url}/v2/products", {})
        products = payload.get("result")
        if not isinstance(products, list):
            raise MarketDataProviderError("invalid_provider_response")
        records = [item for item in products if isinstance(item, dict)]
        type(self)._product_cache = (now, records)
        return records

    @staticmethod
    def _crypto_candidate(product: dict[str, Any]) -> InstrumentCandidate | None:
        symbol = str(product.get("symbol") or "").upper()
        if not symbol:
            return None
        base = product.get("underlying_asset")
        quote_asset = product.get("quoting_asset") or product.get("settling_asset")
        if isinstance(base, dict):
            base = base.get("symbol")
        if isinstance(quote_asset, dict):
            quote_asset = quote_asset.get("symbol")
        display = str(product.get("description") or product.get("name") or symbol)
        return _candidate(
            "delta_exchange",
            str(product.get("id") or symbol),
            symbol,
            symbol,
            display,
            "DELTA",
            str(quote_asset or "USD"),
            "IN",
            "UTC",
            asset_class="crypto",
        )

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not query.strip():
            raise MarketDataProviderError("search_query_required")
        term = query.strip().casefold()
        candidates = []
        for product in self._products():
            product_type = str(product.get("product_type") or "").casefold()
            if product_type and product_type not in {"spot", "perpetual_futures", "futures"}:
                continue
            candidate = self._crypto_candidate(product)
            if candidate and (
                term in candidate.symbol.casefold()
                or term in candidate.display_name.casefold()
            ):
                candidates.append(candidate)
        return candidates[:limit]

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        for product in self._products():
            if str(product.get("id")) == provider_instrument_id or str(
                product.get("symbol", "")
            ).upper() == provider_instrument_id.upper():
                candidate = self._crypto_candidate(product)
                if candidate:
                    return candidate
        raise MarketDataProviderError("instrument_not_found")

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        payload = self._get_json(
            f"{self._base_url}/v2/tickers/{quote(instrument.provider_symbol, safe='')}", {}
        )
        record = payload.get("result")
        if isinstance(record, list):
            record = record[0] if record else None
        if not isinstance(record, dict):
            raise MarketDataProviderError("provider_quote_unavailable")
        price = record.get("close") or record.get("mark_price") or record.get("spot_price")
        if price is None:
            raise MarketDataProviderError("provider_quote_unavailable")
        stamp = record.get("timestamp") or int(time.time())
        if isinstance(stamp, (int, float)) and stamp > 10_000_000_000:
            stamp = stamp / 1000
        bar = self._bar(
            stamp,
            record.get("open", price),
            record.get("high", price),
            record.get("low", price),
            price,
            record.get("volume", 0),
        )
        return bar

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        resolutions = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}
        resolution = resolutions.get(interval)
        if resolution is None:
            raise MarketDataProviderError("unsupported_interval")
        step_seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "1d": 86400,
        }[interval]
        start_seconds, end_seconds = int(start.timestamp()), int(end.timestamp())
        cursor = end_seconds
        collected: dict[datetime, HistoricalBar] = {}
        while cursor > start_seconds and len(collected) < limit:
            chunk_start = max(start_seconds, cursor - (2000 * step_seconds))
            payload = self._get_json(
                f"{self._base_url}/v2/history/candles",
                {
                    "symbol": instrument.provider_symbol,
                    "resolution": resolution,
                    "start": chunk_start,
                    "end": cursor,
                },
            )
            records = payload.get("result")
            if not isinstance(records, list):
                raise MarketDataProviderError("invalid_provider_response")
            for record in records:
                if not isinstance(record, dict):
                    continue
                bar = self._bar(
                    record.get("time"),
                    record.get("open"),
                    record.get("high"),
                    record.get("low"),
                    record.get("close"),
                    record.get("volume"),
                )
                if start <= bar.timestamp < end:
                    collected[bar.timestamp] = bar
            if chunk_start == start_seconds:
                break
            cursor = chunk_start
        return [collected[key] for key in sorted(collected)][:limit]


class BinanceProvider(_HttpProvider):
    name = "binance"
    _base_url = "https://api.binance.com"
    _intervals = {"1m": "1m", "15m": "15m", "1h": "1h", "1d": "1d"}
    _exchange_cache: tuple[float, list[dict[str, Any]]] | None = None

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=False,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote"),
            status="public_provider_connectivity_unverified",
        )

    def _exchange_info(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = type(self)._exchange_cache
        if cached and now - cached[0] < 86400:
            return cached[1]
        payload = self._get_json(f"{self._base_url}/api/v3/exchangeInfo", {})
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise MarketDataProviderError("invalid_provider_response")
        type(self)._exchange_cache = (
            now,
            [item for item in symbols if isinstance(item, dict) and item.get("status") == "TRADING"],
        )
        return self._exchange_cache[1]

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not query.strip():
            raise MarketDataProviderError("search_query_required")
        term = query.strip().upper()
        results = []
        for item in self._exchange_info():
            symbol = str(item.get("symbol") or "")
            if term not in symbol:
                continue
            base = str(item.get("baseAsset") or "")
            quote_asset = str(item.get("quoteAsset") or "USDT")
            results.append(
                _candidate(
                    self.name,
                    symbol,
                    symbol,
                    symbol,
                    f"{base}/{quote_asset}",
                    "BINANCE",
                    quote_asset,
                    "US",
                    "UTC",
                    asset_class="crypto",
                )
            )
            if len(results) == limit:
                break
        return results

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        for item in self._exchange_info():
            if str(item.get("symbol", "")).upper() == provider_instrument_id.upper():
                base, quote_asset = item.get("baseAsset", ""), item.get("quoteAsset", "USDT")
                return _candidate(
                    self.name,
                    provider_instrument_id,
                    provider_instrument_id,
                    provider_instrument_id,
                    f"{base}/{quote_asset}",
                    "BINANCE",
                    str(quote_asset),
                    "US",
                    "UTC",
                    asset_class="crypto",
                )
        raise MarketDataProviderError("instrument_not_found")

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        payload = self._get_json(
            f"{self._base_url}/api/v3/ticker/24hr",
            {"symbol": instrument.provider_symbol},
        )
        try:
            price = payload["lastPrice"]
            close_time = datetime.fromtimestamp(
                int(payload.get("closeTime", int(time.time() * 1000))) / 1000,
                tz=timezone.utc,
            )
            return self._bar(
                close_time,
                payload.get("openPrice", price),
                payload.get("highPrice", price),
                payload.get("lowPrice", price),
                price,
                payload.get("volume", 0),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MarketDataProviderError("invalid_provider_response") from error

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        if interval not in self._intervals:
            raise MarketDataProviderError("unsupported_interval")
        cursor_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        collected: dict[datetime, HistoricalBar] = {}
        while cursor_ms < end_ms and len(collected) < limit:
            payload = self._get_json_payload(
                f"{self._base_url}/api/v3/klines",
                {
                    "symbol": instrument.provider_symbol,
                    "interval": self._intervals[interval],
                    "startTime": cursor_ms,
                    "endTime": end_ms,
                    "limit": min(1000, limit - len(collected)),
                },
            )
            if not isinstance(payload, list):
                raise MarketDataProviderError("invalid_provider_response")
            if not payload:
                break
            for record in payload:
                if not isinstance(record, list) or len(record) < 7:
                    raise MarketDataProviderError("invalid_provider_response")
                bar = self._bar(
                    datetime.fromtimestamp(int(record[0]) / 1000, tz=timezone.utc),
                    record[1],
                    record[2],
                    record[3],
                    record[4],
                    record[5],
                )
                if start <= bar.timestamp < end:
                    collected[bar.timestamp] = bar
            next_cursor = int(payload[-1][0]) + 1
            if next_cursor <= cursor_ms:
                break
            cursor_ms = next_cursor
        return [collected[key] for key in sorted(collected)][:limit]


class UpstoxProvider(_HttpProvider):
    name = "upstox"
    _base_url = "https://api.upstox.com"
    _master_url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    _master_cache: tuple[float, list[dict[str, str]]] | None = None

    def __init__(self, access_token: str, *, transport: httpx.BaseTransport | None = None):
        super().__init__(transport=transport)
        if not access_token.strip():
            raise ValueError("Upstox account token is required")
        self._access_token = access_token.strip()

    def _authorized_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                transport=self._transport,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                    "User-Agent": "TradingCopilot/1.5 (market data)",
                },
            ) as client:
                response = client.get(url, params=params)
                if response.status_code == 429:
                    raise MarketDataProviderError("provider_rate_limit_exceeded")
                if response.status_code >= 400:
                    raise MarketDataProviderError("provider_unavailable")
                payload = response.json()
        except httpx.TimeoutException as error:
            raise MarketDataProviderError("provider_timeout") from error
        except httpx.TransportError as error:
            raise MarketDataProviderError("provider_network_error") from error
        except ValueError as error:
            raise MarketDataProviderError("invalid_provider_response") from error
        if not isinstance(payload, dict):
            raise MarketDataProviderError("invalid_provider_response")
        return payload

    def _instrument_master(self) -> list[dict[str, str]]:
        now = time.monotonic()
        if self._master_cache and now - self._master_cache[0] < 86400:
            return self._master_cache[1]
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "TradingCopilot/1.5 (market data)"},
            ) as client:
                response = client.get(self._master_url)
                response.raise_for_status()
                compressed = response.content
            content = gzip.decompress(compressed) if compressed[:2] == b"\x1f\x8b" else compressed
            records = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
        except (httpx.HTTPError, OSError, UnicodeDecodeError) as error:
            raise MarketDataProviderError("provider_instrument_master_unavailable") from error
        self._master_cache = (now, records)
        return records

    @staticmethod
    def _upstox_candidate(record: dict[str, str]) -> InstrumentCandidate | None:
        instrument_key = record.get("instrument_key", "")
        symbol = record.get("trading_symbol", "").upper()
        exchange = record.get("exchange", "")
        if not instrument_key or not symbol or exchange not in {"NSE", "BSE"}:
            return None
        return _candidate(
            "upstox",
            instrument_key,
            instrument_key,
            symbol,
            record.get("name") or symbol,
            exchange,
            record.get("exchange_token_currency") or "INR",
            "IN",
            "Asia/Kolkata",
        )

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not query.strip():
            raise MarketDataProviderError("search_query_required")
        term = query.strip().casefold()
        matches = []
        for record in self._instrument_master():
            if term not in record.get("trading_symbol", "").casefold() and term not in record.get(
                "name", ""
            ).casefold():
                continue
            candidate = self._upstox_candidate(record)
            if candidate:
                matches.append(candidate)
                if len(matches) >= limit:
                    break
        return matches

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        for record in self._instrument_master():
            if record.get("instrument_key") == provider_instrument_id:
                candidate = self._upstox_candidate(record)
                if candidate:
                    return candidate
        raise MarketDataProviderError("instrument_not_found")

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        payload = self._authorized_json(
            f"{self._base_url}/v2/market-quote/quotes",
            {"instrument_key": instrument.provider_symbol},
        )
        quotes = payload.get("data")
        if not isinstance(quotes, dict):
            raise MarketDataProviderError("invalid_provider_response")
        record = quotes.get(instrument.provider_symbol)
        if not isinstance(record, dict):
            record = next(iter(quotes.values()), None)
        if not isinstance(record, dict):
            raise MarketDataProviderError("provider_quote_unavailable")
        ohlc = record.get("ohlc") or {}
        price = record.get("last_price")
        if price is None:
            raise MarketDataProviderError("provider_quote_unavailable")
        return self._bar(
            datetime.now(timezone.utc),
            ohlc.get("open", price),
            ohlc.get("high", price),
            ohlc.get("low", price),
            price,
            record.get("volume", 0),
        )

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        units = {"1m": ("minutes", "1"), "15m": ("minutes", "15"), "1h": ("hours", "1"), "1d": ("days", "1")}
        if interval not in units:
            raise MarketDataProviderError("unsupported_interval")
        unit, candle_interval = units[interval]
        url = (
            f"{self._base_url}/v3/historical-candle/"
            f"{quote(instrument.provider_symbol, safe='')}/{unit}/{candle_interval}/"
            f"{end.date().isoformat()}/{start.date().isoformat()}"
        )
        payload = self._authorized_json(url)
        data = payload.get("data")
        candles = data.get("candles") if isinstance(data, dict) else None
        if not isinstance(candles, list):
            raise MarketDataProviderError("invalid_provider_response")
        bars = []
        for candle in candles:
            if not isinstance(candle, list) or len(candle) < 6:
                continue
            timestamp = datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00"))
            bar = self._bar(timestamp, candle[1], candle[2], candle[3], candle[4], candle[5])
            if start <= bar.timestamp < end:
                bars.append(bar)
        return sorted(bars, key=lambda item: item.timestamp)[:limit]


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
    _lookback = {"1m": 7, "15m": 60, "1h": 730}
    _exchange_details = {
        "NSE": ("INR", "IN", "Asia/Kolkata"),
        "XNSE": ("INR", "IN", "Asia/Kolkata"),
        "NSI": ("INR", "IN", "Asia/Kolkata"),
        "BSE": ("INR", "IN", "Asia/Kolkata"),
        "XBOM": ("INR", "IN", "Asia/Kolkata"),
        "BOM": ("INR", "IN", "Asia/Kolkata"),
        "NASDAQ": ("USD", "US", "America/New_York"),
        "NMS": ("USD", "US", "America/New_York"),
        "NYSE": ("USD", "US", "America/New_York"),
        "NYQ": ("USD", "US", "America/New_York"),
    }

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=False,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote"),
            status="public_provider_connectivity_unverified",
        )

    @staticmethod
    def _thread_call(function, *args, **kwargs):
        for attempt in range(3):
            try:
                return _YFINANCE_EXECUTOR.submit(function, *args, **kwargs).result()
            except Exception as error:
                if attempt == 2:
                    raise MarketDataProviderError("provider_request_failed") from error
                time.sleep(random.uniform(0.05, 0.2) * (attempt + 1))
        raise MarketDataProviderError("provider_request_failed")

    @staticmethod
    def _session():
        return curl_requests.Session(
            impersonate="chrome",
            headers={"User-Agent": "TradingCopilot/1.5 (market-data; contact: local deployment)"},
        )

    @classmethod
    def _candidate_from_search(cls, item: dict[str, Any]) -> InstrumentCandidate | None:
        ticker = str(item.get("symbol") or "").upper()
        if not ticker:
            return None
        exchange = str(item.get("exchange") or item.get("exchDisp") or "UNKNOWN").upper()
        details = cls._exchange_details.get(exchange)
        indian_suffix = ticker.endswith((".NS", ".BO"))
        if indian_suffix:
            country, currency, timezone_name = (
                "IN",
                "INR",
                "Asia/Kolkata",
            )
            venue = "NSE" if ticker.endswith(".NS") else "BSE"
        else:
            currency, country, timezone_name = details or (
                str(item.get("currency") or "USD"),
                "US",
                "America/New_York",
            )
            venue = exchange
        symbol = ticker.removesuffix(".NS").removesuffix(".BO")
        return _candidate(
            "yfinance",
            f"{ticker}|{venue}",
            ticker,
            symbol,
            str(item.get("shortname") or item.get("longname") or ticker),
            venue,
            currency,
            country,
            timezone_name,
        )

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        if not query.strip():
            raise MarketDataProviderError("search_query_required")
        def search():
            return yf.Search(
                query.strip(),
                max_results=limit,
                news_count=0,
                lists_count=0,
                include_research=False,
                session=self._session(),
            ).quotes

        records = self._thread_call(search)
        if not isinstance(records, list):
            raise MarketDataProviderError("invalid_provider_response")
        return [
            candidate
            for item in records[:limit]
            if isinstance(item, dict)
            if (candidate := self._candidate_from_search(item)) is not None
        ]

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

    def _history_frame(
        self,
        ticker: str,
        *,
        interval: str,
        start: datetime | None = None,
        end: datetime | None = None,
        period: str | None = None,
    ):
        def fetch():
            return yf.Ticker(ticker, session=self._session()).history(
                start=start,
                end=end,
                period=period,
                interval=interval,
                auto_adjust=False,
                actions=False,
                raise_errors=True,
                timeout=REQUEST_TIMEOUT.read,
            )

        frame = self._thread_call(fetch)
        if frame is None or frame.empty:
            raise MarketDataProviderError("provider_quote_unavailable")
        return frame

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        try:
            frame = self._history_frame(instrument.provider_symbol, interval="1d", period="5d")
            row = frame.iloc[-1]
            return self._bar(
                frame.index[-1].to_pydatetime(),
                row["Open"],
                row["High"],
                row["Low"],
                row["Close"],
                row.get("Volume", 0),
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
        interval_map = {"1m": "1m", "1h": "60m", "15m": "15m", "1d": "1d"}
        yf_interval = interval_map.get(interval)
        if yf_interval is None:
            raise MarketDataProviderError("unsupported_interval")
        lookback_days = self._lookback.get(interval)
        if lookback_days is not None:
            oldest_allowed = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            if start < oldest_allowed:
                raise MarketDataProviderError(
                    f"yfinance_{interval}_lookback_exceeded_{lookback_days}_days"
                )
        frame = self._history_frame(
            instrument.provider_symbol,
            interval=yf_interval,
            start=start,
            end=end,
        )
        try:
            bars = []
            for timestamp, row in frame.iterrows():
                values = [row[key] for key in ("Open", "High", "Low", "Close")]
                if any(value is None or str(value) == "nan" for value in values):
                    continue
                bars.append(
                    self._bar(
                        timestamp.to_pydatetime(),
                        *values,
                        row.get("Volume", 0),
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
    multiplier: Decimal | None = None,
    expiry: date | None = None,
    strike: Decimal | None = None,
    option_type: str | None = None,
) -> InstrumentCandidate:
    if asset_class not in {"equity", "etf", "index", "future", "option", "crypto"}:
        raise MarketDataProviderError("unsupported_provider_asset_class")
    if provider_name != "mock" and asset_class in {"future", "option"}:
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
        timezone=timezone_name or "UTC",
        is_synthetic=is_synthetic,
        multiplier=multiplier,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
    )
