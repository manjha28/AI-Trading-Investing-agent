import asyncio
import logging
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.accounting import LedgerError, calculate_ledger
from app.api.market_data import latest_quote, market_data_provider_for_user
from app.config import settings
from app.corporate_action_service import get_portfolio_corporate_actions
from app.db import engine
from app.models import Portfolio, PortfolioTransaction, User, WatchlistInstrument

logger = logging.getLogger(__name__)


def _refresh_quotes() -> None:
    with Session(engine) as db:
        portfolio_owners = db.execute(
            select(PortfolioTransaction.portfolio_id, Portfolio.user_id)
            .join(Portfolio, Portfolio.id == PortfolioTransaction.portfolio_id)
            .where(PortfolioTransaction.instrument_id.is_not(None))
            .distinct()
        ).all()
        instrument_ids = set()
        for portfolio_id, user_id in portfolio_owners:
            transactions = list(
                db.scalars(
                    select(PortfolioTransaction)
                    .where(PortfolioTransaction.portfolio_id == portfolio_id)
                    .order_by(
                        PortfolioTransaction.occurred_at,
                        PortfolioTransaction.created_at,
                        PortfolioTransaction.id,
                    )
                ).all()
            )
            try:
                held_symbols = set(
                    calculate_ledger(
                        transactions,
                        get_portfolio_corporate_actions(db, portfolio_id, user_id),
                    ).positions
                )
            except LedgerError:
                logger.warning(
                    "Skipping scheduled refresh for portfolio %s with an invalid ledger",
                    portfolio_id,
                )
                continue
            instrument_ids.update(
                transaction.instrument_id
                for transaction in transactions
                if transaction.instrument_id is not None
                and transaction.transaction_type in {"buy", "sell"}
                and transaction.symbol in held_symbols
            )
        instrument_ids.update(
            db.scalars(select(WatchlistInstrument.instrument_id).distinct()).all()
        )
        if not instrument_ids:
            return
        user = db.scalar(select(User).order_by(User.created_at).limit(1))
        if user is None:
            return
        try:
            provider = market_data_provider_for_user(db, user.id)
        except HTTPException as error:
            logger.warning(
                "Scheduled market-data refresh skipped for user %s (HTTP %s)",
                user.id,
                error.status_code,
            )
            return
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
