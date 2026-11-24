"""Read-only Delta India order-book and trade recorder (public websocket, no keys, no order code).

Writes, per symbol and UTC day, gzip JSON-lines files:  <dir>/<SYMBOL>/book-YYYY-MM-DD.jsonl.gz  (top 10 levels each side, once a second)
and  <dir>/<SYMBOL>/trades-YYYY-MM-DD.jsonl.gz  (every trade).  Gaps and reconnects go to <dir>/gaps.log; <dir>/status.json is the health check.
After a couple of weeks `measure_costs.py` turns these into measured spread and impact for the assumed 1 bp slippage.
Decision support, not investment advice."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
BOOK_URL = "wss://socket.india.delta.exchange"            # l2_orderbook is served here
TRADES_URL = "wss://public-socket.india.delta.exchange"   # trades are served here
TOP = 10
GAP_SECONDS = 5.0


class DailyFiles:
    """Append-only gzip files rotated at 00:00 UTC; each append is its own gzip member, so a crash never corrupts earlier data."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, symbol: str, kind: str, now: float) -> Path:
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        p = self.root / symbol / f"{kind}-{day}.jsonl.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write(self, symbol: str, kind: str, now: float, rows: list[dict]) -> None:
        if rows:
            with gzip.open(self.path(symbol, kind, now), "ab") as fh:
                fh.write(("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n").encode())


class Recorder:
    def __init__(self, symbols: list[str], root: Path, clock: Callable[[], float] = time.time):
        self.symbols, self.root, self.clock = list(symbols), Path(root), clock
        self.files = DailyFiles(self.root)
        self.latest_book: dict[str, dict] = {}
        self.last_book_msg: dict[str, float] = {}
        self.last_trade_msg: dict[str, float] = {}
        self.trade_buffer: dict[str, list[dict]] = {s: [] for s in symbols}
        self.counts = {"book_rows": 0, "trades": 0, "reconnects": 0, "gaps": 0}
        self.connected = {"book": False, "trades": False}
        self.gap_open: dict[str, float | None] = {s: None for s in symbols}
        self.started = self.clock()

    # ---- messages ----
    def handle_message(self, msg: dict[str, Any]) -> None:
        now = self.clock()
        kind = msg.get("type") or msg.get("channel")
        if kind == "l2_orderbook" or ("buy" in msg and "sell" in msg):
            sym = msg.get("symbol")
            if sym in self.symbols:
                self.latest_book[sym] = {"t": now, "bids": [[l["limit_price"], l["size"]] for l in (msg.get("buy") or [])[:TOP]], "asks": [[l["limit_price"], l["size"]] for l in (msg.get("sell") or [])[:TOP]],
                                         "exch_ts": msg.get("timestamp")}
                self.last_book_msg[sym] = now
        elif kind == "trades":
            sym = msg.get("sy")
            if sym in self.symbols:
                self.last_trade_msg[sym] = now
                self.trade_buffer[sym].append({"t": now, "ts": msg.get("ts"), "tt": msg.get("t"), "p": msg.get("p"), "s": msg.get("s"), "r": msg.get("r")})   # r: m = buyer was maker (a sell hit it) as Delta reports

    # ---- periodic work ----
    def sample_books(self) -> None:
        """Once a second: write the latest top-10 snapshot per symbol, or log a gap if the feed went quiet."""
        now = self.clock()
        for sym in self.symbols:
            book = self.latest_book.get(sym)
            last = self.last_book_msg.get(sym)
            if book is None or last is None or now - last > GAP_SECONDS:
                if self.gap_open[sym] is None and self.connected["book"] is not None:
                    self.gap_open[sym] = last or self.started
                    self.log_gap(f"book gap start {sym} since {self.gap_open[sym]:.0f}")
                    self.counts["gaps"] += 1
                continue
            if self.gap_open[sym] is not None:
                self.log_gap(f"book gap end {sym} after {now - self.gap_open[sym]:.0f}s")
                self.gap_open[sym] = None
            self.files.write(sym, "book", now, [{"t": round(now, 3), "bids": book["bids"], "asks": book["asks"]}])
            self.counts["book_rows"] += 1

    def flush_trades(self) -> None:
        for sym, rows in self.trade_buffer.items():
            if rows:
                self.files.write(sym, "trades", rows[-1]["t"], rows)
                self.counts["trades"] += len(rows)
                self.trade_buffer[sym] = []

    def log_gap(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "gaps.log").open("a") as fh:
            fh.write(f"{datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat()} {text}\n")

    def status(self) -> dict:
        now = self.clock()
        return {"as_of": now, "uptime_s": round(now - self.started), "connected": dict(self.connected), "counts": dict(self.counts),
                "seconds_since_last_book": {s: (round(now - t, 1) if (t := self.last_book_msg.get(s)) else None) for s in self.symbols},
                "seconds_since_last_trade": {s: (round(now - t, 1) if (t := self.last_trade_msg.get(s)) else None) for s in self.symbols},
                "healthy": all(self.connected.values()) and all((now - t) < 30 for t in self.last_book_msg.values()) and len(self.last_book_msg) == len(self.symbols)}

    def write_status(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "status.json").write_text(json.dumps(self.status()))

    # ---- connections (reconnect with exponential backoff) ----
    async def _listen(self, name: str, url: str, channel: str, connect: Callable[..., Any]) -> None:
        delay = 1.0
        while True:
            try:
                async with connect(url, ping_interval=20, ping_timeout=20, max_size=1024 * 1024) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [{"name": channel, "symbols": self.symbols}]}}))
                    self.connected[name] = True
                    delay = 1.0
                    async for raw in ws:
                        try:
                            self.handle_message(json.loads(raw))
                        except (ValueError, TypeError, KeyError):
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any connection failure: log, back off, reconnect
                logger.warning("recorder %s connection lost (%s)", name, type(error).__name__)
            self.connected[name] = False
            self.counts["reconnects"] += 1
            self.log_gap(f"{name} connection lost; reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)

    async def run(self, connect: Callable[..., Any] | None = None, stop: asyncio.Event | None = None) -> None:
        if connect is None:
            import websockets
            connect = websockets.connect
        tasks = [asyncio.create_task(self._listen("book", BOOK_URL, "l2_orderbook", connect)), asyncio.create_task(self._listen("trades", TRADES_URL, "trades", connect))]
        try:
            while not (stop and stop.is_set()):
                await asyncio.sleep(1.0)
                self.sample_books()
                self.flush_trades()
                if int(self.clock()) % 10 == 0:
                    self.write_status()
        finally:
            for t in tasks:
                t.cancel()
            self.flush_trades()
            self.write_status()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Record Delta India L2 book snapshots and trades (read-only).")
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parents[3] / "data" / "delta_book")); ap.add_argument("--symbols", default="BTCUSD,ETHUSD")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(Recorder(a.symbols.split(","), Path(a.dir)).run())


if __name__ == "__main__":
    main()
