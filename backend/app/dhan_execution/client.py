"""The ONLY code in this application that can reach Dhan's order endpoints.

Hard properties (each covered by a test):
  * it refuses to run under pytest or CI, whatever transport is supplied for real HTTP (a mocked transport is allowed in tests);
  * every call needs ``DhanOrderClient.allow_live`` resolved by the service layer after the guard-rails pass;
  * credentials are never logged, never stored here, and never appear in a returned reply;
  * the broker's reply is returned verbatim so a rejection (for example a static-IP restriction) is recorded as it happened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.dhan_execution import policy
from app.dhan_execution.intent import DhanOrderIntent

logger = logging.getLogger("dhan_execution")
TICK = Decimal("0.05")


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
        value = self.body.get("orderId") or self.body.get("order_id")
        return str(value) if value else None


def _round_tick(price: Decimal, tick: Decimal = TICK) -> Decimal:
    return (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


class DhanOrderClient:
    def __init__(self, api_root: str, access_token: str, client_id: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 10.0) -> None:
        self._root = api_root.rstrip("/")
        self._token = access_token
        self._client_id = client_id
        self._transport = transport
        self._timeout = timeout

    # ── safety ──
    def _guard(self) -> None:
        blocked = policy.environment_block()
        if blocked and self._transport is None:
            raise ExecutionBlocked(f"Dhan order endpoints are unreachable from this process ({blocked}).")
        if not policy.deployment_enabled():
            raise ExecutionBlocked("DHAN_LIVE_EXECUTION_ENABLED is not set on this deployment.")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> BrokerReply:
        self._guard()
        headers = {"access-token": self._token, "client-id": self._client_id, "Accept": "application/json", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
                response = http.request(method, f"{self._root}{path}", json=payload, headers=headers)
        except httpx.HTTPError as error:
            return BrokerReply(False, None, {}, f"network error: {type(error).__name__}")
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:500]}
        if not isinstance(body, dict):
            body = {"data": body}
        logger.info("dhan_order_call method=%s path=%s status=%s", method, path, response.status_code)   # never the token, payload or client id
        return BrokerReply(200 <= response.status_code < 300, response.status_code, body, None if response.is_success else str(body.get("errorMessage") or body.get("message") or response.reason_phrase))

    # ── orders ──
    @staticmethod
    def build_super_order(intent: DhanOrderIntent, correlation_id: str, client_id: str) -> dict[str, Any]:
        """Entry as a marketable LIMIT (0.3% through the reference price) with the protective stop and the target attached as one Dhan Super Order."""
        buy = intent.side == "buy"
        entry = _round_tick(intent.reference_price * (Decimal(1) + (Decimal(str(policy.MARKETABLE_LIMIT_BUFFER)) if buy else -Decimal(str(policy.MARKETABLE_LIMIT_BUFFER)))))
        return {
            "dhanClientId": client_id, "correlationId": correlation_id, "transactionType": "BUY" if buy else "SELL",
            "exchangeSegment": policy.EXCHANGE_SEGMENT, "productType": policy.PRODUCT_TYPE, "orderType": "LIMIT", "securityId": intent.security_id,
            "quantity": intent.quantity, "price": float(entry), "targetPrice": float(_round_tick(intent.target_price)), "stopLossPrice": float(_round_tick(intent.stop_price)),
            "trailingJump": 0,
        }

    def place_super_order(self, intent: DhanOrderIntent, correlation_id: str) -> BrokerReply:
        if intent.quantity > policy.MAX_ORDER_QUANTITY or intent.strategy_key not in policy.ALLOWED_STRATEGY_KEYS:
            raise ExecutionBlocked("intent violates the hard live-execution limits")
        return self._request("POST", "/v2/super/orders", self.build_super_order(intent, correlation_id, self._client_id))

    def get_order(self, order_id: str) -> BrokerReply:
        return self._request("GET", f"/v2/orders/{order_id}")

    def cancel_order(self, order_id: str) -> BrokerReply:
        return self._request("DELETE", f"/v2/super/orders/{order_id}/ENTRY_LEG")

    def positions(self) -> BrokerReply:
        return self._request("GET", "/v2/positions")
