"""Live depth for one Delta India perpetual (the Crypto watch side panel). Display only: nothing here can place or imply an order.

One backend socket per symbol (app/delta_depth/hub.py) is shared by every open panel. Same session rules as the other market streams:
signed in, email verified, 2FA-verified session, allowed origin."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.market_data import _websocket_origin_allowed
from app.api.research import require_verified_session
from app.db import get_db
from app.delta_depth.book import group_levels
from app.delta_depth.hub import hub
from app.models import UserSession
from app.security import hash_token

router = APIRouter(prefix="/delta-depth", tags=["delta-depth"])
SYMBOL = re.compile(r"^[A-Z0-9]{2,24}USD$")                  # USD-quoted perpetuals only


def _symbol(value: str) -> str:
    symbol = value.upper()
    if not SYMBOL.match(symbol):
        raise HTTPException(status_code=422, detail="Depth is available for USD-quoted perpetual symbols such as BTCUSD.")
    return symbol


@router.get("/{symbol}/snapshot")
async def depth_snapshot(symbol: str, group: str = Query(default="", max_length=16), _: UserSession = Depends(require_verified_session)) -> dict:
    """One REST snapshot (no socket kept open): the same view the stream sends, for a first paint or a manual refresh."""
    from app.delta_depth.book import OrderBook
    from app.delta_depth.hub import default_rest_snapshot

    symbol = _symbol(symbol)
    try:
        bids, asks = await default_rest_snapshot(symbol)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Delta depth snapshot is unavailable.") from exc
    book = OrderBook(symbol)
    book.load_snapshot(bids, asks, None, 0.0, provisional=True)
    top = book.top(50)
    if group:
        try:
            step = Decimal(group)
            if step > 0:
                top = {"bids": group_levels(top["bids"], step, "bid"), "asks": group_levels(top["asks"], step, "ask")}
        except InvalidOperation:
            pass
    return {"symbol": symbol, **top, "metrics": book.metrics(), "provisional": True, "disclaimer": "Decision support, not investment advice."}


@router.websocket("/{symbol}/stream")
async def depth_stream(websocket: WebSocket, symbol: str, db: Session = Depends(get_db)) -> None:
    await websocket.accept()
    if not _websocket_origin_allowed("delta_depth", websocket):
        await websocket.close(code=4403, reason="Origin rejected")
        return
    try:
        symbol = _symbol(symbol)
    except HTTPException:
        await websocket.send_json({"type": "status", "status": "unavailable", "message": "Depth is available for USD-quoted perpetual symbols only."})
        await websocket.close(code=1008)
        return
    raw_token = websocket.cookies.get("session_token")
    active = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(raw_token), UserSession.revoked_at.is_(None)).options(selectinload(UserSession.user))) if raw_token else None
    expires = active.expires_at if active is not None else None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if active is None or expires <= datetime.now(timezone.utc):
        await websocket.send_json({"type": "status", "status": "unavailable", "message": "Sign in again to use the depth panel."})
        await websocket.close(code=4401)
        return
    user = active.user
    if not user.email_verified or not user.two_factor_enabled or not active.two_factor_verified:
        await websocket.send_json({"type": "status", "status": "unavailable", "message": "Verify this session before using market data."})
        await websocket.close(code=4403)
        return
    db.close()                                                    # no database work during the long-lived stream
    queue = hub.subscribe(symbol)

    async def watch_client() -> None:
        try:
            while True:
                await websocket.receive_text()                    # pings keep the socket alive; a disconnect raises
        except WebSocketDisconnect:
            return

    monitor = asyncio.create_task(watch_client())
    try:
        while not monitor.done():
            try:
                message = await asyncio.wait_for(queue.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        monitor.cancel()
        hub.unsubscribe(symbol, queue)                            # the feed itself is dropped 30 s after the last viewer leaves
