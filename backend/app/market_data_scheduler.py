import asyncio
import logging
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.market_data import get_market_data_provider, latest_quote
from app.config import settings
from app.db import engine
from app.models import PortfolioTransaction, User, WatchlistInstrument

logger = logging.getLogger(__name__)


def _refresh_quotes() -> None:
    with Session(engine) as db:
        instrument_ids = set(
            db.scalars(
                select(PortfolioTransaction.instrument_id)
                .where(PortfolioTransaction.instrument_id.is_not(None))
                .distinct()
            ).all()
        )
        instrument_ids.update(
            db.scalars(select(WatchlistInstrument.instrument_id).distinct()).all()
        )
        if not instrument_ids:
            return
        user = db.scalar(select(User).order_by(User.created_at).limit(1))
        if user is None:
            return
        provider = get_market_data_provider()
        for instrument_id in sorted(instrument_ids, key=str):
            try:
                latest_quote(
                    instrument_id=UUID(str(instrument_id)),
                    refresh=True,
                    provider=provider,
                    _user=user,
                    db=db,
                )
            except HTTPException as error:
                logger.warning(
                    "Scheduled market-data refresh failed for instrument %s (HTTP %s)",
                    instrument_id,
                    error.status_code,
                )


async def market_data_refresh_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_refresh_quotes)
        except SQLAlchemyError:
            logger.exception("Scheduled market-data refresh encountered a database error")
        await asyncio.sleep(settings.market_data_refresh_seconds)
