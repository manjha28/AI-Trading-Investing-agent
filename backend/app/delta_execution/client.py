"""The ONLY code in this application that can reach Delta's order endpoints.

Hard properties (each covered by a test):
  * it refuses to run under pytest or CI unless a mocked transport is supplied;
  * it refuses unless DELTA_LIVE_EXECUTION_ENABLED is set and both credentials exist;
  * the key and secret are read once from settings, never logged and never returned;
  * the broker's reply is returned verbatim so a rejection (for example an IP restriction) is recorded as it happened.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.config import settings
from app.delta_execution import policy
from app.delta_execution.intent import DeltaOrderIntent

logger = logging.getLogger("delta_execution")


class ExecutionBlocked(RuntimeError):
    """Raised before any network call when this process must not reach an order endpoint."""


@dataclass(frozen=True)
class BrokerReply:
    ok: bool
    status_code: int | None
    body: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def order_id(self) -> str | None:
        result = self.body.get("result")
        value = result.get("id") if isinstance(result, dict) else None
        return str(value) if value else None


def round_tick(price: Decimal, tick: Decimal | None) -> Decimal:
    return (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick if tick else price


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


class DeltaOrderClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = 10.0, clock=time.time) -> None:
        self._transport, self._timeout, self._clock = transport, timeout, clock

    def _guard(self) -> tuple[str, str]:
        blocked = policy.environment_block()
        if blocked and self._transport is None:
            raise ExecutionBlocked(f"Delta order endpoints are unreachable from this process ({blocked}).")
        if not policy.deployment_enabled():
            raise ExecutionBlocked("DELTA_LIVE_EXECUTION_ENABLED is not set on this deployment.")
        if not policy.credentials_present():
            raise ExecutionBlocked("Delta API credentials are not configured on this deployment.")
        return settings.delta_api_key.get_secret_value(), settings.delta_api_secret.get_secret_value()  # type: ignore[union-attr]

    @staticmethod
    def sign(secret: str, method: str, timestamp: str, path: str, query: str, body: str) -> str:
        return hmac.new(secret.encode(), (method + timestamp + path + query + body).encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, query: str = "") -> BrokerReply:
        key, secret = self._guard()
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        timestamp = str(int(self._clock()))
        headers = {"api-key": key, "timestamp": timestamp, "signature": self.sign(secret, method, timestamp, path, query, body),
                   "User-Agent": "FinByteNexus/1.0", "Content-Type": "application/json", "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
                response = http.request(method, f"{policy.BASE_URL}{path}{query}", content=body or None, headers=headers)
        except httpx.HTTPError as error:
            return BrokerReply(False, None, {}, f"network error: {type(error).__name__}")
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        data = data if isinstance(data, dict) else {"data": data}
        logger.info("delta_order_call method=%s path=%s status=%s", method, path, response.status_code)     # never the key, secret, signature or payload
        error = None if response.is_success else str((data.get("error") or {}).get("code") if isinstance(data.get("error"), dict) else data.get("error") or response.reason_phrase)
        return BrokerReply(response.is_success, response.status_code, data, error)

    # ── orders ──
    @staticmethod
    def build_entry(intent: DeltaOrderIntent, client_order_id: str, tick: Decimal | None) -> dict[str, Any]:
        """Entry as a marketable LIMIT (0.3% through the reference price) with the protective stop and the target attached as one bracket."""
        buy = intent.side == "buy"
        entry = round_tick(intent.reference_price * (Decimal(1) + (policy.MARKETABLE_LIMIT_BUFFER if buy else -policy.MARKETABLE_LIMIT_BUFFER)), tick)
        return {"product_id": intent.product_id, "size": intent.contracts, "side": intent.side, "order_type": "limit_order", "limit_price": _plain(entry),
                "time_in_force": "gtc", "client_order_id": client_order_id, "bracket_stop_loss_price": _plain(round_tick(intent.stop_price, tick)),
                "bracket_take_profit_price": _plain(round_tick(intent.target_price, tick)), "bracket_stop_trigger_method": "last_traded_price"}

    def set_leverage(self, product_id: int, leverage: int) -> BrokerReply:
        if not 1 <= leverage <= policy.LEVERAGE_CAP:
            raise ExecutionBlocked("leverage is outside the hard cap")
        return self._request("POST", f"/v2/products/{product_id}/orders/leverage", {"product_id": product_id, "leverage": str(leverage)})

    def place_bracket(self, intent: DeltaOrderIntent, client_order_id: str, tick: Decimal | None) -> BrokerReply:
        if intent.contracts > policy.MAX_CONTRACTS or intent.leverage > policy.LEVERAGE_CAP or intent.strategy_key not in policy.ALLOWED_STRATEGY_KEYS:
            raise ExecutionBlocked("intent violates the hard live-execution limits")
        return self._request("POST", "/v2/orders", self.build_entry(intent, client_order_id, tick))

    def get_order(self, order_id: str) -> BrokerReply:
        return self._request("GET", f"/v2/orders/{order_id}")

    def cancel_order(self, order_id: str, product_id: int) -> BrokerReply:
        return self._request("DELETE", "/v2/orders", {"id": int(order_id), "product_id": product_id})

    def position(self, product_id: int) -> BrokerReply:
        return self._request("GET", "/v2/positions", query=f"?product_id={product_id}")
