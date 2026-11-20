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
        if corporate_task is not None:
            corporate_task.cancel()
            try:
                await corporate_task
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
app.include_router(strategies_router, prefix="/api/v1")
app.include_router(signals_lab_router, prefix="/api/v1")
app.include_router(var_strategy_router, prefix="/api/v1")
app.include_router(costs_router, prefix="/api/v1")
app.include_router(derivatives_router, prefix="/api/v1")
app.include_router(market_stream_router, prefix="/api/v1")


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Trading Copilot API is running", "docs": "/docs"}
