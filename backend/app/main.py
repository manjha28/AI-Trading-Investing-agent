import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.alerts import router as alerts_router
from app.api.brokers import router as brokers_router
from app.api.corporate_actions import router as corporate_actions_router
from app.api.news import router as news_router
from app.api.delta_research import router as delta_research_router
from app.api.execution import router as execution_router
from app.api.forward_tests import live_router as dhan_live_router, router as forward_tests_router
from app.dhan_master_warm import dhan_master_warm_loop
from app.master_bar_refresh import master_bar_refresh_loop
from app.forward_test_scheduler import forward_test_loop
from app.api.crypto_delta import router as crypto_delta_router
from app.delta_research.collector import delta_research_manager
from app.api.position_theses import router as position_theses_router
from app.api.strategies import router as strategies_router
from app.api.signals_lab import router as signals_lab_router
from app.api.var_strategy import router as var_strategy_router
from app.api.costs import router as costs_router
from app.api.derivatives import router as derivatives_router
from app.db import engine as _db_engine
from app.leader_lock import LeaderElection, run_as_leader
from app.services import fundamentals_feed
from app.api.market_stream import router as market_stream_router
from app.api.research import router as research_router
from app.api.health import router as health_router
from app.api.market_data import (
    dhan_live_feed_hub,
    indicators_router,
    router as market_data_router,
)
from app.api.portfolios import router as portfolios_router
from app.api.portfolio_transactions import router as portfolio_transactions_router
from app.api.trades import router as trades_router
from app.config import settings
from app.db import engine
from app.services.news_sentiment import analyze_news_items
from sqlalchemy.orm import Session
from app.market_data_scheduler import (
    alert_evaluation_loop,
    dhan_backend_capture_loop,
    corporate_action_sync_loop,
    universe_close_refresh_loop,
    market_data_refresh_loop,
    portfolio_snapshot_loop,
)


class BrokerCallbackAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if isinstance(arguments, tuple) and len(arguments) >= 5:
            request_path = arguments[2]
            if isinstance(request_path, str) and "/api/v1/brokers/" in request_path and "/callback" in request_path:
                record.args = (*arguments[:2], request_path.split("?", 1)[0] + "?<redacted>", *arguments[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(BrokerCallbackAccessLogFilter())


def _process_sentiment_batch() -> None:
    with Session(engine) as db:
        analyze_news_items(db, limit=500, period_days=3650)


async def news_sentiment_processing_loop() -> None:
    """Drain persisted news in bounded batches; pending rows survive restarts."""
    while True:
        try:
            await asyncio.to_thread(_process_sentiment_batch)
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Scheduled news sentiment batch failed error_class=%s", type(error).__name__
            )
        await asyncio.sleep(30)


scheduler_election = LeaderElection(_db_engine)


def _leader(loop_factory, name: str) -> "asyncio.Task":
    """Start a periodic loop that only runs on the elected scheduler leader (see app/leader_lock.py)."""
    if not settings.scheduler_leader_election_enabled:
        return asyncio.create_task(loop_factory())
    return asyncio.create_task(run_as_leader(scheduler_election, loop_factory, name))


import logging as _logging
_logging.getLogger("yfinance").setLevel(_logging.CRITICAL)   # Yahoo's own 401 spam; the shared circuit breaker (app/yahoo_guard.py) reports the pause once


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await dhan_live_feed_hub.start()
    scheduler_task = None
    snapshot_task = None
    alert_task = None
    delta_task = None
    dhan_capture_task = None
    sentiment_task = None
    fundamentals_task = None
    corporate_task = None
    news_ingest_task = None
    research_daily_task = None
    keepalive_task = None
    valuation_task = None
    universe_task = None
    forward_task = None
    master_warm_task = None
    master_bars_task = None
    derivatives_tasks: list = []
    jobs_task = None
    if settings.job_worker_enabled and "pytest" not in sys.modules:
        from app.jobs_worker import job_worker_loop
        jobs_task = asyncio.create_task(job_worker_loop())
    if "pytest" not in sys.modules:
        snapshot_task = _leader(portfolio_snapshot_loop, 'portfolio_snapshot')
        dhan_capture_task = _leader(dhan_backend_capture_loop, 'dhan_capture')
        alert_task = _leader(alert_evaluation_loop, 'alert_evaluation')
        sentiment_task = _leader(news_sentiment_processing_loop, 'news_sentiment')
    if settings.market_data_scheduler_enabled and "pytest" not in sys.modules:
        scheduler_task = _leader(market_data_refresh_loop, 'market_data_refresh')
    if settings.corporate_action_sync_enabled and "pytest" not in sys.modules:
        corporate_task = _leader(corporate_action_sync_loop, 'corporate_actions')
    if settings.news_ingest_enabled and "pytest" not in sys.modules:
        from app.news_ingestion import news_ingest_loop
        news_ingest_task = _leader(news_ingest_loop, 'news_ingest')
    if "pytest" not in sys.modules:
        from app.dhan_token_keepalive import dhan_token_keepalive_loop
        keepalive_task = _leader(dhan_token_keepalive_loop, 'dhan_token_keepalive')
    if settings.valuation_refresh_enabled and "pytest" not in sys.modules:
        from app.valuation_refresh import valuation_refresh_loop
        valuation_task = _leader(valuation_refresh_loop, 'valuation_refresh')
    if settings.research_daily_enabled and "pytest" not in sys.modules:
        from app.research_daily import research_daily_loop
        research_daily_task = _leader(research_daily_loop, 'research_daily')
    if settings.universe_close_refresh_enabled and "pytest" not in sys.modules:
        universe_task = _leader(universe_close_refresh_loop, 'universe_closes')
    if "pytest" not in sys.modules:
        master_warm_task = asyncio.create_task(dhan_master_warm_loop())      # every process warms its own in-memory copy
    if settings.master_bar_refresh_enabled and "pytest" not in sys.modules:
        master_bars_task = _leader(master_bar_refresh_loop, 'master_bars')
    if settings.forward_test_enabled and "pytest" not in sys.modules:
        forward_task = _leader(forward_test_loop, 'forward_tests')
    if fundamentals_feed.feed_configured() and "pytest" not in sys.modules:
        fundamentals_task = _leader(fundamentals_feed.fundamentals_collection_loop, 'fundamentals')
    if "pytest" not in sys.modules:
        from app import derivatives_scheduler
        if settings.derivatives_scheduler_enabled:
            derivatives_tasks.append(_leader(derivatives_scheduler.master_refresh_loop, 'derivatives_master'))
        if settings.derivatives_chain_poll_enabled:
            derivatives_tasks.append(_leader(derivatives_scheduler.chain_polling_loop, 'derivatives_chain'))
    if settings.delta_research_enabled and "pytest" not in sys.modules:
        delta_task = _leader(delta_research_manager.start, 'delta_research')
    try:
        yield
    finally:
        # Correlates upstream socket closes in the logs: a close right after this line is our own
        # shutdown (including uvicorn --reload), not an upstream failure.
        logging.getLogger(__name__).info("Application shutdown: closing upstream market-data WebSockets (not an upstream failure)")
        if dhan_capture_task is not None:
            dhan_capture_task.cancel()
            try:
                await dhan_capture_task
            except asyncio.CancelledError:
                pass
        await dhan_live_feed_hub.stop()
        for task in derivatives_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if delta_task is not None:
            await delta_research_manager.stop()
        if jobs_task is not None:
            jobs_task.cancel()
            try:
                await jobs_task
            except asyncio.CancelledError:
                pass
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        if snapshot_task is not None:
            snapshot_task.cancel()
            try:
                await snapshot_task
            except asyncio.CancelledError:
                pass
        if alert_task is not None:
            alert_task.cancel()
            try:
                await alert_task
            except asyncio.CancelledError:
                pass
        if master_bars_task is not None:
            master_bars_task.cancel()
            try:
                await master_bars_task
            except asyncio.CancelledError:
                pass
        if master_warm_task is not None:
            master_warm_task.cancel()
            try:
                await master_warm_task
            except asyncio.CancelledError:
                pass
        if forward_task is not None:
            forward_task.cancel()
            try:
                await forward_task
            except asyncio.CancelledError:
                pass
        if universe_task is not None:
            universe_task.cancel()
            try:
                await universe_task
            except asyncio.CancelledError:
                pass
        if corporate_task is not None:
            corporate_task.cancel()
            try:
                await corporate_task
            except asyncio.CancelledError:
                pass
        if keepalive_task is not None:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
        if valuation_task is not None:
            valuation_task.cancel()
            try:
                await valuation_task
            except asyncio.CancelledError:
                pass
        if research_daily_task is not None:
            research_daily_task.cancel()
            try:
                await research_daily_task
            except asyncio.CancelledError:
                pass
        if news_ingest_task is not None:
            news_ingest_task.cancel()
            try:
                await news_ingest_task
            except asyncio.CancelledError:
                pass
        if fundamentals_task is not None:
            fundamentals_task.cancel()
            try:
                await fundamentals_task
            except asyncio.CancelledError:
                pass
        if sentiment_task is not None:
            sentiment_task.cancel()
            try:
                await sentiment_task
            except asyncio.CancelledError:
                pass
        scheduler_election.release()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Backend foundation for a personal trading and investing copilot.",
    lifespan=lifespan,
)

from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

from app.request_timeout import RequestTimeoutMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=2048)

app.add_middleware(RequestTimeoutMiddleware, seconds=settings.request_timeout_seconds)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)

app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(alerts_router, prefix="/api/v1")
app.include_router(portfolios_router, prefix="/api/v1")
app.include_router(portfolio_transactions_router, prefix="/api/v1")
app.include_router(trades_router, prefix="/api/v1")
app.include_router(market_data_router, prefix="/api/v1")
app.include_router(indicators_router, prefix="/api/v1")
app.include_router(brokers_router, prefix="/api/v1")
app.include_router(corporate_actions_router, prefix="/api/v1")
app.include_router(news_router, prefix="/api/v1")
app.include_router(research_router, prefix="/api/v1")
app.include_router(position_theses_router, prefix="/api/v1")
app.include_router(delta_research_router, prefix="/api/v1")
app.include_router(execution_router, prefix="/api/v1")
app.include_router(forward_tests_router, prefix="/api/v1")
app.include_router(dhan_live_router, prefix="/api/v1")
app.include_router(crypto_delta_router, prefix="/api/v1")
app.include_router(strategies_router, prefix="/api/v1")
app.include_router(signals_lab_router, prefix="/api/v1")
app.include_router(var_strategy_router, prefix="/api/v1")
app.include_router(costs_router, prefix="/api/v1")
app.include_router(derivatives_router, prefix="/api/v1")
app.include_router(market_stream_router, prefix="/api/v1")


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "FinByte Nexus API is running", "docs": "/docs"}
