"""Execution readiness API (research/monitor only).

There is no endpoint here, or anywhere, that places, modifies or cancels an order. ``/dry-run``
asks "would this intent pass the risk checks right now?" against stored real Delta market data
and records the intent and the decision in the append-only audit trail.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.config import settings
from app.db import get_db
from app.delta_research.store import DeltaResearchStore
from app.execution.guardrails import Limits, MarketInputs, evaluate
from app.execution.intent import OrderIntent, client_order_id
from app.execution.mode import current_mode
from app.models import UserSession
from app.models.execution import ExecutionAuditEvent

router = APIRouter(prefix="/execution", tags=["execution"])
DISCLAIMER = "Decision support, not investment advice. No order is placed or transmitted."


def require_verified_session(active_session: UserSession = Depends(get_active_session)) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={"code": "email_verification_required", "message": "Verify your email address first."},
        )
    return active_session


def _limits() -> Limits:
    return Limits(
        max_order_notional=settings.execution_max_order_notional_usd,
        max_total_exposure=settings.execution_max_total_exposure_usd,
        max_price_age_seconds=settings.execution_price_max_age_seconds,
    )


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _market_inputs(symbol: str) -> MarketInputs:
    """Read-only look-up in the stored Delta research data. Anything missing stays None."""
    empty = MarketInputs(None, None, None, None, None, None, None)
    if not settings.delta_research_enabled or not settings.delta_research_db_path:
        return empty
    try:
        with DeltaResearchStore(settings.delta_research_db_path, read_only=True) as store:
            product_id = store.product_id_for_symbol(symbol)
            detail = store.product_detail(product_id, 1) if product_id else None
    except Exception:
        return empty
    if detail is None:
        return empty
    metadata = detail["product"].get("metadata") or {}
    quote = detail.get("quote")
    quoting = metadata.get("quoting_asset") or {}
    observed: datetime | None = None
    if quote and quote.get("observed_at"):
        try:
            observed = datetime.fromisoformat(str(quote["observed_at"]).replace("Z", "+00:00"))
        except ValueError:
            observed = None
    source = (quote or {}).get("source")
    return MarketInputs(
        mark_price=_decimal(quote.get("mark_price")) if quote else None,
        price_as_of=observed,
        price_is_synthetic=(None if not source else any(word in str(source).lower() for word in ("synthetic", "mock", "test", "sample"))),
        quote_currency=quoting.get("symbol") if isinstance(quoting, dict) else None,
        contract_value=_decimal(metadata.get("contract_value")),
        product_state=detail["product"].get("state"),
        current_exposure_usd=None,  # the account monitor is disabled: exposure is unverifiable, not zero
    )


def _audit(db: Session, user_id: UUID, kind: str, mode: str, correlation: UUID, order_id: str,
           intent: OrderIntent, payload: dict[str, Any], now: datetime) -> ExecutionAuditEvent:
    row = ExecutionAuditEvent(
        user_id=user_id, created_at=now, event_type=kind, mode=mode, correlation_id=correlation,
        client_order_id=order_id, venue=intent.venue, symbol=intent.symbol, hypothetical=True, payload=payload,
    )
    db.add(row)
    return row


class DryRunRequest(BaseModel):
    intent: OrderIntent


@router.get("/status")
def execution_status(
    response: Response,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    mode = current_mode()
    limits = _limits()
    return {
        "mode": {
            "configured": mode.configured,
            "effective": mode.effective.value,
            "live_blocked": mode.live_blocked,
            "reason": mode.reason,
        },
        "capabilities": {
            "order_transmission": bool(settings.delta_live_execution_enabled),          # layer 1 of 3: orders go out only when the deployment flag, your arming and the per-test switch are all on
            "live_adapter": True,
            "execution_credentials": "read from backend secrets only (not stored in the database); 1 contract max, leverage min(25x, exchange max)",
            "simulated_fills": "backtest only, completed bars, labelled hypothetical",
        },
        "limits": {
            "currency": limits.currency,
            "max_order_notional": str(limits.max_order_notional),
            "max_total_exposure": str(limits.max_total_exposure),
            "max_price_age_seconds": limits.max_price_age_seconds,
        },
        "audit_events": db.scalar(
            select(func.count(ExecutionAuditEvent.id)).where(ExecutionAuditEvent.user_id == active_session.user_id)
        ) or 0,
        "disclaimer": DISCLAIMER,
    }


@router.post("/dry-run")
def dry_run(
    payload: DryRunRequest,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    mode = current_mode()
    intent = payload.intent
    order_id = client_order_id(intent, active_session.user_id)
    correlation = uuid4()
    decision = evaluate(intent, _market_inputs(intent.symbol), _limits(), now)
    _audit(db, active_session.user_id, "intent", mode.effective.value, correlation, order_id, intent,
           {"intent": intent.canonical()}, now)
    _audit(db, active_session.user_id, "decision", mode.effective.value, correlation, order_id, intent, {
        "approved": decision.approved,
        "notional": str(decision.notional) if decision.notional is not None else None,
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in decision.checks],
    }, now)
    db.commit()
    return {
        "mode": mode.effective.value,
        "client_order_id": order_id,
        "correlation_id": str(correlation),
        "decision": {
            "approved": decision.approved,
            "notional": str(decision.notional) if decision.notional is not None else None,
            "currency": "USD",
            "reasons": decision.reasons,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in decision.checks],
        },
        "order_transmission": "not_available",
        "note": "An approved decision means the risk checks would pass. It is not an order and none is sent.",
        "disclaimer": DISCLAIMER,
    }


@router.get("/audit")
def audit_trail(
    limit: int = Query(default=50, ge=1, le=200),
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(ExecutionAuditEvent)
        .where(ExecutionAuditEvent.user_id == active_session.user_id)
        .order_by(ExecutionAuditEvent.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": str(row.id), "created_at": row.created_at.isoformat(), "event_type": row.event_type,
            "mode": row.mode, "correlation_id": str(row.correlation_id), "client_order_id": row.client_order_id,
            "symbol": row.symbol, "hypothetical": row.hypothetical, "payload": row.payload,
        }
        for row in rows
    ]
