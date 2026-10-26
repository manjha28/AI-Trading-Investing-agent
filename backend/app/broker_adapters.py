from __future__ import annotations

import asyncio
import hashlib
import csv
import json
import logging
import re
import struct
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from app.config import settings
from app.market_data import HistoricalBar

logger = logging.getLogger(__name__)

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
                records = [
                    row
                    for row in reader
                    if row.get("EXCH_ID", "").upper() in {"NSE", "BSE"}
                    and row.get("SEGMENT", "").upper() == "E"
                    and row.get("INSTRUMENT", "").upper() == "EQUITY"
                    and row.get("SERIES", "").upper() == "EQ"
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

    def get_market_quote(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        exchange_segment: str,
        security_id: str,
    ) -> HistoricalBar:
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
            json={exchange_segment: [int(security_id)]},
        )
        self._check_market_data_payload(payload)
        if payload.get("status") != "success":
            raise BrokerAdapterError(
                "Dhan market quote response was unsuccessful",
                category="invalid_provider_response",
            )
        data = payload.get("data")
        segment_data = data.get(exchange_segment) if isinstance(data, dict) else None
        record = segment_data.get(security_id) if isinstance(segment_data, dict) else None
        ohlc = record.get("ohlc") if isinstance(record, dict) else None
        if not isinstance(record, dict) or not isinstance(ohlc, dict):
            raise BrokerAdapterError("Dhan market quote response has an unexpected format")
        try:
            raw_timestamp = record["last_trade_time"]
            timestamp = datetime.strptime(
                str(raw_timestamp), "%d/%m/%Y %H:%M:%S"
            ).replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
            return HistoricalBar(
                timestamp=timestamp,
                open=Decimal(str(ohlc["open"])),
                high=Decimal(str(ohlc["high"])),
                low=Decimal(str(ohlc["low"])),
                close=Decimal(str(record["last_price"])),
                volume=Decimal(str(record["volume"])),
            )
        except (KeyError, ValueError, TypeError, InvalidOperation) as error:
            raise BrokerAdapterError(
                "Dhan market quote response is missing valid price or trade-time data"
            ) from error

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
        if interval not in {"1d", "1m", "5m", "15m", "1h"}:
            raise BrokerAdapterError("Dhan does not support this candle interval")
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise BrokerAdapterError("Dhan historical request range is invalid")
        intraday = interval != "1d"
        if intraday and end - start > timedelta(days=90):
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
            "toDate": (
                end_local.strftime("%Y-%m-%d %H:%M:%S")
                if intraday else end_local.strftime("%Y-%m-%d")
            ),
        }
        if intraday:
            body["interval"] = {
                "1m": "1",
                "5m": "5",
                "15m": "15",
                "1h": "60",
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
        try:
            for index in range(next(iter(lengths), 0)):
                timestamp = datetime.fromtimestamp(
                    float(arrays["timestamp"][index]), timezone.utc
                )
                bar = HistoricalBar(
                    timestamp=timestamp,
                    open=Decimal(str(arrays["open"][index])),
                    high=Decimal(str(arrays["high"][index])),
                    low=Decimal(str(arrays["low"][index])),
                    close=Decimal(str(arrays["close"][index])),
                    volume=Decimal(str(arrays["volume"][index])),
                )
                if start.astimezone(timezone.utc) <= bar.timestamp < end.astimezone(timezone.utc):
                    bars.append(bar)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, InvalidOperation) as error:
            raise BrokerAdapterError("Dhan historical response contains invalid candles") from error
        return sorted({bar.timestamp: bar for bar in bars}.values(), key=lambda bar: bar.timestamp)

    @staticmethod
    def decode_market_feed_packet(
        packet: bytes,
        exchange_segment: str,
        security_id: str,
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
        if exchange_segment not in segment_codes:
            raise BrokerAdapterError("Dhan live feed packet has an invalid header")
        packet_security_id = int.from_bytes(packet[4:8], "little", signed=True)
        if (
            packet[3] != segment_codes[exchange_segment]
            or packet_security_id != int(security_id)
        ):
            raise BrokerAdapterError("Dhan live feed packet does not match its subscription")
        if packet[0] != 4:
            return None
        if packet_length != 50:
            raise BrokerAdapterError("Dhan live feed quote packet has an invalid length")
        try:
            last_price = struct.unpack_from("<f", packet, 8)[0]
            trade_timestamp = struct.unpack_from("<i", packet, 14)[0]
            volume = struct.unpack_from("<i", packet, 22)[0]
            opening = struct.unpack_from("<f", packet, 34)[0]
            high = struct.unpack_from("<f", packet, 42)[0]
            low = struct.unpack_from("<f", packet, 46)[0]
            return HistoricalBar(
                timestamp=datetime.fromtimestamp(trade_timestamp, timezone.utc),
                open=Decimal(str(opening)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(last_price)),
                volume=Decimal(volume),
            )
        except (OverflowError, OSError, ValueError, TypeError, InvalidOperation) as error:
            raise BrokerAdapterError("Dhan live feed quote packet contains invalid values") from error

    async def stream_market_quotes(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        exchange_segment: str,
        security_id: str,
        status_callback: Callable[[str], Awaitable[None]],
    ) -> AsyncIterator[HistoricalBar]:
        query = urlencode({
            "version": "2",
            "token": access_token,
            "clientId": tokens["client_id"],
            "authType": "2",
        })
        url = f"wss://api-feed.dhan.co?{query}"
        subscription = json.dumps({
            "RequestCode": 17,
            "InstrumentCount": 1,
            "InstrumentList": [
                {"ExchangeSegment": exchange_segment, "SecurityId": security_id}
            ],
        })
        for attempt in range(3):
            if attempt:
                await status_callback("reconnecting")
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
            try:
                async with websocket_connect(
                    url, ping_interval=10, ping_timeout=40, close_timeout=5
                ) as connection:
                    await connection.send(subscription)
                    await status_callback("active")
                    async for message in connection:
                        if not isinstance(message, bytes):
                            continue
                        try:
                            quote = self.decode_market_feed_packet(
                                message, exchange_segment, security_id
                            )
                        except BrokerFeedDisconnected:
                            break
                        if quote is not None:
                            yield quote
            except InvalidStatus as error:
                if error.response.status_code == 401:
                    raise BrokerReconnectRequired(
                        "Reconnect to this broker to renew the session"
                    ) from None
                if attempt == 2:
                    break
            except (OSError, TimeoutError, WebSocketException):
                if attempt == 2:
                    break
        raise BrokerAdapterError("Dhan live feed disconnected after retrying")

    async def stream_watchlist_market_quotes(
        self,
        access_token: str,
        tokens: Mapping[str, str],
        initial_subscriptions: Mapping[str, tuple[str, str]],
        subscription_updates: asyncio.Queue[Mapping[str, tuple[str, str]]],
        status_callback: Callable[[str], Awaitable[None]],
    ) -> AsyncIterator[tuple[str, HistoricalBar]]:
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
                key = (exchange_segment, str(security_id))
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

        query = urlencode({
            "version": "2",
            "token": access_token,
            "clientId": tokens["client_id"],
            "authType": "2",
        })
        url = f"wss://api-feed.dhan.co?{query}"
        current = subscription_items(initial_subscriptions)
        retry_delay = 1
        reconnects = 0
        while True:
            await status_callback("connecting" if reconnects == 0 else "reconnecting")
            try:
                async with websocket_connect(
                    url, ping_interval=10, ping_timeout=40, close_timeout=5
                ) as connection:
                    for start in range(0, len(current), 100):
                        batch = list(current)[start : start + 100]
                        await connection.send(request_message(17, batch))
                    logger.info(
                        "Dhan live feed connected subscriptions=%d",
                        len(current),
                    )
                    await status_callback("active")
                    retry_delay = 1
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
                                for start in range(0, len(removed), 100):
                                    await connection.send(
                                        request_message(18, removed[start : start + 100])
                                    )
                                for start in range(0, len(added), 100):
                                    await connection.send(
                                        request_message(17, added[start : start + 100])
                                    )
                                current = desired
                                logger.info(
                                    "Dhan live feed subscriptions updated subscribed=%d unsubscribed=%d total=%d",
                                    len(added),
                                    len(removed),
                                    len(current),
                                )
                                update_task = asyncio.create_task(subscription_updates.get())
                            if receive_task in done:
                                message = receive_task.result()
                                receive_task = asyncio.create_task(connection.recv())
                                if not isinstance(message, bytes):
                                    continue
                                if message and message[0] == 50:
                                    raise BrokerFeedDisconnected(
                                        "Dhan feed requested reconnection"
                                    )
                                try:
                                    if len(message) < 8:
                                        raise BrokerAdapterError(
                                            "Dhan live feed packet has an invalid header"
                                        )
                                    segment = segment_codes.get(message[3])
                                    security_id = str(
                                        int.from_bytes(message[4:8], "little", signed=True)
                                    )
                                    instrument_id = current.get((segment or "", security_id))
                                    if instrument_id is None:
                                        continue
                                    quote = self.decode_market_feed_packet(
                                        message, segment or "", security_id
                                    )
                                except BrokerAdapterError as error:
                                    logger.warning(
                                        "Dhan live feed packet parse failed error_class=%s",
                                        type(error).__name__,
                                    )
                                    continue
                                if quote is not None:
                                    yield instrument_id, quote
                    finally:
                        receive_task.cancel()
                        update_task.cancel()
                        await asyncio.gather(
                            receive_task, update_task, return_exceptions=True
                        )
            except BrokerReconnectRequired:
                raise
            except InvalidStatus as error:
                if error.response.status_code == 401:
                    raise BrokerReconnectRequired(
                        "Reconnect to this broker to renew the session"
                    ) from None
                logger.warning(
                    "Dhan live feed connection failed status=%s",
                    error.response.status_code,
                )
            except (BrokerFeedDisconnected, OSError, TimeoutError, WebSocketException):
                logger.warning("Dhan live feed disconnected; reconnecting")
            reconnects += 1
            logger.info("Dhan live feed reconnect attempt=%d", reconnects)
            await status_callback("reconnecting")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)

    def get_positions(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return self._get_dhan_account_data(
            "/v2/positions", "positions_api_request", access_token, tokens
        )

    def get_order_history(self, access_token: str, tokens: Mapping[str, str]) -> list[dict[str, Any]]:
        return self.get("/v2/orders", access_token, tokens)

    def get_funds(self, access_token: str, tokens: Mapping[str, str]) -> Any:
        return self.get("/v2/fundlimit", access_token, tokens)


ADAPTERS: dict[str, type[BrokerAdapter]] = {
    adapter.slug: adapter
    for adapter in (MockBrokerAdapter, ZerodhaAdapter, UpstoxAdapter, AngelOneAdapter, DhanAdapter)
}


def get_broker_adapter(slug: str) -> BrokerAdapter:
    adapter_type = ADAPTERS.get(slug)
    if adapter_type is None:
        raise KeyError(slug)
    return adapter_type()
