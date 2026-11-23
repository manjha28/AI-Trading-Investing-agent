"""A live Delta order intent: ONE bracket entry (stop and target attached) of at most one contract. Creating one never contacts a broker."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.delta_execution import policy


class DeltaOrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9_]+$")
    product_id: int = Field(ge=1)
    side: Literal["buy", "sell"]
    contracts: int = Field(ge=1, le=policy.MAX_CONTRACTS)
    leverage: int = Field(ge=1, le=policy.LEVERAGE_CAP)
    strategy_key: str
    signal_id: str = Field(min_length=1, max_length=96)
    signal_time: datetime
    reference_price: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    target_price: Decimal = Field(gt=0)

    @field_validator("reference_price", "stop_price", "target_price", mode="before")
    @classmethod
    def no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("send prices as strings or Decimals, never floats")
        return value

    @field_validator("strategy_key")
    @classmethod
    def allowlisted(cls, value: str) -> str:
        if value not in policy.ALLOWED_STRATEGY_KEYS:
            raise ValueError(f"strategy {value!r} is not on the live-execution allowlist")
        return value

    @model_validator(mode="after")
    def levels_make_sense(self) -> "DeltaOrderIntent":
        if self.signal_time.utcoffset() is None:
            raise ValueError("signal_time must include a timezone")
        if self.side == "buy" and not self.stop_price < self.reference_price < self.target_price:
            raise ValueError("a buy needs stop < price < target")
        if self.side == "sell" and not self.target_price < self.reference_price < self.stop_price:
            raise ValueError("a sell needs target < price < stop")
        return self

    def canonical(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json())


def client_order_id(intent: DeltaOrderIntent, owner_id: UUID) -> str:
    """Deterministic 30-character ID: the same owner and intent always yield the same ID, so a retry can never double-place."""
    payload = json.dumps({"owner": str(owner_id), "intent": intent.canonical()}, sort_keys=True, separators=(",", ":"))
    return "dl" + hashlib.sha256(payload.encode()).hexdigest()[:28]
