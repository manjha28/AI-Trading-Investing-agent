"""Keeps the valuation engine's inputs fresh automatically (replaces running scripts/refresh_valuation_inputs.py by hand).

Every cycle it refreshes only what is older than 24 hours (Dhan sector/industry/P-E/P-B/market cap for the tracked NSE universe, Yahoo
inputs for each user's own stocks), a bounded amount at a time. Because freshness is read from stored timestamps, a long gap (app closed for days)
is simply caught up over the next cycles; nothing depends on the app having been open. A missing or expired Dhan connection skips the Dhan step
and is retried next cycle; it never raises.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import BrokerCredential, User
from app.research_daily import user_universe
from app.services import peer_universe, research_pipeline

logger = logging.getLogger(__name__)


def run_cycle() -> dict[str, object]:
    summary: dict[str, object] = {"dhan": "skipped", "yahoo_refreshed": 0}
    with Session(engine) as db:
        credential = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        if credential is not None:
            from app.api.market_data import _market_data_credentials
            try:
                _, provider, _ = _market_data_credentials(db, credential.user_id, "dhan")
            except Exception as exc:  # noqa: BLE001 - an expired or unreachable connection must not stop the cycle
                provider = None
                summary["dhan"] = f"connection_error:{type(exc).__name__}"
            if provider is None:
                summary["dhan"] = summary["dhan"] if str(summary["dhan"]).startswith("connection_error") else "no_authenticated_connection"
            else:
                result = peer_universe.refresh_dhan_universe(db, provider.adapter, provider.tokens["access_token"], provider.tokens,
                                                              max_age_hours=settings.valuation_refresh_max_age_hours, limit=settings.valuation_refresh_max_per_cycle)
                summary["dhan"] = {k: v for k, v in result.items() if k != "failures"}
        now = datetime.now(timezone.utc)
        for user_id in db.scalars(select(User.id).where(User.email_verified.is_(True))).all():
            for instrument in user_universe(db, user_id):
                try:
                    research_pipeline._refresh_yahoo_valuation_inputs(db, instrument, now)
                    summary["yahoo_refreshed"] = int(summary["yahoo_refreshed"]) + 1  # type: ignore[call-overload]
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning("valuation_yahoo_refresh_failed symbol=%s error_class=%s", instrument.symbol, type(exc).__name__)
    return summary


async def valuation_refresh_loop() -> None:
    await asyncio.sleep(180)
    while True:
        try:
            logger.info("valuation_refresh_cycle %s", await asyncio.to_thread(run_cycle))
        except Exception as exc:  # noqa: BLE001 - the loop must survive any single failure
            logger.warning("valuation_refresh_cycle_failed error_class=%s", type(exc).__name__)
        await asyncio.sleep(settings.valuation_refresh_interval_seconds)
