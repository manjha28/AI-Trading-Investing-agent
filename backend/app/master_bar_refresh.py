"""Keep real 1-minute bars fresh for the whole Market watch master list while the market is open.

Dhan data calls are paced at one per second, so each pass (every minute) spends a time budget on the STALEST symbols first and the next pass
continues: with ~340 symbols every one is refreshed roughly every 7 minutes. Consumers (the scanner, whole-list forward tests) read the stored bars
and their own freshness gates decide what is current enough; live orders additionally require data fresher than that, so they stay blocked on
anything the rotation has not reached recently. Only runs for users who actually use the master list and have Dhan as their data source.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import BrokerCredential, Strategy, StrategyVersion, User, UserMarketDataSettings
from app.models.forward_live import ForwardTest
from app.services import forward_test as fts
from app.services import lab

logger = logging.getLogger("master_bar_refresh")


def _strategy_uses_master(definition: dict | None) -> bool:
    d = definition or {}
    universe = d.get("universe") or []
    return lab.MASTER_TOKEN in universe or (not universe and str(((d.get("source") or {}).get("provenance") or {}).get("starter_key", "")).startswith("reg_"))


def users_needing_master(db: Session) -> list[User]:
    wanted: set = set()
    for ft in db.scalars(select(ForwardTest).where(ForwardTest.status == "active")):
        if fts.uses_master(ft):
            wanted.add(ft.user_id)
    for strategy in db.scalars(select(Strategy).where(Strategy.status == "active")):
        version = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == strategy.current_version))
        if version is not None and _strategy_uses_master(version.definition):
            wanted.add(strategy.user_id)
    if not wanted:
        return []
    return list(db.scalars(select(User).join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id).join(BrokerCredential, BrokerCredential.user_id == User.id).where(
        User.id.in_(wanted), User.email_verified.is_(True), UserMarketDataSettings.selected_provider == "dhan", BrokerCredential.broker == "dhan")))


def run_pass(now: datetime | None = None, *, budget_seconds: float | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    if not fts.session_open(now):
        return {"users": 0, "refreshed": 0, "errors": 0}
    budget = budget_seconds if budget_seconds is not None else float(settings.master_bar_refresh_budget_seconds)
    refreshed = errors = users = 0
    with Session(engine) as db:
        for user in users_needing_master(db):
            instruments = lab.master_instruments(db, user.id)
            if not instruments:
                continue
            users += 1
            result = fts.refresh_bars(db, user, instruments, now, max_seconds=budget)
            refreshed += sum(1 for v in result.values() if v is None)
            errors += sum(1 for v in result.values() if v is not None)
    return {"users": users, "refreshed": refreshed, "errors": errors}


async def master_bar_refresh_loop() -> None:
    while True:
        try:
            if settings.master_bar_refresh_enabled:
                await asyncio.to_thread(run_pass)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("master_bar_refresh_failed")
        await asyncio.sleep(10)
