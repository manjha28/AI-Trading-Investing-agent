"""Fail-closed risk checks for an order intent. Pure Decimal math; no I/O.

Every check must positively pass. Missing, stale, synthetic, unverifiable or currency-incompatible
inputs fail the check (never default to a favourable value). An approved decision means only
"would pass the configured risk checks": it never implies that an order exists or may be sent.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from app.execution.intent import OrderIntent


@dataclass(frozen=True)
class MarketInputs:
    mark_price: Decimal | None
    price_as_of: datetime | None
    price_is_synthetic: bool | None
    quote_currency: str | None
    contract_value: Decimal | None  # underlying units per contract
    product_state: str | None
    current_exposure_usd: Decimal | None  # None = unverifiable (never treated as zero)


@dataclass(frozen=True)
class Limits:
    max_order_notional: Decimal
    max_total_exposure: Decimal
    max_price_age_seconds: int
    currency: str = "USD"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GuardDecision:
    approved: bool
    notional: Decimal | None
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{check.name}: {check.detail}" for check in self.checks if not check.passed]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def evaluate(intent: OrderIntent, inputs: MarketInputs, limits: Limits, now: datetime) -> GuardDecision:
    now = _utc(now)
    checks: list[CheckResult] = []

    def check(name: str, passed: bool, detail: str) -> bool:
        checks.append(CheckResult(name, passed, detail))
        return passed

    check("price_available", inputs.mark_price is not None and inputs.mark_price > 0,
          "mark price is available" if inputs.mark_price else "no mark price stored for this contract")
    check("price_not_synthetic", inputs.price_is_synthetic is False,
          "price is real market data" if inputs.price_is_synthetic is False else "price provenance is synthetic or unknown")
    if inputs.price_as_of is None:
        check("price_fresh", False, "price timestamp is unknown")
    else:
        age = (now - _utc(inputs.price_as_of)).total_seconds()
        if age < 0:
            check("price_fresh", False, "price is dated in the future")
        else:
            check("price_fresh", age <= limits.max_price_age_seconds,
                  f"price is {int(age)}s old (limit {limits.max_price_age_seconds}s)")
    quote_ok = bool(inputs.quote_currency) and inputs.quote_currency.upper() == limits.currency.upper()
    check("currency_compatible", quote_ok,
          f"quoted in {inputs.quote_currency}; no FX conversion is ever applied" if not quote_ok
          else f"quoted in {limits.currency}")
    check("product_tradable", (inputs.product_state or "").lower() == "live",
          f"product state is {inputs.product_state or 'unknown'}")
    check("order_not_expired", intent.valid_until is None or _utc(intent.valid_until) > now,
          "order validity window is open" if intent.valid_until is None or _utc(intent.valid_until) > now
          else "order validity has expired")

    notional: Decimal | None = None
    reference = intent.limit_price if intent.limit_price is not None else inputs.mark_price
    if reference is not None and inputs.contract_value is not None and inputs.contract_value > 0:
        notional = intent.quantity * inputs.contract_value * reference
    check("notional_computable", notional is not None,
          f"notional {notional} {limits.currency}" if notional is not None else "contract value or price is unknown")
    check("order_notional_within_cap", notional is not None and notional <= limits.max_order_notional,
          f"notional {notional} vs cap {limits.max_order_notional}" if notional is not None else "cannot compare an unknown notional")

    if inputs.current_exposure_usd is None:
        check("exposure_verifiable", False,
              "current exposure is unverifiable (account monitor is disabled); unknown is never treated as zero")
        check("total_exposure_within_cap", False, "cannot add to an unknown exposure")
    else:
        check("exposure_verifiable", True, f"current exposure {inputs.current_exposure_usd}")
        if intent.reduce_only:
            check("total_exposure_within_cap", True, "reduce-only order does not add exposure")
        else:
            after = inputs.current_exposure_usd + (notional if notional is not None else Decimal("0"))
            check("total_exposure_within_cap", notional is not None and after <= limits.max_total_exposure,
                  f"exposure after {after} vs cap {limits.max_total_exposure}" if notional is not None
                  else "cannot add an unknown notional")
    return GuardDecision(approved=all(item.passed for item in checks), notional=notional, checks=checks)
