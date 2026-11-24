"""Local L2 order book for one Delta India perpetual, kept from the public `l2_updates` channel.

Channel facts (checked against the live socket, 24 Nov 2026): wss://socket.india.delta.exchange, subscribe {"name": "l2_updates", "symbols": [..]};
first message action="snapshot" (asks/bids as [price, size] strings), then action="update" messages carrying changed levels (size "0" removes a level),
`sequence_no` (increments by 1), `cs` (checksum) and `timestamp` (microseconds). REST snapshot: GET https://api.india.delta.exchange/v2/l2orderbook/{symbol}.
Rules: apply updates strictly in sequence order; a gap, or a book that would be crossed, discards the book and asks for a resync from the REST snapshot.
Prices and sizes are Decimal. Display only: nothing here places or implies an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

D = Decimal


@dataclass
class OrderBook:
    symbol: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    sequence: int | None = None
    needs_resync: bool = True            # no book yet
    provisional: bool = False            # loaded from the REST snapshot (no sequence number): shown, but updates are not applied until the socket sends a fresh snapshot
    gaps: int = 0
    crossed_rejections: int = 0
    last_update_monotonic: float | None = None

    # ---- loading ----
    @staticmethod
    def _levels(rows: list[Any]) -> dict[Decimal, Decimal]:
        out: dict[Decimal, Decimal] = {}
        for row in rows:
            price, size = (row[0], row[1]) if isinstance(row, (list, tuple)) else (row.get("limit_price") or row.get("price"), row.get("size"))
            if D(str(size)) > 0:
                out[D(str(price))] = D(str(size))
        return out

    def load_snapshot(self, bids: list[Any], asks: list[Any], sequence: int | None, now: float, *, provisional: bool = False) -> bool:
        b, a = self._levels(bids), self._levels(asks)
        if b and a and max(b) >= min(a):                       # never show a crossed book
            self.crossed_rejections += 1
            self.needs_resync = True
            return False
        self.bids, self.asks, self.sequence, self.needs_resync, self.provisional, self.last_update_monotonic = b, a, sequence, provisional, provisional, now
        return True

    def apply_update(self, bids: list[Any], asks: list[Any], sequence: int, now: float) -> str:
        """Returns 'ok', 'stale' (an old message, ignored), 'gap' (resync needed) or 'crossed' (resync needed)."""
        if self.needs_resync or self.sequence is None:
            return "gap"
        if sequence <= self.sequence:
            return "stale"
        if sequence != self.sequence + 1:
            self.gaps += 1
            self.needs_resync = True
            return "gap"
        nb, na = dict(self.bids), dict(self.asks)
        for side, rows in ((nb, bids), (na, asks)):
            for row in rows:
                price, size = D(str(row[0])), D(str(row[1]))
                if size > 0:
                    side[price] = size
                else:
                    side.pop(price, None)
        if nb and na and max(nb) >= min(na):                   # apply nothing: a crossed book is never shown
            self.crossed_rejections += 1
            self.needs_resync = True
            return "crossed"
        self.bids, self.asks, self.sequence, self.last_update_monotonic = nb, na, sequence, now
        return "ok"

    # ---- views ----
    def top(self, n: int = 20) -> dict[str, list[dict[str, str]]]:
        bids = sorted(self.bids.items(), reverse=True)[:n]
        asks = sorted(self.asks.items())[:n]

        def cum(rows: list[tuple[Decimal, Decimal]]) -> list[dict[str, str]]:
            total, out = D(0), []
            for price, size in rows:
                total += size
                out.append({"price": str(price), "size": str(size), "cum": str(total)})
            return out
        return {"bids": cum(bids), "asks": cum(asks)}

    def metrics(self) -> dict[str, str | None]:
        if not self.bids or not self.asks:
            return {"best_bid": None, "best_ask": None, "mid": None, "spread": None, "spread_bps": None, "imbalance_5": None, "imbalance_20": None}
        bb, ba = max(self.bids), min(self.asks)
        mid = (bb + ba) / 2
        spread = ba - bb

        def imbalance(n: int) -> str | None:
            bid = sum(s for _, s in sorted(self.bids.items(), reverse=True)[:n])
            ask = sum(s for _, s in sorted(self.asks.items())[:n])
            return str(((bid - ask) / (bid + ask)).quantize(D("0.0001"))) if bid + ask > 0 else None
        return {"best_bid": str(bb), "best_ask": str(ba), "mid": str(mid), "spread": str(spread), "spread_bps": str((spread / mid * D(10000)).quantize(D("0.01"))),
                "imbalance_5": imbalance(5), "imbalance_20": imbalance(20)}


def group_levels(rows: list[dict[str, str]], step: Decimal, side: str) -> list[dict[str, str]]:
    """Merge price levels into buckets of `step` (bids round down, asks round up) and recompute the cumulative size."""
    merged: dict[Decimal, Decimal] = {}
    for r in rows:
        p = D(r["price"])
        bucket = (p / step).to_integral_value(rounding="ROUND_FLOOR" if side == "bid" else "ROUND_CEILING") * step
        merged[bucket] = merged.get(bucket, D(0)) + D(r["size"])
    ordered = sorted(merged.items(), reverse=(side == "bid"))
    total, out = D(0), []
    for p, s in ordered:
        total += s
        out.append({"price": str(p), "size": str(s), "cum": str(total)})
    return out


def funding_countdown_seconds(now_utc_seconds: float) -> int:
    """Seconds to the next 8-hourly funding time (00:00, 08:00, 16:00 UTC). The schedule is Delta's standard one and is ASSUMED, not read per contract."""
    interval = 8 * 3600
    return int(interval - (now_utc_seconds % interval))
