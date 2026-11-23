from __future__ import annotations

import asyncio
import random
import json
import logging
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.config import settings
from app.delta_research.store import DeltaResearchStore
from app.ws_diagnostics import describe_ws_failure

logger = logging.getLogger(__name__)

REST_URL = "https://api.india.delta.exchange"
PUBLIC_WS_URL = "wss://public-socket.india.delta.exchange"
MAX_CONSECUTIVE_FAILURES = 8
MAX_RECONNECT_SECONDS = 60
# Delta sends heartbeats about every 30 s once enabled; silence beyond this means a dead link
# that TCP/ping keepalive has not yet noticed, so reconnect instead of waiting indefinitely.
WS_IDLE_TIMEOUT_SECONDS = 75  # assumes ~30 s heartbeats; the all-symbol ticker normally sends far more often
STREAM_STALE_AFTER_SECONDS = 90
MAX_SUBSCRIBERS = 32
SUBSCRIBER_QUEUE_SIZE = 256
WRITE_QUEUE_SIZE = 128
PRODUCT_REFRESH_SECONDS = 6 * 60 * 60
TICK_FLUSH_SECONDS = 1


def reconnect_delay(consecutive_failures: int) -> float:
    """Bounded exponential backoff with jitter (50-100% of the capped step)."""
    step = min(2 ** max(0, consecutive_failures - 1), MAX_RECONNECT_SECONDS)
    return random.uniform(step * 0.5, step)


def classify_stream_state(connected: bool, last_event_at: datetime | None, now: datetime,
                          state: str, stale_after: float = STREAM_STALE_AFTER_SECONDS) -> str:
    """connected | reconnecting | unavailable | stale. Only "connected" means live data."""
    if connected:
        if last_event_at is None or (now - last_event_at).total_seconds() > stale_after:
            return "stale"
        return "connected"
    if state in {"unavailable", "stopped", "disabled", "not_started"}:
        return "unavailable"
    return "reconnecting"


def _event_time(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if numeric <= 0:
        return None
    unit_divisor = 1_000_000 if numeric > 1e14 else 1000 if numeric > 1e11 else 1
    try:
        return datetime.fromtimestamp(numeric / unit_divisor, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


class DeltaPublicCollector:
    def __init__(
        self,
        path: str | Path,
        *,
        refresh_seconds: int | None = None,
        websocket_url: str = PUBLIC_WS_URL,
        rest_url: str = REST_URL,
    ):
        self.path = Path(path)
        self.refresh_seconds = max(30, refresh_seconds or settings.delta_research_refresh_seconds)
        self.websocket_url = websocket_url
        self.rest_url = rest_url.rstrip("/")
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._write_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(WRITE_QUEUE_SIZE)
        self._write_lock = threading.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._connected = False
        self._state = "starting"
        self._reconnects = 0
        self._consecutive_failures = 0
        self._dropped_events = 0
        self._last_event_at: datetime | None = None
        self._last_error: str | None = None
        self._last_failure_reason: str | None = None
        self._last_failure_at: datetime | None = None
        self._last_logged_error: str | None = None
        self._source_errors: dict[str, str] = {}
        self._last_rest_success_at: datetime | None = None
        self._last_health_write_at = 0.0
        self._started = False
        self._latest_ticks: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        async with httpx.AsyncClient(
            base_url=self.rest_url,
            timeout=httpx.Timeout(15),
            headers={"Accept": "application/json"},
            trust_env=False,
        ) as client:
            self._tasks = [
                asyncio.create_task(self._rest_loop(client), name="delta-rest-collector"),
                asyncio.create_task(self._websocket_loop(), name="delta-public-stream"),
                asyncio.create_task(self._writer_loop(), name="delta-sqlite-writer"),
                asyncio.create_task(self._tick_flush_loop(), name="delta-tick-flush"),
            ]
            await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks[:2]:
            task.cancel()
        for task in self._tasks[:2]:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if len(self._tasks) > 2:
            await self._tasks[2]
        for task in self._tasks[3:]:
            await task
        self._tasks.clear()
        self._started = False
        self._connected = False
        self._state = "stopped"
        await asyncio.to_thread(self._save_health, force=True)

    def status(self) -> dict[str, Any]:
        store_summary: dict[str, Any] | None = None
        if self.path.is_file():
            try:
                with DeltaResearchStore(self.path, read_only=True) as store:
                    store_summary = store.summary()
            except (OSError, RuntimeError, sqlite3.Error, ValueError):
                store_summary = None
        stream_state = classify_stream_state(
            self._connected, self._last_event_at, datetime.now(timezone.utc), self._state
        )
        return {
            "enabled": settings.delta_research_enabled,
            "configured": bool(settings.delta_research_db_path),
            "state": self._state,
            "connected": self._connected,
            "stream_state": stream_state,
            "is_live": stream_state == "connected",
            "reconnects": self._reconnects,
            "consecutive_failures": self._consecutive_failures,
            "dropped_events": self._dropped_events,
            "last_event_at": self._last_event_at,
            "last_error": self._last_error,
            "last_failure_reason": self._last_failure_reason,
            "last_failure_at": self._last_failure_at,
            "store": store_summary,
        }

    def read_store(self) -> DeltaResearchStore:
        if not settings.delta_research_db_path:
            raise FileNotFoundError("research data store is not configured")
        return DeltaResearchStore(settings.delta_research_db_path, read_only=True)

    def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            raise RuntimeError("maximum Delta research stream subscribers reached")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)

        async def messages() -> AsyncIterator[dict[str, Any]]:
            try:
                yield {"event": "snapshot", "data": await asyncio.to_thread(self.status)}
                while not self._stop.is_set():
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield {"event": "heartbeat", "data": {"at": datetime.now(timezone.utc)}}
                    else:
                        yield message
            finally:
                self._subscribers.discard(queue)

        return messages()

    async def _rest_loop(self, client: httpx.AsyncClient) -> None:
        last_products: float | None = None      # None = never fetched (time.monotonic() counts from boot, so 0.0 would skip the first fetch on a young machine)
        while not self._stop.is_set():
            try:
                if last_products is None or time.monotonic() - last_products >= PRODUCT_REFRESH_SECONDS:
                    products = await self._fetch_products(client)
                    await asyncio.to_thread(self._persist_products, products)
                    last_products = time.monotonic()
                tickers = await self._fetch_tickers(client)
                observed_at = datetime.now(timezone.utc)
                await asyncio.to_thread(self._persist_rest_snapshot, tickers, observed_at)
                if self._state != "unavailable":
                    self._state = "connected" if self._connected else "collecting"
                    self._source_errors.pop("rest", None)
                    self._last_rest_success_at = datetime.now(timezone.utc)
                    self._refresh_state()
                    await self._publish({
                        "event": "snapshot",
                        "data": await asyncio.to_thread(self.status),
                    })
            except asyncio.CancelledError:
                raise
            except (
                httpx.HTTPError, ValueError, KeyError, TypeError, OSError,
                RuntimeError, sqlite3.Error,
            ) as exc:
                await self._record_failure(exc, "rest")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_seconds)
            except TimeoutError:
                pass

    async def _fetch_products(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            params: dict[str, Any] = {
                "contract_types": "perpetual_futures",
                "states": "upcoming,live,expired,settled",
                "page_size": 1000,
            }
            if cursor:
                params["after"] = cursor
            response = await client.get("/v2/products", params=params)
            response.raise_for_status()
            payload = await asyncio.to_thread(response.json, parse_float=Decimal)
            batch = payload.get("result")
            if not isinstance(batch, list):
                raise ValueError("Delta product catalog response is not a list")
            products.extend(await asyncio.to_thread(
                lambda page: [
                    item for item in page
                    if isinstance(item, dict)
                    and item.get("contract_type") == "perpetual_futures"
                    and item.get("id") is not None
                    and item.get("symbol")
                ],
                batch,
            ))
            next_cursor = (payload.get("meta") or {}).get("after")
            if not next_cursor:
                if len(batch) >= params["page_size"]:
                    raise ValueError("Delta product page omitted its next cursor")
                return products
            next_cursor = str(next_cursor)
            if next_cursor == cursor or next_cursor in seen:
                raise ValueError("Delta product cursor did not advance")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ValueError("Delta product catalog exceeded the page safety limit")

    def _persist_products(self, products: list[dict[str, Any]]) -> None:
        with self._write_lock, DeltaResearchStore(self.path) as store:
            store.save_products(products)

    def _persist_rest_snapshot(
        self, tickers: list[dict[str, Any]], observed_at: datetime
    ) -> None:
        with self._write_lock, DeltaResearchStore(self.path) as store:
            store.save_quotes(tickers, observed_at, "delta_rest_ticker")
            store.save_event(
                "rest_snapshot",
                {"ticker_count": len(tickers), "source": "delta_rest_ticker"},
            )

    async def _fetch_tickers(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.get(
            "/v2/tickers", params={"contract_types": "perpetual_futures"}
        )
        response.raise_for_status()
        rows = (await asyncio.to_thread(response.json, parse_float=Decimal)).get("result")
        if not isinstance(rows, list):
            raise ValueError("Delta ticker response is not a list")
        return await asyncio.to_thread(
            lambda values: [
                row for row in values
                if isinstance(row, dict) and row.get("symbol")
            ],
            rows,
        )

    async def _websocket_loop(self) -> None:
        # Never gives up permanently: after repeated failures the stream is reported
        # "unavailable" and retried at the capped interval, so it recovers on its own.
        while not self._stop.is_set():
            try:
                async with connect(
                    self.websocket_url,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=512 * 1024,
                ) as websocket:
                    await websocket.send(json.dumps({"type": "enable_heartbeat"}))
                    await websocket.send(json.dumps({
                        "type": "subscribe",
                        "payload": {
                            "channels": [
                                {"name": "v2/ticker", "symbols": ["all"]},
                                {
                                    "name": "candlestick_1m",
                                    "symbols": ["perpetual_futures"],
                                },
                            ]
                        },
                    }))
                    self._connected = True
                    self._refresh_state()
                    await asyncio.to_thread(self._save_health, force=True)
                    await self._publish({
                        "event": "health",
                        "data": await asyncio.to_thread(self.status),
                    })
                    while True:
                        try:
                            frame = await asyncio.wait_for(websocket.recv(), WS_IDLE_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError:
                            raise ConnectionError("Delta public websocket idle: no frame or heartbeat") from None
                        await self._handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure must reconnect, never kill the collector
                if self._stop.is_set():
                    # Application shutdown (e.g. uvicorn --reload) closes the socket itself; it is not an upstream failure.
                    logger.info("Delta public websocket closed by application shutdown")
                    break
                await self._record_failure(exc, "websocket")
                self._reconnects += 1
            finally:
                self._connected = False
            if self._stop.is_set():
                break
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._state = "unavailable"
            else:
                self._state = "reconnecting"
            await asyncio.to_thread(self._save_health, force=True)
            await self._publish({
                "event": "health",
                "data": await asyncio.to_thread(self.status),
            })
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=reconnect_delay(self._consecutive_failures))
            except TimeoutError:
                pass

    async def _handle_frame(self, frame: str | bytes) -> None:
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8")
        message = await asyncio.to_thread(json.loads, frame, parse_float=Decimal)
        if not isinstance(message, dict):
            raise ValueError("Delta public message must be an object")
        message_type = message.get("type")
        if message_type == "heartbeat":
            await self._mark_event()
            return
        if message_type in {"subscriptions", "pong"}:
            return
        if message_type == "v2/ticker":
            symbol = str(message.get("symbol") or "")
            if symbol:
                self._latest_ticks[symbol] = message     # only the newest tick per contract is kept; a batch writer stores them (cheap, and bounded)
                await self._mark_event()
            return
        if message_type not in {"ticker", "candlestick_1m"}:
            return
        symbol = str(message.get("sy") or "")
        if not symbol:
            return
        try:
            self._write_queue.put_nowait(message)
        except asyncio.QueueFull:
            self._dropped_events += 1
            await asyncio.to_thread(self._save_health, force=True)
            await self._publish({
                "event": "health",
                "data": await asyncio.to_thread(self.status),
            })
            return
        await self._mark_event()

    async def _tick_flush_loop(self) -> None:
        """Store the newest live ticker of every contract in ONE write every TICK_FLUSH_SECONDS, so the watch glows on real price changes without a write per frame."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_FLUSH_SECONDS)
            except TimeoutError:
                pass
            if not self._latest_ticks:
                continue
            rows, self._latest_ticks = list(self._latest_ticks.values()), {}
            try:
                await asyncio.to_thread(self._persist_ticks, rows)
            except (OSError, RuntimeError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                await self._record_failure(exc, "storage")

    def _persist_ticks(self, rows: list[dict[str, Any]]) -> None:
        with self._write_lock, DeltaResearchStore(self.path) as store:
            store.save_quotes(rows, datetime.now(timezone.utc), "delta_public_ws_v2_ticker")

    async def _writer_loop(self) -> None:
        while not self._stop.is_set() or not self._write_queue.empty():
            try:
                message = await asyncio.wait_for(self._write_queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                await asyncio.to_thread(self._persist_message, message)
                self._source_errors.pop("storage", None)
                self._refresh_state()
                await asyncio.to_thread(self._save_health)
                await self._publish({
                    "event": "market_data",
                    "data": {
                        "type": message["type"],
                        "symbol": message["sy"],
                        "received_at": datetime.now(timezone.utc),
                        "last_event_at": self._last_event_at,
                    },
                })
            except (
                OSError, RuntimeError, sqlite3.Error, ValueError, TypeError, KeyError,
            ) as exc:
                await self._record_failure(exc, "storage")
            finally:
                self._write_queue.task_done()

    def _persist_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        symbol = str(message.get("sy") or "")
        observed_at = _event_time(message.get("ts"))
        with self._write_lock, DeltaResearchStore(self.path) as store:
            product_id = store.product_id_for_symbol(symbol)
            if message_type == "ticker":
                store.save_event(
                    "ticker", message, product_id=product_id, symbol=symbol,
                    exchange_time=observed_at,
                )
                rows = message.get("d")
                if isinstance(rows, list):
                    quotes = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        ohlc = row.get("ohlc")
                        if not isinstance(ohlc, list) or len(ohlc) != 4 or ohlc[3] is None:
                            continue
                        quotes.append({
                            "product_id": row.get("i", product_id),
                            "symbol": symbol,
                            "close": ohlc[3],
                            "mark_price": row.get("m"),
                            "spot_price": message.get("sp"),
                            "raw": row,
                        })
                    store.save_quotes(
                        quotes,
                        observed_at or datetime.now(timezone.utc),
                        "delta_public_ws_ticker_24h_close",
                    )
            elif product_id is not None:
                required = {"o", "h", "l", "c", "res"}
                if required.issubset(message):
                    observed_time = observed_at or datetime.now(timezone.utc)
                    store.save_candle(
                        product_id,
                        str(message["res"]),
                        {
                            "open": message["o"],
                            "high": message["h"],
                            "low": message["l"],
                            "close": message["c"],
                            "volume": message.get("v"),
                        },
                        observed_time,
                        "delta_public_ws_candlestick",
                    )
                store.save_event(
                    "candlestick_1m", message, product_id=product_id, symbol=symbol,
                    exchange_time=observed_at,
                )

    async def _mark_event(self) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        self._consecutive_failures = 0
        self._source_errors.pop("websocket", None)
        self._refresh_state()

    async def _record_failure(self, error: Exception, source: str) -> None:
        if source == "websocket":
            self._consecutive_failures += 1
        reason = describe_ws_failure(error) if source == "websocket" else type(error).__name__
        self._source_errors[source] = reason
        self._last_failure_reason = reason
        self._last_failure_at = datetime.now(timezone.utc)
        self._refresh_state()
        if self._last_error != self._last_logged_error:
            logger.warning(
                "Delta public collector error source=%s reason=%s",
                source,
                self._source_errors.get(source, type(error).__name__),
            )
            self._last_logged_error = self._last_error
        await asyncio.to_thread(self._save_health, force=True)

    def _refresh_state(self) -> None:
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self._connected:
            self._state = "unavailable"
            return
        self._last_error = "; ".join(
            f"{source}: {error}"
            for source, error in sorted(self._source_errors.items())
        ) or None
        if self._source_errors:
            self._state = "degraded"
        elif self._connected:
            self._state = "connected"
        elif self._consecutive_failures:
            self._state = "reconnecting"
        elif self._last_rest_success_at is not None:
            self._state = "collecting"
        else:
            self._state = "starting"

    def _save_health(self, force: bool = False) -> None:
        if not self.path.is_file():
            return
        now = time.monotonic()
        if not force and now - self._last_health_write_at < 5:
            return
        try:
            with self._write_lock, DeltaResearchStore(self.path) as store:
                store.save_health(
                    state=self._state,
                    connected=self._connected,
                    reconnects=self._reconnects,
                    consecutive_failures=self._consecutive_failures,
                    dropped_events=self._dropped_events,
                    last_event_at=self._last_event_at,
                    last_error=self._last_error,
                )
            self._last_health_write_at = now
        except (OSError, RuntimeError, sqlite3.Error):
            logger.exception("Could not persist Delta public feed health")

    async def _publish(self, message: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._dropped_events += 1
                await asyncio.to_thread(self._save_health)
            queue.put_nowait(message)


FEED_FLAG = "delta_feed"
WATCH_SECONDS = 10


def feed_wanted() -> bool | None:
    """The owner's on/off choice for the public feed (default on). None when the database cannot be read, so nothing changes."""
    try:
        from sqlalchemy.orm import Session

        from app.db import engine
        from app.models.crypto_delta import AppFlag
        with Session(engine) as db:
            row = db.get(AppFlag, FEED_FLAG)
            return row is None or row.value != "off"
    except Exception:  # noqa: BLE001
        return None


def set_feed_wanted(on: bool) -> None:
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session

    from app.db import engine
    from app.models.crypto_delta import AppFlag
    with Session(engine) as db:
        row = db.get(AppFlag, FEED_FLAG)
        now = datetime.now(timezone.utc)
        if row is None:
            db.add(AppFlag(key=FEED_FLAG, value="on" if on else "off", updated_at=now))
        else:
            row.value, row.updated_at = "on" if on else "off", now
        db.commit()


class DeltaResearchManager:
    """Runs the public collector while the owner wants it. A watcher follows the stored on/off choice every few seconds, so the switch works from any request."""

    def __init__(self) -> None:
        self.collector: DeltaPublicCollector | None = None
        self.task: asyncio.Task[None] | None = None
        self.watcher: asyncio.Task[None] | None = None

    def _running(self) -> bool:
        return self.task is not None and not self.task.done()

    def _launch(self) -> None:
        self.collector = DeltaPublicCollector(settings.delta_research_db_path or "")
        self.task = asyncio.create_task(self.collector.start(), name="delta-research-manager")

    async def _halt(self) -> None:
        if self.collector is not None:
            await self.collector.stop()
        if self.task is not None:
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.collector = None

    async def _watch(self) -> None:
        while True:
            wanted = await asyncio.to_thread(feed_wanted)
            if wanted is True and not self._running():
                self._launch()
            elif wanted is False and self._running():
                await self._halt()
            await asyncio.sleep(WATCH_SECONDS)

    async def start(self) -> None:
        if not settings.delta_research_enabled or not settings.delta_research_db_path:
            return
        if self.watcher is None or self.watcher.done():
            self.watcher = asyncio.create_task(self._watch(), name="delta-research-watch")

    async def stop(self) -> None:
        if self.watcher is not None:
            self.watcher.cancel()
            try:
                await self.watcher
            except asyncio.CancelledError:
                pass
            self.watcher = None
        await self._halt()

    def status(self) -> dict[str, Any]:
        if not settings.delta_research_enabled:
            return {
                "enabled": False,
                "configured": bool(settings.delta_research_db_path),
                "state": "disabled",
                "connected": False,
                "reconnects": 0,
                "consecutive_failures": 0,
                "dropped_events": 0,
                "last_event_at": None,
                "last_error": None,
                "store": None,
            }
        if self.collector is None:
            return {
                "enabled": True,
                "configured": bool(settings.delta_research_db_path),
                "state": "paused" if feed_wanted() is False else "not_started",
                "connected": False,
                "reconnects": 0,
                "consecutive_failures": 0,
                "dropped_events": 0,
                "last_event_at": None,
                "last_error": None,
                "store": None,
            }
        return self.collector.status()


delta_research_manager = DeltaResearchManager()
