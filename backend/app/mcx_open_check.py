"""Logs what MCX looks like at 09:05 IST on a trading day, when it should be open (contracts, nearest expiry, session state).
The result is also kept in app_flags-free memory only; this is a log line for the operator, never a trading input."""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import engine
from app.services import mcx_calendar

logger = logging.getLogger(__name__)


def check_once(now: datetime | None = None) -> dict:
    from app.api.market_data import _mcx_front_contracts
    now = now or datetime.now(timezone.utc)
    state = mcx_calendar.session_state(now)
    with Session(engine) as db:
        contracts = _mcx_front_contracts(db, now.astimezone(mcx_calendar.IST).date())
    summary = {"open": state["open"], "reason": state["reason"], "contracts": len(contracts),
               "nearest_expiry": str(min((c.expiry for c in contracts), default=None)),
               "expiring_within_3_days": [c.trading_symbol for c in contracts if (c.expiry - now.astimezone(mcx_calendar.IST).date()).days <= 3]}
    logger.info("mcx_0905_check %s", summary)
    return summary


async def mcx_open_check_loop() -> None:
    done: str | None = None
    while True:
        local = datetime.now(timezone.utc).astimezone(mcx_calendar.IST)
        key = local.date().isoformat()
        if local.weekday() < 5 and local.hour == 9 and local.minute >= 5 and done != key:
            try:
                await asyncio.to_thread(check_once)
            except Exception:  # noqa: BLE001 - a log-only check must never disturb the app
                logger.warning("mcx_0905_check failed")
            done = key
        await asyncio.sleep(30)
