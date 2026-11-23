from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


TICKER_FIELDS = ("open", "high", "low", "close", "ltp_change_24h", "mark_change_24h", "funding_rate", "oi_value_usd", "oi_change_usd_6h", "turnover_usd",
                 "underlying_asset_symbol", "leverage", "contract_value", "tick_size")


def _list_item(row: sqlite3.Row, slim: bool = False) -> dict[str, Any]:
    """One watch row: the stored columns plus the ticker fields the Crypto watch shows (a missing field stays null, never zero)."""
    item = dict(row)
    raw = item.pop("raw_quote", None)
    try:
        quote = json.loads(raw) if raw else {}
    except ValueError:
        quote = {}
    book = quote.get("quotes") if isinstance(quote.get("quotes"), dict) else {}
    item["ticker"] = {k: quote.get(k) for k in TICKER_FIELDS} | {"best_bid": book.get("best_bid"), "best_ask": book.get("best_ask"), "tags": quote.get("tags")}
    item.pop("metadata_json", None) if slim else None
    return item if slim else item | {"metadata": json.loads(item["metadata_json"])}


class DeltaResearchStore:
    """Isolated public-market research storage; no account or execution state."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError("research database does not exist")
            uri = f"file:{quote(str(self.path.resolve()))}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if read_only:
            self.connection.execute("PRAGMA query_only = ON")
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if version > 1:
                self.connection.close()
                raise RuntimeError(
                    f"Delta research schema {version} is newer than supported version 1"
                )
        else:
            try:
                self._initialize()
            except (sqlite3.Error, RuntimeError):
                self.connection.close()
                raise

    def __enter__(self) -> DeltaResearchStore:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _initialize(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > 1:
            raise RuntimeError(
                f"Delta research schema {version} is newer than supported version 1"
            )
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                contract_type TEXT NOT NULL,
                state TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS products_symbol_idx ON products(symbol);
            CREATE TABLE IF NOT EXISTS quotes (
                product_id TEXT PRIMARY KEY REFERENCES products(product_id),
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                last_price TEXT,
                mark_price TEXT,
                index_price TEXT,
                volume_24h TEXT,
                raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS quotes_latest_idx
                ON quotes(product_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS candles (
                product_id TEXT NOT NULL REFERENCES products(product_id),
                timeframe TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                open TEXT NOT NULL,
                high TEXT NOT NULL,
                low TEXT NOT NULL,
                close TEXT NOT NULL,
                volume TEXT,
                source TEXT NOT NULL,
                PRIMARY KEY(product_id, timeframe, observed_at)
            );
            CREATE INDEX IF NOT EXISTS candles_latest_idx
                ON candles(product_id, timeframe, observed_at DESC);
            CREATE TABLE IF NOT EXISTS market_events (
                id INTEGER PRIMARY KEY,
                product_id TEXT,
                symbol TEXT,
                event_type TEXT NOT NULL,
                received_at TEXT NOT NULL,
                exchange_time TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS market_events_time_idx
                ON market_events(received_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS feed_health (
                stream_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                connected INTEGER NOT NULL,
                reconnects INTEGER NOT NULL,
                consecutive_failures INTEGER NOT NULL,
                dropped_events INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                last_event_at TEXT,
                last_error TEXT
            );
            PRAGMA user_version = 1;
        """)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def save_products(self, rows: list[dict[str, Any]]) -> None:
        now = _iso()
        with self.connection:
            self.connection.executemany("""
                INSERT INTO products
                (product_id,symbol,contract_type,state,metadata_json,observed_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(product_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    contract_type=excluded.contract_type,
                    state=excluded.state,
                    metadata_json=excluded.metadata_json,
                    observed_at=excluded.observed_at
            """, [
                (
                    str(row["id"]), str(row["symbol"]),
                    str(row.get("contract_type") or "unknown"),
                    str(row.get("state") or "unknown"),
                    json.dumps(row, separators=(",", ":"), sort_keys=True, default=str),
                    now,
                )
                for row in rows
                if row.get("id") is not None and row.get("symbol")
            ])

    def product_id_for_symbol(self, symbol: str) -> str | None:
        row = self.connection.execute(
            "SELECT product_id FROM products WHERE symbol=? ORDER BY product_id LIMIT 1",
            (symbol,),
        ).fetchone()
        return str(row["product_id"]) if row else None

    def save_quote(self, row: dict[str, Any], observed_at: datetime, source: str) -> None:
        self.save_quotes([row], observed_at, source)

    def save_quotes(
        self, rows: list[dict[str, Any]], observed_at: datetime, source: str
    ) -> None:
        timestamp = _iso(observed_at)
        values = []
        for row in rows:
            product_id = row.get("product_id")
            if product_id is None:
                product_id = self.product_id_for_symbol(str(row.get("symbol") or ""))
            if product_id is None:
                continue
            values.append((
                str(product_id), timestamp, source,
                _decimal_text(row.get("close", row.get("last_price"))),
                _decimal_text(row.get("mark_price")),
                _decimal_text(row.get("spot_price", row.get("index_price"))),
                _decimal_text(row.get("turnover_usd", row.get("volume_24h"))),
                json.dumps(row, separators=(",", ":"), sort_keys=True, default=str),
            ))
        if not values:
            return
        with self.connection:
            self.connection.executemany("""
                INSERT OR REPLACE INTO quotes
                (product_id,observed_at,source,last_price,mark_price,index_price,
                 volume_24h,raw_json) VALUES (?,?,?,?,?,?,?,?)
            """, values)

    def live_symbols(self) -> list[str]:
        """Symbols of live perpetuals, for the websocket subscription (Delta's `ticker` channel needs them listed; \"all\" returns nothing)."""
        return [str(r[0]) for r in self.connection.execute("SELECT symbol FROM products WHERE contract_type='perpetual_futures' AND state='live' ORDER BY symbol")]

    def save_live_ticks(self, messages: list[dict[str, Any]], observed_at: datetime, source: str, keep_price_for: frozenset[str] = frozenset()) -> int:
        """Merge compact live `ticker` frames into each contract's stored quote: price, mark, 24h range, open interest and best bid/ask move; everything else
        (funding, turnover, 6h open-interest change) keeps the last full REST value. A field the frame lacks stays as it was, never zero."""
        def dec(value: Any) -> Decimal | None:
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return number if number.is_finite() else None

        parsed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for message in messages:
            for row in message.get("d") if isinstance(message.get("d"), list) else []:
                ohlc = row.get("ohlc") if isinstance(row, dict) else None
                if not isinstance(ohlc, list) or len(ohlc) != 4 or dec(ohlc[3]) is None:
                    continue
                product_id = row.get("i") if row.get("i") is not None else self.product_id_for_symbol(str(message.get("sy") or ""))
                if product_id is not None:
                    parsed.append((str(product_id), message, row))
        if not parsed:
            return 0
        ids = [p[0] for p in parsed]
        existing = {r[0]: r[1] for r in self.connection.execute(f"SELECT product_id,raw_json FROM quotes WHERE product_id IN ({','.join('?' * len(ids))})", ids)}
        timestamp, values = _iso(observed_at), []
        for product_id, message, row in parsed:
            try:
                raw = json.loads(existing.get(product_id) or "{}")
            except ValueError:
                raw = {}
            o, h, low, c = (dec(x) for x in row["ohlc"])
            if str(message.get("sy")) in keep_price_for and dec(raw.get("close")) is not None:
                c = dec(raw.get("close"))                         # a newer trade price is already stored; the 6-second summary must not roll it back
            mark = dec(row.get("m"))
            raw.update(close=str(c), mark_price=str(mark) if mark is not None else raw.get("mark_price"), spot_price=str(message.get("sp")) if message.get("sp") is not None else raw.get("spot_price"))
            for key, value in (("open", o), ("high", h), ("low", low)):
                if value is not None:
                    raw[key] = str(value)
            if o is not None and o != 0:
                raw["ltp_change_24h"] = str(((c - o) / o * 100).quantize(Decimal("0.0001")))
            if dec(row.get("m24hc")) is not None:
                raw["mark_change_24h"] = str(dec(row.get("m24hc")))
            oi = row.get("oi")
            contracts, value_unit = (dec(oi[0]) if isinstance(oi, list) and oi else None), dec(raw.get("contract_value"))
            if contracts is not None and value_unit is not None and mark is not None:
                raw["oi_contracts"] = str(contracts)
                raw["oi_value_usd"] = str((contracts * value_unit * mark).quantize(Decimal("0.01")))
            q = row.get("q")
            if isinstance(q, list) and len(q) >= 4 and dec(q[0]) is not None and dec(q[2]) is not None:
                raw["quotes"] = {**(raw.get("quotes") if isinstance(raw.get("quotes"), dict) else {}), "best_ask": str(dec(q[0])), "best_bid": str(dec(q[2])),
                                 "ask_size": str(q[1]), "bid_size": str(q[3])}
            raw["time"] = timestamp
            values.append((product_id, timestamp, source, str(c), raw.get("mark_price"), raw.get("spot_price"), raw.get("turnover_usd"), json.dumps(raw, separators=(",", ":"), sort_keys=True, default=str)))
        with self.connection:
            self.connection.executemany("INSERT OR REPLACE INTO quotes (product_id,observed_at,source,last_price,mark_price,index_price,volume_24h,raw_json) VALUES (?,?,?,?,?,?,?,?)", values)
        return len(values)

    def save_live_trades(self, trades: dict[str, dict[str, Any]], observed_at: datetime, source: str) -> int:
        """Apply the newest trade price per contract: last price, the 24h range it extends, and the 24h change from the stored open. Contracts without a stored quote are skipped."""
        symbols = list(trades)
        if not symbols:
            return 0
        rows = self.connection.execute(
            f"SELECT p.symbol,q.product_id,q.raw_json,q.mark_price,q.index_price,q.volume_24h FROM quotes q JOIN products p ON p.product_id=q.product_id WHERE p.symbol IN ({','.join('?' * len(symbols))})", symbols).fetchall()
        timestamp, values = _iso(observed_at), []
        for symbol, product_id, raw_json, mark, spot, volume in rows:
            try:
                price = Decimal(str(trades[symbol]["p"]))
                raw = json.loads(raw_json or "{}")
            except (InvalidOperation, ValueError, KeyError):
                continue
            if not price.is_finite() or price <= 0:
                continue
            raw["close"] = str(price)
            for key, better in (("high", price.__gt__), ("low", price.__lt__)):
                try:
                    current = Decimal(str(raw.get(key)))
                except InvalidOperation:
                    continue
                if current.is_finite() and better(current):
                    raw[key] = str(price)
            try:
                opened = Decimal(str(raw.get("open")))
                if opened.is_finite() and opened != 0:
                    raw["ltp_change_24h"] = str(((price - opened) / opened * 100).quantize(Decimal("0.0001")))
            except InvalidOperation:
                pass
            raw["time"] = timestamp
            values.append((str(product_id), timestamp, source, str(price), mark, spot, volume, json.dumps(raw, separators=(",", ":"), sort_keys=True, default=str)))
        with self.connection:
            self.connection.executemany("INSERT OR REPLACE INTO quotes (product_id,observed_at,source,last_price,mark_price,index_price,volume_24h,raw_json) VALUES (?,?,?,?,?,?,?,?)", values)
        return len(values)

    def save_candle(
        self,
        product_id: str,
        timeframe: str,
        row: dict[str, Any],
        observed_at: datetime,
        source: str,
    ) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT OR REPLACE INTO candles
                (product_id,timeframe,observed_at,open,high,low,close,volume,source)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                product_id, timeframe, _iso(observed_at),
                _decimal_text(row["open"]), _decimal_text(row["high"]),
                _decimal_text(row["low"]), _decimal_text(row["close"]),
                _decimal_text(row.get("volume")),
                source,
            ))

    def save_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        product_id: str | None = None,
        symbol: str | None = None,
        exchange_time: datetime | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT INTO market_events
                (product_id,symbol,event_type,received_at,exchange_time,payload_json)
                VALUES (?,?,?,?,?,?)
            """, (
                product_id, symbol, event_type, _iso(),
                _iso(exchange_time) if exchange_time else None,
                json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
            ))
            self.connection.execute("""
                DELETE FROM market_events
                WHERE id NOT IN (
                    SELECT id FROM market_events ORDER BY id DESC LIMIT 10000
                )
            """)

    def save_health(
        self,
        *,
        state: str,
        connected: bool,
        reconnects: int,
        consecutive_failures: int,
        dropped_events: int,
        last_event_at: datetime | None,
        last_error: str | None,
    ) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT OR REPLACE INTO feed_health
                (stream_id,state,connected,reconnects,consecutive_failures,dropped_events,
                 observed_at,last_event_at,last_error)
                VALUES ('public',?,?,?,?,?,?,?,?)
            """, (
                state, int(connected), reconnects, consecutive_failures,
                dropped_events, _iso(), _iso(last_event_at) if last_event_at else None,
                last_error,
            ))

    def summary(self) -> dict[str, Any]:
        counts = {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("products", "quotes", "candles", "market_events")
        }
        health = self.connection.execute(
            "SELECT * FROM feed_health WHERE stream_id='public'"
        ).fetchone()
        return {
            **counts,
            "latest_quote_at": self._max("quotes", "observed_at"),
            "latest_candle_at": self._max("candles", "observed_at"),
            "feed_health": dict(health) if health else None,
        }

    def list_products(self, search: str, limit: int, offset: int, slim: bool = False, since: str | None = None) -> dict[str, Any]:
        conditions, args = [], []
        if search:
            conditions.append("lower(p.symbol) LIKE ?"); args.append(f"%{search.casefold()}%")
        if since:
            conditions.append("q.observed_at > ?"); args.append(since)          # only contracts whose quote changed after the caller's newest one
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params: tuple[Any, ...] = tuple(args)
        total = int(self.connection.execute(
            f"SELECT COUNT(*) FROM products p LEFT JOIN quotes q ON q.product_id=p.product_id {where}", params
        ).fetchone()[0])
        rows = self.connection.execute(f"""
            SELECT p.product_id,p.symbol,p.contract_type,p.state,p.metadata_json,
                   q.observed_at AS quote_time,q.last_price,q.mark_price,q.index_price,
                   q.volume_24h,q.source AS quote_source,q.raw_json AS raw_quote
            FROM products p
            LEFT JOIN quotes q ON q.product_id=p.product_id
              AND q.observed_at=(SELECT MAX(q2.observed_at) FROM quotes q2
                                 WHERE q2.product_id=p.product_id)
            {where}
            ORDER BY p.symbol,p.product_id LIMIT ? OFFSET ?
        """, (*params, limit, offset)).fetchall()
        return {
            "items": [_list_item(row, slim) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def product_detail(self, product_id: str, candle_limit: int) -> dict[str, Any] | None:
        product = self.connection.execute(
            "SELECT * FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if product is None:
            return None
        quote = self.connection.execute("""
            SELECT observed_at,source,last_price,mark_price,index_price,volume_24h,raw_json
            FROM quotes WHERE product_id=? ORDER BY observed_at DESC LIMIT 1
        """, (product_id,)).fetchone()
        candles = self.connection.execute("""
            SELECT timeframe,observed_at,open,high,low,close,volume,source
            FROM candles WHERE product_id=? ORDER BY observed_at DESC LIMIT ?
        """, (product_id, candle_limit)).fetchall()
        return {
            "product": dict(product) | {"metadata": json.loads(product["metadata_json"])},
            "quote": dict(quote) | {"raw": json.loads(quote["raw_json"])} if quote else None,
            "candles": [dict(row) for row in reversed(candles)],
        }

    def list_events(self, limit: int, before_id: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE id < ?" if before_id is not None else ""
        params: tuple[Any, ...] = (before_id, limit) if before_id is not None else (limit,)
        rows = self.connection.execute(f"""
            SELECT id,product_id,symbol,event_type,received_at,exchange_time,payload_json
            FROM market_events {where} ORDER BY id DESC LIMIT ?
        """, params).fetchall()
        return [
            dict(row) | {"payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def _max(self, table: str, column: str) -> str | None:
        row = self.connection.execute(
            f"SELECT MAX({column}) AS value FROM {table}"
        ).fetchone()
        return row["value"] if row else None


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("market data numeric field is invalid") from None
    if not number.is_finite():
        raise ValueError("market data numeric field must be finite")
    return format(number, "f")
