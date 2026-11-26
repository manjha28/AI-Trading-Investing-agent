"""Today's price for every Market watch stock, from one bulk Dhan quote request a minute while NSE is open.

The live feed only sends a stock when it trades (and can drop), so after a page load most rows showed the previous close. Dhan's quote endpoint returns each stock's
last traded price WITH its trade time; a quote is stored only when that trade happened today. Read-only market-data calls: no order endpoint is touched."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, time, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import NSE_NON_TRADING_DATES
from app.models import InstrumentProviderIdentifier, MarketQuote

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
EVERY_SECONDS = 60
OPEN_FROM, OPEN_TO = time(9, 15), time(15, 45)          # a few minutes past the close so the last trades are picked up
BATCH = 1000                                             # Dhan's limit per segment and request

Fetch = Callable[[dict[str, list[str]]], dict[str, dict[str, Any]]]


def market_hours(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and local.date() not in NSE_NON_TRADING_DATES and OPEN_FROM <= local.time() <= OPEN_TO


def refresh_universe_quotes(db: Session, instrument_ids: set[UUID], fetch: Fetch, now: datetime) -> int:
    """Store the newest today-traded quote per instrument (source 'dhan'). Returns how many rows were written."""
    by_key: dict[tuple[str, str], UUID] = {}
    for mapping in db.scalars(select(InstrumentProviderIdentifier).where(
            InstrumentProviderIdentifier.provider == "dhan", InstrumentProviderIdentifier.is_verified.is_(True),
            InstrumentProviderIdentifier.instrument_id.in_(list(instrument_ids)))):
        security_id, _, segment = mapping.provider_instrument_id.partition("|")
        if security_id.isdigit():
            by_key[(segment or "NSE_EQ", security_id)] = mapping.instrument_id
    keys = sorted(by_key)
    existing = {q.instrument_id: q for q in db.scalars(select(MarketQuote).where(MarketQuote.source == "dhan", MarketQuote.instrument_id.in_(list(instrument_ids))))}
    today, written = now.astimezone(IST).date(), 0
    for start in range(0, len(keys), BATCH):
        request: dict[str, list[str]] = {}
        for segment, security_id in keys[start:start + BATCH]:
            request.setdefault(segment, []).append(security_id)
        for segment, rows in fetch(request).items():
            for security_id, bar in rows.items():
                instrument_id = by_key.get((segment, security_id))
                if instrument_id is None or bar.timestamp.astimezone(IST).date() != today:
                    continue                                       # not traded today: nothing new is claimed for it
                row = existing.get(instrument_id)
                if row is None:
                    row = existing[instrument_id] = MarketQuote(instrument_id=instrument_id, source="dhan", is_synthetic=False)
                    db.add(row)
                elif (row.timestamp if row.timestamp.tzinfo else row.timestamp.replace(tzinfo=timezone.utc)) > bar.timestamp:
                    continue
                row.timestamp, row.retrieved_at = bar.timestamp, now
                row.open, row.high, row.low, row.close, row.volume = bar.open, bar.high, bar.low, bar.close, bar.volume
                written += 1
    db.commit()
    return written


def _refresh_once() -> int:
    from app.api.market_data import _fresh_dhan_tokens_worker, _universe_instrument_ids
    from app.broker_adapters import DhanAdapter
    from app.db import engine
    from app.models import BrokerCredential, User, UserMarketDataSettings

    now = datetime.now(timezone.utc)
    if not market_hours(now):
        return 0
    with Session(engine) as db:
        user = db.scalar(select(User).join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id).join(BrokerCredential, BrokerCredential.user_id == User.id)
                         .where(User.email_verified.is_(True), UserMarketDataSettings.selected_provider == "dhan", BrokerCredential.broker == "dhan")
                         .order_by(User.created_at).limit(1))
        if user is None:
            return 0
        adapter = DhanAdapter()
        tokens = _fresh_dhan_tokens_worker(user.id, adapter)
        return refresh_universe_quotes(db, _universe_instrument_ids(db),
                                       lambda request: adapter.get_market_quotes_batch(tokens["access_token"], tokens, request, force_refresh=True), now)


async def universe_quote_refresh_loop() -> None:
    runs = 0
    while True:
        try:
            written = await asyncio.to_thread(_refresh_once)
            if market_hours(datetime.now(timezone.utc)):
                runs += 1
                if runs <= 3 or runs % 30 == 0:                    # warning level so it shows in production logs: the first minutes, then half-hourly
                    logger.warning("universe_quotes_refreshed run=%s written=%s", runs, written)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a failed minute is retried; the table keeps the last stored quotes with their times
            logger.warning("universe_quote_refresh_failed error_class=%s", type(error).__name__)
        await asyncio.sleep(EVERY_SECONDS)
