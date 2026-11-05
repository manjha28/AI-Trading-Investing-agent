from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
import hashlib
import csv
import json
import logging
import math
import re
import random
import struct
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from app.config import settings
from app.market_data import HistoricalBar

logger = logging.getLogger(__name__)
_DHAN_MAX_CONSECUTIVE_FAILURES = 5
_DHAN_MAX_RETRY_DELAY_SECONDS = 60
_DHAN_FRAME_DIAGNOSTIC_CAPTURED = False
_DHAN_LIVE_SOCKET_LOCK = threading.Lock()
_DHAN_LIVE_CONNECTIONS = threading.BoundedSemaphore(5)


@asynccontextmanager
async def _single_dhan_live_connection(*args: Any, **kwargs: Any):
    """Limit Dhan sockets to five and serialize only their handshakes."""
    while not _DHAN_LIVE_CONNECTIONS.acquire(blocking=False):
        await asyncio.sleep(0.05)
    context = websocket_connect(*args, **kwargs)
    connection = None
    try:
        while not _DHAN_LIVE_SOCKET_LOCK.acquire(blocking=False):
            await asyncio.sleep(0.05)
        try:
            connection = await context.__aenter__()
        finally:
            _DHAN_LIVE_SOCKET_LOCK.release()
        yield connection
    finally:
        try:
            if connection is not None:
                await context.__aexit__(None, None, None)
        finally:
            _DHAN_LIVE_CONNECTIONS.release()


def _dhan_retry_after_seconds(error: InvalidStatus) -> float | None:
    raw = error.response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _dhan_backoff_seconds(base: float, retry_after: float | None = None) -> float:
    if retry_after is not None:
        cooldown = max(300.0, retry_after)
        return cooldown + random.uniform(0.0, min(30.0, max(1.0, cooldown * 0.1)))
    bounded = min(max(1.0, base), _DHAN_MAX_RETRY_DELAY_SECONDS)
    return random.uniform(max(0.1, bounded * 0.8), bounded)


def _log_dhan_bad_frame_once(frame: bytes | str) -> None:
    global _DHAN_FRAME_DIAGNOSTIC_CAPTURED
    if not logger.isEnabledFor(logging.DEBUG) or _DHAN_FRAME_DIAGNOSTIC_CAPTURED:
        return
    frame_bytes = frame.encode("utf-8") if isinstance(frame, str) else frame
    logger.debug(
        "Dhan live feed failing frame length=%d type=%s first_eight_bytes_hex=%s",
        len(frame_bytes),
        "text" if isinstance(frame, str) else "binary",
        frame_bytes[:8].hex(),
    )
    _DHAN_FRAME_DIAGNOSTIC_CAPTURED = True
_OPTION_CHAIN_REQUEST_LOCK = threading.Lock()
_OPTION_CHAIN_LAST_REQUEST = 0.0

_SENSITIVE_PROVIDER_FIELD = re.compile(
    r"(?i)\b(access[-_ ]?token|refresh[-_ ]?token|tokenid|token|authorization|"
    r"app[_ -]?secret|api[_ -]?secret|api[_ -]?key|dhanclient(?:id|ucc|name)|"
    r"account(?:[-_ ]?id)?|"
    r"client(?:[-_ ]?id)?|ucc|password|pin|otp|securityid|isin|symbol|"
    r"quantity|holdings?)\b(?:\s*[:=]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_OPAQUE_PROVIDER_VALUE = re.compile(r"\b[A-Za-z0-9_~+/=-]{24,}\b")
_LONG_NUMBER = re.compile(r"\b\d{5,}\b")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_DHAN_TICK_EXCHANGES = {"NSE_EQ": "NSE", "BSE_EQ": "BSE"}
_DHAN_TICK_CACHE: dict[tuple[str, str], Decimal | None] = {}


def _float32_wire_to_price(raw: float, tick: Decimal) -> Decimal:
    """Quantise a float32 wire value to ``tick`` without a binary-float Decimal conversion.

    The shortest decimal string (at most 9 significant digits) that rounds back to the
    same float32 is chosen, then quantised to the tick. A float32 has ~7 significant
    digits; above roughly Rs 1,31,072 its ULP exceeds Rs 0.01, so paise cannot be
    recovered there. That is a Dhan wire-format limit, not something this rounding hides.
    """
    wire = struct.unpack("<f", struct.pack("<f", raw))[0]
    if not math.isfinite(wire):
        raise ValueError("non-finite float32 price")
    candidate = Decimal(format(wire, ".9g"))
    for digits in range(1, 10):
        text = Decimal(format(wire, f".{digits}g"))
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == wire:
            candidate = text
            break
    return candidate.quantize(tick, rounding=ROUND_HALF_EVEN)


def _quantize_price_text(value: object, tick: Decimal) -> Decimal:
    """Quantise a JSON price to ``tick`` via its string form, never ``Decimal(float)``."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a price")
    return Decimal(str(value)).quantize(tick, rounding=ROUND_HALF_EVEN)


def _dhan_ist_epoch_to_utc(timestamp: int) -> datetime:
    encoded_wall_clock = datetime.fromtimestamp(timestamp, timezone.utc)
    return encoded_wall_clock.replace(
        tzinfo=ZoneInfo("Asia/Kolkata")
    ).astimezone(timezone.utc)


_DHAN_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "60m": 60}


def _dhan_candle_is_complete(timestamp: datetime, interval: str, now: datetime) -> bool:
    if interval == "1d":
        local_date = timestamp.astimezone(ZoneInfo("Asia/Kolkata")).date()
        session_close = datetime(
            local_date.year, local_date.month, local_date.day, 15, 30,
            tzinfo=ZoneInfo("Asia/Kolkata"),
        )
        return now >= session_close.astimezone(timezone.utc)
    return timestamp + timedelta(minutes=_DHAN_INTERVAL_MINUTES[interval]) <= now


def _safe_provider_error(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    details = payload.get("data")
    if isinstance(details, dict):
        payload = {**payload, **details}
    raw_code = next(
        (payload.get(key) for key in ("errorCode", "error_code", "code") if payload.get(key) is not None),
        None,
    )
    code = None
    if isinstance(raw_code, (str, int)) and re.fullmatch(
        r"(?:[A-Z]{1,8}[-_.]?)?\d{1,5}", str(raw_code)
    ):
        code = str(raw_code)
    raw_message = next(
        (
            payload.get(key)
            for key in ("errorMessage", "error_message", "message", "remarks")
            if isinstance(payload.get(key), str)
        ),
        None,
    )
    if raw_message is None:
        return code, None
    message = _SENSITIVE_PROVIDER_FIELD.sub(lambda match: f"{match.group(1)}=[REDACTED]", raw_message)
    message = _URL.sub("[URL_REDACTED]", message)
    message = _OPAQUE_PROVIDER_VALUE.sub("[VALUE_REDACTED]", message)
    message = _LONG_NUMBER.sub("[NUMBER_REDACTED]", message)
    message = "".join(char if char.isprintable() else " " for char in message)
    return code, message[:160]


def _log_dhan_request_failure(
    stage: str,
    path: str,
    *,
    error_class: str,
    description: str,
    status_code: int | None = None,
    payload: Any = None,
) -> None:
    provider_code, provider_message = _safe_provider_error(payload)
    logger.warning(
        "Dhan sync diagnostic stage=%s endpoint=%s status_code=%s "
        "error_class=%s description=%s provider_code=%s provider_message=%s",
        stage,
        path,
        status_code if status_code is not None else "unavailable",
        error_class,
        description,
        provider_code or "unavailable",
        provider_message or "unavailable",
    )


class BrokerAdapterError(Exception):
    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_request_failed",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


class BrokerReconnectRequired(BrokerAdapterError):
    def __init__(self, message: str, *, status_code: int | None = 401) -> None:
        super().__init__(
            message,
            category="authentication_expired",
            status_code=status_code,
        )


class BrokerFeedDisconnected(BrokerAdapterError):
    pass


class BrokerRateLimited(BrokerAdapterError):
    def __init__(self, message: str, *, status_code: int | None = 429) -> None:
        super().__init__(
            message,
            category="rate_limited",
            status_code=status_code,
        )


_dhan_request_lock = threading.Lock()
_dhan_next_request_at = 0.0


def _wait_for_dhan_market_data() -> None:
    global _dhan_next_request_at
    with _dhan_request_lock:
        now = time.monotonic()
        delay = max(0.0, _dhan_next_request_at - now)
        _dhan_next_request_at = now + delay + 1.0
    if delay:
        time.sleep(delay)


def _dhan_feed_url(access_token: str, client_id: str) -> str:
    query = urlencode({
        "version": "2",
        "token": access_token,
        "clientId": client_id,
        "authType": "2",
    })
    return f"wss://api-feed.dhan.co?{query}"


def _dhan_depth_feed_url(access_token: str, client_id: str) -> str:
    query = urlencode({
        "token": access_token,
        "clientId": client_id,
        "authType": "2",
    })
    return f"wss://depth-api-feed.dhan.co/twentydepth?{query}"


def _safe_dhan_close_reason(reason: str | None, *secrets: str) -> str:
    safe_reason = reason or ""
    for secret in secrets:
        if secret:
            safe_reason = safe_reason.replace(secret, "[REDACTED]")
    return "".join(char if char.isprintable() else " " for char in safe_reason)[:160]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def _required(*values: str | None) -> bool:
    return all(value and value.strip() for value in values)


def _expires_at(payload: Mapping[str, Any], default: timedelta) -> datetime:
    raw = next(
        (payload[key] for key in ("expiryTime", "expires_at", "expiresAt") if payload.get(key) is not None),
        None,
    )
    if isinstance(raw, (int, float)):
        timestamp = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise BrokerAdapterError("Broker returned an invalid token expiry") from error
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError as error:
            raise BrokerAdapterError("Broker returned an invalid token expiry") from error
    if raw is not None:
        raise BrokerAdapterError("Broker returned an invalid token expiry")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return _now() + timedelta(seconds=int(expires_in))
    if expires_in is not None:
        raise BrokerAdapterError("Broker returned an invalid token lifetime")
    return _now() + default


def _body(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as error:
        raise BrokerAdapterError("Broker returned an invalid response") from error
    if response.is_error:
        raise BrokerAdapterError("Broker request failed")
    return payload


def _request(
    method: str,
    url: str,
    *,
    diagnostic_stage: str | None = None,
    allow_empty_dhan_holdings: bool = False,
    **kwargs: Any,
) -> Any:
    path = urlsplit(url).path
    try:
        response = httpx.request(method, url, timeout=20.0, **kwargs)
    except httpx.HTTPError as error:
        if diagnostic_stage is not None:
            _log_dhan_request_failure(
                diagnostic_stage,
                path,
                error_class=type(error).__name__,
                description="Upstream request did not complete.",
            )
        raise BrokerAdapterError("Broker is unavailable") from None
    if diagnostic_stage is not None and response.is_error:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = None
        _log_dhan_request_failure(
            diagnostic_stage,
            path,
            status_code=response.status_code,
            error_class="HTTPStatusError",
            description="Upstream request returned an HTTP error.",
            payload=error_payload,
        )
        if response.status_code == 401:
            raise BrokerReconnectRequired("Reconnect to this broker to renew the session")
        if diagnostic_stage is not None and response.status_code == 429:
            raise BrokerRateLimited("Dhan market-data request was rate limited")
        provider_code, provider_message = _safe_provider_error(error_payload)
        rate_markers = ("rate", "limit", "throttle", "too many")
        is_rate_limited = any(
            marker in f"{provider_code or ''} {provider_message or ''}".casefold()
            for marker in rate_markers
        )
        if diagnostic_stage is not None and is_rate_limited:
            raise BrokerRateLimited(
                "Dhan market-data request was rate limited",
                status_code=response.status_code,
            )
        if (
            allow_empty_dhan_holdings
            and path == "/v2/holdings"
            and response.status_code == 500
            and isinstance(error_payload, dict)
            and error_payload.get("errorCode") == "DH-1111"
            and isinstance(error_payload.get("errorMessage"), str)
            and error_payload["errorMessage"].casefold().startswith("no holdings")
        ):
            return []
    try:
        return _body(response)
    except BrokerAdapterError as error:
        if diagnostic_stage is not None and not response.is_error:
            _log_dhan_request_failure(
                diagnostic_stage,
                path,
                status_code=response.status_code,
                error_class=type(error).__name__,
                description="Upstream response could not be parsed.",
            )
        raise


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict):
        status = payload.get("status")
        if payload.get("success") is False or status is False or (
            isinstance(status, str) and status.casefold() in {"failure", "failed", "error"}
        ):
            raise BrokerAdapterError(
                "Broker rejected the request",
                category="provider_response_error",
            )
        if "errorCode" in payload or "error_code" in payload:
            raise BrokerAdapterError(
                "Broker returned an error payload",
                category="provider_response_error",
            )
        if "data" in payload and payload["data"] is None:
            raise BrokerAdapterError(
                "Broker returned an empty data payload",
                category="provider_response_error",
            )
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _list_payload(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise BrokerAdapterError("Broker account data has an unexpected format")
    return value


class BrokerAdapter(ABC):
    slug: str
    display_name: str
    is_synthetic = False

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    def authenticate(self, *, state: str, redirect_uri: str, credentials: Mapping[str, str]) -> str | None: ...

    @abstractmethod
    def exchange_callback(
        self, parameters: Mapping[str, str], credentials: Mapping[str, str]
    ) -> tuple[dict[str, str], datetime]: ...

    @abstractmethod
    def refresh_token(self, tokens: Mapping[str, str]) -> tuple[dict[str, str], datetime]: ...

    @abstractmethod
    def get_profile(self, access_token: str, tokens: Mapping[str, str]) -> Any: ...

    @abstractmethod
    def get_holdings(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_positions(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_order_history(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_funds(self, access_token: str, tokens: Mapping[str, str]) -> Any: ...

    def account_headers(self, access_token: str, tokens: Mapping[str, str]) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    def get(self, path: str, access_token: str, tokens: Mapping[str, str]) -> Any:
        return _unwrap(
            _request(
                "GET",
                f"{self.api_root}{path}",
                headers=self.account_headers(access_token, tokens),
            )
        )

    @property
    def api_root(self) -> str:
        raise NotImplementedError


class MockBrokerAdapter(BrokerAdapter):
    slug = "mock_broker"
    display_name = "Mock Broker (synthetic)"
    is_synthetic = True

    @property
    def enabled(self) -> bool:
        return True

    @property
    def api_root(self) -> str:
        return ""

    def authenticate(self, *, state: str, redirect_uri: str, credentials: Mapping[str, str]) -> str:
        return f"{redirect_uri}?{urlencode({'state': state, 'code': 'mock'})}"

    def exchange_callback(
        self, parameters: Mapping[str, str], credentials: Mapping[str, str]
    ) -> tuple[dict[str, str], datetime]:
        if parameters.get("code") != "mock":
            raise BrokerAdapterError("Mock broker callback is invalid")
        return {"access_token": "synthetic-mock-access-token"}, _now() + timedelta(hours=1)

    def refresh_token(self, tokens: Mapping[str, str]) -> tuple[dict[str, str], datetime]:
        return {"access_token": "synthetic-mock-access-token-refreshed"}, _now() + timedelta(hours=1)

    def get_profile(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return {"provider": self.display_name, "account": "SYNTHETIC-DEMO"}

    def get_holdings(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload([
            {"tradingsymbol": "DEMO-A", "exchange": "MOCK", "quantity": "10", "average_price": "25.00"},
            {"tradingsymbol": "DEMO-B", "exchange": "MOCK", "quantity": "4", "average_price": "50.00"},
        ])

    def get_positions(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload([
            {"tradingsymbol": "DEMO-A", "exchange": "MOCK", "quantity": "10", "average_price": "25.00", "product": "CNC"}
        ])

    def get_order_history(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload([{"order_id": "SYNTHETIC-ORDER-1", "status": "COMPLETE", "symbol": "DEMO-A"}])

    def get_funds(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return {"available_cash": "10000.00", "currency": "INR", "is_synthetic": True}


class ZerodhaAdapter(BrokerAdapter):
    slug = "zerodha"
    display_name = "Zerodha Kite Connect"
    api_root = "https://api.kite.trade"

    @property
    def api_key(self) -> str | None:
        return _secret(settings.zerodha_api_key)

    @property
    def api_secret(self) -> str | None:
        return _secret(settings.zerodha_api_secret)

    @property
    def redirect_url(self) -> str | None:
        return settings.zerodha_redirect_url

    @property
    def enabled(self) -> bool:
        return settings.broker_zerodha_enabled and _required(
            self.api_key, self.api_secret, self.redirect_url
        )

    def authenticate(self, *, state: str, redirect_uri: str, credentials: Mapping[str, str]) -> str:
        if not self.enabled:
            raise BrokerAdapterError("Broker is disabled or credentials are not configured")
        query = urlencode({"v": "3", "api_key": self.api_key})
        return f"https://kite.zerodha.com/connect/login?{query}"

    def exchange_callback(
        self, parameters: Mapping[str, str], credentials: Mapping[str, str]
    ) -> tuple[dict[str, str], datetime]:
        request_token = parameters.get("request_token", "")
        if not self.enabled or not request_token:
            raise BrokerAdapterError("Broker callback is invalid")
        checksum = hashlib.sha256(
            f"{self.api_key}{request_token}{self.api_secret}".encode()
        ).hexdigest()
        payload = _request(
            "POST",
            f"{self.api_root}/session/token",
            data={"api_key": self.api_key, "request_token": request_token, "checksum": checksum},
        )
        token = _unwrap(payload)
        access_token = token.get("access_token") if isinstance(token, dict) else None
        if not access_token:
            raise BrokerAdapterError("Broker did not issue an access token")
        now_india = datetime.now(ZoneInfo("Asia/Kolkata"))
        expiry = (now_india + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        if expiry <= now_india:
            expiry += timedelta(days=1)
        return {"access_token": str(access_token)}, expiry.astimezone(timezone.utc)

    def refresh_token(self, tokens: Mapping[str, str]) -> tuple[dict[str, str], datetime]:
        raise BrokerReconnectRequired("Reconnect to this broker to renew the session")

    def account_headers(self, access_token: str, tokens: Mapping[str, str]) -> dict[str, str]:
        return {"Authorization": f"token {self.api_key}:{access_token}", "Accept": "application/json"}

    def get_profile(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return self.get("/user/profile", access_token, tokens)

    def get_holdings(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload(self.get("/portfolio/holdings", access_token, tokens))

    def get_positions(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        positions = self.get("/portfolio/positions", access_token, tokens)
        return _list_payload(positions.get("net") if isinstance(positions, dict) else positions)

    def get_order_history(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload(self.get("/orders", access_token, tokens))

    def get_funds(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return self.get("/user/margins", access_token, tokens)


class UpstoxAdapter(BrokerAdapter):
    slug = "upstox"
    display_name = "Upstox"
    api_root = "https://api.upstox.com"

    @property
    def client_id(self) -> str | None:
        return _secret(settings.upstox_client_id)

    @property
    def client_secret(self) -> str | None:
        return _secret(settings.upstox_client_secret)

    @property
    def redirect_url(self) -> str | None:
        return settings.upstox_redirect_uri

    @property
    def enabled(self) -> bool:
        return settings.broker_upstox_enabled and _required(
            self.client_id, self.client_secret, self.redirect_url
        )

    def authenticate(self, *, state: str, redirect_uri: str, credentials: Mapping[str, str]) -> str:
        if not self.enabled:
            raise BrokerAdapterError("Broker is disabled or credentials are not configured")
        query = urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_url,
            "state": state,
        })
        return f"{self.api_root}/v2/login/authorization/dialog?{query}"

    def exchange_callback(
        self, parameters: Mapping[str, str], credentials: Mapping[str, str]
    ) -> tuple[dict[str, str], datetime]:
        code = parameters.get("code", "")
        if not self.enabled or not code:
            raise BrokerAdapterError("Broker callback is invalid")
        payload = _request(
            "POST",
            f"{self.api_root}/v2/login/authorization/token",
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_url,
                "grant_type": "authorization_code",
            },
        )
        token = _unwrap(payload)
        access_token = token.get("access_token") if isinstance(token, dict) else None
        if not access_token:
            raise BrokerAdapterError("Broker did not issue an access token")
        return {"access_token": str(access_token)}, _expires_at(token, timedelta(hours=24))

    def refresh_token(self, tokens: Mapping[str, str]) -> tuple[dict[str, str], datetime]:
        raise BrokerReconnectRequired("Reconnect to this broker to renew the session")

    def get_profile(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return self.get("/v2/user/profile", access_token, tokens)

    def get_holdings(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload(self.get("/v2/portfolio/long-term-holdings", access_token, tokens))

    def get_positions(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload(self.get("/v2/portfolio/short-term-positions", access_token, tokens))

    def get_order_history(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return _list_payload(self.get("/v2/order/retrieve-all", access_token, tokens))

    def get_funds(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return self.get("/v2/user/get-funds-and-margin", access_token, tokens)


class AngelOneAdapter(BrokerAdapter):
    slug = "angel_one"
    display_name = "Angel One SmartAPI (pending)"
    api_root = ""

    @property
    def enabled(self) -> bool:
        return False

    def authenticate(self, *, state: str, redirect_uri: str, credentials: Mapping[str, str]) -> str:
        raise BrokerAdapterError(
            "Angel One integration is pending a supported redirect-only authentication flow"
        )

    def exchange_callback(
        self, parameters: Mapping[str, str], credentials: Mapping[str, str]
    ) -> tuple[dict[str, str], datetime]:
        raise BrokerAdapterError("Angel One integration is pending")

    def refresh_token(self, tokens: Mapping[str, str]) -> tuple[dict[str, str], datetime]:
        raise BrokerReconnectRequired("Angel One integration is pending")

    def get_profile(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        raise BrokerAdapterError("Angel One integration is pending")

    def get_holdings(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        raise BrokerAdapterError("Angel One integration is pending")

    def get_positions(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        raise BrokerAdapterError("Angel One integration is pending")

    def get_order_history(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        raise BrokerAdapterError("Angel One integration is pending")

    def get_funds(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        raise BrokerAdapterError("Angel One integration is pending")


class DhanAdapter(BrokerAdapter):
    slug = "dhan"
    name = "dhan"
    is_mock = False
    display_name = "Dhan"
    api_root = "https://api.dhan.co"
    auth_root = "https://auth.dhan.co"
    scrip_master_url = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    _scrip_master_cache: tuple[float, list[dict[str, str]]] | None = None
    _equity_index_cache: tuple[
        float, dict[tuple[str, str], dict[str, str]]
    ] | None = None
    _market_quote_cache: dict[
        tuple[str, tuple[tuple[str, tuple[str, ...]], ...], str],
        tuple[float, dict[tuple[str, str], HistoricalBar]],
    ] = {}
    _market_quote_cache_lock = threading.Lock()
    _market_quote_cache_seconds = 1.0

    @property
    def api_key(self) -> str | None:
        return _secret(settings.dhan_api_key)

    @property
    def api_secret(self) -> str | None:
        return _secret(settings.dhan_api_secret)

    @property
    def redirect_url(self) -> str | None:
        return settings.dhan_redirect_uri

    @property
    def enabled(self) -> bool:
        return settings.broker_dhan_enabled and _required(
            self.api_key, self.api_secret, self.redirect_url
        )

    def authenticate(self, *, state: str, redirect_uri: str, credentials: Mapping[str, str]) -> str:
        client_id = credentials.get("client_id", "")
        if not self.enabled or not client_id:
            raise BrokerAdapterError("Dhan client ID is required and broker credentials must be configured")
        payload = _request(
            "POST",
            f"{self.auth_root}/app/generate-consent?{urlencode({'client_id': client_id})}",
            headers={"app_id": self.api_key or "", "app_secret": self.api_secret or ""},
        )
        data = _unwrap(payload)
        consent_id = data.get("consentAppId") if isinstance(data, dict) else None
        if not consent_id:
            raise BrokerAdapterError("Broker did not create a consent session")
        return f"{self.auth_root}/login/consentApp-login?{urlencode({'consentAppId': consent_id})}"

    def exchange_callback(
        self, parameters: Mapping[str, str], credentials: Mapping[str, str]
    ) -> tuple[dict[str, str], datetime]:
        token_id = parameters.get("tokenId", "")
        client_id = credentials.get("client_id", "")
        if not self.enabled or not token_id or not client_id:
            raise BrokerAdapterError("Broker callback is invalid")
        payload = _request(
            "POST",
            f"{self.auth_root}/app/consumeApp-consent?{urlencode({'tokenId': token_id})}",
            headers={"app_id": self.api_key or "", "app_secret": self.api_secret or ""},
        )
        data = _unwrap(payload)
        access_token = data.get("accessToken") if isinstance(data, dict) else None
        if not access_token:
            raise BrokerAdapterError("Broker did not issue an access token")
        tokens = {
            "access_token": str(access_token),
            "client_id": client_id,
        }
        for key in ("dhanClientName", "dhanClientUcc", "givenPowerOfAttorney"):
            if data.get(key) is not None:
                tokens[key] = str(data[key])
        return tokens, _expires_at(data, timedelta(hours=24))

    def refresh_token(self, tokens: Mapping[str, str]) -> tuple[dict[str, str], datetime]:
        if not self.enabled or not tokens.get("client_id"):
            raise BrokerReconnectRequired("Reconnect to this broker to renew the session")
        payload = _request(
            "POST",
            f"{self.api_root}/v2/RenewToken",
            headers={
                "access-token": tokens["access_token"],
                "dhanClientId": tokens["client_id"],
            },
        )
        data = _unwrap(payload)
        access_token = data.get("accessToken") if isinstance(data, dict) else None
        if not access_token:
            raise BrokerReconnectRequired("Reconnect to this broker to renew the session")
        renewed_tokens = dict(tokens)
        renewed_tokens["access_token"] = str(access_token)
        return renewed_tokens, _expires_at(data, timedelta(hours=24))

    def account_headers(self, access_token: str, tokens: Mapping[str, str]) -> dict[str, str]:
        return {
            "access-token": access_token,
            "client-id": tokens["client_id"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get_dhan_account_data(
        self,
        path: str,
        stage: str,
        access_token: str,
        tokens: Mapping[str, str],
        *,
        allow_empty_holdings: bool = False,
    ) -> Any:
        payload = _request(
            "GET",
            f"{self.api_root}{path}",
            diagnostic_stage=stage,
            allow_empty_dhan_holdings=allow_empty_holdings,
            headers=self.account_headers(access_token, tokens),
        )
        try:
            return _unwrap(payload)
        except BrokerAdapterError as error:
            provider_code, provider_message = _safe_provider_error(payload)
            logger.warning(
                "Dhan sync diagnostic stage=%s endpoint=%s status_code=200 "
                "error_class=%s description=Provider rejected account request "
                "provider_code=%s provider_message=%s",
                stage,
                path,
                type(error).__name__,
                provider_code or "unavailable",
                provider_message or "unavailable",
            )
            raise

    def get_profile(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return {
            "provider": self.display_name,
            "client_id": tokens["client_id"],
            "client_name": tokens.get("dhanClientName"),
            "client_code": tokens.get("dhanClientUcc"),
            "given_power_of_attorney": tokens.get("givenPowerOfAttorney"),
        }

    def get_holdings(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return self._get_dhan_account_data(
            "/v2/holdings",
            "holdings_api_request",
            access_token,
            tokens,
            allow_empty_holdings=True,
        )

    @classmethod
    def _scrip_master(cls) -> list[dict[str, str]]:
        now = time.monotonic()
        if cls._scrip_master_cache and now - cls._scrip_master_cache[0] < 86400:
            return cls._scrip_master_cache[1]
        try:
            with httpx.stream(
                "GET",
                cls.scrip_master_url,
                timeout=20.0,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                reader = csv.DictReader(response.iter_lines())
                required_fields = {
                    "EXCH_ID",
                    "SEGMENT",
                    "SECURITY_ID",
                    "SYMBOL_NAME",
                    "INSTRUMENT",
                    "SERIES",
                }
                if not required_fields <= set(reader.fieldnames or ()):
                    raise BrokerAdapterError(
                        "Dhan instrument master has an unexpected format"
                    )
                master_fields = (
                    "EXCH_ID",
                    "SEGMENT",
                    "SECURITY_ID",
                    "SYMBOL_NAME",
                    "UNDERLYING_SYMBOL",
                    "INSTRUMENT",
                    "SERIES",
                    "ISIN",
                    "LOT_SIZE",
                    "TICK_SIZE",
                    "LISTING_DATE",
                    "LIST_DATE",
                    "LISTINGDATE",
                    "SM_EXPIRY_DATE",
                    "EXPIRY_DATE",
                    "EXPIRY",
                )
                records = [
                    {field: row.get(field, "") or "" for field in master_fields}
                    for row in reader
                    if row.get("EXCH_ID", "").strip()
                    and row.get("SECURITY_ID", "").strip().isdigit()
                ]
        except BrokerAdapterError:
            raise
        except (httpx.HTTPError, UnicodeDecodeError, csv.Error) as error:
            raise BrokerAdapterError("Dhan instrument master is unavailable") from error
        if not records:
            raise BrokerAdapterError("Dhan instrument master has an unexpected format")
        cls._scrip_master_cache = (now, records)
        cls._equity_index_cache = None
        return records

    @classmethod
    def get_instrument_master_details(
        cls,
        provider_instrument_id: str,
        venue: str,
        symbol: str,
    ) -> dict[str, str | None] | None:
        exchange_aliases = {
            "NSE": "NSE",
            "XNSE": "NSE",
            "NSI": "NSE",
            "NSE_EQ": "NSE",
            "BSE": "BSE",
            "XBOM": "BSE",
            "BOM": "BSE",
            "BSE_EQ": "BSE",
        }
        exchange = exchange_aliases.get(venue.upper())
        security_id = provider_instrument_id.split("|", maxsplit=1)[0].strip()
        if exchange is None or not security_id.isdigit():
            return None
        candidates = [
            row
            for row in cls._scrip_master()
            if row.get("EXCH_ID", "").strip().upper() == exchange
            and row.get("SECURITY_ID", "").strip() == security_id
        ]
        normalized_symbol = symbol.strip().upper()
        row = next(
            (
                candidate
                for candidate in candidates
                if normalized_symbol
                in {
                    candidate.get("SYMBOL_NAME", "").strip().upper(),
                    candidate.get("UNDERLYING_SYMBOL", "").strip().upper(),
                }
            ),
            candidates[0] if candidates else None,
        )
        if row is None:
            return None
        return {
            "isin": row.get("ISIN") or None,
            "exchange": row.get("EXCH_ID") or None,
            "segment": row.get("SEGMENT") or None,
            "instrument_type": row.get("INSTRUMENT") or None,
            "lot_size": row.get("LOT_SIZE") or None,
            "tick_size": row.get("TICK_SIZE") or None,
            "listing_date": (
                row.get("LISTING_DATE")
                or row.get("LIST_DATE")
                or row.get("LISTINGDATE")
                or None
            ),
            "expiry": (
                row.get("SM_EXPIRY_DATE")
                or row.get("EXPIRY_DATE")
                or row.get("EXPIRY")
                or None
            ),
        }

    @classmethod
    def _equity_index(cls) -> dict[tuple[str, str], dict[str, str]]:
        rows = cls._scrip_master()
        master_timestamp = cls._scrip_master_cache[0] if cls._scrip_master_cache else 0
        cached = cls._equity_index_cache
        if cached is not None and cached[0] == master_timestamp:
            return cached[1]
        index: dict[tuple[str, str], dict[str, str]] = {}
        ambiguous: set[tuple[str, str]] = set()
        for row in rows:
            exchange = row.get("EXCH_ID", "").upper()
            if (
                exchange not in {"NSE", "BSE"}
                or row.get("SEGMENT", "").upper() != "E"
                or row.get("INSTRUMENT", "").upper() != "EQUITY"
                or row.get("SERIES", "").upper() != "EQ"
            ):
                continue
            for raw_symbol in (
                row.get("UNDERLYING_SYMBOL", ""),
                row.get("SYMBOL_NAME", ""),
            ):
                symbol = raw_symbol.strip().upper().removesuffix("-EQ")
                if symbol and row.get("SECURITY_ID", "").strip().isdigit():
                    key = (exchange, symbol)
                    if key in ambiguous:
                        continue
                    if key in index and index[key]["SECURITY_ID"] != row["SECURITY_ID"]:
                        index.pop(key)
                        ambiguous.add(key)
                    else:
                        index[key] = row
        cls._equity_index_cache = (master_timestamp, index)
        return index

    def find_verified_equity_mapping(
        self, symbol: str, venue: str
    ) -> tuple[str, str] | None:
        exchange = {
            "NSE": "NSE",
            "XNSE": "NSE",
            "NSI": "NSE",
            "NSE_EQ": "NSE",
            "BSE": "BSE",
            "XBOM": "BSE",
            "BOM": "BSE",
            "BSE_EQ": "BSE",
        }.get(venue.upper())
        if exchange is None:
            return None
        row = self._equity_index().get((exchange, symbol.strip().upper()))
        if (
            row is None
            or row.get("SEGMENT", "").upper() != "E"
            or row.get("INSTRUMENT", "").upper() != "EQUITY"
            or row.get("SERIES", "").upper() != "EQ"
        ):
            return None
        segment = "NSE_EQ" if exchange == "NSE" else "BSE_EQ"
        return row["SECURITY_ID"].strip(), segment

    @classmethod
    def find_verified_equity_mapping_by_identity(
        cls,
        symbol: str,
        isin: str | None,
        security_id: str | None = None,
    ) -> tuple[str, str] | None:
        normalized_symbol = symbol.strip().upper().removesuffix("-EQ")
        normalized_isin = isin.strip().upper() if isin else ""
        if not normalized_symbol or not normalized_isin:
            return None
        candidates = [
            row
            for row in cls._scrip_master()
            if row.get("EXCH_ID", "").strip().upper() in {"NSE", "BSE"}
            and row.get("SEGMENT", "").strip().upper() == "E"
            and row.get("INSTRUMENT", "").strip().upper() == "EQUITY"
            and row.get("SERIES", "").strip().upper() == "EQ"
            and row.get("ISIN", "").strip().upper() == normalized_isin
            and normalized_symbol in {
                row.get("SYMBOL_NAME", "").strip().upper().removesuffix("-EQ"),
                row.get("UNDERLYING_SYMBOL", "").strip().upper().removesuffix("-EQ"),
            }
            and row.get("SECURITY_ID", "").strip().isdigit()
        ]
        if security_id and security_id.isdigit():
            candidates = [
                row for row in candidates
                if row.get("SECURITY_ID", "").strip() == security_id
            ]
        resolved = {
            (
                row["SECURITY_ID"].strip(),
                "NSE_EQ" if row["EXCH_ID"].strip().upper() == "NSE" else "BSE_EQ",
            )
            for row in candidates
        }
        return next(iter(resolved)) if len(resolved) == 1 else None

    @staticmethod
    def _check_market_data_payload(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise BrokerAdapterError(
                "Dhan market-data response has an unexpected format",
                category="invalid_provider_response",
            )
        status = payload.get("status")
        if (
            payload.get("success") is False
            or status is False
            or (
                isinstance(status, str)
                and status.casefold() in {"failure", "failed", "error"}
            )
            or payload.get("errorCode")
            or payload.get("error_code")
            or ("data" in payload and payload.get("data") is None)
        ):
            raise BrokerAdapterError(
                "Dhan returned an error response",
                category="provider_response_error",
            )

    def get_company_info(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        security_id: str,
        exchange_segment: str,
    ) -> dict[str, Any]:
        if (
            not security_id.isdigit()
            or exchange_segment not in {"NSE_EQ", "BSE_EQ"}
        ):
            raise BrokerAdapterError("Dhan company info requires a verified equity security")
        _wait_for_dhan_market_data()
        payload = _request(
            "POST",
            f"{self.api_root}/v2/data/companyinfo",
            diagnostic_stage="company_info_api_request",
            headers=self.account_headers(access_token, tokens),
            json={
                "securityId": security_id,
                "exchangeSegment": exchange_segment,
                "instrument": "EQUITY",
                "metrics": ["CO", "RATIOS"],
            },
        )
        self._check_market_data_payload(payload)
        if (
            payload.get("status") not in {None, "success"}
            or not isinstance(payload.get("data"), dict)
        ):
            raise BrokerAdapterError(
                "Dhan company info response has an unexpected format",
                category="invalid_provider_response",
            )
        return payload["data"]

    def get_market_quote(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        exchange_segment: str,
        security_id: str,
    ) -> HistoricalBar:
        quotes = self.get_market_quotes(
            access_token, tokens, exchange_segment, [security_id]
        )
        quote = quotes.get(str(security_id))
        if quote is None:
            raise BrokerAdapterError(
                "Dhan market quote response did not include the requested instrument",
                category="invalid_provider_response",
            )
        return quote

    def get_market_quotes(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        exchange_segment: str,
        security_ids: list[str],
    ) -> dict[str, HistoricalBar]:
        quotes = self.get_market_quotes_batch(
            access_token,
            tokens,
            {exchange_segment: security_ids},
        )
        return quotes.get(exchange_segment, {})

    def get_market_quotes_batch(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        instruments_by_segment: Mapping[str, list[str]],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, HistoricalBar]]:
        supported_segments = {
            "NSE_EQ",
            "NSE_FNO",
            "NSE_CURRENCY",
            "BSE_EQ",
            "BSE_FNO",
            "BSE_CURRENCY",
            "MCX_COMM",
        }
        normalized = tuple(
            (segment, tuple(sorted(set(str(value) for value in security_ids))))
            for segment, security_ids in sorted(instruments_by_segment.items())
            if security_ids
        )
        if not normalized:
            return {}
        if any(
            segment not in supported_segments
            or len(ids) > 1000
            or any(not security_id.isdigit() for security_id in ids)
            for segment, ids in normalized
        ):
            raise BrokerAdapterError("Dhan market quote batch is invalid")
        token_fingerprint = hashlib.sha256(access_token.encode()).hexdigest()
        cache_key = (tokens["client_id"], normalized, token_fingerprint)
        with self._market_quote_cache_lock:
            cached = self._market_quote_cache.get(cache_key)
            if (
                not force_refresh
                and
                cached is not None
                and time.monotonic() - cached[0] <= self._market_quote_cache_seconds
            ):
                return {
                    segment: {
                        security_id: bar
                        for (cached_segment, security_id), bar in cached[1].items()
                        if cached_segment == segment
                    }
                    for segment, _ids in normalized
                }

            _wait_for_dhan_market_data()
            payload = _request(
                "POST",
                f"{self.api_root}/v2/marketfeed/quote",
                diagnostic_stage="market_quote_api_request",
                headers={
                    "access-token": access_token,
                    "client-id": tokens["client_id"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    segment: [int(security_id) for security_id in ids]
                    for segment, ids in normalized
                },
            )
            self._check_market_data_payload(payload)
            if payload.get("status") != "success":
                raise BrokerAdapterError(
                    "Dhan market quote response was unsuccessful",
                    category="invalid_provider_response",
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise BrokerAdapterError(
                    "Dhan market quote response has an unexpected format"
                )
            quotes: dict[tuple[str, str], HistoricalBar] = {}
            for exchange_segment, ids in normalized:
                segment_data = data.get(exchange_segment)
                if not isinstance(segment_data, dict):
                    continue
                for security_id in ids:
                    record = segment_data.get(security_id)
                    if record is None:
                        record = segment_data.get(int(security_id))
                    ohlc = record.get("ohlc") if isinstance(record, dict) else None
                    if not isinstance(record, dict) or not isinstance(ohlc, dict):
                        continue
                    tick = DhanAdapter.price_tick_for(exchange_segment, security_id)
                    if tick is None:
                        logger.warning(
                            "dhan_quote_rejected reason=no_resolvable_tick segment=%s",
                            exchange_segment,
                        )
                        continue
                    try:
                        raw_timestamp = record["last_trade_time"]
                        timestamp = datetime.strptime(
                            str(raw_timestamp), "%d/%m/%Y %H:%M:%S"
                        ).replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
                        quotes[(exchange_segment, security_id)] = HistoricalBar(
                            timestamp=timestamp,
                            open=_quantize_price_text(ohlc["open"], tick),
                            high=_quantize_price_text(ohlc["high"], tick),
                            low=_quantize_price_text(ohlc["low"], tick),
                            close=_quantize_price_text(record["last_price"], tick),
                            volume=Decimal(str(record["volume"])),
                            session_close=_quantize_price_text(ohlc["close"], tick),
                            provider_observed_at=datetime.now(timezone.utc),
                        )
                    except (KeyError, ValueError, TypeError, InvalidOperation) as error:
                        raise BrokerAdapterError(
                            "Dhan market quote response is missing valid price or trade-time data"
                        ) from error
            now = time.monotonic()
            self._market_quote_cache = {
                key: value
                for key, value in self._market_quote_cache.items()
                if now - value[0] <= self._market_quote_cache_seconds
            }
            self._market_quote_cache[cache_key] = (now, quotes)
            return {
                segment: {
                    security_id: quotes[(segment, security_id)]
                    for security_id in ids
                    if (segment, security_id) in quotes
                }
                for segment, ids in normalized
            }

    def get_historical_bars(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        exchange_segment: str,
        security_id: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[HistoricalBar]:
        if interval not in {"1d", "1m"}:
            raise BrokerAdapterError("Dhan history sync stores only 1m intraday and 1d candles")
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise BrokerAdapterError("Dhan historical request range is invalid")
        intraday = interval != "1d"
        if intraday and end - start >= timedelta(days=90):
            raise BrokerAdapterError(
                "Dhan intraday history is limited to 90 days per request",
                category="provider_window_exceeded",
            )
        path = "/v2/charts/intraday" if intraday else "/v2/charts/historical"
        start_local = start.astimezone(ZoneInfo("Asia/Kolkata"))
        end_local = end.astimezone(ZoneInfo("Asia/Kolkata"))
        body: dict[str, Any] = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": "EQUITY",
            "oi": False,
            "fromDate": (
                start_local.strftime("%Y-%m-%d %H:%M:%S")
                if intraday else start_local.strftime("%Y-%m-%d")
            ),
            # Dhan daily toDate is non-inclusive, so the end date is shifted one day
            # forward to include the final requested session.
            "toDate": (
                end_local.strftime("%Y-%m-%d %H:%M:%S")
                if intraday else (end_local.date() + timedelta(days=1)).strftime("%Y-%m-%d")
            ),
        }
        if intraday:
            body["interval"] = {
                "1m": "1",
                "5m": "5",
                "15m": "15",
                "1h": "60",
                "60m": "60",
            }[interval]
        _wait_for_dhan_market_data()
        payload = _request(
            "POST",
            f"{self.api_root}{path}",
            diagnostic_stage="historical_data_api_request",
            headers={
                "access-token": access_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
        )
        self._check_market_data_payload(payload)
        arrays = {
            key: payload.get(key)
            for key in ("open", "high", "low", "close", "volume", "timestamp")
        }
        if any(not isinstance(values, list) for values in arrays.values()):
            raise BrokerAdapterError("Dhan historical response has an unexpected format")
        lengths = {len(values) for values in arrays.values() if isinstance(values, list)}
        if len(lengths) != 1:
            raise BrokerAdapterError("Dhan historical response arrays do not align")
        bars: list[HistoricalBar] = []
        now_utc = datetime.now(timezone.utc)
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        try:
            for index in range(next(iter(lengths), 0)):
                # Dhan candle epochs are true UTC instants (e.g. 00:00 IST session
                # start is 18:30 UTC of the prior day).
                timestamp = datetime.fromtimestamp(float(arrays["timestamp"][index]), timezone.utc)
                bar = HistoricalBar(
                    timestamp=timestamp,
                    open=Decimal(str(arrays["open"][index])),
                    high=Decimal(str(arrays["high"][index])),
                    low=Decimal(str(arrays["low"][index])),
                    close=Decimal(str(arrays["close"][index])),
                    volume=Decimal(str(arrays["volume"][index])),
                )
                if not (
                    bar.low <= min(bar.open, bar.close)
                    and bar.high >= max(bar.open, bar.close)
                    and bar.low <= bar.high
                ):
                    raise BrokerAdapterError("Dhan historical response contains invalid OHLC values")
                if not (start_utc <= bar.timestamp < end_utc):
                    continue
                # Candles still forming (current session or interval) are excluded.
                if not _dhan_candle_is_complete(bar.timestamp, interval, now_utc):
                    continue
                bars.append(bar)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, InvalidOperation) as error:
            raise BrokerAdapterError("Dhan historical response contains invalid candles") from error
        return sorted({bar.timestamp: bar for bar in bars}.values(), key=lambda bar: bar.timestamp)

    @staticmethod
    def price_tick_for(exchange_segment: str, security_id: str) -> Decimal | None:
        """Tick size from the Dhan scrip master for an NSE/BSE equity, or None (fail closed).

        Only NSE_EQ and BSE_EQ are resolved. Other segments return None, so their price
        packets are rejected rather than kept unquantised.
        """
        key = (exchange_segment, security_id)
        if key in _DHAN_TICK_CACHE:
            return _DHAN_TICK_CACHE[key]
        exchange = _DHAN_TICK_EXCHANGES.get(exchange_segment)
        tick: Decimal | None = None
        reason = "segment_not_resolvable"
        if exchange is not None and security_id.isdigit():
            ticks = {
                (row.get("TICK_SIZE") or "").strip()
                for row in DhanAdapter._scrip_master()
                if (row.get("EXCH_ID") or "").strip().upper() == exchange
                and (row.get("SECURITY_ID") or "").strip() == security_id
                and (row.get("INSTRUMENT") or "").strip().upper() == "EQUITY"
            }
            ticks.discard("")
            if len(ticks) == 1:
                try:
                    parsed = Decimal(next(iter(ticks)))
                except InvalidOperation:
                    parsed = Decimal(0)
                if parsed.is_finite() and parsed > 0:
                    tick = parsed
                    reason = ""
                else:
                    reason = "tick_size_invalid"
            else:
                reason = "tick_size_missing_or_ambiguous"
        if tick is None:
            logger.warning(
                "dhan_price_tick_unresolved reason=%s segment=%s security_id=%s",
                reason,
                exchange_segment,
                security_id,
            )
        _DHAN_TICK_CACHE[key] = tick
        return tick

    @staticmethod
    def decode_market_feed_packet(
        packet: bytes,
        exchange_segment: str,
        security_id: str,
        *,
        price_tick: Decimal | None = None,
    ) -> HistoricalBar | None:
        segment_codes = {"NSE_EQ": 1, "NSE_FNO": 2, "NSE_CURRENCY": 3, "BSE_EQ": 4,
                         "MCX_COMM": 5, "BSE_CURRENCY": 7, "BSE_FNO": 8}
        if len(packet) < 8:
            raise BrokerAdapterError("Dhan live feed packet has an invalid header")
        packet_length = int.from_bytes(packet[1:3], "little")
        if packet_length != len(packet):
            raise BrokerAdapterError("Dhan live feed packet has an invalid header")
        if packet[0] == 50:
            raise BrokerFeedDisconnected("Dhan feed requested a reconnect")
        if packet[0] in {1, 5, 6, 7} or packet[0] not in {2, 4}:
            return None
        if exchange_segment not in segment_codes:
            raise BrokerAdapterError("Dhan live feed packet has an invalid header")
        packet_security_id = int.from_bytes(packet[4:8], "little", signed=True)
        if (
            packet[3] != segment_codes[exchange_segment]
            or packet_security_id != int(security_id)
        ):
            raise BrokerAdapterError("Dhan live feed packet does not match its subscription")
        if packet[0] == 2:
            if packet_length != 16:
                raise BrokerAdapterError("Dhan live feed ticker packet has an invalid length")
            if price_tick is None:
                raise BrokerAdapterError("Dhan live feed price has no resolvable tick size")
            try:
                price = _float32_wire_to_price(
                    struct.unpack_from("<f", packet, 8)[0], price_tick
                )
                trade_timestamp = struct.unpack_from("<i", packet, 12)[0]
                return HistoricalBar(
                    timestamp=_dhan_ist_epoch_to_utc(trade_timestamp),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=Decimal(0),
                )
            except (OverflowError, OSError, ValueError, TypeError, InvalidOperation) as error:
                raise BrokerAdapterError("Dhan live feed ticker packet contains invalid values") from error
        if packet[0] != 4:
            return None
        if packet_length != 50:
            raise BrokerAdapterError("Dhan live feed quote packet has an invalid length")
        if price_tick is None:
            raise BrokerAdapterError("Dhan live feed price has no resolvable tick size")
        try:
            last_price = struct.unpack_from("<f", packet, 8)[0]
            trade_timestamp = struct.unpack_from("<i", packet, 14)[0]
            volume = struct.unpack_from("<i", packet, 22)[0]
            opening = struct.unpack_from("<f", packet, 34)[0]
            high = struct.unpack_from("<f", packet, 42)[0]
            low = struct.unpack_from("<f", packet, 46)[0]
            return HistoricalBar(
                timestamp=_dhan_ist_epoch_to_utc(trade_timestamp),
                open=_float32_wire_to_price(opening, price_tick),
                high=_float32_wire_to_price(high, price_tick),
                low=_float32_wire_to_price(low, price_tick),
                close=_float32_wire_to_price(last_price, price_tick),
                volume=Decimal(volume),
            )
        except (OverflowError, OSError, ValueError, TypeError, InvalidOperation) as error:
            raise BrokerAdapterError("Dhan live feed quote packet contains invalid values") from error

    @staticmethod
    def decode_market_feed_frame(
        frame: bytes,
        subscriptions: Mapping[tuple[str, str], str],
    ) -> tuple[
        list[tuple[str, HistoricalBar]],
        list[tuple[str, int, Decimal | bytes]],
        int,
    ]:
        segment_codes = {
            1: "NSE_EQ",
            2: "NSE_FNO",
            3: "NSE_CURRENCY",
            4: "BSE_EQ",
            5: "MCX_COMM",
            7: "BSE_CURRENCY",
            8: "BSE_FNO",
        }
        decoded: list[tuple[str, HistoricalBar]] = []
        retained_packets: list[tuple[str, int, Decimal | bytes]] = []
        unknown_response_count = 0
        if not frame:
            raise BrokerAdapterError("Dhan live feed packet has an invalid header")
        offset = 0
        while offset < len(frame):
            remaining = len(frame) - offset
            if remaining < 8:
                raise BrokerAdapterError("Dhan live feed packet has an invalid header")
            packet_length = int.from_bytes(frame[offset + 1 : offset + 3], "little")
            if packet_length < 8 or packet_length > remaining:
                raise BrokerAdapterError("Dhan live feed packet has an invalid header")
            packet = frame[offset : offset + packet_length]
            response_code = packet[0]
            if response_code == 50:
                raise BrokerFeedDisconnected("Dhan feed requested a reconnect")
            if response_code in {1, 5}:
                offset += packet_length
                continue
            if response_code not in {2, 4, 6, 7}:
                unknown_response_count += 1
                offset += packet_length
                continue
            segment = segment_codes.get(packet[3])
            if segment is not None:
                security_id = str(
                    int.from_bytes(packet[4:8], "little", signed=True)
                )
                instrument_id = subscriptions.get((segment, security_id))
                if instrument_id is not None:
                    if response_code == 6:
                        if packet_length != 16:
                            raise BrokerAdapterError(
                                "Dhan previous-close packet has an invalid length"
                            )
                        tick = DhanAdapter.price_tick_for(segment, security_id)
                        if tick is None:
                            raise BrokerAdapterError(
                                "Dhan live feed price has no resolvable tick size"
                            )
                        try:
                            previous_close = _float32_wire_to_price(
                                struct.unpack_from("<f", packet, 8)[0], tick
                            )
                        except (OverflowError, OSError, ValueError, TypeError) as error:
                            raise BrokerAdapterError(
                                "Dhan previous-close packet contains invalid values"
                            ) from error
                        if not previous_close.is_finite() or previous_close <= 0:
                            raise BrokerAdapterError(
                                "Dhan previous-close packet contains invalid values"
                            )
                        retained_packets.append(
                            (instrument_id, response_code, previous_close)
                        )
                    elif response_code == 7:
                        retained_packets.append(
                            (instrument_id, response_code, packet)
                        )
                    else:
                        tick = DhanAdapter.price_tick_for(segment, security_id)
                        if tick is None:
                            raise BrokerAdapterError(
                                "Dhan live feed price has no resolvable tick size"
                            )
                        quote = DhanAdapter.decode_market_feed_packet(
                            packet, segment, security_id, price_tick=tick
                        )
                        if quote is not None:
                            decoded.append((instrument_id, quote))
            offset += packet_length
        return decoded, retained_packets, unknown_response_count

    @staticmethod
    def decode_market_depth_packet(
        packet: bytes,
        exchange_segment: str,
        security_id: str,
    ) -> dict[str, Any] | None:
        segment_codes = {"NSE_EQ": 1, "NSE_FNO": 2, "NSE_CURRENCY": 3, "BSE_EQ": 4,
                         "MCX_COMM": 5, "BSE_CURRENCY": 7, "BSE_FNO": 8}
        if len(packet) < 12:
            raise BrokerAdapterError("Dhan live feed packet has an invalid header")
        if int.from_bytes(packet[0:2], "little") != len(packet):
            raise BrokerAdapterError("Dhan live feed packet has an invalid header")
        if packet[2] == 50:
            raise BrokerFeedDisconnected("Dhan feed requested a reconnect")
        if packet[2] not in {41, 51}:
            return None
        if (
            len(packet) != 332
            or exchange_segment not in segment_codes
            or packet[3] != segment_codes[exchange_segment]
            or int.from_bytes(packet[4:8], "little", signed=True) != int(security_id)
        ):
            raise BrokerAdapterError("Dhan live feed depth packet does not match its subscription")
        try:
            levels = []
            for level in range(20):
                offset = 12 + level * 16
                levels.append({
                    "price": Decimal(str(struct.unpack_from("<d", packet, offset)[0])),
                    "quantity": struct.unpack_from("<I", packet, offset + 8)[0],
                    "orders": struct.unpack_from("<I", packet, offset + 12)[0],
                })
            return {
                "side": "bids" if packet[2] == 41 else "asks",
                "levels": levels,
            }
        except (OverflowError, OSError, ValueError, TypeError, InvalidOperation) as error:
            raise BrokerAdapterError("Dhan live feed depth packet contains invalid values") from error

    async def stream_watchlist_market_depth(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        initial_subscriptions: Mapping[str, tuple[str, str]],
        subscription_updates: asyncio.Queue[Mapping[str, tuple[str, str]]],
        status_callback: Callable[[str], Awaitable[None]],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        segment_codes = {
            1: "NSE_EQ",
            2: "NSE_FNO",
            3: "NSE_CURRENCY",
            4: "BSE_EQ",
            5: "MCX_COMM",
            7: "BSE_CURRENCY",
            8: "BSE_FNO",
        }

        def subscription_items(
            subscriptions: Mapping[str, tuple[str, str]],
        ) -> dict[tuple[str, str], str]:
            return {
                (segment, str(security_id).split("|", 1)[0].strip()): instrument_id
                for instrument_id, (segment, security_id) in subscriptions.items()
                if str(security_id).split("|", 1)[0].strip().isdigit()
            }

        def request_message(request_code: int, items: list[tuple[str, str]]) -> str:
            return json.dumps({
                "RequestCode": request_code,
                "InstrumentCount": len(items),
                "InstrumentList": [
                    {"ExchangeSegment": segment, "SecurityId": security_id}
                    for segment, security_id in items
                ],
            })

        safe_url = (
            "wss://depth-api-feed.dhan.co/twentydepth?token=[REDACTED]"
            "&clientId=[REDACTED]&authType=2"
        )
        url = _dhan_depth_feed_url(access_token, tokens["client_id"])
        current = subscription_items(initial_subscriptions)
        connection = None
        logger.info("Dhan depth feed connect attempt url=%s", safe_url)
        try:
            async with _single_dhan_live_connection(
                url, open_timeout=10, ping_interval=10, ping_timeout=40, close_timeout=5
            ) as connection:
                response = connection.response
                logger.info(
                    "Dhan depth feed handshake result=accepted status=%s reason=%s",
                    response.status_code,
                    response.reason_phrase,
                )
                items = list(current)
                for start in range(0, len(items), 100):
                    batch = items[start:start + 100]
                    await connection.send(request_message(23, batch))
                logger.info("Dhan depth feed subscribed subscriptions=%d", len(current))
                await status_callback("subscribed")
                snapshots: dict[str, dict[str, Any]] = {}
                receive_task = asyncio.create_task(connection.recv())
                update_task = asyncio.create_task(subscription_updates.get())
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            (receive_task, update_task),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if update_task in done:
                            desired = subscription_items(update_task.result())
                            removed = list(current.keys() - desired.keys())
                            added = list(desired.keys() - current.keys())
                            previous_ids = set(current.values())
                            current = desired  # record before sending; see ticker feed
                            for request_code, changed in ((24, removed), (23, added)):
                                for start in range(0, len(changed), 100):
                                    await connection.send(
                                        request_message(request_code, changed[start:start + 100])
                                    )
                            for instrument_id in previous_ids - set(desired.values()):
                                snapshots.pop(instrument_id, None)
                            logger.info(
                                "Dhan depth feed subscriptions updated subscribed=%d "
                                "unsubscribed=%d total=%d",
                                len(added),
                                len(removed),
                                len(current),
                            )
                            if not current:
                                return
                            update_task = asyncio.create_task(subscription_updates.get())
                        if receive_task in done:
                            message = receive_task.result()
                            receive_task = asyncio.create_task(connection.recv())
                            if not isinstance(message, bytes):
                                # Provider text frames (errors/notices) were silently dropped before;
                                # log them (credentials redacted) so a silent feed is diagnosable.
                                logger.warning(
                                    "Dhan depth feed text frame=%s",
                                    _safe_dhan_close_reason(
                                        str(message)[:200], access_token, tokens.get("client_id", "")
                                    ),
                                )
                                continue
                            offset = 0
                            while offset < len(message):
                                if offset + 2 > len(message):
                                    raise BrokerAdapterError(
                                        "Dhan live feed depth packet has an invalid length"
                                    )
                                packet_length = int.from_bytes(
                                    message[offset:offset + 2], "little"
                                )
                                packet_end = offset + packet_length
                                if packet_length < 12 or packet_end > len(message):
                                    raise BrokerAdapterError(
                                        "Dhan live feed depth packet has an invalid length"
                                    )
                                packet = message[offset:packet_end]
                                offset = packet_end
                                if packet[2] == 50:
                                    raise BrokerFeedDisconnected(
                                        "Dhan depth feed requested a reconnect"
                                    )
                                segment = segment_codes.get(packet[3])
                                security_id = str(
                                    int.from_bytes(packet[4:8], "little", signed=True)
                                )
                                instrument_id = current.get((segment or "", security_id))
                                if instrument_id is None:
                                    continue
                                try:
                                    update = await asyncio.to_thread(
                                        self.decode_market_depth_packet,
                                        packet,
                                        segment or "",
                                        security_id,
                                    )
                                except BrokerAdapterError as error:
                                    logger.warning(
                                        "Dhan depth feed parse error=%s", str(error)
                                    )
                                    continue
                                if update is None:
                                    continue
                                snapshot = snapshots.setdefault(instrument_id, {})
                                snapshot[update["side"]] = update["levels"]
                                if "bids" in snapshot and "asks" in snapshot:
                                    yield instrument_id, {
                                        "timestamp": datetime.now(timezone.utc),
                                        "bids": snapshot["bids"],
                                        "asks": snapshot["asks"],
                                    }
                finally:
                    receive_task.cancel()
                    update_task.cancel()
                    await asyncio.gather(
                        receive_task, update_task, return_exceptions=True
                    )
        except InvalidStatus as error:
            logger.error(
                "Dhan depth feed handshake result=rejected status=%s reason=%s",
                error.response.status_code,
                error.response.reason_phrase,
            )
            raise BrokerAdapterError(
                "Dhan depth feed handshake rejected "
                f"HTTP {error.response.status_code}: {error.response.reason_phrase}",
                status_code=error.response.status_code,
            ) from error
        except (OSError, TimeoutError, WebSocketException) as error:
            close_code = connection.close_code if connection is not None else None
            close_reason = _safe_dhan_close_reason(
                connection.close_reason if connection is not None else str(error),
                access_token,
                tokens.get("client_id", ""),
            )
            logger.warning(
                "Dhan depth feed disconnected code=%s reason=%s error_class=%s",
                close_code,
                close_reason,
                type(error).__name__,
            )
            raise BrokerAdapterError(
                f"Dhan depth feed closed code={close_code or 'unavailable'} "
                f"reason={close_reason or type(error).__name__}",
                status_code=close_code,
            ) from error
        finally:
            if connection is not None:
                logger.info(
                    "Dhan depth feed closed code=%s reason=%s",
                    connection.close_code,
                    _safe_dhan_close_reason(
                        connection.close_reason, access_token, tokens.get("client_id", "")
                    ),
                )

    async def stream_market_depth(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        exchange_segment: str,
        security_id: str,
        status_callback: Callable[[str], Awaitable[None]],
    ) -> AsyncIterator[dict[str, Any]]:
        url = _dhan_depth_feed_url(access_token, tokens["client_id"])
        safe_url = (
            "wss://depth-api-feed.dhan.co/twentydepth?token=[REDACTED]"
            "&clientId=[REDACTED]&authType=2"
        )
        logger.info("Dhan depth feed connect attempt url=%s", safe_url)
        connection = None
        try:
            async with _single_dhan_live_connection(
                url, open_timeout=10, ping_interval=10, ping_timeout=40, close_timeout=5
            ) as connection:
                response = connection.response
                logger.info(
                    "Dhan depth feed handshake result=accepted status=%s reason=%s",
                    response.status_code,
                    response.reason_phrase,
                )
                await connection.send(json.dumps({
                    "RequestCode": 23,
                    "InstrumentCount": 1,
                    "InstrumentList": [{
                        "ExchangeSegment": exchange_segment,
                        "SecurityId": security_id,
                    }],
                }))
                await status_callback("active")
                latest: dict[str, list[dict[str, Any]]] = {}
                try:
                    async for message in connection:
                        if not isinstance(message, bytes):
                            continue
                        offset = 0
                        try:
                            while offset < len(message):
                                if offset + 2 > len(message):
                                    raise BrokerAdapterError(
                                        "Dhan live feed depth packet has an invalid length"
                                    )
                                packet_length = int.from_bytes(
                                    message[offset:offset + 2], "little"
                                )
                                packet_end = offset + packet_length
                                if packet_length < 12 or packet_end > len(message):
                                    raise BrokerAdapterError(
                                        "Dhan live feed depth packet has an invalid length"
                                    )
                                update = await asyncio.to_thread(
                                    self.decode_market_depth_packet,
                                    message[offset:packet_end],
                                    exchange_segment,
                                    security_id,
                                )
                                offset = packet_end
                                if update is not None:
                                    latest[update["side"]] = update["levels"]
                                    if "bids" in latest and "asks" in latest:
                                        yield {
                                            "timestamp": datetime.now(timezone.utc),
                                            "bids": latest["bids"],
                                            "asks": latest["asks"],
                                        }
                        except BrokerFeedDisconnected:
                            raise
                        except BrokerAdapterError as error:
                            logger.warning("Dhan depth feed parse error=%s", str(error))
                finally:
                    if connection.close_code is None:
                        try:
                            await connection.send(json.dumps({
                                "RequestCode": 24,
                                "InstrumentCount": 1,
                                "InstrumentList": [{
                                    "ExchangeSegment": exchange_segment,
                                    "SecurityId": security_id,
                                }],
                            }))
                            logger.info("Dhan depth feed unsubscribed instrument=%s", security_id)
                        except WebSocketException as error:
                            logger.info(
                                "Dhan depth feed unsubscribe failed error_class=%s",
                                type(error).__name__,
                            )
        except InvalidStatus as error:
            logger.error(
                "Dhan depth feed handshake result=rejected status=%s reason=%s",
                error.response.status_code,
                error.response.reason_phrase,
            )
            raise
        except (OSError, TimeoutError, WebSocketException) as error:
            logger.warning(
                "Dhan depth feed connection failed error_class=%s reason=%s",
                type(error).__name__,
                _safe_dhan_close_reason(
                    str(error), access_token, tokens.get("client_id", "")
                ),
            )
            raise
        finally:
            if connection is not None:
                logger.info(
                    "Dhan depth feed closed code=%s reason=%s",
                    connection.close_code,
                    _safe_dhan_close_reason(
                        connection.close_reason, access_token, tokens.get("client_id", "")
                    ),
                )

    async def stream_market_quotes(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        exchange_segment: str,
        security_id: str,
        status_callback: Callable[[str], Awaitable[None]],
    ) -> AsyncIterator[HistoricalBar]:
        url = _dhan_feed_url(access_token, tokens["client_id"])
        safe_url = (
            "wss://api-feed.dhan.co?version=2&token=[REDACTED]"
            "&clientId=[REDACTED]&authType=2"
        )
        subscription = json.dumps({
            "RequestCode": 17,
            "InstrumentCount": 1,
            "InstrumentList": [
                {"ExchangeSegment": exchange_segment, "SecurityId": security_id}
            ],
        })
        last_failure = "Dhan live feed disconnected after retrying."
        last_error: str | None = None
        repeated_error_count = 0

        def record_error(reason: str) -> None:
            nonlocal last_error, repeated_error_count
            if reason == last_error:
                repeated_error_count += 1
                return
            if last_error is not None and repeated_error_count > 1:
                logger.warning(
                    "Dhan live feed repeated failure count=%d reason=%s",
                    repeated_error_count,
                    last_error,
                )
            last_error = reason
            repeated_error_count = 1
            logger.warning("Dhan live feed failure reason=%s", reason)

        def flush_repeated_error() -> None:
            nonlocal last_error, repeated_error_count
            if last_error is not None and repeated_error_count > 1:
                logger.warning(
                    "Dhan live feed repeated failure count=%d reason=%s",
                    repeated_error_count,
                    last_error,
                )
            last_error = None
            repeated_error_count = 0

        for attempt in range(_DHAN_MAX_CONSECUTIVE_FAILURES):
            if attempt:
                await status_callback("reconnecting")
                await asyncio.sleep(
                    min(2 ** (attempt - 1), _DHAN_MAX_RETRY_DELAY_SECONDS)
                )
            logger.info(
                "Dhan live feed connect attempt=%d url=%s", attempt + 1, safe_url
            )
            connection = None
            try:
                logger.info("dhan_upstream_connecting feed=ticker")
                async with _single_dhan_live_connection(
                    url, open_timeout=10, ping_interval=10, ping_timeout=40, close_timeout=5
                ) as connection:
                    response = connection.response
                    logger.info(
                        "dhan_upstream_open feed=ticker http_status=%s",
                        response.status_code,
                    )
                    logger.info(
                        "Dhan live feed handshake result=accepted status=%s reason=%s",
                        response.status_code,
                        response.reason_phrase,
                    )
                    await connection.send(subscription)
                    await status_callback("active")
                    async for message in connection:
                        if not isinstance(message, bytes):
                            continue
                        try:
                            quotes, retained_packets, unknown_response_count = (
                                await asyncio.to_thread(
                                    self.decode_market_feed_frame,
                                    message,
                                    {(exchange_segment, security_id): ""},
                                )
                            )
                        except BrokerAdapterError as error:
                            _log_dhan_bad_frame_once(message)
                            raise error
                        flush_repeated_error()
                        for _instrument_id, response_code, _value in retained_packets:
                            if response_code == 7:
                                await status_callback("market_status_received")
                        for _instrument_id, quote in quotes:
                            yield quote
            except InvalidStatus as error:
                status_code = error.response.status_code
                logger.error(
                    "Dhan live feed handshake result=rejected status=%s reason=%s",
                    status_code,
                    error.response.reason_phrase,
                )
                last_failure = (
                    f"Dhan live feed handshake rejected (HTTP {status_code})."
                )
                if status_code == 401:
                    await status_callback("failed")
                    raise BrokerReconnectRequired(
                        "Reconnect to this broker to renew the session"
                    ) from None
                record_error(last_failure)
                if attempt == _DHAN_MAX_CONSECUTIVE_FAILURES - 1:
                    break
            except BrokerAdapterError as error:
                if error.status_code is not None:
                    await status_callback("failed")
                    raise
                last_failure = str(error)
                record_error(last_failure)
                if attempt == _DHAN_MAX_CONSECUTIVE_FAILURES - 1:
                    break
            except (OSError, TimeoutError, WebSocketException) as error:
                logger.warning(
                    "Dhan live feed connection failed error_class=%s reason=%s",
                    type(error).__name__,
                    _safe_dhan_close_reason(
                        str(error), access_token, tokens.get("client_id", "")
                    ),
                )
                last_failure = (
                    f"Dhan live feed connection failed ({type(error).__name__})."
                )
                record_error(last_failure)
                if attempt == _DHAN_MAX_CONSECUTIVE_FAILURES - 1:
                    break
            finally:
                if connection is not None:
                    logger.info(
                        "Dhan live feed closed code=%s reason=%s",
                        connection.close_code,
                        _safe_dhan_close_reason(
                            connection.close_reason,
                            access_token,
                            tokens.get("client_id", ""),
                        ),
                    )
        flush_repeated_error()
        await status_callback("failed")
        raise BrokerAdapterError(last_failure)

    async def stream_watchlist_market_quotes(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        initial_subscriptions: Mapping[str, tuple[str, str]],
        subscription_updates: asyncio.Queue[Mapping[str, tuple[str, str]]],
        status_callback: Callable[[str, str | None, float | None, str | None], Awaitable[None]],
    ) -> AsyncIterator[
        tuple[str, HistoricalBar]
        | tuple[str, str, Decimal | bytes]
    ]:
        segment_codes = {
            1: "NSE_EQ",
            2: "NSE_FNO",
            3: "NSE_CURRENCY",
            4: "BSE_EQ",
            5: "MCX_COMM",
            7: "BSE_CURRENCY",
            8: "BSE_FNO",
        }

        def subscription_items(
            subscriptions: Mapping[str, tuple[str, str]],
        ) -> dict[tuple[str, str], str]:
            if len(subscriptions) > 5000:
                raise BrokerAdapterError("Dhan feed subscription exceeds its 5000-instrument limit")
            items: dict[tuple[str, str], str] = {}
            for instrument_id, (exchange_segment, security_id) in subscriptions.items():
                raw_security_id = str(security_id).strip()
                security_parts = raw_security_id.split("|", maxsplit=1)
                normalized_security_id = security_parts[0].strip()
                normalized_segment = (
                    security_parts[1].strip()
                    if len(security_parts) == 2
                    else exchange_segment.strip()
                )
                if not re.fullmatch(r"[0-9]+", normalized_security_id):
                    logger.warning(
                        "Skipping invalid Dhan live-feed mapping "
                        "instrument_id=%s provider_instrument_id=%r",
                        instrument_id,
                        raw_security_id,
                    )
                    continue
                if normalized_segment not in segment_codes.values():
                    logger.warning(
                        "Skipping Dhan live-feed mapping with invalid segment "
                        "instrument_id=%s segment=%r",
                        instrument_id,
                        normalized_segment,
                    )
                    continue
                key = (normalized_segment, normalized_security_id)
                if key not in items:
                    items[key] = instrument_id
            return items

        def request_message(
            request_code: int,
            instruments: list[tuple[str, str]],
        ) -> str:
            return json.dumps(
                {
                    "RequestCode": request_code,
                    "InstrumentCount": len(instruments),
                    "InstrumentList": [
                        {"ExchangeSegment": segment, "SecurityId": security_id}
                        for segment, security_id in instruments
                    ],
                }
            )

        url = _dhan_feed_url(access_token, tokens["client_id"])
        safe_url = (
            "wss://api-feed.dhan.co?version=2&token=[REDACTED]"
            "&clientId=[REDACTED]&authType=2"
        )
        current = subscription_items(initial_subscriptions)
        retry_delay = 1
        reconnects = 0
        last_error: str | None = None
        repeated_error_count = 0

        def record_error(reason: str) -> None:
            nonlocal last_error, repeated_error_count
            if reason == last_error:
                repeated_error_count += 1
                return
            if last_error is not None and repeated_error_count > 1:
                logger.warning(
                    "Dhan live feed repeated failure count=%d reason=%s",
                    repeated_error_count,
                    last_error,
                )
            last_error = reason
            repeated_error_count = 1
            logger.warning("Dhan live feed failure reason=%s", reason)

        def flush_repeated_error() -> None:
            nonlocal last_error, repeated_error_count
            if last_error is not None and repeated_error_count > 1:
                logger.warning(
                    "Dhan live feed repeated failure count=%d reason=%s",
                    repeated_error_count,
                    last_error,
                )
            last_error = None
            repeated_error_count = 0

        while True:
            if not current:
                await status_callback("idle")
                current = subscription_items(await subscription_updates.get())
                if not current:
                    continue
            await status_callback("connecting" if reconnects == 0 else "reconnecting")
            logger.info(
                "Dhan live feed connect attempt=%d url=%s",
                reconnects + 1,
                safe_url,
            )
            connection = None
            failure_reason: str | None = None
            retry_after_seconds: float | None = None
            subscription_acknowledged: set[str] = set()
            acknowledgement_complete = False
            try:
                async with _single_dhan_live_connection(
                    url, open_timeout=10, ping_interval=10, ping_timeout=40, close_timeout=5
                ) as connection:
                    response = connection.response
                    logger.info(
                        "Dhan live feed handshake result=accepted status=%s reason=%s",
                        response.status_code,
                        response.reason_phrase,
                    )
                    for start in range(0, len(current), 100):
                        batch = list(current)[start : start + 100]
                        payload = request_message(17, batch)
                        logger.info(
                            "Dhan live feed subscription payload_json=%s payload_utf8_hex=%s",
                            payload,
                            payload.encode("utf-8").hex(),
                        )
                        await connection.send(payload)
                        logger.info(
                            "dhan_subscribe_sent feed=ticker request_code=17 instruments=%d",
                            len(batch),
                        )
                    logger.info(
                        "Dhan live feed upstream subscription sent subscriptions=%d "
                        "request_code=17",
                        len(current),
                    )
                    await status_callback("connecting")
                    receive_task = asyncio.create_task(connection.recv())
                    update_task = asyncio.create_task(subscription_updates.get())
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                (receive_task, update_task),
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if update_task in done:
                                desired = subscription_items(update_task.result())
                                removed = list(current.keys() - desired.keys())
                                added = list(desired.keys() - current.keys())
                                # Record the desired set before any send: if the socket drops
                                # mid-update the reconnect must restore the new subscriptions
                                # (the queued update has already been consumed).
                                current = desired
                                subscription_acknowledged.intersection_update(current.values())
                                if added:
                                    acknowledgement_complete = False
                                    await status_callback("connecting")
                                for start in range(0, len(removed), 100):
                                    payload = request_message(
                                        18, removed[start : start + 100]
                                    )
                                    logger.info(
                                        "Dhan live feed unsubscription payload_json=%s "
                                        "payload_utf8_hex=%s",
                                        payload,
                                        payload.encode("utf-8").hex(),
                                    )
                                    await connection.send(payload)
                                    logger.info(
                                        "dhan_unsubscribe_sent feed=ticker request_code=18 instruments=%d",
                                        len(removed[start : start + 100]),
                                    )
                                for start in range(0, len(added), 100):
                                    payload = request_message(
                                        17, added[start : start + 100]
                                    )
                                    logger.info(
                                        "Dhan live feed subscription payload_json=%s "
                                        "payload_utf8_hex=%s",
                                        payload,
                                        payload.encode("utf-8").hex(),
                                    )
                                    await connection.send(payload)
                                logger.info(
                                    "Dhan live feed subscriptions updated subscribed=%d unsubscribed=%d total=%d",
                                    len(added),
                                    len(removed),
                                    len(current),
                                )
                                if not current:
                                    await status_callback("idle")
                                elif not added:
                                    await status_callback("active")
                                update_task = asyncio.create_task(subscription_updates.get())
                            if receive_task in done:
                                message = receive_task.result()
                                receive_task = asyncio.create_task(connection.recv())
                                if not isinstance(message, bytes):
                                    continue
                                try:
                                    quotes, retained_packets, unknown_response_count = (
                                        await asyncio.to_thread(
                                            self.decode_market_feed_frame,
                                            message,
                                            current,
                                        )
                                    )
                                except BrokerAdapterError as error:
                                    _log_dhan_bad_frame_once(message)
                                    raise error
                                flush_repeated_error()
                                acknowledged_this_packet = {
                                    instrument_id
                                    for instrument_id, response_code, _value in retained_packets
                                    if response_code == 6
                                }
                                subscription_acknowledged.update(acknowledged_this_packet)
                                expected_acknowledgements = set(current.values())
                                if (
                                    not acknowledgement_complete
                                    and expected_acknowledgements.issubset(subscription_acknowledged)
                                ):
                                    acknowledgement_complete = True
                                    retry_delay = 1
                                    await status_callback("packet_received")
                                elif acknowledgement_complete:
                                    retry_delay = 1
                                    await status_callback("packet_received")
                                for _ in range(unknown_response_count):
                                    await status_callback("unknown_response")
                                for instrument_id, response_code, value in retained_packets:
                                    if response_code == 6:
                                        yield instrument_id, "previous_close", value
                                    elif response_code == 7 and acknowledgement_complete:
                                        yield instrument_id, "market_status", value
                                for instrument_id, quote in quotes:
                                    logger.info(
                                        "dhan_tick_received feed=ticker instrument_id=%s",
                                        instrument_id,
                                    )
                                    yield instrument_id, quote
                    finally:
                        receive_task.cancel()
                        update_task.cancel()
                        await asyncio.gather(
                            receive_task, update_task, return_exceptions=True
                        )
            except BrokerReconnectRequired:
                await status_callback(
                    "connection_failure", "Dhan authentication requires reconnecting."
                )
                raise
            except InvalidStatus as error:
                failure_reason = (
                    "Dhan live feed handshake rejected "
                    f"HTTP {error.response.status_code}: {error.response.reason_phrase}"
                )
                logger.error(
                    "Dhan live feed handshake result=rejected status=%s reason=%s",
                    error.response.status_code,
                    error.response.reason_phrase,
                )
                if error.response.status_code == 401:
                    await status_callback("connection_failure", failure_reason)
                    await status_callback("failed")
                    raise BrokerReconnectRequired(
                        "Reconnect to this broker to renew the session"
                    ) from None
                if error.response.status_code in {403, 4403}:
                    await status_callback("connection_failure", failure_reason)
                    await status_callback("failed")
                    raise BrokerAdapterError(
                        "Dhan live feed handshake rejected "
                        f"HTTP {error.response.status_code}: {error.response.reason_phrase}",
                        status_code=error.response.status_code,
                    ) from error
                if error.response.status_code == 429:
                    retry_after_seconds = _dhan_retry_after_seconds(error)
                record_error(failure_reason)
            except BrokerFeedDisconnected as error:
                failure_reason = str(error)
                record_error(failure_reason)
            except BrokerAdapterError as error:
                if error.status_code is not None:
                    await status_callback("connection_failure", str(error))
                    await status_callback("failed")
                    raise
                failure_reason = str(error)
                record_error(failure_reason)
            except (OSError, TimeoutError, WebSocketException) as error:
                if connection is None or connection.close_code is None or connection.close_code < 4000:
                    failure_reason = (
                        f"{type(error).__name__}: "
                        f"{_safe_dhan_close_reason(str(error), access_token, tokens.get('client_id', ''))}"
                    )
                    record_error(failure_reason)
            finally:
                if connection is not None:
                    close_reason = connection.close_reason or ""
                    for secret in (access_token, tokens.get("client_id", "")):
                        if secret:
                            close_reason = close_reason.replace(secret, "[REDACTED]")
                    logger.info(
                        "Dhan live feed closed code=%s reason=%r",
                        connection.close_code,
                        close_reason,
                    )
            if (
                connection is not None
                and connection.close_code is not None
                and connection.close_code >= 4000
            ):
                close_reason = _safe_dhan_close_reason(
                    connection.close_reason,
                    access_token,
                    tokens.get("client_id", ""),
                )
                failure_reason = (
                    f"Dhan live feed closed code={connection.close_code} "
                    f"reason={close_reason or 'unavailable'}"
                )
                await status_callback("connection_failure", failure_reason)
                await status_callback("failed")
                raise BrokerAdapterError(failure_reason, status_code=connection.close_code)
            if not current:
                continue
            if failure_reason is None:
                failure_reason = "Dhan live feed disconnected without an error"
                record_error(failure_reason)
            await status_callback("connection_failure", failure_reason)
            reconnects += 1
            delay = _dhan_backoff_seconds(retry_delay, retry_after_seconds)
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            logger.info(
                "Dhan live feed reconnect scheduled attempt=%d backoff_seconds=%.2f next_attempt_at=%s",
                reconnects,
                delay,
                retry_at.isoformat(),
            )
            await status_callback(
                "reconnecting", failure_reason, delay, retry_at.isoformat()
            )
            await asyncio.sleep(delay)
            retry_delay = min(retry_delay * 2, _DHAN_MAX_RETRY_DELAY_SECONDS)

    def get_positions(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return self._get_dhan_account_data(
            "/v2/positions", "positions_api_request", access_token, tokens
        )

    def get_order_history(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return self.get("/v2/orders", access_token, tokens)

    def get_funds(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return self.get("/v2/fundlimit", access_token, tokens)

    def _post_option_chain(
        self,
        path: str,
        access_token: str,
        tokens: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> Any:
        global _OPTION_CHAIN_LAST_REQUEST
        with _OPTION_CHAIN_REQUEST_LOCK:
            delay = 3 - (time.monotonic() - _OPTION_CHAIN_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
            try:
                payload = _request(
                    "POST",
                    f"{self.api_root}{path}",
                    diagnostic_stage="option_chain_api_request",
                    headers=self.account_headers(access_token, tokens),
                    json=body,
                )
                return _unwrap(payload)
            finally:
                _OPTION_CHAIN_LAST_REQUEST = time.monotonic()

    def get_option_expiries(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        underlying_scrip: int,
        underlying_segment: str,
    ) -> list[str]:
        payload = self._post_option_chain(
            "/v2/optionchain/expirylist",
            access_token,
            tokens,
            {
                "UnderlyingScrip": underlying_scrip,
                "UnderlyingSeg": underlying_segment,
            },
        )
        if not isinstance(payload, list) or any(
            not isinstance(expiry, str) for expiry in payload
        ):
            raise BrokerAdapterError(
                "Dhan option expiry response has an unexpected format",
                category="invalid_provider_response",
            )
        return payload

    def get_option_chain(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        underlying_scrip: int,
        underlying_segment: str,
        expiry: str,
    ) -> dict[str, Any]:
        payload = self._post_option_chain(
            "/v2/optionchain",
            access_token,
            tokens,
            {
                "UnderlyingScrip": underlying_scrip,
                "UnderlyingSeg": underlying_segment,
                "Expiry": expiry,
            },
        )
        if not isinstance(payload, dict):
            raise BrokerAdapterError(
                "Dhan option-chain response has an unexpected format",
                category="invalid_provider_response",
            )
        return payload


ADAPTERS: dict[str, type[BrokerAdapter]] = {
    adapter.slug: adapter
    for adapter in (MockBrokerAdapter, ZerodhaAdapter, UpstoxAdapter, AngelOneAdapter, DhanAdapter)
}


def get_broker_adapter(slug: str) -> BrokerAdapter:
    adapter_type = ADAPTERS.get(slug)
    if adapter_type is None:
        raise KeyError(slug)
    return adapter_type()
