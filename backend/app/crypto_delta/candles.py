"""Completed public candles from Delta Exchange India (GET /v2/history/candles, no authentication).

Only COMPLETED candles are stored or returned (open_time + resolution <= now), so a backtest can
never see a forming bar. Prices are parsed straight to Decimal (never via float).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.crypto_delta import CryptoCandle

BASE_URL = "https://api.india.delta.exchange"
SOURCE = "delta_rest_history"
RESOLUTION_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
MAX_PER_REQUEST = 2000
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]{2,32}$")
# Longest window one sync may request, per resolution (keeps a request bounded).
MAX_SYNC_DAYS = {"1m": 14, "5m": 60, "15m": 120, "1h": 365, "4h": 730, "1d": 1460}


class CandleDataError(Exception):
    pass


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_synthetic: bool = False


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_candles(payload: dict[str, Any]) -> tuple[list[Candle], int]:
    """Validate a history response. Returns (candles oldest-first, number of rejected rows)."""
    result = payload.get("result")
    if payload.get("success") is not True or not isinstance(result, list):
        raise CandleDataError("Delta candle response was unsuccessful or malformed.")
    candles: list[Candle] = []
    rejected = 0
    for row in result:
        try:
            open_, high, low, close, volume = (Decimal(str(row[k])) for k in ("open", "high", "low", "close", "volume"))
            opened = datetime.fromtimestamp(int(row["time"]), timezone.utc)
        except Exception:
            rejected += 1
            continue
        valid = (
            all(v.is_finite() for v in (open_, high, low, close, volume))
            and min(open_, high, low, close) > 0 and volume >= 0
            and high >= max(open_, close) and low <= min(open_, close)
        )
        if not valid:
            rejected += 1
            continue
        candles.append(Candle(opened, open_, high, low, close, volume))
    candles.sort(key=lambda candle: candle.open_time)
    return candles, rejected


def fetch_range(
    symbol: str, resolution: str, start: datetime, end: datetime, *, transport: httpx.BaseTransport | None = None,
) -> tuple[list[Candle], int]:
    if resolution not in RESOLUTION_SECONDS or not SYMBOL_PATTERN.match(symbol):
        raise CandleDataError("Unsupported symbol or resolution.")
    step = RESOLUTION_SECONDS[resolution]
    out: dict[datetime, Candle] = {}
    rejected = 0
    window_start = int(_utc(start).timestamp())
    final = int(_utc(end).timestamp())
    with httpx.Client(timeout=20, transport=transport, headers={"User-Agent": "FinByteNexus/1.0 (research)"}) as client:
        while window_start < final:
            window_end = min(window_start + MAX_PER_REQUEST * step, final)
            response = client.get(
                f"{BASE_URL}/v2/history/candles",
                params={"symbol": symbol, "resolution": resolution, "start": window_start, "end": window_end},
            )
            if response.status_code == 429:
                raise CandleDataError("Delta rate limit reached; try again shortly.")
            if response.status_code != 200:
                raise CandleDataError(f"Delta candle request failed (HTTP {response.status_code}).")
            candles, bad = parse_candles(json.loads(response.text, parse_float=Decimal))
            rejected += bad
            for candle in candles:
                out[candle.open_time] = candle
            window_start = window_end
    return sorted(out.values(), key=lambda candle: candle.open_time), rejected


def sync_candles(
    db: Session, symbol: str, resolution: str, start: datetime, end: datetime, *,
    now: datetime | None = None, transport: httpx.BaseTransport | None = None,
) -> dict[str, int]:
    """Download completed candles for [start, end) and insert the ones not stored yet."""
    now = _utc(now or datetime.now(timezone.utc))
    step = timedelta(seconds=RESOLUTION_SECONDS[resolution])
    fetched, rejected = fetch_range(symbol, resolution, start, min(_utc(end), now), transport=transport)
    complete = [candle for candle in fetched if candle.open_time + step <= now]
    existing = set(db.scalars(
        select(CryptoCandle.open_time).where(
            CryptoCandle.symbol == symbol, CryptoCandle.resolution == resolution,
            CryptoCandle.open_time >= _utc(start), CryptoCandle.open_time < _utc(end),
        )
    ))
    existing = {_utc(value) for value in existing}
    inserted = 0
    for candle in complete:
        if candle.open_time in existing:
            continue
        db.add(CryptoCandle(
            symbol=symbol, resolution=resolution, open_time=candle.open_time, open=candle.open, high=candle.high,
            low=candle.low, close=candle.close, volume=candle.volume, source=SOURCE, fetched_at=now,
        ))
        inserted += 1
    db.commit()
    return {"fetched": len(fetched), "incomplete_skipped": len(fetched) - len(complete), "rejected": rejected, "inserted": inserted}


def load_candles(
    db: Session, symbol: str, resolution: str, start: datetime, end: datetime, *, now: datetime | None = None,
) -> list[Candle]:
    """Stored COMPLETED candles in [start, end), oldest first."""
    now = _utc(now or datetime.now(timezone.utc))
    step = timedelta(seconds=RESOLUTION_SECONDS[resolution])
    rows = db.scalars(
        select(CryptoCandle)
        .where(
            CryptoCandle.symbol == symbol, CryptoCandle.resolution == resolution,
            CryptoCandle.open_time >= _utc(start), CryptoCandle.open_time < _utc(end),
        )
        .order_by(CryptoCandle.open_time)
    ).all()
    return [
        Candle(_utc(row.open_time), row.open, row.high, row.low, row.close, row.volume)
        for row in rows if _utc(row.open_time) + step <= now
    ]


def find_gaps(candles: list[Candle], resolution: str) -> list[tuple[datetime, datetime, int]]:
    """Runs of missing candles: (last bar before, first bar after, missing count)."""
    step = timedelta(seconds=RESOLUTION_SECONDS[resolution])
    gaps = []
    for left, right in zip(candles, candles[1:]):
        missing = int((right.open_time - left.open_time) / step) - 1
        if missing > 0:
            gaps.append((left.open_time, right.open_time, missing))
    return gaps
