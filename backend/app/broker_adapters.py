from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

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
    pass


class BrokerReconnectRequired(BrokerAdapterError):
    pass


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


def _request(method: str, url: str, *, diagnostic_stage: str | None = None, **kwargs: Any) -> Any:
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
            raise BrokerAdapterError("Broker rejected the account request")
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
    display_name = "Dhan"
    api_root = "https://api.dhan.co"
    auth_root = "https://auth.dhan.co"

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
            "dhanClientId": tokens["client_id"],
            "Accept": "application/json",
        }

    def _get_dhan_account_data(
        self,
        path: str,
        stage: str,
        access_token: str,
        tokens: Mapping[str, str],
    ) -> Any:
        payload = _request(
            "GET",
            f"{self.api_root}{path}",
            diagnostic_stage=stage,
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
            "/v2/holdings", "holdings_api_request", access_token, tokens
        )

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
