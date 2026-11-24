"""Fan-out of Delta India depth data: one backend websocket per symbol, shared by every browser that has that symbol's panel open.

A symbol is subscribed only while a panel is open and dropped DROP_AFTER_SECONDS after the last viewer leaves. Views go out at most RENDER_HZ times a second.
Everything is display only. Sockets, REST and the clock are injectable so the tests never touch the network."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from app.delta_depth.book import OrderBook, funding_countdown_seconds

logger = logging.getLogger(__name__)
BOOK_URL = "wss://socket.india.delta.exchange"
REST_URL = "https://api.india.delta.exchange/v2/l2orderbook/{symbol}"
DROP_AFTER_SECONDS = 30.0
STALE_AFTER_SECONDS = 3.0
RENDER_HZ = 5
LEVELS_SENT = 50
TAPE_SIZE = 50

Connect = Callable[[str], Any]                                     # url -> async context manager yielding a websocket-like object
RestSnapshot = Callable[[str], Awaitable[tuple[list[Any], list[Any]]]]


async def default_rest_snapshot(symbol: str) -> tuple[list[Any], list[Any]]:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(REST_URL.format(symbol=symbol), params={"depth": LEVELS_SENT})
        r.raise_for_status()
        result = r.json().get("result") or {}
    return result.get("buy") or [], result.get("sell") or []


def default_connect(url: str) -> Any:
    import websockets
    return websockets.connect(url, open_timeout=15, ping_interval=20)


class DepthFeed:
    def __init__(self, symbol: str, clock: Callable[[], float], connect: Connect, rest: RestSnapshot) -> None:
        self.symbol = symbol
        self.clock, self.connect, self.rest = clock, connect, rest
        self.book = OrderBook(symbol)
        self.tape: deque[dict[str, str]] = deque(maxlen=TAPE_SIZE)
        self.mark: dict[str, str | None] = {"mark_price": None, "index_price": None, "funding_rate": None, "open_interest_usd": None}
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.idle_since: float | None = None
        self.task: asyncio.Task[None] | None = None
        self.dirty = False
        self.resyncs = 0
        self.last_message: float | None = None

    # ---- incoming ----
    def handle(self, msg: dict[str, Any]) -> str | None:
        """Apply one websocket message. Returns 'resync' when the book must be rebuilt."""
        kind, now = msg.get("type"), self.clock()
        if kind == "l2_updates" or "action" in msg and "asks" in msg:
            self.last_message = now
            seq = msg.get("sequence_no")
            if msg.get("action") == "snapshot":
                self.book.load_snapshot(msg.get("bids") or [], msg.get("asks") or [], int(seq) if seq is not None else None, now)
                self.dirty = True
                return None if not self.book.needs_resync else "resync"
            result = self.book.apply_update(msg.get("bids") or [], msg.get("asks") or [], int(seq) if seq is not None else -1, now)
            self.dirty = self.dirty or result == "ok"
            return "resync" if result in ("gap", "crossed") else None
        if kind == "all_trades":
            self.last_message = now
            self._tape(msg)
        elif kind == "trades" and isinstance(msg.get("trades"), list):
            for t in reversed(msg["trades"][:TAPE_SIZE]):
                self._tape(t)
        elif kind == "mark_price":
            self.mark["mark_price"] = str(msg.get("price")) if msg.get("price") is not None else self.mark["mark_price"]
        elif kind == "v2/ticker":
            for key, src in (("mark_price", "mark_price"), ("index_price", "spot_price"), ("funding_rate", "funding_rate"), ("open_interest_usd", "oi_value_usd")):
                if msg.get(src) is not None:
                    self.mark[key] = str(msg[src])
        else:
            return None
        self.dirty = True
        return None

    def _tape(self, t: dict[str, Any]) -> None:
        if t.get("price") is None:
            return
        buyer_taker = t.get("buyer_role") == "taker"                   # the taker side is the aggressor
        self.tape.appendleft({"price": str(t["price"]), "size": str(t.get("size")), "aggressor": "buy" if buyer_taker else "sell", "time": str(t.get("timestamp", ""))})

    # ---- outgoing ----
    def view(self) -> dict[str, Any]:
        now = self.clock()
        age = None if self.book.last_update_monotonic is None else round(now - self.book.last_update_monotonic, 2)
        top = self.book.top(LEVELS_SENT)
        return {"type": "depth", "symbol": self.symbol, "stale": age is None or age > STALE_AFTER_SECONDS, "age_seconds": age, "provisional": self.book.provisional, "gaps": self.book.gaps,
                "resyncs": self.resyncs, "bids": top["bids"], "asks": top["asks"], "metrics": self.book.metrics(), "trades": list(self.tape), "mark": dict(self.mark),
                "funding_countdown_seconds": funding_countdown_seconds(time.time()), "funding_schedule": "assumed 8-hourly (00:00, 08:00, 16:00 UTC)",
                "disclaimer": "Decision support, not investment advice."}

    def publish(self) -> None:
        message = self.view()
        for q in list(self.subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()                                       # a slow viewer gets the newest view, never a backlog
            q.put_nowait(message)
        self.dirty = False

    async def resync(self) -> None:
        """Rebuild from the REST snapshot for display, then wait for the socket's fresh snapshot (which carries a sequence number) before applying updates again."""
        self.resyncs += 1
        try:
            bids, asks = await self.rest(self.symbol)
            self.book.load_snapshot(bids, asks, None, self.clock(), provisional=True)
        except Exception:  # noqa: BLE001
            logger.warning("delta_depth_rest_snapshot_failed symbol=%s", self.symbol)
        self.dirty = True

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with self.connect(BOOK_URL) as ws:
                    channels = [{"name": "l2_updates", "symbols": [self.symbol]}, {"name": "all_trades", "symbols": [self.symbol]},
                                {"name": "mark_price", "symbols": [f"MARK:{self.symbol}"]}, {"name": "v2/ticker", "symbols": [self.symbol]}]
                    await ws.send(json.dumps({"type": "subscribe", "payload": {"channels": channels}}))
                    backoff = 1.0
                    async for raw in ws:
                        outcome = self.handle(json.loads(raw))
                        if outcome == "resync":
                            await self.resync()
                            await ws.send(json.dumps({"type": "unsubscribe", "payload": {"channels": [{"name": "l2_updates", "symbols": [self.symbol]}]}}))
                            await ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [{"name": "l2_updates", "symbols": [self.symbol]}]}}))   # a new snapshot with a fresh sequence
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("delta_depth_socket_error symbol=%s", self.symbol)
            self.book.needs_resync = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def publisher(self) -> None:
        while True:
            await asyncio.sleep(1 / RENDER_HZ)
            if self.dirty or any(True for _ in self.subscribers):
                self.publish()                                           # also sent when idle, so the viewer's stale badge keeps moving


class DepthHub:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, connect: Connect = default_connect, rest: RestSnapshot = default_rest_snapshot, start_tasks: bool = True) -> None:
        self.clock, self.connect, self.rest, self.start_tasks = clock, connect, rest, start_tasks
        self.feeds: dict[str, DepthFeed] = {}
        self.reaper: asyncio.Task[None] | None = None

    def subscribe(self, symbol: str) -> asyncio.Queue[dict[str, Any]]:
        symbol = symbol.upper()
        feed = self.feeds.get(symbol)
        if feed is None:
            feed = self.feeds[symbol] = DepthFeed(symbol, self.clock, self.connect, self.rest)
            if self.start_tasks:
                feed.task = asyncio.create_task(self._run(feed), name=f"depth-{symbol}")
        feed.idle_since = None
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)
        feed.subscribers.add(q)
        if self.start_tasks and (self.reaper is None or self.reaper.done()):
            self.reaper = asyncio.create_task(self._reap_loop(), name="depth-reaper")
        return q

    def unsubscribe(self, symbol: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        feed = self.feeds.get(symbol.upper())
        if feed is None:
            return
        feed.subscribers.discard(q)
        if not feed.subscribers:
            feed.idle_since = self.clock()

    async def _run(self, feed: DepthFeed) -> None:
        pub = asyncio.create_task(feed.publisher())
        try:
            await feed.run()
        finally:
            pub.cancel()

    def reap(self) -> list[str]:
        """Drop feeds that have had no viewer for DROP_AFTER_SECONDS. Returns the symbols dropped."""
        dropped = []
        for symbol, feed in list(self.feeds.items()):
            if not feed.subscribers and feed.idle_since is not None and self.clock() - feed.idle_since >= DROP_AFTER_SECONDS:
                if feed.task is not None:
                    feed.task.cancel()
                del self.feeds[symbol]
                dropped.append(symbol)
        return dropped

    async def _reap_loop(self) -> None:
        while self.feeds:
            await asyncio.sleep(5)
            self.reap()


hub = DepthHub()
