import json
import logging
import random
import time
import csv
import gzip
import io
import base64
import hashlib
import hmac
import struct
import threading
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import yfinance as yf
from curl_cffi import requests as curl_requests
from pydantic import ValidationError
from websockets.asyncio.client import connect as websocket_connect

from app.config import settings
from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
    ProviderHealth,
)

REQUEST_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)
MAX_RESPONSE_BYTES = 32_000_000
MAX_ANGEL_ONE_MASTER_BYTES = 64_000_000
_YFINANCE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="yfinance")
_YFINANCE_COOLDOWN_SECONDS = 60.0
_YFINANCE_RATE_LIMIT_COOLDOWN_SECONDS = 900.0
_yfinance_cooldown_lock = threading.Lock()
_yfinance_cooldown_until = 0.0
logger = logging.getLogger(__name__)
_SENSITIVE_DIAGNOSTIC_FIELDS = re.compile(
    r'(?i)(["\']?(?:authorization|access[_-]?token|refresh[_-]?token|feed[_-]?token|'
    r'jwt|api[_-]?key|private[_-]?key|client[_-]?(?:code|id)|password|pin|totp|otp)'
    r'["\']?\s*[:=]\s*["\']?)[^"\',}\s&]+'
)
_JWT_DIAGNOSTIC_VALUE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def _redact_diagnostic_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    redacted = _SENSITIVE_DIAGNOSTIC_FIELDS.sub(r"\1[REDACTED]", value)
    redacted = _JWT_DIAGNOSTIC_VALUE.sub("[REDACTED]", redacted)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_diagnostic_url(url: str) -> str:
    parsed = urlsplit(url)
    query = urlencode([
        (
            key,
            "[REDACTED]" if any(
                token in key.casefold()
                for token in ("token", "key", "secret", "client", "pin", "totp", "otp", "password")
            ) else value,
        )
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


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
        self.last_response_status: int | None = None

    def _get_json_payload(self, url: str, params: dict[str, Any]) -> Any:
        self.last_response_status = None
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "TradingCopilot/1.0 (market data)"},
            ) as client:
                with client.stream("GET", url, params=params) as response:
                    self.last_response_status = response.status_code
                    if response.status_code == 429:
                        raise MarketDataProviderError(
                            "provider_rate_limit_exceeded",
                            status_code=response.status_code,
                            retry_after=response.headers.get("Retry-After"),
                        )
                    if response.status_code in {401, 403}:
                        raise MarketDataProviderError(
                            "provider_authentication_failed",
                            status_code=response.status_code,
                        )
                    if response.status_code >= 500:
                        raise MarketDataProviderError(
                            "provider_server_error",
                            status_code=response.status_code,
                        )
                    if response.status_code >= 400:
                        raise MarketDataProviderError(
                            "provider_request_failed",
                            status_code=response.status_code,
                        )
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


class DhanV2Adapter:
    _base_url = "https://api.dhan.co/v2"
    _intraday_intervals = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "25m": "25",
        "1h": "60",
        "60m": "60",
    }

    def __init__(self, tokens: dict[str, str]):
        self.access_token = tokens.get("access_token", "")
        self.client_id = (
            tokens.get("client_id")
            or tokens.get("clientId")
            or tokens.get("client_code")
            or ""
        )
        self.last_response_status: int | None = None
        self.last_http_diagnostics: list[dict[str, Any]] = []

    def get_historical_bars(
        self,
        exchange_segment: str,
        security_id: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[HistoricalBar]:
        if not self.access_token:
            raise MarketDataProviderError(
                "provider_authentication_failed", status_code=401
            )
        if exchange_segment not in {"NSE_EQ", "BSE_EQ"}:
            raise MarketDataProviderError("invalid_instrument_mapping")
        start_local = start.astimezone(ZoneInfo("Asia/Kolkata"))
        end_local = end.astimezone(ZoneInfo("Asia/Kolkata"))
        body: dict[str, Any] = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": "EQUITY",
            "expiryCode": 0,
            "oi": False,
            "fromDate": (
                start_local.strftime("%Y-%m-%d %H:%M:%S")
                if interval != "1d"
                else start_local.date().isoformat()
            ),
            "toDate": (
                end_local.strftime("%Y-%m-%d %H:%M:%S")
                if interval != "1d"
                else end_local.date().isoformat()
            ),
        }
        if interval == "1d":
            path = "/charts/historical"
        else:
            dhan_interval = self._intraday_intervals.get(interval)
            if dhan_interval is None:
                raise MarketDataProviderError("unsupported_interval")
            path = "/charts/intraday"
            body["interval"] = dhan_interval
        payload = self._post_payload(path, body)
        arrays = {
            name: payload.get(name)
            for name in ("open", "high", "low", "close", "volume", "timestamp")
        }
        if any(not isinstance(values, list) for values in arrays.values()):
            raise MarketDataProviderError("invalid_provider_response")
        if len({len(values) for values in arrays.values()}) != 1:
            raise MarketDataProviderError("invalid_provider_response")
        try:
            return [
                _HttpProvider._bar(
                    arrays["timestamp"][index],
                    arrays["open"][index],
                    arrays["high"][index],
                    arrays["low"][index],
                    arrays["close"][index],
                    arrays["volume"][index],
                )
                for index in range(len(arrays["timestamp"]))
            ]
        except (IndexError, TypeError):
            raise MarketDataProviderError("invalid_provider_response") from None

    def _post_payload(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    url,
                    json=body,
                    headers={
                        "access-token": self.access_token,
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException:
            self._record_http_diagnostic(url, None, None, started, body)
            raise MarketDataProviderError("provider_timeout") from None
        except httpx.TransportError:
            self._record_http_diagnostic(url, None, None, started, body)
            raise MarketDataProviderError("provider_network_error") from None
        response_body = response.text
        self._record_http_diagnostic(
            str(response.request.url),
            response.status_code,
            response_body,
            started,
            body,
        )
        safe_response_body = _redact_diagnostic_text(
            response_body[:500],
            (self.access_token, self.client_id),
        )
        safe_request_url = _redact_diagnostic_url(str(response.request.url))
        safe_request_body = _redact_diagnostic_text(
            json.dumps(body, separators=(",", ":"), sort_keys=True),
            (self.access_token, self.client_id),
        )
        if (
            path in {"/charts/historical", "/charts/intraday"}
            and re.search(r"\bDH-907\b", response_body, re.IGNORECASE)
        ):
            raise MarketDataProviderError(
                "provider_no_history_for_range",
                status_code=response.status_code,
                response_body=safe_response_body,
                request_url=safe_request_url,
                request_body=safe_request_body,
            )
        if response.status_code == 429:
            raise MarketDataProviderError(
                "provider_rate_limit_exceeded",
                status_code=429,
                retry_after=response.headers.get("Retry-After"),
                response_body=safe_response_body,
                request_url=safe_request_url,
                request_body=safe_request_body,
            )
        if response.status_code in {401, 403}:
            raise MarketDataProviderError(
                "provider_authentication_failed",
                status_code=response.status_code,
                response_body=safe_response_body,
                request_url=safe_request_url,
                request_body=safe_request_body,
            )
        if response.status_code >= 500:
            raise MarketDataProviderError(
                "provider_server_error",
                status_code=response.status_code,
                response_body=safe_response_body,
                request_url=safe_request_url,
                request_body=safe_request_body,
            )
        if response.status_code >= 400:
            raise MarketDataProviderError(
                "provider_request_failed",
                status_code=response.status_code,
                response_body=safe_response_body,
                request_url=safe_request_url,
                request_body=safe_request_body,
            )
        try:
            payload = response.json()
        except ValueError:
            raise MarketDataProviderError("invalid_provider_response") from None
        if not isinstance(payload, dict):
            raise MarketDataProviderError("invalid_provider_response")
        if str(payload.get("status", "")).casefold() in {
            "failure", "failed", "false",
        }:
            raise MarketDataProviderError(
                "provider_request_failed",
                status_code=response.status_code,
                response_body=safe_response_body,
                request_url=safe_request_url,
                request_body=safe_request_body,
            )
        return payload

    def _record_http_diagnostic(
        self,
        url: str,
        status: int | None,
        body: str | None,
        started: float,
        request_body: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "url": _redact_diagnostic_url(url),
            "http_status": status,
            "request_body": (
                _redact_diagnostic_text(
                    json.dumps(request_body, separators=(",", ":"), sort_keys=True),
                    (self.access_token, self.client_id),
                )
                if request_body is not None
                else None
            ),
            "response_body": (
                _redact_diagnostic_text(
                    body[:500],
                    (self.access_token, self.client_id),
                )
                if body is not None
                else None
            ),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
        self.last_response_status = status
        self.last_http_diagnostics.append(entry)
        logger.info("dhan_http_request", extra={"dhan_http": entry})


class DhanAccountRequestError(Exception):
    def __init__(self, code: str, status_code: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class DhanReadOnlyAccountAdapter:
    _base_url = "https://api.dhan.co/v2"

    def __init__(self, access_token: str, client_id: str):
        self.access_token = access_token
        self.client_id = client_id
        self.last_http_diagnostics: list[dict[str, Any]] = []

    def get_holdings(self) -> list[dict[str, Any]]:
        payload = self._get("/holdings")
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise DhanAccountRequestError("invalid_provider_response")
        return payload

    def get_positions(self) -> list[dict[str, Any]]:
        payload = self._get("/positions")
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise DhanAccountRequestError("invalid_provider_response")
        return payload

    def get_fundlimit(self) -> dict[str, Any]:
        payload = self._get("/fundlimit")
        if not isinstance(payload, dict):
            raise DhanAccountRequestError("invalid_provider_response")
        return payload

    def get_past_trades(
        self, from_date: str, to_date: str, page: int
    ) -> list[dict[str, Any]]:
        payload = self._get(f"/trades/{from_date}/{to_date}/{page}")
        return self._records(payload)

    def get_ledger(self, from_date: str, to_date: str) -> list[dict[str, Any]]:
        payload = self._get(
            "/ledger",
            params={"from-date": from_date, "to-date": to_date},
        )
        return self._records(payload)

    @staticmethod
    def _records(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                payload = data
            elif isinstance(data, dict):
                payload = [data]
            elif any(
                key in payload
                for key in ("exchangeTradeId", "vouchernumber")
            ):
                payload = [payload]
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise DhanAccountRequestError("invalid_provider_response")
        return payload

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    url,
                    params=params,
                    headers={
                        "access-token": self.access_token,
                        "client-id": self.client_id,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException:
            self._record_http_diagnostic(url, None, None, started)
            raise DhanAccountRequestError("provider_timeout") from None
        except httpx.TransportError:
            self._record_http_diagnostic(url, None, None, started)
            raise DhanAccountRequestError("provider_network_error") from None
        body = response.text
        self._record_http_diagnostic(
            str(response.request.url), response.status_code, body, started
        )
        if path == "/holdings" and response.status_code == 500:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if (
                isinstance(error_payload, dict)
                and error_payload.get("errorCode") == "DH-1111"
            ):
                return []
        if response.status_code in {401, 403}:
            raise DhanAccountRequestError(
                "provider_authentication_failed", response.status_code
            )
        if response.status_code == 429:
            raise DhanAccountRequestError("provider_rate_limited", 429)
        if response.status_code >= 500:
            raise DhanAccountRequestError(
                "provider_server_error", response.status_code
            )
        if response.status_code >= 400:
            raise DhanAccountRequestError(
                "provider_request_failed", response.status_code
            )
        try:
            return response.json()
        except ValueError:
            raise DhanAccountRequestError(
                "invalid_provider_response", response.status_code
            ) from None

    def _record_http_diagnostic(
        self,
        url: str,
        status: int | None,
        body: str | None,
        started: float,
    ) -> None:
        entry = {
            "url": _redact_diagnostic_url(url),
            "http_status": status,
            "response_body": (
                _redact_diagnostic_text(
                    body[:500],
                    (self.access_token, self.client_id),
                )
                if body is not None
                else None
            ),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
        self.last_http_diagnostics.append(entry)
        logger.info("dhan_account_http_request", extra={"dhan_http": entry})


class AngelOneProvider(_HttpProvider):
    name = "angel_one"
    api_root = "https://apiconnect.angelone.in"
    scrip_master_url = (
        "https://margincalculator.angelone.in/OpenAPI_File/files/"
        "OpenAPIScripMaster.json"
    )
    stream_url = "wss://smartapisocket.angelone.in/smart-stream"
    max_history_chunk = timedelta(days=30)
    max_history_requests = 24
    _scrip_master_cache: tuple[float, list[dict[str, Any]]] | None = None
    _scrip_master_cache_seconds = 86_400
    _intervals = {
        "1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE",
        "1h": "ONE_HOUR", "1d": "ONE_DAY",
    }

    def __init__(
        self,
        api_key: str,
        tokens: dict[str, str],
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(transport=transport)
        self.api_key = api_key
        self.tokens = tokens
        self.last_http_diagnostics: list[dict[str, Any]] = []

    def _request_payload(
        self,
        path: str,
        body: dict[str, Any],
        *,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        self.last_response_status = None
        diagnostic_count = len(self.last_http_diagnostics)
        try:
            return self._post_payload(
                path,
                self.api_key,
                body,
                access_token=access_token,
                transport=self._transport,
                diagnostics=self.last_http_diagnostics,
            )
        finally:
            if len(self.last_http_diagnostics) > diagnostic_count:
                self.last_response_status = self.last_http_diagnostics[-1]["http_status"]

    @classmethod
    def _headers(cls, api_key: str, access_token: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-PrivateKey": api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": settings.angel_one_client_local_ip,
            "X-ClientPublicIP": settings.angel_one_client_public_ip,
            "X-MACAddress": settings.angel_one_client_mac_address,
        }
        if access_token:
            headers["Authorization"] = (
                access_token if access_token.startswith("Bearer ") else f"Bearer {access_token}"
            )
        return headers

    @classmethod
    def _post_payload(
        cls,
        path: str,
        api_key: str,
        body: dict[str, Any],
        *,
        access_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        request_url = f"{cls.api_root}{path}"
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT, follow_redirects=False, transport=transport
            ) as client:
                response = client.post(
                    request_url,
                    json=body,
                    headers=cls._headers(api_key, access_token),
                )
        except httpx.TimeoutException:
            cls._record_http_diagnostic(
                diagnostics, request_url, None, None, started, api_key, access_token
            )
            raise MarketDataProviderError("provider_timeout") from None
        except httpx.TransportError:
            cls._record_http_diagnostic(
                diagnostics, request_url, None, None, started, api_key, access_token
            )
            raise MarketDataProviderError("provider_network_error") from None
        response_body = response.text
        cls._record_http_diagnostic(
            diagnostics,
            str(response.request.url),
            response.status_code,
            response_body[:500],
            started,
            api_key,
            access_token,
        )
        if response.status_code == 429:
            raise MarketDataProviderError(
                "provider_rate_limit_exceeded",
                status_code=429,
                retry_after=response.headers.get("Retry-After"),
            )
        if response.status_code == 401:
            raise MarketDataProviderError("provider_authentication_failed", status_code=401)
        if response.status_code == 403:
            raise MarketDataProviderError("provider_subscription_required", status_code=403)
        if response.status_code >= 500:
            raise MarketDataProviderError("provider_server_error", status_code=response.status_code)
        if response.status_code >= 400:
            raise MarketDataProviderError("provider_request_failed", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise MarketDataProviderError("invalid_provider_response") from None
        if not isinstance(payload, dict):
            raise MarketDataProviderError("invalid_provider_response")
        if payload.get("status") is False or payload.get("success") is False:
            code = str(
                payload.get("errorcode") or payload.get("errorCode") or ""
            ).casefold()
            message = str(payload.get("message") or "").casefold()
            if any(value in f"{code} {message}" for value in ("token", "session", "jwt")):
                raise MarketDataProviderError("provider_authentication_failed", status_code=401)
            if any(value in f"{code} {message}" for value in ("subscription", "entitlement")):
                raise MarketDataProviderError("provider_subscription_required")
            raise MarketDataProviderError("provider_request_failed")
        return payload

    @classmethod
    def _record_http_diagnostic(
        cls,
        diagnostics: list[dict[str, Any]] | None,
        url: str,
        status: int | None,
        body: str | None,
        started: float,
        *secrets: str | None,
    ) -> None:
        entry = {
            "url": _redact_diagnostic_url(url),
            "http_status": status,
            "request_headers": {
                name: "[REDACTED]"
                for name in (
                    "Authorization",
                    "X-PrivateKey",
                    "X-UserType",
                    "X-SourceID",
                    "X-ClientLocalIP",
                    "X-ClientPublicIP",
                    "X-MACAddress",
                    "Accept",
                    "Content-Type",
                )
            },
            "response_body": (
                _redact_diagnostic_text(body[:500], tuple(secret for secret in secrets if secret))
                if body is not None
                else None
            ),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
        if diagnostics is not None:
            diagnostics.append(entry)
        logger.info("angel_one_http_request", extra={"angel_one_http": entry})

    @staticmethod
    def _expiry(access_token: str) -> datetime:
        try:
            encoded = access_token.split(".")[1]
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            expiry = datetime.fromtimestamp(json.loads(decoded)["exp"], tz=timezone.utc)
            return expiry
        except (IndexError, KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
            return datetime.now(timezone.utc) + timedelta(hours=12)

    @classmethod
    def login(
        cls,
        api_key: str,
        client_code: str,
        pin: str,
        totp: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> tuple[dict[str, str], datetime]:
        try:
            payload = cls._post_payload(
                "/rest/auth/angelbroking/user/v1/loginByPassword",
                api_key,
                {"clientcode": client_code, "password": pin, "totp": totp},
                transport=transport,
            )
        except MarketDataProviderError as error:
            if error.code == "provider_request_failed" and error.status_code is None:
                raise MarketDataProviderError(
                    "provider_authentication_failed", status_code=401
                ) from None
            raise
        data = payload.get("data")
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), str) and data[key]
            for key in ("jwtToken", "refreshToken", "feedToken")
        ):
            raise MarketDataProviderError("invalid_provider_response")
        tokens = {
            "client_code": client_code,
            "access_token": data["jwtToken"],
            "refresh_token": data["refreshToken"],
            "feed_token": data["feedToken"],
        }
        return tokens, cls._expiry(tokens["access_token"])

    @classmethod
    def refresh_session(
        cls,
        api_key: str,
        tokens: dict[str, str],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> tuple[dict[str, str], datetime]:
        payload = cls._post_payload(
            "/rest/auth/angelbroking/jwt/v1/generateTokens",
            api_key,
            {"refreshToken": tokens["refresh_token"]},
            transport=transport,
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("jwtToken"), str):
            raise MarketDataProviderError("provider_authentication_failed", status_code=401)
        renewed = {
            **tokens,
            "access_token": data["jwtToken"],
            "refresh_token": data.get("refreshToken", tokens["refresh_token"]),
            "feed_token": data.get("feedToken", tokens["feed_token"]),
        }
        return renewed, cls._expiry(renewed["access_token"])

    @classmethod
    def _master_rows(
        cls, transport: httpx.BaseTransport | None = None
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        if (
            cls._scrip_master_cache
            and now - cls._scrip_master_cache[0] < cls._scrip_master_cache_seconds
            and cls._valid_master_rows(cls._scrip_master_cache[1])
        ):
            return cls._scrip_master_cache[1]
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": "TradingCopilot/1.0"},
            ) as client:
                with client.stream("GET", cls.scrip_master_url) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if (
                        content_length is not None
                        and content_length.isdigit()
                        and int(content_length) > MAX_ANGEL_ONE_MASTER_BYTES
                    ):
                        raise MarketDataProviderError("provider_response_too_large")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > MAX_ANGEL_ONE_MASTER_BYTES:
                            raise MarketDataProviderError("provider_response_too_large")
                        content.extend(chunk)
        except MarketDataProviderError:
            raise
        except httpx.TimeoutException:
            raise MarketDataProviderError("provider_timeout") from None
        except httpx.HTTPError:
            raise MarketDataProviderError("provider_network_error") from None
        try:
            rows = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise MarketDataProviderError("invalid_provider_response") from None
        if not isinstance(rows, list):
            raise MarketDataProviderError("invalid_provider_response")
        equities = [row for row in rows if cls._valid_master_row(row)]
        if not equities:
            raise MarketDataProviderError("invalid_provider_response")
        cls._scrip_master_cache = (now, equities)
        return equities

    @staticmethod
    def _valid_master_row(row: Any) -> bool:
        return (
            isinstance(row, dict)
            and str(row.get("exch_seg", "")).upper() in {"NSE", "BSE"}
            and str(row.get("instrumenttype", "")).upper() == "EQ"
            and str(row.get("symbol", "")).upper().endswith("-EQ")
            and str(row.get("token", "")).isdigit()
        )

    @classmethod
    def _valid_master_rows(cls, rows: Any) -> bool:
        return isinstance(rows, list) and bool(rows) and all(
            cls._valid_master_row(row) for row in rows
        )

    @staticmethod
    def _candidate(row: dict[str, Any]) -> InstrumentCandidate:
        exchange = str(row["exch_seg"]).upper()
        provider_symbol = str(row["symbol"]).upper()
        return _candidate(
            "angel_one",
            f"{row['token']}|{exchange}",
            provider_symbol,
            provider_symbol.removesuffix("-EQ"),
            str(row.get("name") or provider_symbol.removesuffix("-EQ")),
            exchange,
            "INR",
            "IN",
            "Asia/Kolkata",
        ).model_copy(update={"is_verified": True})

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        return self._search_instruments(query, ("NSE", "BSE"), limit)

    def search_instruments_for_exchange(
        self, query: str, exchange: str, limit: int
    ) -> list[InstrumentCandidate]:
        exchange_aliases = {
            "NSE": "NSE", "XNSE": "NSE", "NSI": "NSE", "NSE_EQ": "NSE",
            "BSE": "BSE", "XBOM": "BSE", "BOM": "BSE", "BSE_EQ": "BSE",
        }
        normalized_exchange = exchange_aliases.get(exchange.upper())
        if normalized_exchange is None:
            raise MarketDataProviderError("invalid_instrument_mapping")
        return self._search_instruments(query, (normalized_exchange,), limit)

    def _search_instruments(
        self, query: str, exchanges: tuple[str, ...], limit: int
    ) -> list[InstrumentCandidate]:
        search_term = query.strip()
        if not search_term:
            raise MarketDataProviderError("search_query_required")
        access_token = self.tokens.get("access_token", "")
        if not access_token:
            raise MarketDataProviderError(
                "provider_authentication_failed", status_code=401
            )
        candidates: list[InstrumentCandidate] = []
        failures: list[MarketDataProviderError] = []
        for exchange in exchanges:
            try:
                payload = self._request_payload(
                    "/rest/secure/angelbroking/order/v1/searchScrip",
                    {"exchange": exchange, "searchscrip": search_term},
                    access_token=access_token,
                )
            except MarketDataProviderError as error:
                failures.append(error)
                continue
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise MarketDataProviderError("invalid_provider_response")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                token = str(row.get("symboltoken") or "")
                symbol = str(row.get("tradingsymbol") or "").upper()
                row_exchange = str(row.get("exchange") or exchange).upper()
                if (
                    row_exchange != exchange
                    or not token.isdigit()
                    or not symbol.endswith("-EQ")
                ):
                    continue
                provider_candidate = _candidate(
                    "angel_one",
                    f"{token}|{exchange}",
                    symbol,
                    symbol.removesuffix("-EQ"),
                    str(row.get("name") or symbol.removesuffix("-EQ")),
                    exchange,
                    "INR",
                    "IN",
                    "Asia/Kolkata",
                ).model_copy(update={"is_verified": True})
                candidates.append(provider_candidate)
        if not candidates and failures:
            raise failures[0]
        return candidates[:limit]

    def _row_for_id(self, provider_instrument_id: str) -> dict[str, Any]:
        token, separator, exchange = provider_instrument_id.partition("|")
        if not separator or exchange not in {"NSE", "BSE"} or not token.isdigit():
            raise MarketDataProviderError("invalid_instrument_mapping")
        row = next(
            (item for item in self._master_rows(self._transport)
             if str(item.get("token")) == token
             and str(item.get("exch_seg", "")).upper() == exchange),
            None,
        )
        if row is None:
            raise MarketDataProviderError("instrument_not_found")
        return row

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        return self._candidate(self._row_for_id(provider_instrument_id))

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise MarketDataProviderError("market_timestamp_unavailable")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%d-%m-%Y %H:%M:%S")
            except ValueError:
                raise MarketDataProviderError("invalid_provider_response") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return parsed.astimezone(timezone.utc)

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        token, exchange = instrument.provider_instrument_id.split("|", 1)
        payload = self._request_payload(
            "/rest/secure/angelbroking/market/v1/quote/",
            {"mode": "FULL", "exchangeTokens": {exchange: [token]}},
            access_token=self.tokens["access_token"],
        )
        data = payload.get("data")
        rows = data.get("fetched") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise MarketDataProviderError("provider_quote_unavailable")
        row = next(
            (item for item in rows
             if isinstance(item, dict) and str(item.get("symbolToken")) == token),
            None,
        )
        if row is None:
            raise MarketDataProviderError("provider_quote_unavailable")
        timestamp = self._timestamp(
            row.get("exchangeFeedTime")
            or row.get("exchFeedTime")
            or row.get("exchTradeTime")
        )
        return self._bar(
            timestamp,
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            row.get("tradeVolume"),
        )

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
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise MarketDataProviderError("invalid_time_range")
        self.last_response_status = None
        token, exchange = instrument.provider_instrument_id.split("|", 1)
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        remaining_end = end_utc
        bars: dict[datetime, HistoricalBar] = {}
        for _ in range(self.max_history_requests):
            if remaining_end <= start_utc or len(bars) >= limit:
                break
            chunk_start = max(start_utc, remaining_end - self.max_history_chunk)
            payload = self._request_payload(
                "/rest/secure/angelbroking/historical/v1/getCandleData",
                {
                    "exchange": exchange,
                    "symboltoken": token,
                    "interval": self._intervals[interval],
                    "fromdate": chunk_start.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M"),
                    "todate": remaining_end.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M"),
                },
                access_token=self.tokens["access_token"],
            )
            rows = payload.get("data")
            if rows is not None and not isinstance(rows, list):
                raise MarketDataProviderError("invalid_provider_response")
            for row in rows or []:
                if not isinstance(row, list) or len(row) < 6:
                    raise MarketDataProviderError("invalid_provider_response")
                bar = self._bar(row[0], *row[1:6])
                if start_utc <= bar.timestamp < end_utc:
                    bars[bar.timestamp] = bar
            if not rows:
                break
            remaining_end = chunk_start
        return sorted(bars.values(), key=lambda bar: bar.timestamp)[-limit:]

    @staticmethod
    def decode_stream_tick(packet: bytes) -> dict[str, Any]:
        if len(packet) < 123 or packet[0] not in (2, 3):
            raise MarketDataProviderError("invalid_provider_response")
        try:
            token = packet[2:27].split(b"\0", 1)[0].decode("ascii", errors="strict")
        except UnicodeDecodeError:
            raise MarketDataProviderError("invalid_provider_response") from None
        timestamp_ms = struct.unpack_from("<q", packet, 35)[0]
        if not token or timestamp_ms <= 0:
            raise MarketDataProviderError("market_timestamp_unavailable")
        price_at = lambda offset: Decimal(struct.unpack_from("<q", packet, offset)[0]) / Decimal(100)
        return {
            "token": token,
            "exchange_type": packet[1],
            "timestamp": datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
            "open": price_at(91),
            "high": price_at(99),
            "low": price_at(107),
            "close": price_at(115),
            "ltp": price_at(43),
            "volume": Decimal(struct.unpack_from("<q", packet, 67)[0]),
        }

    async def stream_quote(self, instrument: InstrumentCandidate):
        token, exchange = instrument.provider_instrument_id.split("|", 1)
        headers = {
            "Authorization": self.tokens["access_token"],
            "x-api-key": self.api_key,
            "x-client-code": self.tokens["client_code"],
            "x-feed-token": self.tokens["feed_token"],
        }
        async with websocket_connect(
            self.stream_url,
            additional_headers=headers,
            ping_interval=10,
            ping_timeout=10,
            close_timeout=5,
        ) as socket:
            await socket.send(json.dumps({
                "action": 1,
                "params": {"mode": 2, "tokenList": [{
                    "exchangeType": {"NSE": 1, "BSE": 3}[exchange],
                    "tokens": [token],
                }]},
            }))
            async for message in socket:
                if not isinstance(message, bytes):
                    continue
                tick = self.decode_stream_tick(message)
                if (
                    tick["token"] == token
                    and tick["exchange_type"] == {"NSE": 1, "BSE": 3}[exchange]
                ):
                    yield HistoricalBar(
                        timestamp=tick["timestamp"],
                        open=tick["open"],
                        high=tick["high"],
                        low=tick["low"],
                        close=tick["ltp"],
                        volume=tick["volume"],
                    )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=False,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote", "stream"),
            status="authenticated_session_connectivity_unverified",
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
        stamp = record.get("timestamp")
        if stamp is None:
            raise MarketDataProviderError("provider_quote_timestamp_unavailable")
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
            if payload.get("closeTime") is None:
                raise MarketDataProviderError("provider_quote_timestamp_unavailable")
            close_time = datetime.fromtimestamp(
                int(payload["closeTime"]) / 1000,
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
        timestamp = record.get("last_trade_time")
        if timestamp is None:
            raise MarketDataProviderError("provider_quote_timestamp_unavailable")
        return self._bar(
            timestamp,
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
        global _yfinance_cooldown_until
        with _yfinance_cooldown_lock:
            if time.monotonic() < _yfinance_cooldown_until:
                raise MarketDataProviderError("yfinance_cooldown_active")
        for attempt in range(3):
            try:
                return _YFINANCE_EXECUTOR.submit(function, *args, **kwargs).result()
            except Exception as error:
                if attempt == 2:
                    error_text = f"{type(error).__name__} {error}".casefold()
                    if "429" in error_text or "rate limit" in error_text:
                        category = "rate_limited"
                    elif "401" in error_text or "403" in error_text or "access denied" in error_text:
                        category = "access_denied"
                    elif "crumb" in error_text or "cookie" in error_text or "unauthorized" in error_text:
                        category = "session_invalid"
                    elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
                        category = "provider_timeout"
                    else:
                        category = "provider_request_failed"
                    with _yfinance_cooldown_lock:
                        _yfinance_cooldown_until = (
                            time.monotonic()
                            + (
                                _YFINANCE_RATE_LIMIT_COOLDOWN_SECONDS
                                if category == "rate_limited"
                                else _YFINANCE_COOLDOWN_SECONDS
                            )
                        )
                    raise MarketDataProviderError(
                        category
                    ) from None
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
    if provider_name == "mock":
        raise MarketDataProviderError("synthetic_market_data_disabled")
    if asset_class not in {"equity", "etf", "index", "future", "option", "crypto"}:
        raise MarketDataProviderError("unsupported_provider_asset_class")
    if asset_class in {"future", "option"}:
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
