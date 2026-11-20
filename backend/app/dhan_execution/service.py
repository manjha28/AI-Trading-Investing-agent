"""Live-order orchestration: guard-rail context, audit, place / block, kill switch, reconcile. Every decision is written to the audit trail."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dhan_execution import policy
from app.dhan_execution.client import BrokerReply, DhanOrderClient, ExecutionBlocked
from app.dhan_execution.guardrails import IST, Decision, LiveContext, evaluate
from app.dhan_execution.intent import DhanOrderIntent, client_order_id
from app.models import Instrument
from app.models.execution import ExecutionAuditEvent
from app.models.forward_live import DhanLiveOrder, DhanLiveState, ForwardTest

logger = logging.getLogger("dhan_execution")
ClientFactory = Callable[[UUID], DhanOrderClient]
OPEN = ("placed", "filled")
VENUE = "dhan_nse_equity"
TWO = Decimal("0.01")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def audit(db: Session, user_id: UUID, kind: str, correlation: UUID, client_id: str, symbol: str, payload: dict[str, Any], now: datetime) -> None:
    db.add(ExecutionAuditEvent(user_id=user_id, created_at=now, event_type=kind, mode="live", correlation_id=correlation, client_order_id=client_id,
                               venue=VENUE, symbol=symbol, hypothetical=False, payload=payload))


def get_state(db: Session, user_id: UUID) -> DhanLiveState:
    row = db.get(DhanLiveState, user_id)
    if row is None:
        row = DhanLiveState(user_id=user_id, armed=False, updated_at=_now())
        db.add(row)
        db.flush()
    return row


def _day_start_utc(now: datetime) -> datetime:
    local = now.astimezone(IST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def build_context(db: Session, user_id: UUID, ft: ForwardTest, instrument_id: UUID, cid: str, *, now: datetime, last_bar_close: datetime | None,
                  last_price: Decimal | None) -> LiveContext:
    state = get_state(db, user_id)
    since = _day_start_utc(now)
    todays = select(DhanLiveOrder).where(DhanLiveOrder.user_id == user_id, DhanLiveOrder.created_at >= since, DhanLiveOrder.status.in_(("placed", "filled", "closed")))
    rows = list(db.scalars(todays))
    realized = sum((r.realized_pnl for r in rows if r.status == "closed" and r.realized_pnl is not None), Decimal(0))
    return LiveContext(
        now=now, deployment_enabled=policy.deployment_enabled(), environment_block=policy.environment_block(), armed=state.armed,
        forward_test_execute_live=ft.execute_live, max_daily_loss_inr=state.max_daily_loss_inr, max_trades_per_day=state.max_trades_per_day,
        max_open_positions=state.max_open_positions, max_notional_inr=state.max_notional_inr, last_bar_close=last_bar_close, last_price=last_price,
        orders_today=len(rows), open_positions=sum(1 for r in rows if r.status in OPEN),
        realized_pnl_today=realized, instrument_has_open_order=any(r.instrument_id == instrument_id and r.status in OPEN for r in rows),
        duplicate_signal=db.scalar(select(func.count()).select_from(DhanLiveOrder).where(DhanLiveOrder.client_order_id == cid)) > 0,
    )


def attempt_order(db: Session, user_id: UUID, ft: ForwardTest, instrument: Instrument, intent: DhanOrderIntent, *, now: datetime,
                  last_bar_close: datetime | None, last_price: Decimal | None, client_factory: ClientFactory) -> DhanLiveOrder | None:
    """Run the guard-rails on one intent and, only if EVERY check passes, place it. Returns the ledger row (None for a paper-only test that was blocked)."""
    cid = client_order_id(intent, user_id)
    existing = db.scalar(select(DhanLiveOrder).where(DhanLiveOrder.client_order_id == cid))
    if existing is not None:
        return existing                                   # idempotent: a retried signal can never place a second order
    correlation = uuid4()
    ctx = build_context(db, user_id, ft, instrument.id, cid, now=now, last_bar_close=last_bar_close, last_price=last_price)
    decision: Decision = evaluate(intent, ctx)
    audit(db, user_id, "intent", correlation, cid, intent.symbol, intent.canonical(), now)
    audit(db, user_id, "decision", correlation, cid, intent.symbol, decision.as_dict(), now)
    if not decision.allowed and not ft.execute_live:
        db.commit()
        return None                                       # a paper-only test: nothing to record in the live ledger
    row = DhanLiveOrder(
        user_id=user_id, forward_test_id=ft.id, strategy_key=intent.strategy_key, instrument_id=instrument.id, symbol=intent.symbol, security_id=intent.security_id,
        side=intent.side, quantity=intent.quantity, signal_time=intent.signal_time, reference_price=intent.reference_price, stop_price=intent.stop_price,
        target_price=intent.target_price, client_order_id=cid, status="blocked", block_reason=(decision.reason or "")[:200] or None, created_at=now, updated_at=now,
        request=None, response=None)
    db.add(row)
    if decision.allowed:
        try:
            reply = client_factory(user_id).place_super_order(intent, cid)
            row.request = {"correlation_id": cid, "side": intent.side, "quantity": intent.quantity, "reference_price": str(intent.reference_price),
                           "stop": str(intent.stop_price), "target": str(intent.target_price), "product": policy.PRODUCT_TYPE, "segment": policy.EXCHANGE_SEGMENT}
            row.response = {"http_status": reply.status_code, "body": reply.body, "error": reply.error}
            row.dhan_order_id = reply.order_id
            row.status = "placed" if reply.ok and reply.order_id else "rejected"
            row.block_reason = None if row.status == "placed" else (reply.error or "Dhan did not accept the order")[:200]
        except ExecutionBlocked as error:
            row.status, row.block_reason = "blocked", str(error)[:200]
        except Exception as error:  # noqa: BLE001 - recorded, never raised into the scheduler
            row.status, row.block_reason = "error", f"{type(error).__name__}: {error}"[:200]
        audit(db, user_id, "broker_reply", correlation, cid, intent.symbol, {"status": row.status, "order_id": row.dhan_order_id, "response": row.response, "reason": row.block_reason}, now)
    row.updated_at = now
    db.commit()
    return row


def _price(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return number if number > 0 else None


def reconcile(db: Session, user_id: UUID, client_factory: ClientFactory, *, now: datetime | None = None) -> dict[str, int]:
    """Refresh this app's open orders from Dhan. Parsing is defensive: anything unrecognised is stored verbatim and left as it was."""
    now = now or _now()
    counts = {"checked": 0, "updated": 0, "errors": 0}
    rows = list(db.scalars(select(DhanLiveOrder).where(DhanLiveOrder.user_id == user_id, DhanLiveOrder.status.in_(OPEN), DhanLiveOrder.dhan_order_id.is_not(None))))
    if not rows:
        return counts
    client = client_factory(user_id)
    for row in rows:
        counts["checked"] += 1
        reply: BrokerReply = client.get_order(row.dhan_order_id or "")
        if not reply.ok:
            counts["errors"] += 1
            row.response = {**(row.response or {}), "last_status_check": {"http_status": reply.status_code, "error": reply.error}}
            continue
        body = reply.body
        status = str(body.get("orderStatus") or "").upper()
        legs = body.get("legDetails") or []
        before = (row.status, row.entry_price, row.exit_price)
        if status in ("REJECTED", "CANCELLED", "EXPIRED"):
            row.status = "rejected" if status == "REJECTED" else "cancelled"
        elif status in ("TRADED", "TRANSIT") or status == "PART_TRADED":
            row.status = "filled" if status == "TRADED" else row.status
            row.entry_price = _price(body.get("averageTradedPrice")) or row.entry_price
        for leg in legs if isinstance(legs, list) else []:
            if str(leg.get("orderStatus") or "").upper() == "TRADED":
                exit_price = _price(leg.get("averageTradedPrice") or leg.get("price"))
                name = str(leg.get("legName") or "").upper()
                if exit_price is not None and ("TARGET" in name or "STOP" in name):
                    row.exit_price, row.exit_reason, row.status = exit_price, ("target" if "TARGET" in name else "stop_loss"), "closed"
        if row.status == "closed" and row.entry_price is not None and row.exit_price is not None:
            row.realized_pnl = estimate_net_pnl(row.side, row.quantity, row.entry_price, row.exit_price)
        row.response = {**(row.response or {}), "last_status": body}
        if (row.status, row.entry_price, row.exit_price) != before:
            counts["updated"] += 1
            row.updated_at = now
    db.commit()
    return counts


def estimate_net_pnl(side: str, quantity: int, entry: Decimal, exit_: Decimal) -> Decimal:
    """Gross P&L less ESTIMATED statutory charges (the app's own Dhan schedule). The broker's contract note is the authority."""
    from app.services import transaction_costs as costs

    qty = Decimal(quantity)
    gross = (exit_ - entry) * qty * (1 if side == "buy" else -1)
    charges = costs.trade_charges("long" if side == "buy" else "short", qty, entry, exit_, costs.schedule_for_market("equity_in", None)).total
    return (gross - charges).quantize(TWO, rounding=ROUND_HALF_UP)


def disarm(db: Session, user_id: UUID, *, now: datetime | None = None) -> DhanLiveState:
    now = now or _now()
    state = get_state(db, user_id)
    state.armed, state.disarmed_at, state.updated_at = False, now, now
    audit(db, user_id, "decision", uuid4(), "disarm", "-", {"event": "live execution disarmed"}, now)
    db.commit()
    return state


def kill_switch(db: Session, user_id: UUID, client_factory: ClientFactory, *, now: datetime | None = None) -> dict[str, Any]:
    """Disarm FIRST (so nothing new can be placed), then cancel this app's orders that have not filled. Filled positions are reported, never guessed at."""
    now = now or _now()
    disarm(db, user_id, now=now)
    cancelled, failed, filled = 0, [], []
    for row in db.scalars(select(DhanLiveOrder).where(DhanLiveOrder.user_id == user_id, DhanLiveOrder.status.in_(OPEN))):
        if row.status == "filled":
            filled.append(row.symbol)
            continue
        try:
            reply = client_factory(user_id).cancel_order(row.dhan_order_id or "")
        except ExecutionBlocked as error:
            failed.append({"symbol": row.symbol, "error": str(error)})
            continue
        if reply.ok:
            row.status, row.updated_at, cancelled = "cancelled", now, cancelled + 1
        else:
            failed.append({"symbol": row.symbol, "error": reply.error or "cancel refused"})
        audit(db, user_id, "broker_reply", uuid4(), row.client_order_id, row.symbol, {"event": "cancel", "ok": reply.ok, "response": reply.body}, now)
    db.commit()
    return {"armed": False, "cancelled": cancelled, "cancel_failed": failed, "filled_positions_still_open": filled,
            "note": "Filled intraday positions are not closed by the kill switch: exit them in the Dhan app (the broker also auto-squares-off intraday positions near 15:20)."}
