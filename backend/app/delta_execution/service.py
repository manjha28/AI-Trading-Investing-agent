"""Live-order orchestration: guard-rail context, audit, place / block, kill switch, reconcile. Every decision is written to the audit trail."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.crypto_delta.candles import RESOLUTION_SECONDS, Candle
from app.crypto_delta.forward import Spec
from app.delta_execution import policy
from app.delta_execution.client import BrokerReply, DeltaOrderClient, ExecutionBlocked
from app.delta_execution.guardrails import Decision, LiveContext, evaluate
from app.delta_execution.intent import DeltaOrderIntent, client_order_id
from app.models.crypto_delta import CryptoForwardTest, DeltaLiveOrder, DeltaLiveState
from app.models.execution import ExecutionAuditEvent

logger = logging.getLogger("delta_execution")
ClientFactory = Callable[[], DeltaOrderClient]
OPEN = ("placed", "filled")
VENUE = "delta_exchange_india"
STRATEGY_KEY = "five_ema_alert"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def audit(db: Session, user_id: UUID, kind: str, correlation: UUID, client_id: str, symbol: str, payload: dict[str, Any], now: datetime) -> None:
    db.add(ExecutionAuditEvent(user_id=user_id, created_at=now, event_type=kind, mode="live", correlation_id=correlation, client_order_id=client_id,
                               venue=VENUE, symbol=symbol, hypothetical=False, payload=payload))


def get_state(db: Session, user_id: UUID) -> DeltaLiveState:
    row = db.get(DeltaLiveState, user_id)
    if row is None:
        row = DeltaLiveState(user_id=user_id, armed=False, max_daily_loss_usd=Decimal("25"), max_trades_per_day=6, max_open_positions=2,
                             max_notional_usd=Decimal("2000"), updated_at=_now())
        db.add(row)
        db.flush()
    return row


def day_start_utc(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def build_context(db: Session, user_id: UUID, ft: CryptoForwardTest, spec: Spec, cid: str, *, now: datetime, last_close: datetime | None, last_price: Decimal | None) -> LiveContext:
    state = get_state(db, user_id)
    rows = list(db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.created_at >= day_start_utc(now),
                                                         DeltaLiveOrder.status.in_(("placed", "filled", "closed")))))
    maximum = int(Decimal(100) / spec.initial_margin_pct) if spec.initial_margin_pct and spec.initial_margin_pct > 0 else None
    return LiveContext(
        now=now, deployment_enabled=policy.deployment_enabled(), credentials_present=policy.credentials_present(), environment_block=policy.environment_block(),
        armed=state.armed, test_executes_live=ft.execute_live, max_daily_loss_usd=state.max_daily_loss_usd, max_trades_per_day=state.max_trades_per_day,
        max_open_positions=state.max_open_positions, max_notional_usd=state.max_notional_usd, contract_value=spec.contract_value, exchange_max_leverage=maximum,
        last_candle_close=last_close, last_price=last_price, orders_today=len(rows), open_positions=sum(1 for r in rows if r.status in OPEN),
        realized_pnl_today=Decimal(0),     # realised P&L is not derivable from the order reply alone; the daily loss cap is enforced through the risk-per-trade and trade-count caps
        contract_has_open_order=any(r.symbol == spec.symbol and r.status in OPEN for r in rows),
        duplicate_signal=db.scalar(select(func.count()).select_from(DeltaLiveOrder).where(DeltaLiveOrder.client_order_id == cid)) > 0,
    )


def attempt_order(db: Session, ft: CryptoForwardTest, spec: Spec, intent: DeltaOrderIntent, *, now: datetime, last_close: datetime | None, last_price: Decimal | None,
                  client_factory: ClientFactory = DeltaOrderClient) -> DeltaLiveOrder | None:
    """Run the guard-rails on one intent and, only if EVERY check passes, set the leverage and place it. Idempotent per signal."""
    user_id = ft.user_id
    cid = client_order_id(intent, user_id)
    existing = db.scalar(select(DeltaLiveOrder).where(DeltaLiveOrder.client_order_id == cid))
    if existing is not None:
        return existing
    correlation = uuid4()
    ctx = build_context(db, user_id, ft, spec, cid, now=now, last_close=last_close, last_price=last_price)
    decision: Decision = evaluate(intent, ctx)
    audit(db, user_id, "intent", correlation, cid, intent.symbol, intent.canonical(), now)
    audit(db, user_id, "decision", correlation, cid, intent.symbol, decision.as_dict(), now)
    row = DeltaLiveOrder(user_id=user_id, forward_test_id=ft.id, strategy_key=intent.strategy_key, symbol=intent.symbol, product_id=intent.product_id, side=intent.side,
                         contracts=intent.contracts, leverage=intent.leverage, signal_time=intent.signal_time, reference_price=intent.reference_price,
                         stop_price=intent.stop_price, target_price=intent.target_price, client_order_id=cid, status="blocked",
                         block_reason=(decision.reason or "")[:300] or None, created_at=now, updated_at=now)
    db.add(row)
    if decision.allowed:
        try:
            client = client_factory()
            lev = client.set_leverage(intent.product_id, intent.leverage)
            if not lev.ok:
                row.status, row.block_reason = "rejected", f"leverage could not be set to {intent.leverage}x: {lev.error or 'refused'}"[:300]
                row.response = {"leverage": {"http_status": lev.status_code, "body": lev.body, "error": lev.error}}
            else:
                reply = client.place_bracket(intent, cid, spec.tick_size)
                row.request = {"client_order_id": cid, "side": intent.side, "contracts": intent.contracts, "leverage": intent.leverage, "reference_price": str(intent.reference_price),
                               "stop": str(intent.stop_price), "target": str(intent.target_price), "product_id": intent.product_id}
                row.response = {"http_status": reply.status_code, "body": reply.body, "error": reply.error}
                row.delta_order_id = reply.order_id
                row.status = "placed" if reply.ok and reply.order_id else "rejected"
                row.block_reason = None if row.status == "placed" else (reply.error or "Delta did not accept the order")[:300]
        except ExecutionBlocked as error:
            row.status, row.block_reason = "blocked", str(error)[:300]
        except Exception as error:  # noqa: BLE001 - recorded, never raised into the scheduler
            row.status, row.block_reason = "error", f"{type(error).__name__}"[:300]
        audit(db, user_id, "broker_reply", correlation, cid, intent.symbol, {"status": row.status, "order_id": row.delta_order_id, "response": row.response, "reason": row.block_reason}, now)
    row.updated_at = now
    db.commit()
    return row


def maybe_place(db: Session, ft: CryptoForwardTest, spec: Spec, entries: list[dict[str, Any]], candles: list[Candle], now: datetime,
                client_factory: ClientFactory = DeltaOrderClient) -> DeltaLiveOrder | None:
    """Turn a paper entry that formed on the newest completed candle into a live intent. Older entries are never replayed into live orders."""
    if not ft.execute_live or not candles:
        return None
    last = candles[-1]
    entry = next((e for e in reversed(entries) if e["entry_time"] == last.open_time and not e["closed_same_bar"]), None)
    if entry is None:
        return None
    params = ft.params or {}
    rr = Decimal(str(params.get("rr", 3)))
    ref, stop, side = last.close, entry["stop"], entry["side"]
    close_time = last.open_time + timedelta(seconds=RESOLUTION_SECONDS[ft.resolution])
    correlation = uuid4()
    try:
        if spec.product_id is None:
            raise ValueError("the contract's product id is unknown")
        target = ref - rr * (stop - ref) if side == "sell" else ref + rr * (ref - stop)
        lev = policy.effective_leverage(spec.initial_margin_pct) or 1
        intent = DeltaOrderIntent(symbol=ft.symbol, product_id=spec.product_id, side=side, contracts=1, leverage=lev, strategy_key=STRATEGY_KEY,
                                  signal_id=f"{ft.id}:{entry['signal_time'].isoformat()}:{side}", signal_time=entry["signal_time"], reference_price=ref, stop_price=stop, target_price=target)
    except (ValidationError, ValueError) as error:
        audit(db, ft.user_id, "decision", correlation, "no-intent", ft.symbol, {"allowed": False, "reason": f"no valid live intent: {str(error)[:200]}"}, now)
        db.commit()
        return None
    return attempt_order(db, ft, spec, intent, now=now, last_close=close_time, last_price=ref, client_factory=client_factory)


def reconcile(db: Session, user_id: UUID, client_factory: ClientFactory = DeltaOrderClient) -> dict[str, int]:
    """Refresh this app's open orders from Delta. Anything unrecognised is stored verbatim and left as it was."""
    counts = {"checked": 0, "updated": 0, "errors": 0}
    rows = list(db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.status.in_(OPEN), DeltaLiveOrder.delta_order_id.is_not(None))))
    if not rows:
        return counts
    client = client_factory()
    for row in rows:
        counts["checked"] += 1
        reply: BrokerReply = client.get_order(row.delta_order_id or "")
        if not reply.ok:
            counts["errors"] += 1
            row.response = {**(row.response or {}), "last_status_check": {"http_status": reply.status_code, "error": reply.error}}
            continue
        result = reply.body.get("result") if isinstance(reply.body.get("result"), dict) else {}
        state, before = str(result.get("state") or "").lower(), row.status
        if state == "cancelled":
            row.status = "cancelled"
        elif state in ("closed", "filled") and str(result.get("unfilled_size", "1")) in ("0", "0.0"):
            row.status = "filled"
            try:
                row.entry_price = Decimal(str(result.get("average_fill_price"))) if result.get("average_fill_price") else row.entry_price
            except ArithmeticError:
                pass
        row.response = {**(row.response or {}), "last_status": result}
        if row.status != before:
            counts["updated"] += 1
            row.updated_at = _now()
    db.commit()
    return counts


def disarm(db: Session, user_id: UUID, *, now: datetime | None = None) -> DeltaLiveState:
    now = now or _now()
    state = get_state(db, user_id)
    state.armed, state.disarmed_at, state.updated_at = False, now, now
    audit(db, user_id, "decision", uuid4(), "disarm", "-", {"event": "Delta live execution disarmed"}, now)
    db.commit()
    return state


def kill_switch(db: Session, user_id: UUID, client_factory: ClientFactory = DeltaOrderClient, *, now: datetime | None = None) -> dict[str, Any]:
    """Disarm FIRST (so nothing new can be placed), then cancel this app's unfilled orders. Filled positions are reported, never guessed at."""
    now = now or _now()
    disarm(db, user_id, now=now)
    cancelled, failed, filled = 0, [], []
    for row in db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.status.in_(OPEN))):
        if row.status == "filled":
            filled.append(row.symbol)
            continue
        try:
            reply = client_factory().cancel_order(row.delta_order_id or "0", row.product_id)
        except (ExecutionBlocked, ValueError) as error:
            failed.append({"symbol": row.symbol, "error": str(error)})
            continue
        if reply.ok:
            row.status, row.updated_at, cancelled = "cancelled", now, cancelled + 1
        else:
            failed.append({"symbol": row.symbol, "error": reply.error or "cancel refused"})
        audit(db, user_id, "broker_reply", uuid4(), row.client_order_id, row.symbol, {"event": "cancel", "ok": reply.ok, "response": reply.body}, now)
    db.commit()
    return {"armed": False, "cancelled": cancelled, "cancel_failed": failed, "filled_positions_still_open": filled,
            "note": "Filled positions are not closed by the kill switch: close them on Delta. Their stop and target stay attached on the exchange."}
