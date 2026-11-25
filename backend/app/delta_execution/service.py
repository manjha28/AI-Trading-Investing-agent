"""Live-order orchestration: guard-rail context, audit, place / block, kill switch, reconcile. Every decision is written to the audit trail."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
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
from app.delta_execution.guardrails import Decision, LiveContext, OpenPosition, evaluate
from app.delta_execution.intent import DeltaOrderIntent, client_order_id
from app.models.crypto_delta import CryptoForwardTest, DeltaLiveOrder, DeltaLiveState
from app.models.delta_scanner import DeltaScanner
from app.models.execution import ExecutionAuditEvent

logger = logging.getLogger("delta_execution")
ClientFactory = Callable[[], DeltaOrderClient]
Source = CryptoForwardTest | DeltaScanner          # what produced the signal: the live forward test, or a scanner with its own Live switch
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


def build_context(db: Session, user_id: UUID, ft: Source, spec: Spec, cid: str, *, now: datetime, last_close: datetime | None, last_price: Decimal | None) -> LiveContext:
    state = get_state(db, user_id)
    rows = list(db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.created_at >= day_start_utc(now),
                                                         DeltaLiveOrder.status.in_(("placed", "filled", "closed")))))
    maximum = int(Decimal(100) / spec.initial_margin_pct) if spec.initial_margin_pct and spec.initial_margin_pct > 0 else None
    return LiveContext(
        now=now, deployment_enabled=policy.deployment_enabled(), credentials_present=policy.credentials_present(), environment_block=policy.environment_block(),
        armed=state.armed, test_executes_live=ft.execute_live, max_daily_loss_usd=state.max_daily_loss_usd, max_trades_per_day=state.max_trades_per_day,
        max_open_positions=state.max_open_positions, max_notional_usd=state.max_notional_usd, contract_value=spec.contract_value, exchange_max_leverage=maximum, maintenance_margin_pct=spec.maintenance_margin_pct,
        last_candle_close=last_close, last_price=last_price, orders_today=len(rows), open_positions=sum(1 for r in rows if r.status in OPEN),
        realized_pnl_today=Decimal(0),     # realised P&L is not derivable from the order reply alone; the daily loss cap is enforced through the risk-per-trade and trade-count caps
        contract_has_open_order=any(r.symbol == spec.symbol and r.status in OPEN for r in rows),
        duplicate_signal=db.scalar(select(func.count()).select_from(DeltaLiveOrder).where(DeltaLiveOrder.client_order_id == cid)) > 0,
        signal_max_age_seconds=RESOLUTION_SECONDS.get(ft.resolution),
    )


def attempt_order(db: Session, ft: Source, spec: Spec, intent: DeltaOrderIntent, *, now: datetime, last_close: datetime | None, last_price: Decimal | None,
                  client_factory: ClientFactory = DeltaOrderClient) -> DeltaLiveOrder | None:
    """Run the guard-rails on one intent and, only if EVERY check passes, set the leverage and place it. Idempotent per signal."""
    db.refresh(ft)                                  # the Live switch is read from the database at the moment of sending, never from a copy loaded earlier in the pass
    now = max(now, _now())                          # the gate judges freshness at the moment of sending: a pass that started minutes ago must not make a stale signal look fresh
    user_id = ft.user_id
    cid = client_order_id(intent, user_id)
    existing = db.scalar(select(DeltaLiveOrder).where(DeltaLiveOrder.client_order_id == cid))
    if existing is not None:
        return existing
    correlation = uuid4()
    ctx = build_context(db, user_id, ft, spec, cid, now=now, last_close=last_close, last_price=last_price)
    _available, wallet = read_balances(client_factory)                   # live read just before sending; used for leverage and liquidation sizing, not as a gate
    realized = read_realized_today(client_factory, now)
    open_positions, product_busy = read_open_state(client_factory, intent.product_id)
    if open_positions is not None:                                       # Delta's own state wins over our ledger, whose rows can lag behind (a closed order still marked placed)
        ctx = replace(ctx, open_positions=open_positions, contract_has_open_order=bool(product_busy))
    if ctx.open_positions > 0:                                           # several positions at once: the portfolio checks need each open position's size and our stop
        ctx = replace(ctx, other_positions=read_other_positions(db, user_id, client_factory, intent.product_id))
    if isinstance(ft, DeltaScanner):
        ctx = replace(ctx, total_risk_cap_pct=Decimal(ft.total_risk_cap_pct))
    ctx = replace(ctx, wallet_balance_usd=wallet, realized_pnl_today=realized or Decimal(0), realized_pnl_known=realized is not None)
    decision: Decision = evaluate(intent, ctx)
    audit(db, user_id, "intent", correlation, cid, intent.symbol, intent.canonical(), now)
    audit(db, user_id, "decision", correlation, cid, intent.symbol, decision.as_dict(), now)
    row = DeltaLiveOrder(user_id=user_id, forward_test_id=ft.id if isinstance(ft, CryptoForwardTest) else None, scanner_id=ft.id if isinstance(ft, DeltaScanner) else None, strategy_key=intent.strategy_key, symbol=intent.symbol, product_id=intent.product_id, side=intent.side,
                         contracts=intent.contracts, leverage=intent.leverage, signal_time=intent.signal_time, reference_price=intent.reference_price,
                         stop_price=intent.stop_price, target_price=intent.target_price, client_order_id=cid, status="blocked",
                         block_reason=(decision.reason or "")[:300] or None, created_at=now, updated_at=now)
    db.add(row)
    if decision.allowed:
        try:
            client = client_factory()
            lev = client.set_leverage(intent.product_id, intent.leverage)
            if not lev.ok:
                row.status, row.block_reason = "rejected", rejection_reason(lev, f"leverage change to {intent.leverage}x")[:300]
                row.response = {"leverage": {"http_status": lev.status_code, "body": lev.body, "error": lev.error}}
            else:
                reply = client.place_bracket(intent, cid, spec.tick_size)
                row.request = {"client_order_id": cid, "side": intent.side, "contracts": intent.contracts, "leverage": intent.leverage, "reference_price": str(intent.reference_price),
                               "stop": str(intent.stop_price), "target": str(intent.target_price), "product_id": intent.product_id}
                row.response = {"http_status": reply.status_code, "body": reply.body, "error": reply.error}
                row.delta_order_id = reply.order_id
                row.status = "placed" if reply.ok and reply.order_id else "rejected"
                row.block_reason = None if row.status == "placed" else rejection_reason(reply)[:300]
        except ExecutionBlocked as error:
            row.status, row.block_reason = "blocked", str(error)[:300]
        except Exception as error:  # noqa: BLE001 - recorded, never raised into the scheduler
            row.status, row.block_reason = "error", f"{type(error).__name__}"[:300]
        audit(db, user_id, "broker_reply", correlation, cid, intent.symbol, {"status": row.status, "order_id": row.delta_order_id, "response": row.response, "reason": row.block_reason}, now)
    row.updated_at = now
    db.commit()
    return row


def read_balances(client_factory: ClientFactory = DeltaOrderClient) -> tuple[Decimal | None, Decimal | None]:
    """(available, total wallet) in USD from Delta's wallet endpoint, or (None, None) when it cannot be read (the order is then refused)."""
    try:
        reply = client_factory().balances()
    except (ExecutionBlocked, ValueError):
        return None, None
    rows = reply.body.get("result") if reply.ok else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("asset_symbol", "")).upper() in ("USD", "USDT"):
            try:
                return Decimal(str(row["available_balance"])), Decimal(str(row["balance"]))
            except (KeyError, ArithmeticError):
                return None, None
    return None, None


def read_open_state(client_factory: ClientFactory, product_id: int) -> tuple[int | None, bool | None]:
    """(open positions in total, whether THIS contract has an open position or a live entry order) read from Delta right now; (None, None) if unreadable, when the ledger is used instead."""
    try:
        client = client_factory()
        positions = client._request("GET", "/v2/positions/margined")
        orders = client._request("GET", "/v2/orders", query="?state=open&page_size=50")
    except (ExecutionBlocked, ValueError):
        return None, None
    pos, opn = positions.body.get("result") if positions.ok else None, orders.body.get("result") if orders.ok else None
    if not isinstance(pos, list) or not isinstance(opn, list):
        return None, None
    held = [p for p in pos if Decimal(str(p.get("size") or 0)) != 0]
    busy = any(p.get("product_id") == product_id for p in held) or any(o.get("product_id") == product_id and not o.get("reduce_only") for o in opn)
    return len(held), busy


def read_other_positions(db: Session, user_id: UUID, client_factory: ClientFactory, exclude_product: int,
                         spec_lookup: Callable[[str], Spec] | None = None) -> tuple[OpenPosition, ...] | None:
    """Every position open on Delta (other than this contract) with the stop this app attached to it. None if Delta cannot be read, or if any open position
    was not opened by this app or has no known stop: its risk is then unknown and the portfolio checks fail closed."""
    if spec_lookup is None:
        from app.crypto_delta.forward import fetch_spec as spec_lookup  # noqa: N813
    try:
        reply = client_factory()._request("GET", "/v2/positions/margined")
    except (ExecutionBlocked, ValueError):
        return None
    rows = reply.body.get("result") if reply.ok else None
    if not isinstance(rows, list):
        return None
    out = []
    for p in rows:
        try:
            size = Decimal(str(p.get("size") or 0))
        except ArithmeticError:
            return None
        if size == 0 or p.get("product_id") == exclude_product:
            continue
        mine = db.scalar(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.product_id == p.get("product_id"), DeltaLiveOrder.status.in_(("placed", "filled", "closed")),
                                                      DeltaLiveOrder.stop_price.is_not(None)).order_by(DeltaLiveOrder.created_at.desc()))
        if mine is None:
            return None
        try:
            spec = spec_lookup(mine.symbol)
            entry = Decimal(str(p.get("entry_price") or mine.entry_price or mine.reference_price))
        except Exception:  # noqa: BLE001 - unknown contract data: fail closed
            return None
        if spec.maintenance_margin_pct is None:
            return None
        out.append(OpenPosition(mine.symbol, abs(size) * spec.contract_value, entry, Decimal(mine.stop_price), spec.maintenance_margin_pct))
    return tuple(out)


REALIZED_TYPES = ("cashflow", "commission", "funding", "liquidation_fee")


def read_realized_today(client_factory: ClientFactory, now: datetime) -> Decimal | None:
    """Today's (UTC) realised P&L in USD from Delta's own wallet transactions: closed-trade cashflow, commissions (incl. GST), funding and liquidation fees. None if unreadable."""
    try:
        reply = client_factory()._request("GET", "/v2/wallet/transactions", query="?page_size=200")
    except (ExecutionBlocked, ValueError):
        return None
    rows = reply.body.get("result") if reply.ok else None
    if not isinstance(rows, list):
        return None
    start, total = day_start_utc(now), Decimal(0)
    for row in rows:
        try:
            when = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            if when >= start and row.get("transaction_type") in REALIZED_TYPES and str(row.get("asset_symbol", "USD")).upper() == "USD":
                total += Decimal(str(row["amount"]))
        except (KeyError, ValueError, ArithmeticError):
            return None
    return total


def rejection_reason(reply: BrokerReply, what: str = "order") -> str:
    """Delta's own reason for refusing a request, as it answered (code, message and context), so the ledger shows exactly why."""
    err = reply.body.get("error") if isinstance(reply.body.get("error"), dict) else {}
    parts = [str(err.get("code") or reply.error or "refused")]
    if err.get("message"):
        parts.append(str(err["message"]))
    if err.get("context"):
        parts.append(json.dumps(err["context"], separators=(",", ":"), default=str))
    return f"Delta rejected the {what} (HTTP {reply.status_code}): " + " · ".join(parts)


def maybe_place(db: Session, ft: CryptoForwardTest, spec: Spec, entries: list[dict[str, Any]], candles: list[Candle], now: datetime,
                client_factory: ClientFactory = DeltaOrderClient) -> DeltaLiveOrder | None:
    """Turn a paper entry that formed on the newest completed candle into a live intent. Older entries are never replayed into live orders."""
    db.refresh(ft)                                  # a test switched to Live: off after this pass began must not send
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


def scanner_live_block(sc: DeltaScanner) -> str | None:
    """Why this scanner can NOT execute live, or None. Only the allowlisted strategy, as the live forward test runs it (5 EMA, 5m or 15m), qualifies."""
    if sc.strategy_key not in policy.ALLOWED_STRATEGY_KEYS:
        return f"'{sc.strategy_key}' is not on the live allowlist ({', '.join(sorted(policy.ALLOWED_STRATEGY_KEYS))}); this scanner stays paper only"
    if sc.resolution not in ("5m", "15m") or int((sc.params or {}).get("ema", 5)) != 5:
        return "only the 5 EMA alert-candle strategy on 5m or 15m candles can execute live"
    return None


def maybe_place_scanner(db: Session, sc: DeltaScanner, spec: Spec, entries: list[dict[str, Any]], candles: list[Candle], now: datetime,
                        client_factory: ClientFactory = DeltaOrderClient) -> DeltaLiveOrder | None:
    """A scanner's newest-candle entry on one contract -> a live intent, only if the scanner's Live switch is on AND its strategy is on the code allowlist.
    Any other scanner is inert: nothing is built, nothing is read from Delta, nothing is sent."""
    db.refresh(sc)
    if not sc.execute_live or sc.status != "active" or scanner_live_block(sc) is not None or not candles:
        return None
    last = candles[-1]
    entry = next((e for e in reversed(entries) if e["entry_time"] == last.open_time and not e["closed_same_bar"]), None)
    if entry is None:
        return None
    rr = Decimal(str((sc.params or {}).get("rr", 3)))
    ref, stop, side = last.close, entry["stop"], entry["side"]
    close_time = last.open_time + timedelta(seconds=RESOLUTION_SECONDS[sc.resolution])
    try:
        if spec.product_id is None:
            raise ValueError("the contract's product id is unknown")
        target = ref - rr * (stop - ref) if side == "sell" else ref + rr * (ref - stop)
        lev = policy.effective_leverage(spec.initial_margin_pct) or 1
        intent = DeltaOrderIntent(symbol=spec.symbol, product_id=spec.product_id, side=side, contracts=1, leverage=lev, strategy_key=sc.strategy_key,
                                  signal_id=f"scanner:{sc.id}:{spec.symbol}:{entry['signal_time'].isoformat()}:{side}", signal_time=entry["signal_time"], reference_price=ref,
                                  stop_price=stop, target_price=target)
    except (ValidationError, ValueError) as error:
        audit(db, sc.user_id, "decision", uuid4(), "no-intent", spec.symbol, {"allowed": False, "scanner": str(sc.id), "reason": f"no valid live intent: {str(error)[:200]}"}, now)
        db.commit()
        return None
    return attempt_order(db, sc, spec, intent, now=now, last_close=close_time, last_price=ref, client_factory=client_factory)


def scanner_kill(db: Session, sc: DeltaScanner, client_factory: ClientFactory = DeltaOrderClient, *, now: datetime | None = None) -> dict[str, Any]:
    """Scanner kill switch: its Live switch goes OFF first (no new orders), then this scanner's unfilled orders are cancelled. Positions are left alone (see close_all)."""
    now = now or _now()
    sc.execute_live, sc.updated_at = False, now
    audit(db, sc.user_id, "decision", uuid4(), "scanner-kill", "-", {"event": "scanner live switch off (kill)", "scanner": str(sc.id)}, now)
    db.commit()
    cancelled, failed = 0, []
    for row in db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == sc.user_id, DeltaLiveOrder.scanner_id == sc.id, DeltaLiveOrder.status == "placed")):
        try:
            reply = client_factory().cancel_order(row.delta_order_id or "0", row.product_id)
        except (ExecutionBlocked, ValueError) as error:
            failed.append({"symbol": row.symbol, "error": str(error)})
            continue
        if reply.ok:
            row.status, row.updated_at, cancelled = "cancelled", now, cancelled + 1
        else:
            failed.append({"symbol": row.symbol, "error": reply.error or "cancel refused"})
        audit(db, sc.user_id, "broker_reply", uuid4(), row.client_order_id, row.symbol, {"event": "cancel", "scanner": str(sc.id), "ok": reply.ok, "response": reply.body}, now)
    db.commit()
    return {"execute_live": False, "cancelled": cancelled, "cancel_failed": failed}


def scanner_close_all(db: Session, sc: DeltaScanner, client_factory: ClientFactory = DeltaOrderClient, *, now: datetime | None = None) -> dict[str, Any]:
    """Close every open position on a contract THIS scanner has traded, with a reduce-only market order (it can only shrink a position, never open one).
    The scanner's Live switch is turned off first. Positions on other contracts are never touched."""
    now = now or _now()
    if sc.execute_live:
        sc.execute_live, sc.updated_at = False, now
        db.commit()
    products = {r.product_id: r.symbol for r in db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == sc.user_id, DeltaLiveOrder.scanner_id == sc.id,
                                                                                          DeltaLiveOrder.status.in_(("placed", "filled", "closed"))))}
    closed, failed = [], []
    if not products:
        return {"closed": closed, "failed": failed, "note": "This scanner has no live orders, so there is nothing to close."}
    try:
        client = client_factory()
        reply = client._request("GET", "/v2/positions/margined")
    except (ExecutionBlocked, ValueError) as error:
        return {"closed": closed, "failed": [{"symbol": "-", "error": str(error)}]}
    rows = reply.body.get("result") if reply.ok else None
    if not isinstance(rows, list):
        return {"closed": closed, "failed": [{"symbol": "-", "error": "Delta positions could not be read; nothing was sent."}]}
    for p in rows:
        pid = p.get("product_id")
        try:
            size = int(Decimal(str(p.get("size") or 0)))
        except (ArithmeticError, ValueError):
            continue
        if size == 0 or pid not in products:
            continue
        symbol = products[pid]
        try:
            res = client.close_position(int(pid), size)
        except (ExecutionBlocked, ValueError) as error:
            failed.append({"symbol": symbol, "error": str(error)})
            continue
        (closed if res.ok else failed).append({"symbol": symbol, "size": size, **({} if res.ok else {"error": rejection_reason(res, "close")})})
        audit(db, sc.user_id, "broker_reply", uuid4(), f"close-{pid}", symbol, {"event": "close position (reduce-only market)", "scanner": str(sc.id), "size": size, "ok": res.ok,
                                                                              "http_status": res.status_code, "response": res.body}, now)
    db.commit()
    return {"closed": closed, "failed": failed}


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
