"""Delivery percentage (DELIV_PER) from the NSE full bhavcopy ``sec_bhavdata_full_DDMMYYYY.csv``: parse, store for the symbols the app tracks, nightly top-up of the last sessions. Missing / '-' is skipped (never 0).
A reference outage stores nothing and never raises into the nightly cycle."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)
URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"
SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
LOOKBACK = 5


def parse(content: str) -> list[tuple[str, str, Decimal]]:
    """[(symbol, series, deliv_per)] for equity series with a real delivery percentage."""
    out = []
    for r in csv.DictReader(io.StringIO(content), skipinitialspace=True):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") not in SERIES:
            continue
        try:
            v = Decimal(r.get("DELIV_PER", ""))
        except (InvalidOperation, ValueError):
            continue
        if 0 <= v <= 100:
            out.append((r["SYMBOL"].upper(), r["SERIES"], v))
    return out


class FetchError(RuntimeError):
    """The file could not be fetched (network / server error), as opposed to 'there is no file that day' (404: weekend or holiday)."""


def fetch_day(day: date, *, http_get: Any = None, retries: int = 3) -> list[tuple[str, str, Decimal]] | None:
    """Rows for the day; None when NSE has no file (404). Raises FetchError after the retries so a transient outage is never mistaken for a holiday."""
    import time
    url = URL.format(d=day.strftime("%d%m%Y"))
    last: Exception | None = None
    for attempt in range(retries):
        try:
            if http_get is None:
                import httpx
                r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; research-client)", "Accept": "*/*"}, timeout=30, follow_redirects=True)
                status, text = r.status_code, r.text
            else:
                status, text = http_get(url)
            if status == 404:
                return None
            if status == 200:
                return parse(text)
            last = RuntimeError(f"http {status}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise FetchError(type(last).__name__)


def store(db: Any, day: date, rows: list[tuple[str, str, Decimal]], symbols: set[str]) -> int:
    from sqlalchemy import select

    from app.models.delivery import DeliveryPercentage
    have = {(s, se) for s, se in db.execute(select(DeliveryPercentage.symbol, DeliveryPercentage.series).where(DeliveryPercentage.session_date == day))}
    n = 0
    for sym, series, v in rows:
        if sym in symbols and (sym, series) not in have:
            db.add(DeliveryPercentage(symbol=sym, session_date=day, series=series, deliv_per=v))
            n += 1
    db.commit()
    return n


def tracked_symbols(db: Any) -> set[str]:
    from sqlalchemy import select

    from app.models import Instrument
    return {s.upper() for s in db.scalars(select(Instrument.symbol).where(Instrument.country == "IN", Instrument.asset_class == "equity", Instrument.is_synthetic.is_(False)))}


def run(db: Any, *, now: datetime | None = None, fetch: Any = None, lookback: int = LOOKBACK) -> dict[str, int]:
    """Top up the last ``lookback`` weekdays (weekends and holidays return no file and are skipped)."""
    fetch = fetch or fetch_day
    now = now or datetime.now(timezone.utc)
    syms = tracked_symbols(db)
    days = stored = missing = 0
    d = now.astimezone(timezone(timedelta(hours=5, minutes=30))).date()
    for _ in range(lookback + 3):
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        try:
            rows = fetch(d)
        except FetchError:
            logger.warning("delivery_fetch_failed day=%s", d)
            missing += 1
            continue
        if rows is None:
            missing += 1
            continue
        days += 1
        stored += store(db, d, rows, syms)
        if days >= lookback:
            break
    return {"days": days, "stored": stored, "no_file": missing}


def log_nightly(db: Any) -> None:
    try:
        logger.info("delivery_ingest %s", run(db))
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("delivery_ingest_failed error_class=%s", type(exc).__name__)
