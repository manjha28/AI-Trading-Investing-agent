"""After each NSE session, fetch the whole day's 1-minute candles for every Market watch stock, so nothing is left out of storage.

The in-session rotation reaches each stock only every several minutes and stops at the close, so the last minutes (and any hole from a restart or an outage)
would otherwise stay missing. The last few days are asked for again, which also fills recent holes. Only what Dhan returns is stored; nothing is padded.
Read-only market-data calls: no order endpoint is touched."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import NSE_NON_TRADING_DATES
from app.models import BrokerCredential, User, UserMarketDataSettings

logger = logging.getLogger("minute_bar_completion")
IST = ZoneInfo("Asia/Kolkata")
RUN_AFTER = time(15, 45)
LOOK_BACK_DAYS = 5          # calendar days re-asked each evening: covers a weekend and a missed run
CHECK_SECONDS = 300


def due(now: datetime, done_for: date | None) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and local.date() not in NSE_NON_TRADING_DATES and local.time() >= RUN_AFTER and done_for != local.date()


def run_once(now: datetime | None = None) -> dict[str, int] | None:
    """One completion pass. None when there is no user with Dhan as data source."""
    from app.api import market_data as md
    from app.db import engine
    from app.services import history_backfill, lab

    now = now or datetime.now(timezone.utc)
    start = datetime.combine((now.astimezone(IST) - timedelta(days=LOOK_BACK_DAYS)).date(), time(0, 0), tzinfo=IST).astimezone(timezone.utc)
    with Session(engine) as db:
        user = db.scalar(select(User).join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id).join(BrokerCredential, BrokerCredential.user_id == User.id)
                         .where(User.email_verified.is_(True), UserMarketDataSettings.selected_provider == "dhan", BrokerCredential.broker == "dhan")
                         .order_by(User.created_at).limit(1))
        if user is None:
            return None
        provider = md.market_data_provider_for_user(db, user.id)
        instruments = lab.master_instruments(db, user.id)
        failed = 0
        for instrument in instruments:
            symbol = instrument.symbol
            reason = history_backfill._sync(md, db, user, provider, instrument, start, now, "1m", min(md.MAX_DHAN_1M_BACKFILL_BARS, md.MAX_DHAN_1M_CHUNK_BARS))
            if reason:
                failed += 1
                logger.warning("minute_completion_failed symbol=%s reason=%s", symbol, reason[:120])
        return {"instruments": len(instruments), "failed": failed}


async def minute_bar_completion_loop() -> None:
    done_for: date | None = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if due(now, done_for):
                result = await asyncio.to_thread(run_once, now)
                if result is not None:
                    done_for = now.astimezone(IST).date()
                    logger.warning("minute_completion_done instruments=%s failed=%s", result["instruments"], result["failed"])
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - retried at the next check
            logger.warning("minute_completion_error error_class=%s", type(error).__name__)
        await asyncio.sleep(CHECK_SECONDS)
