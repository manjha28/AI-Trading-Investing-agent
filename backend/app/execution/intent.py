"""Structured order intent. An intent is a *description*; creating one never contacts a broker."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Venue = Literal["delta_exchange_india"]


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Venue = "delta_exchange_india"
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9_\-]+$")
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)  # contracts
    order_type: Literal["market", "limit", "stop_market", "stop_limit"] = "limit"
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    time_in_force: Literal["gtc", "ioc", "fok"] = "gtc"
    valid_until: datetime | None = None
    reduce_only: bool = False
    strategy_id: str = Field(min_length=1, max_length=64)
    strategy_version: str = Field(min_length=1, max_length=32)
    signal_id: str = Field(min_length=1, max_length=64)

    @field_validator("quantity", "limit_price", "stop_price", mode="before")
    @classmethod
    def no_floats(cls, value: Any) -> Any:
        # Decimal(float) is forbidden: money and quantity arrive as strings or Decimals.
        if isinstance(value, float):
            raise ValueError("send numbers as strings, never floats")
        return value

    @model_validator(mode="after")
    def check_prices(self) -> "OrderIntent":
        if self.order_type in {"limit", "stop_limit"} and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if self.order_type in {"stop_market", "stop_limit"} and self.stop_price is None:
            raise ValueError("stop_price is required for stop orders")
        if self.order_type == "market" and (self.limit_price is not None or self.stop_price is not None):
            raise ValueError("market orders take no prices")
        if self.valid_until is not None and self.valid_until.utcoffset() is None:
            raise ValueError("valid_until must include a timezone")
        return self

    def canonical(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json())


def client_order_id(intent: OrderIntent, owner_id: UUID) -> str:
    """Deterministic ID: the same owner and intent always yield the same ID (idempotent retries).

    30 characters, within Delta's client_order_id limit.
    """
    payload = json.dumps(
        {"owner": str(owner_id), "intent": intent.canonical()}, sort_keys=True, separators=(",", ":")
    )
    return "tc" + hashlib.sha256(payload.encode()).hexdigest()[:28]
