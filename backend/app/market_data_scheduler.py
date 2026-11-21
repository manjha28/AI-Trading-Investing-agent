import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.accounting import LedgerError, calculate_ledger
from app.api.market_data import (
    _fresh_dhan_tokens_worker,
    _expected_market_session_date,
    _market_session_state,
    _process_stream_tick,
    dhan_live_feed_hub,
    latest_quote,
    market_data_provider_for_user,
)
from app.broker_adapters import DhanAdapter
from app.config import (
    NSE_CALENDAR_YEARS,
    NSE_EQUITY_HOLIDAYS,
    NSE_FRESHNESS_SESSION_END,
    NSE_FRESHNESS_SESSION_START,
    NSE_NON_TRADING_DATES,
    NSE_SPECIAL_SESSION_DATES,
    NSE_UNVERIFIED_HOLIDAYS,
    settings,
)
from app.corporate_action_service import get_portfolio_corporate_actions
from app.db import engine
from app.models import (
    BrokerAccountMirror,
    BrokerCredential,
    BrokerMirror,
    Instrument,
    InstrumentProviderIdentifier,
    MarketBar,
    Portfolio,
    PortfolioTransaction,
    PortfolioValuationSnapshot,
    User,
    UserMarketDataSettings,
    WatchlistInstrument,
)
from app.services.alert_engine import evaluate_rules
from starlette.responses import Response

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_BACKEND_DHAN_FEED_GROUP = "backend:market-hours-capture"


def _tracked_instrument_ids(db: Session, user_id: UUID) -> set[UUID]:
    """Instruments the user holds (ledger and Dhan mirror), watches, or has alert rules on."""
    from app.api.market_data import _user_market_data_instrument_ids
    from app.models.alerts import AlertRule

    ids = set(_user_market_data_instrument_ids(db, user_id))  # watchlist + ledger holdings
    ids.update(db.scalars(
        select(AlertRule.instrument_id).where(
            AlertRule.user_id == user_id, AlertRule.instrument_id.is_not(None)
        )
    ).all())
    security_ids = {
        (row.security_id or "").strip()
        for row in db.scalars(select(BrokerMirror).where(
            BrokerMirror.user_id == user_id,
            BrokerMirror.broker == "dhan",
            BrokerMirror.kind == "holding",
            BrokerMirror.quantity > 0,
        ))
    } - {""}
    if security_ids:
        for instrument_id, provider_instrument_id in db.execute(
            select(
                InstrumentProviderIdentifier.instrument_id,
                InstrumentProviderIdentifier.provider_instrument_id,
            ).where(
                InstrumentProviderIdentifier.provider == "dhan",
                InstrumentProviderIdentifier.is_verified.is_(True),
            )
        ).all():
            if provider_instrument_id.partition("|")[0] in security_ids:
                ids.add(instrument_id)
    return ids


def _sync_corporate_actions_sync() -> int:
    """Two passes, no manual confirmation needed:
    1. instruments you hold/watch/alert on: full action history per instrument (also marks coverage);
    2. the rest of the market universe: recent dividends/splits in a few bulk requests, so an ex-date on
       any listed symbol (e.g. a dividend on ONGC) adjusts its day change without anyone entering it."""
    from app.api.market_data import _universe_instrument_ids
    from app.corporate_action_service import (
        PrefetchedActionsProvider, _yfinance_ticker, fetch_recent_actions, sync_yfinance_corporate_actions,
    )

    from app import yahoo_guard
    if not yahoo_guard.allowed():
        return 0          # Yahoo is refusing this server; retried automatically after the cooldown
    inserted = 0
    with Session(engine) as db:
        users = db.scalars(select(User.id).where(User.email_verified.is_(True))).all()
        universe_ids = set(_universe_instrument_ids(db))
        tickers: dict[str, UUID] = {}
        for instrument in db.scalars(select(Instrument).where(Instrument.id.in_(list(universe_ids)))) if universe_ids else []:
            try:
                tickers[_yfinance_ticker(instrument)] = instrument.id
            except ValueError:
                continue
        bulk_events, bulk_failed = fetch_recent_actions(list(tickers)) if tickers else ({}, set())
        if tickers and len(bulk_failed) >= 0.8 * len(tickers):
            yahoo_guard.trip()
            return inserted
        if tickers:
            yahoo_guard.record(True)
        for user_id in users:
            try:
                tracked = _tracked_instrument_ids(db, user_id)
                inserted += sync_yfinance_corporate_actions(db, user_id, instrument_ids=tracked)["actions_inserted"]
                rest = {iid for ticker, iid in tickers.items() if iid not in tracked}
                if rest:
                    inserted += sync_yfinance_corporate_actions(
                        db, user_id, provider=PrefetchedActionsProvider(bulk_events, bulk_failed),
                        instrument_ids=rest, record_coverage=False,
                    )["actions_inserted"]
            except Exception as error:
                db.rollback()
                logger.warning("Corporate action sync failed user_id=%s error_class=%s", user_id, type(error).__name__)
    return inserted


_UNIVERSE_RETRY_AFTER = timedelta(hours=3)
_universe_last_attempt: dict[UUID, datetime] = {}  # symbols already asked recently (e.g. today's candle not published yet)


def refresh_stale_universe_closes(
    db: Session, user: User, provider: Any, *, limit: int = 60, now: datetime | None = None, sync: Any = None,
) -> dict[str, int]:
    """Fetch the real daily bars of universe symbols whose last stored close is older than the latest
    completed session (or absent), a bounded batch per call. Nothing is estimated; a failed symbol is
    simply retried on the next cycle."""
    from fastapi import BackgroundTasks

    from app.api import market_data as md

    now = now or datetime.now(timezone.utc)
    sync = sync or md.sync_history
    ids = md._universe_instrument_ids(db)
    if not ids:
        return {"stale": 0, "attempted": 0, "failed": 0}
    latest: dict[UUID, datetime] = {}
    for instrument_id, stamp in db.execute(
        select(MarketBar.instrument_id, func.max(MarketBar.timestamp)).where(
            MarketBar.instrument_id.in_(list(ids)), MarketBar.interval == "1d", MarketBar.source == "dhan",
            MarketBar.is_synthetic.is_(False),
        ).group_by(MarketBar.instrument_id)
    ):
        latest[instrument_id] = stamp
    stale: list[tuple[Instrument, datetime | None]] = []
    for instrument in db.scalars(select(Instrument).where(Instrument.id.in_(list(ids)))):
        expected = md._last_completed_session(instrument, now)
        last = latest.get(instrument.id)
        if last is None or md._daily_session_date(last) < expected:
            stale.append((instrument, last))
    stale.sort(key=lambda item: item[0].symbol)
    due = [item for item in stale if now - _universe_last_attempt.get(item[0].id, now - _UNIVERSE_RETRY_AFTER * 2) >= _UNIVERSE_RETRY_AFTER]
    attempted = failed = 0
    reasons: dict[str, list[str]] = {}          # reason -> symbols, logged once per run instead of one warning per symbol
    for instrument, last in due[:limit]:
        _universe_last_attempt[instrument.id] = now
        start = (md._utc(last) - timedelta(days=4)) if last is not None else now - timedelta(days=12)
        try:
            result = sync(
                instrument_id=instrument.id, start=start, end=now, background_tasks=BackgroundTasks(), interval="1d",
                limit=100, provider=provider, _user=user, _csrf=None, db=db,
            )
            # sync_history reports a provider failure in its response instead of raising
            if getattr(result, "sync_status", None) == "unavailable":
                failed += 1
                attempts = getattr(result, "provider_attempts", None) or []
                why = "; ".join(sorted({f"{getattr(a, 'provider', '?')}: {getattr(a, 'reason', None) or getattr(a, 'reason_category', None) or getattr(a, 'outcome', 'unavailable')}" for a in attempts})) or "provider returned no candle"
                reasons.setdefault(why, []).append(instrument.symbol)
        except Exception as error:
            db.rollback()
            failed += 1
            reasons.setdefault(f"error {type(error).__name__}", []).append(instrument.symbol)
        attempted += 1
    for why, symbols in reasons.items():
        logger.warning("Universe close refresh unavailable for %d symbols (%s): %s%s", len(symbols), why, ", ".join(symbols[:12]), " ..." if len(symbols) > 12 else "")
    return {"stale": len(stale), "attempted": attempted, "failed": failed}


def _refresh_universe_closes_sync() -> dict[str, int]:
    with Session(engine) as db:
        user = db.scalar(
            select(User)
            .join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id)
            .join(BrokerCredential, BrokerCredential.user_id == User.id)
            .where(
                User.email_verified.is_(True), UserMarketDataSettings.selected_provider == "dhan",
                BrokerCredential.broker == "dhan",
            )
            .order_by(User.created_at).limit(1)
        )
        if user is None:
            return {"stale": 0, "attempted": 0, "failed": 0}
        try:
            provider = market_data_provider_for_user(db, user.id)
        except HTTPException as error:
            logger.warning("Universe close refresh skipped (HTTP %s)", error.status_code)
            return {"stale": 0, "attempted": 0, "failed": 0}
        return refresh_stale_universe_closes(db, user, provider)


async def universe_close_refresh_loop() -> None:
    """Keep the Market watch's daily closes current after every session, without a manual button."""
    await asyncio.sleep(90)
    while True:
        try:
            report = await asyncio.to_thread(_refresh_universe_closes_sync)
            if report["stale"]:
                logger.info("Universe close refresh stale=%d attempted=%d failed=%d", report["stale"], report["attempted"], report["failed"])
        except Exception as error:
            logger.warning("Universe close refresh failed error_class=%s", type(error).__name__)
        await asyncio.sleep(settings.universe_close_refresh_interval_seconds)


async def corporate_action_sync_loop() -> None:
    """Keep dividends/splits current so day-change and returns adjust for ex-dates automatically."""
    await asyncio.sleep(60)
    while True:
        try:
            inserted = await asyncio.to_thread(_sync_corporate_actions_sync)
            logger.info("Corporate action sync completed new_actions=%d", inserted)
        except Exception as error:
            logger.warning("Corporate action sync failed error_class=%s", type(error).__name__)
        await asyncio.sleep(settings.corporate_action_sync_interval_seconds)


ALERT_QUOTE_REFRESH_SECONDS = 300


def _refresh_alert_rule_quotes(db: Session, user_id: UUID, now: datetime | None = None) -> int:
    """Refresh the latest real quote of every instrument an enabled alert rule watches.

    Rules on instruments outside the watchlist and holdings otherwise never receive fresh
    quotes and stay suppressed as stale forever. A quote is requested at most once per
    ALERT_QUOTE_REFRESH_SECONDS per instrument; provider failures are logged and skipped
    (the rule then reports why it could not be evaluated).
    """
    from app.models.alerts import AlertRule
    from app.models import MarketQuote

    now = now or datetime.now(timezone.utc)
    instrument_ids = set(db.scalars(
        select(AlertRule.instrument_id).where(
            AlertRule.user_id == user_id,
            AlertRule.enabled.is_(True),
            AlertRule.instrument_id.is_not(None),
        )
    ).all())
    if not instrument_ids:
        return 0
    try:
        provider = market_data_provider_for_user(db, user_id)
    except HTTPException as error:
        logger.warning("Alert quote refresh skipped user_id=%s (HTTP %s)", user_id, error.status_code)
        return 0
    user = db.get(User, user_id)
    refreshed = 0
    for instrument_id in sorted(instrument_ids, key=str):
        newest = db.scalar(
            select(MarketQuote.retrieved_at)
            .where(MarketQuote.instrument_id == instrument_id)
            .order_by(MarketQuote.retrieved_at.desc())
            .limit(1)
        )
        if newest is not None:
            newest = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
            if (now - newest).total_seconds() < ALERT_QUOTE_REFRESH_SECONDS:
                continue
        try:
            latest_quote(
                instrument_id=UUID(str(instrument_id)),
                refresh=True,
                provider=provider,
                _user=user,
                db=db,
            )
            refreshed += 1
        except HTTPException as error:
            logger.warning("Alert quote refresh failed instrument_id=%s (HTTP %s)", instrument_id, error.status_code)
        except Exception as error:
            db.rollback()
            logger.warning("Alert quote refresh failed instrument_id=%s error_class=%s", instrument_id, type(error).__name__)
    return refreshed


def _evaluate_all_alert_rules_sync() -> int:
    evaluated = 0
    with Session(engine) as db:
        user_ids = db.scalars(select(User.id).where(User.email_verified.is_(True))).all()
        for user_id in user_ids:
            try:
                try:
                    _refresh_alert_rule_quotes(db, user_id)
                except Exception as error:  # never block evaluation on a refresh problem
                    db.rollback()
                    logger.warning("Alert quote refresh failed user_id=%s error_class=%s", user_id, type(error).__name__)
                evaluate_rules(db, user_id)
                evaluated += 1
            except Exception as error:
                db.rollback()
                logger.warning("Scheduled alert evaluation failed user_id=%s error_class=%s", user_id, type(error).__name__)
    return evaluated


async def alert_evaluation_loop() -> None:
    while True:
        try:
            evaluated = await asyncio.to_thread(_evaluate_all_alert_rules_sync)
            logger.info("Scheduled alert evaluation completed verified_users=%d", evaluated)
        except Exception as error:
            logger.warning("Scheduled alert evaluation failed error_class=%s", type(error).__name__)
        await asyncio.sleep(60)


def _dhan_backend_capture_inputs_sync() -> tuple[UUID, dict[str, tuple[str, str]]] | None:
    with Session(engine) as db:
        user = db.scalar(
            select(User)
            .join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id)
            .join(BrokerCredential, BrokerCredential.user_id == User.id)
            .where(
                User.email_verified.is_(True),
                UserMarketDataSettings.selected_provider == "dhan",
                BrokerCredential.broker == "dhan",
            )
            .order_by(User.created_at)
            .limit(1)
        )
        if user is None:
            return None

        mapping_rows = db.execute(
            select(Instrument, InstrumentProviderIdentifier)
            .join(
                InstrumentProviderIdentifier,
                InstrumentProviderIdentifier.instrument_id == Instrument.id,
            )
            .where(
                InstrumentProviderIdentifier.provider == "dhan",
                InstrumentProviderIdentifier.is_verified.is_(True),
            )
        ).all()
        mappings_by_instrument: dict[UUID, list[tuple[Instrument, InstrumentProviderIdentifier]]] = {}
        mappings_by_security: dict[str, list[tuple[Instrument, InstrumentProviderIdentifier]]] = {}
        for instrument, mapping in mapping_rows:
            mappings_by_instrument.setdefault(instrument.id, []).append((instrument, mapping))
            security_id = mapping.provider_instrument_id.partition("|")[0]
            mappings_by_security.setdefault(security_id, []).append((instrument, mapping))

        candidates: dict[UUID, tuple[Instrument, InstrumentProviderIdentifier]] = {}
        watchlist_ids = db.scalars(
            select(WatchlistInstrument.instrument_id).where(
                WatchlistInstrument.user_id == user.id
            )
        ).all()
        for instrument_id in watchlist_ids:
            mapped = mappings_by_instrument.get(instrument_id, [])
            if mapped:
                candidates[instrument_id] = mapped[0]

        holdings = db.scalars(
            select(BrokerMirror).where(
                BrokerMirror.user_id == user.id,
                BrokerMirror.broker == "dhan",
                BrokerMirror.kind == "holding",
                BrokerMirror.quantity > 0,
            )
        ).all()
        segment_by_exchange = {
            "NSE": "NSE_EQ",
            "NSE_EQ": "NSE_EQ",
            "BSE": "BSE_EQ",
            "BSE_EQ": "BSE_EQ",
        }
        for holding in holdings:
            security_matches = mappings_by_security.get((holding.security_id or "").strip(), [])
            expected_segment = segment_by_exchange.get(holding.exchange.upper())
            symbol = holding.symbol.strip().upper().removesuffix("-EQ")
            matching = next(
                (
                    (instrument, mapping)
                    for instrument, mapping in security_matches
                    if (expected_segment is None or mapping.provider_instrument_id.endswith(f"|{expected_segment}"))
                    and instrument.symbol.strip().upper().removesuffix("-EQ") == symbol
                ),
                None,
            )
            if matching is not None:
                candidates[matching[0].id] = matching

        now = datetime.now(timezone.utc)
        supported_segments = {
            "NSE_EQ", "NSE_FNO", "NSE_CURRENCY", "BSE_EQ", "BSE_FNO",
            "BSE_CURRENCY", "MCX_COMM",
        }
        subscriptions: dict[str, tuple[str, str]] = {}
        for instrument, mapping in candidates.values():
            segment = mapping.provider_instrument_id.rpartition("|")[2]
            if not segment:
                segment = segment_by_exchange.get(instrument.venue.upper(), "")
            security_id = mapping.provider_instrument_id.partition("|")[0]
            if (
                instrument.country.upper() != "IN"
                or instrument.currency.upper() != "INR"
                or instrument.is_synthetic
                or segment not in supported_segments
                or not security_id.isdigit()
                or _market_session_state(instrument, now) != "open"
            ):
                continue
            subscriptions[str(instrument.id)] = (segment, security_id)
        return user.id, subscriptions


async def _consume_backend_dhan_ticks(
    user_id: UUID,
    queue: asyncio.Queue[dict],
) -> None:
    while True:
        event = await queue.get()
        if event.get("type") != "tick":
            continue
        try:
            await asyncio.to_thread(
                _process_stream_tick,
                event["instrument_id"],
                user_id,
                event["bar"],
                quote_source="dhan_ws",
                freshness_message="Live Dhan WebSocket quote packet received by backend capture.",
                watchlist=True,
            )
        except Exception as error:
            logger.warning(
                "Dhan backend tick persistence failed instrument_id=%s error_class=%s",
                event.get("instrument_id"),
                type(error).__name__,
            )


async def dhan_backend_capture_loop() -> None:
    adapter = DhanAdapter()
    group_user_id: UUID | None = None
    group_identity: tuple[str, str] | None = None
    feed_queue: asyncio.Queue[dict] | None = None
    consumer: asyncio.Task[None] | None = None

    async def release_group() -> None:
        nonlocal group_user_id, group_identity, feed_queue, consumer
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            consumer = None
        if group_user_id is not None:
            await dhan_live_feed_hub.leave("ticker", _BACKEND_DHAN_FEED_GROUP)
        group_user_id = None
        group_identity = None
        feed_queue = None

    try:
        while True:
            try:
                inputs = await asyncio.to_thread(_dhan_backend_capture_inputs_sync)
                if inputs is None or not inputs[1]:
                    await release_group()
                else:
                    user_id, subscriptions = inputs
                    tokens = await asyncio.to_thread(_fresh_dhan_tokens_worker, user_id, adapter)
                    identity = (tokens["access_token"], tokens["client_id"])
                    if group_user_id != user_id or group_identity != identity or feed_queue is None:
                        await release_group()
                        dhan_live_feed_hub._owner_ids["ticker"] = user_id
                        feed_queue = await dhan_live_feed_hub.join(
                            "ticker",
                            _BACKEND_DHAN_FEED_GROUP,
                            subscriptions,
                            identity[0],
                            tokens,
                        )
                        group_user_id = user_id
                        group_identity = identity
                        consumer = asyncio.create_task(
                            _consume_backend_dhan_ticks(user_id, feed_queue),
                            name="dhan-backend-tick-capture",
                        )
                        logger.info(
                            "Dhan backend capture subscribed owner_id=%s instruments=%d",
                            user_id,
                            len(subscriptions),
                        )
                    else:
                        await dhan_live_feed_hub.update(
                            "ticker", _BACKEND_DHAN_FEED_GROUP, subscriptions
                        )
                        if consumer is None or consumer.done():
                            consumer = asyncio.create_task(
                                _consume_backend_dhan_ticks(user_id, feed_queue),
                                name="dhan-backend-tick-capture",
                            )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Dhan backend capture reconciliation failed error_class=%s",
                    type(error).__name__,
                )
            await asyncio.sleep(30)
    finally:
        await release_group()


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


def _nse_snapshot_slot(now: datetime) -> str | None:
    local = now.astimezone(IST)
    if local.year not in NSE_CALENDAR_YEARS:
        return None
    day = local.date()
    if (
        day.weekday() >= 5
        or day in NSE_NON_TRADING_DATES
        or day in NSE_SPECIAL_SESSION_DATES
        or day in NSE_UNVERIFIED_HOLIDAYS
    ):
        return None
    if NSE_FRESHNESS_SESSION_START <= local.time() < NSE_FRESHNESS_SESSION_END:
        elapsed_minutes = (
            (local.hour * 60 + local.minute)
            - (NSE_FRESHNESS_SESSION_START.hour * 60 + NSE_FRESHNESS_SESSION_START.minute)
        )
        return f"{day.isoformat()}:open:{elapsed_minutes // 5}"
    close_capture_start = time(
        NSE_FRESHNESS_SESSION_END.hour,
        NSE_FRESHNESS_SESSION_END.minute + 5,
    )
    if close_capture_start <= local.time() < time(16, 10):
        return f"{day.isoformat()}:close"
    return None


def _quote_observations(value) -> list[dict]:
    observations: list[dict] = []
    if isinstance(value, dict):
        if {"symbol", "source", "timestamp", "price_basis"}.issubset(value):
            observations.append(value)
        else:
            for child in value.values():
                observations.extend(_quote_observations(child))
    elif isinstance(value, list):
        for child in value:
            observations.extend(_quote_observations(child))
    return observations


def _record_owner_snapshot(db: Session, owner_id, captured_at: datetime) -> None:
    from app.api.brokers import get_broker_portfolio_summary

    summary = get_broker_portfolio_summary(
        Response(), SimpleNamespace(user_id=owner_id), db
    )
    totals = summary["totals"]
    brokers = summary["brokers"]
    reasons: list[str] = []
    required = {
        "market_value": "market_value",
        "unrealized_pnl": "unrealized_pnl",
        "day_change": "day_change",
        "invested": "invested_cost",
    }
    values: dict[str, Decimal | None] = {}
    for column, key in required.items():
        raw_value = totals.get(key)
        values[column] = Decimal(str(raw_value)) if raw_value is not None else None
        if raw_value is None:
            reasons.append(f"{column.replace('_', ' ')} is unavailable.")

    for broker in brokers:
        for field in required.values():
            reason = broker["unavailable_reasons"].get(field)
            if reason:
                reasons.append(f"{broker['source']} {field}: {reason}")

    account_currencies = set(db.scalars(
        select(BrokerAccountMirror.currency).where(
            BrokerAccountMirror.user_id == owner_id
        )
    ).all())
    non_inr = sorted(currency for currency in account_currencies if currency != "INR")
    if non_inr:
        reasons.append(
            f"Excluded broker currency {', '.join(non_inr)}; no FX conversion was applied."
        )

    observations = _quote_observations(
        [broker.get("provenance", {}) for broker in brokers]
    )
    timestamps = [
        datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        for item in observations
        if item.get("timestamp")
    ]
    bases = {str(item["price_basis"]) for item in observations if item.get("price_basis")}
    valuation_basis = next(iter(bases)) if len(bases) == 1 else "mixed" if bases else "unavailable"
    source = str(summary.get("source") or "unavailable")
    complete = not reasons and all(value is not None for value in values.values())
    db.add(
        PortfolioValuationSnapshot(
            owner_id=owner_id,
            portfolio_id=None,
            scope="owner",
            timestamp=captured_at,
            currency=str(summary.get("currency") or "INR"),
            market_value=values["market_value"],
            unrealized_pnl=values["unrealized_pnl"],
            day_change=values["day_change"],
            invested=values["invested"],
            valuation_basis=valuation_basis,
            source=source,
            provider_timestamp=max(timestamps) if timestamps else None,
            complete=complete,
            reason="; ".join(dict.fromkeys(reasons)) or None,
            price_observations=observations,
        )
    )
    db.commit()
    logger.info(
        "portfolio_snapshot_recorded scope=owner owner_id=%s source=%s complete=%s "
        "valuation_basis=%s observation_count=%d",
        owner_id,
        source,
        complete,
        valuation_basis,
        len(observations),
    )


def _capture_owner_snapshots(captured_at: datetime) -> None:
    with Session(engine) as db:
        owner_ids = list(db.scalars(select(User.id)).all())
        for owner_id in owner_ids:
            try:
                _record_owner_snapshot(db, owner_id, captured_at)
            except Exception as error:
                db.rollback()
                reason = (
                    "Existing owner-wide valuation path failed "
                    f"({type(error).__name__}); no substitute valuation was used."
                )
                db.add(
                    PortfolioValuationSnapshot(
                        owner_id=owner_id,
                        portfolio_id=None,
                        scope="owner",
                        timestamp=captured_at,
                        currency="INR",
                        market_value=None,
                        unrealized_pnl=None,
                        day_change=None,
                        invested=None,
                        valuation_basis="unavailable",
                        source="unavailable",
                        provider_timestamp=None,
                        complete=False,
                        reason=reason,
                        price_observations=[],
                    )
                )
                db.commit()
                logger.warning(
                    "portfolio_snapshot_partial scope=owner owner_id=%s error_class=%s",
                    owner_id,
                    type(error).__name__,
                )


def _capture_dhan_session_closes(captured_at: datetime) -> bool:
    """Fetch and persist missing Dhan closes for each instrument's latest completed session."""
    with Session(engine) as db:
        user = db.scalar(
            select(User)
            .join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id)
            .join(BrokerCredential, BrokerCredential.user_id == User.id)
            .where(
                User.email_verified.is_(True),
                UserMarketDataSettings.selected_provider == "dhan",
                BrokerCredential.broker == "dhan",
            )
            .order_by(User.created_at)
            .limit(1)
        )
        if user is None:
            return True
        mapping_rows = db.execute(
            select(Instrument, InstrumentProviderIdentifier)
            .join(InstrumentProviderIdentifier, InstrumentProviderIdentifier.instrument_id == Instrument.id)
            .where(
                InstrumentProviderIdentifier.provider == "dhan",
                InstrumentProviderIdentifier.is_verified.is_(True),
                Instrument.country == "IN",
                Instrument.currency == "INR",
                Instrument.is_synthetic.is_(False),
            )
        ).all()
        candidates = {instrument.id: (instrument, mapping) for instrument, mapping in mapping_rows}
        requested_ids = set(db.scalars(
            select(WatchlistInstrument.instrument_id).where(WatchlistInstrument.user_id == user.id)
        ).all())
        holdings = db.scalars(select(BrokerMirror).where(
            BrokerMirror.user_id == user.id,
            BrokerMirror.broker == "dhan",
            BrokerMirror.kind.in_(("holding", "position")),
            BrokerMirror.quantity != 0,
            or_(BrokerMirror.kind == "holding", BrokerMirror.position_type.is_(None), BrokerMirror.position_type != "CLOSED"),
            or_(BrokerMirror.product.is_(None), BrokerMirror.product.not_in(("CNC", "DELIVERY")), BrokerMirror.quantity > 0),
        )).all()
        for holding in holdings:
            symbol = holding.symbol.strip().upper().removesuffix("-EQ")
            for instrument, mapping in mapping_rows:
                security_id, _, segment = mapping.provider_instrument_id.partition("|")
                expected_segment = {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}.get(holding.exchange.upper())
                if (security_id == (holding.security_id or "").strip()
                        and instrument.symbol.strip().upper().removesuffix("-EQ") == symbol
                        and (not expected_segment or not segment or segment == expected_segment)):
                    requested_ids.add(instrument.id)
                    break

        local_now = captured_at.astimezone(IST)
        target_by_id: dict[UUID, tuple[Instrument, InstrumentProviderIdentifier, object]] = {}
        for instrument_id in requested_ids:
            candidate = candidates.get(instrument_id)
            if candidate is None:
                continue
            instrument, mapping = candidate
            session_date = _expected_market_session_date(instrument, captured_at)
            if (session_date == local_now.date()
                    and local_now.time() < time(15, 30)):
                session_date = _expected_market_session_date(
                    instrument, captured_at - timedelta(days=1)
                )
            close_boundary = datetime.combine(
                session_date, time(15, 30), tzinfo=ZoneInfo(instrument.timezone)
            ).astimezone(timezone.utc)
            session_key = datetime.combine(session_date, time.min, tzinfo=timezone.utc)
            existing = db.scalar(select(MarketBar).where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == "1d",
                MarketBar.timestamp == session_key,
                MarketBar.source == "dhan",
                MarketBar.is_synthetic.is_(False),
            ).with_for_update())
            if existing is not None and existing.retrieved_at is not None:
                retrieved = existing.retrieved_at
                if retrieved.tzinfo is None:
                    retrieved = retrieved.replace(tzinfo=timezone.utc)
                if retrieved >= close_boundary:
                    continue
            target_by_id[instrument_id] = (instrument, mapping, session_date)
        if not target_by_id:
            return True

        adapter = DhanAdapter()
        tokens = _fresh_dhan_tokens_worker(user.id, adapter)
        targets_by_date: dict[object, list[tuple[Instrument, InstrumentProviderIdentifier]]] = {}
        for instrument, mapping, session_date in target_by_id.values():
            targets_by_date.setdefault(session_date, []).append((instrument, mapping))

        all_resolved = True
        for session_date, selected in targets_by_date.items():
            by_segment: dict[str, list[str]] = {}
            instrument_by_key = {}
            for instrument, mapping in selected:
                security_id, _, segment = mapping.provider_instrument_id.partition("|")
                if not segment:
                    segment = {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}.get(instrument.venue.upper(), "")
                if not segment or not security_id.isdigit():
                    all_resolved = False
                    continue
                by_segment.setdefault(segment, []).append(security_id)
                instrument_by_key[(segment, security_id)] = instrument
            if not by_segment:
                continue
            quotes = adapter.get_market_quotes_batch(
                tokens["access_token"], tokens, by_segment, force_refresh=True
            )
            for segment, rows in quotes.items():
                for security_id, quote in rows.items():
                    instrument = instrument_by_key.get((segment, security_id))
                    close = quote.session_close
                    observed_at = quote.provider_observed_at
                    close_boundary = datetime.combine(
                        session_date, time(15, 30), tzinfo=IST
                    ).astimezone(timezone.utc)
                    if instrument is None or close is None or observed_at is None or observed_at < close_boundary:
                        all_resolved = False
                        continue
                    session_key = datetime.combine(session_date, time.min, tzinfo=timezone.utc)
                    existing = db.scalar(select(MarketBar).where(
                        MarketBar.instrument_id == instrument.id,
                        MarketBar.interval == "1d",
                        MarketBar.timestamp == session_key,
                        MarketBar.source == "dhan",
                        MarketBar.is_synthetic.is_(False),
                    ).with_for_update())
                    if existing is not None:
                        retrieved = existing.retrieved_at
                        if retrieved.tzinfo is None:
                            retrieved = retrieved.replace(tzinfo=timezone.utc)
                        if retrieved >= close_boundary:
                            continue
                        existing.open = quote.open
                        existing.high = quote.high
                        existing.low = quote.low
                        existing.close = close
                        existing.volume = quote.volume
                        existing.retrieved_at = observed_at
                    else:
                        db.add(MarketBar(
                            instrument_id=instrument.id,
                            interval="1d",
                            timestamp=session_key,
                            open=quote.open,
                            high=quote.high,
                            low=quote.low,
                            close=close,
                            volume=quote.volume,
                            source="dhan",
                            retrieved_at=observed_at,
                            is_synthetic=False,
                        ))
        db.commit()
        return all_resolved


async def portfolio_snapshot_loop() -> None:
    last_slot: str | None = None
    last_close_check = 0.0
    while True:
        now = datetime.now(timezone.utc)
        slot = _nse_snapshot_slot(now)
        if slot is not None and slot != last_slot:
            try:
                await asyncio.to_thread(_capture_owner_snapshots, now)
            except SQLAlchemyError:
                logger.exception("Portfolio snapshot capture encountered a database error")
            last_slot = slot
        if asyncio.get_running_loop().time() - last_close_check >= 300:
            try:
                await asyncio.to_thread(_capture_dhan_session_closes, now)
            except Exception as error:
                logger.warning("Dhan session-close catch-up failed error_class=%s", type(error).__name__)
            last_close_check = asyncio.get_running_loop().time()
        await asyncio.sleep(15)
