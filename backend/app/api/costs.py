"""Cost and tax estimate endpoints. Thin wrappers over services/transaction_costs.py and services/indian_tax.py (no formulas here)."""
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_csrf
from app.api.strategies import require_verified_session
from app.models import UserSession
from app.services import indian_tax
from app.services import transaction_costs as tc

router = APIRouter(prefix="/costs", tags=["costs"])
DISCLAIMER = "Decision support, not investment advice. Charge and tax figures are estimates from configurable schedules."


class ExecutionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^(buy|sell)$")
    quantity: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    price: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    liquidity: str = Field(default="taker", pattern="^(maker|taker)$")


class EstimateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: str | None = None
    broker: str = "dhan"
    exchange: str = Field(default="NSE", pattern="^(NSE|BSE)$")
    product: str = Field(default="intraday", pattern="^(intraday|delivery|fno)$")
    executions: list[ExecutionIn] = Field(min_length=1, max_length=500)
    overrides: dict[str, Decimal] = Field(default_factory=dict)
    auto_square_off: bool = False
    call_and_trade_orders: int = Field(default=0, ge=0, le=100)


class TaxIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    regime: str = Field(default="new", pattern="^(new|old)$")
    other_taxable_income: Decimal = Field(default=Decimal(0), ge=0)
    intraday_net_after_charges: Decimal = Decimal(0)
    brought_forward_speculative_loss: Decimal = Field(default=Decimal(0), ge=0)
    delivery_stcg: Decimal = Decimal(0)
    delivery_ltcg: Decimal = Decimal(0)


@router.get("/schedules")
def schedules(_: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    return {"schedules": [s.describe() for s in tc.SCHEDULES.values()], "disclaimer": DISCLAIMER,
            "tax_rules": [{"financial_year": r.fy, "effective_from": r.effective_from, "source": r.source, "verified": r.verified, "note": r.note} for r in indian_tax.RULES.values()]}


@router.post("/estimate")
def estimate_costs(body: EstimateIn, _: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf)) -> dict[str, Any]:
    try:
        schedule = tc.get_schedule(body.schedule_id, broker=body.broker, exchange=body.exchange, product=body.product)
        result = tc.compute_charges([tc.Execution(e.order_id, e.side, e.quantity, e.price, e.liquidity) for e in body.executions], schedule,
                                    overrides=body.overrides, auto_square_off=body.auto_square_off, call_and_trade_orders=body.call_and_trade_orders)
    except (tc.UnsupportedProduct, tc.InvalidExecution, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"schedule": schedule.describe(), "breakdown": result.as_dict(), "disclaimer": DISCLAIMER}


@router.post("/tax-estimate")
def estimate_tax(body: TaxIn, _: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf)) -> dict[str, Any]:
    try:
        return indian_tax.estimate(body.financial_year, body.regime, other_taxable_income=body.other_taxable_income, intraday_net=body.intraday_net_after_charges,
                                   brought_forward_speculative_loss=body.brought_forward_speculative_loss, delivery_stcg=body.delivery_stcg, delivery_ltcg=body.delivery_ltcg)
    except indian_tax.TaxConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
