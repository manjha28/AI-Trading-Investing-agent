import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.brokers import router as brokers_router
from app.api.health import router as health_router
from app.api.market_data import router as market_data_router
from app.api.portfolios import router as portfolios_router
from app.api.portfolio_transactions import router as portfolio_transactions_router
from app.api.trades import router as trades_router
from app.config import settings
from app.market_data_scheduler import market_data_refresh_loop


class BrokerCallbackAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if isinstance(arguments, tuple) and len(arguments) >= 5:
            request_path = arguments[2]
            if isinstance(request_path, str) and "/api/v1/brokers/" in request_path and "/callback" in request_path:
                record.args = (*arguments[:2], request_path.split("?", 1)[0] + "?<redacted>", *arguments[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(BrokerCallbackAccessLogFilter())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler_task = None
    if settings.market_data_scheduler_enabled and "pytest" not in sys.modules:
        scheduler_task = asyncio.create_task(market_data_refresh_loop())
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Backend foundation for a personal trading and investing copilot.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_origin_regex=r"https://.*\.app\.github\.dev",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)

app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(portfolios_router, prefix="/api/v1")
app.include_router(portfolio_transactions_router, prefix="/api/v1")
app.include_router(trades_router, prefix="/api/v1")
app.include_router(market_data_router, prefix="/api/v1")
app.include_router(brokers_router, prefix="/api/v1")


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Trading Copilot API is running", "docs": "/docs"}
