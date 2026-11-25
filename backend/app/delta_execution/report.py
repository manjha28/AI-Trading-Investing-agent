"""Live Delta account report and order-outcome sync. Read only: only GET endpoints are used. Delta is the source of truth for every figure."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.delta_execution import lifecycle
from app.delta_execution.client import DeltaOrderClient, ExecutionBlocked
from app.delta_execution.service import REALIZED_TYPES, day_start_utc
from app.models.crypto_delta import DeltaLiveOrder, DeltaLiveOutcome

CACHE_SECONDS = 10.0
_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _get(client: DeltaOrderClient, path: str, query: str = "") -> list[dict[str, Any]] | None:
    reply = client._request("GET", path, query=query)
    rows = reply.body.get("result") if reply.ok else None
    return rows if isinstance(rows, list) else None


def _pages(client: DeltaOrderClient, path: str, query: str, pages: int = 5) -> list[dict[str, Any]] | None:
    """Follow Delta's `after` cursor for up to `pages` pages (newest first)."""
    out: list[dict[str, Any]] = []
    after = ""
    for _ in range(pages):
        reply = client._request("GET", path, query=query + (f"&after={after}" if after else ""))
        if not reply.ok or not isinstance(reply.body.get("result"), list):
            return None if not out else out
        out += reply.body["result"]
        after = (reply.body.get("meta") or {}).get("after") or ""
        if not after or not reply.body["result"]:
            break
    return out


def _parallel(**calls):
    """Run independent read-only Delta GETs at once (each opens its own connection), so the report waits for the slowest call, not the sum of all."""
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = {k: pool.submit(fn) for k, fn in calls.items()}
        return {k: f.result() for k, f in futures.items()}


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def sync_outcomes(db: Session, user_id: UUID, client: DeltaOrderClient, *, now: datetime | None = None) -> dict[str, int]:
    """Refresh the outcome of every live order that has been placed. Orders already finished (target, stop, manual, liquidated, cancelled) are left as they are."""
    now = now or datetime.now(timezone.utc)
    counts = {"checked": 0, "updated": 0}
    rows = list(db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.status.in_(("placed", "filled", "closed", "cancelled")))))
    if not rows:
        return counts
    finished = {o.order_id for o in db.scalars(select(DeltaLiveOutcome).where(DeltaLiveOutcome.user_id == user_id, DeltaLiveOutcome.outcome.not_in(("still_open", "unknown"))))}
    if all(r.id in finished for r in rows):
        return counts                                   # every order already has a final outcome: nothing to ask Delta
    got = _parallel(orders=lambda: _pages(client, "/v2/orders/history", "?page_size=100"), fills=lambda: _pages(client, "/v2/fills", "?page_size=100"),
                    txns=lambda: _pages(client, "/v2/wallet/transactions", "?page_size=100"), positions=lambda: _get(client, "/v2/positions/margined"),
                    open_orders=lambda: _get(client, "/v2/orders", "?state=open&page_size=50"))
    orders, fills, txns, positions = got["orders"], got["fills"], got["txns"], got["positions"]
    if orders is None or fills is None or txns is None:
        return counts
    seen = {x.get("id") for x in orders}
    orders = orders + [o for o in (got["open_orders"] or []) if o.get("id") not in seen]   # live bracket legs are only in the open list, not in the history
    open_products = {p.get("product_id") for p in (positions or []) if Decimal(str(p.get("size") or 0)) != 0}
    for row in rows:
        existing = db.scalar(select(DeltaLiveOutcome).where(DeltaLiveOutcome.order_id == row.id))
        if existing is not None and existing.outcome not in ("still_open", "unknown"):
            continue
        counts["checked"] += 1
        got = lifecycle.classify(row.client_order_id, orders, fills, txns, None if positions is None else row.product_id in open_products)
        outcome = existing or DeltaLiveOutcome(order_id=row.id, user_id=user_id, outcome="unknown", synced_at=now)
        before = (outcome.outcome, outcome.net_pnl)
        outcome.outcome, outcome.synced_at = got["outcome"], now
        for key in ("entry_price", "entry_at", "exit_price", "exit_at", "realized_pnl", "fees", "funding", "net_pnl"):
            setattr(outcome, key, got.get(key))
        outcome.detail = {k: (str(v) if isinstance(v, (datetime, Decimal)) else v) for k, v in got.items() if k in ("entry_order_id", "exit_order_id", "bracket_leg_ids", "reason")}
        if existing is None:
            db.add(outcome)
        if (outcome.outcome, outcome.net_pnl) != before:
            counts["updated"] += 1
        if got["outcome"] in ("target_hit", "stop_hit", "closed_manually", "liquidated"):
            row.status = "closed"
        if got.get("entry_price") is not None:
            row.entry_price = got["entry_price"]
    db.commit()
    return counts


def wallet_snapshot(client: DeltaOrderClient, now: datetime) -> dict[str, Any]:
    """Wallet, open positions, open orders, today's and all-time realised P&L straight from Delta. Raises on any unreadable core piece so the caller can mark it stale."""
    got = _parallel(balances=lambda: _get(client, "/v2/wallet/balances"), positions=lambda: _get(client, "/v2/positions/margined"),
                    open_orders=lambda: _get(client, "/v2/orders", "?state=open&page_size=50"), txns=lambda: _pages(client, "/v2/wallet/transactions", "?page_size=100", pages=20))
    balances = got["balances"]
    if balances is None:
        raise RuntimeError("wallet unreadable")
    usd = next((b for b in balances if str(b.get("asset_symbol")).upper() == "USD"), None)
    if usd is None:
        raise RuntimeError("no USD wallet")
    positions, open_orders, txns = got["positions"] or [], got["open_orders"] or [], got["txns"] or []
    start = day_start_utc(now)
    today = all_time = Decimal(0)
    for t in txns:
        if t.get("transaction_type") in REALIZED_TYPES and str(t.get("asset_symbol", "USD")).upper() == "USD":
            amount = Decimal(str(t.get("amount") or 0))
            all_time += amount
            when = lifecycle._ts(t.get("created_at"))
            if when and when >= start:
                today += amount
    unrealised = sum((Decimal(str(p.get("unrealized_pnl") or 0)) for p in positions), Decimal(0))
    return {
        "wallet": {"total": usd.get("balance"), "available": usd.get("available_balance"), "margin_in_use": str(Decimal(str(usd.get("position_margin") or 0)) + Decimal(str(usd.get("order_margin") or 0))),
                   "unrealised_pnl": _money(unrealised), "net_equity": None},
        "positions": [{"symbol": p.get("product_symbol"), "size": p.get("size"), "entry_price": p.get("entry_price"), "mark_price": p.get("mark_price"),
                       "liquidation_price": p.get("liquidation_price"), "unrealised_pnl": p.get("unrealized_pnl"), "margin": p.get("margin")} for p in positions if Decimal(str(p.get("size") or 0)) != 0],
        "open_orders": [{"id": o.get("id"), "symbol": o.get("product_symbol"), "side": o.get("side"), "size": o.get("size"), "type": o.get("order_type"), "limit_price": o.get("limit_price"),
                         "stop_price": o.get("stop_price"), "stop_type": o.get("stop_order_type")} for o in open_orders],
        "realized_pnl": {"today": _money(today), "all_time": _money(all_time), "all_time_note": f"sum of the last {len(txns)} wallet records Delta returned (closed-trade cashflow, commission incl. GST, funding)"},
    }


def report(db: Session, user_id: UUID, client_factory=DeltaOrderClient, *, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """The execution report: cached for a few seconds; if Delta cannot be read the last good copy is returned with `stale` true and the reason."""
    now = now or datetime.now(timezone.utc)
    if not force and _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS and _cache["data"].get("user_id") == str(user_id):
        return _cache["data"]
    try:
        client = client_factory()
        snap = wallet_snapshot(client, now)
        try:
            sync_outcomes(db, user_id, client, now=now)
        except Exception:  # noqa: BLE001 - outcome sync problems must not hide the wallet
            db.rollback()
        data = {**snap, "user_id": str(user_id), "synced_at": now.isoformat(), "stale": False, "error": None}
    except (ExecutionBlocked, RuntimeError, ValueError) as error:
        last = dict(_cache["data"] or {})
        data = {**last, "user_id": str(user_id), "stale": True, "error": str(error)[:200] if last else f"Delta could not be read: {str(error)[:200]}"}
        data.setdefault("synced_at", None)
        return data                              # not cached: the next call tries again
    outcomes = db.scalars(select(DeltaLiveOutcome).where(DeltaLiveOutcome.user_id == user_id)).all()
    by_order = {o.order_id: o for o in outcomes}
    orders = db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.status != "blocked").order_by(DeltaLiveOrder.created_at.desc()).limit(50)).all()
    data["orders"] = [_order_view(o, by_order.get(o.id)) for o in orders]
    _cache.update(at=time.monotonic(), data=data)
    return data


def _order_view(o: DeltaLiveOrder, out: DeltaLiveOutcome | None) -> dict[str, Any]:
    if out is None and o.status in ("rejected", "error"):          # Delta refused it: there is no position and no money moved, so it is not "unknown"
        return {"id": str(o.id), "time": o.created_at.isoformat(), "symbol": o.symbol, "side": o.side, "contracts": o.contracts, "leverage": o.leverage, "status": o.status,
                "entry_price": None, "stop_price": _money(o.stop_price), "target_price": _money(o.target_price), "block_reason": o.block_reason, "outcome": "rejected",
                "outcome_label": lifecycle.LABELS["rejected"], "exit_at": None, "exit_price": None, "realized_pnl": None, "fees": None, "funding": None, "net_pnl": None}
    return {
        "id": str(o.id), "time": o.created_at.isoformat(), "symbol": o.symbol, "side": o.side, "contracts": o.contracts, "leverage": o.leverage, "status": o.status,
        "entry_price": _money(out.entry_price if out and out.entry_price is not None else o.entry_price), "stop_price": _money(o.stop_price), "target_price": _money(o.target_price),
        "block_reason": o.block_reason, "outcome": out.outcome if out else "unknown", "outcome_label": lifecycle.LABELS.get(out.outcome if out else "unknown"),
        "exit_at": out.exit_at.isoformat() if out and out.exit_at else None, "exit_price": _money(out.exit_price) if out else None, "realized_pnl": _money(out.realized_pnl) if out else None,
        "fees": _money(out.fees) if out else None, "funding": _money(out.funding) if out else None, "net_pnl": _money(out.net_pnl) if out else None,
    }
