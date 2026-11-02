import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from app.api.research import require_verified_session
from app.config import settings
from app.delta_research.collector import delta_research_manager
from app.delta_research.service import DISCLAIMER, DeltaResearchStatus, get_status
from app.delta_research.store import DeltaResearchStore
from app.models import UserSession

router = APIRouter(prefix="/delta-research", tags=["delta-research"])


def _read_store() -> DeltaResearchStore:
    if not settings.delta_research_enabled:
        raise HTTPException(status_code=503, detail="Delta public research is disabled.")
    if not settings.delta_research_db_path:
        raise HTTPException(status_code=503, detail="Delta research store is not configured.")
    try:
        return DeltaResearchStore(settings.delta_research_db_path, read_only=True)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Delta research store is unavailable.") from exc


@router.get("/status", response_model=DeltaResearchStatus)
def delta_research_status(
    response: Response,
    _: UserSession = Depends(require_verified_session),
) -> DeltaResearchStatus:
    response.headers["Cache-Control"] = "no-store"
    return get_status()


@router.get("/overview")
def delta_research_overview(
    response: Response,
    _: UserSession = Depends(require_verified_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with _read_store() as store:
        return {
            "summary": store.summary(),
            "feed": delta_research_manager.status(),
            "disclaimer": DISCLAIMER,
        }


@router.get("/markets")
def delta_research_markets(
    response: Response,
    search: str = Query(default="", max_length=80),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    _: UserSession = Depends(require_verified_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with _read_store() as store:
        return store.list_products(search, limit, offset) | {"disclaimer": DISCLAIMER}


@router.get("/markets/{product_id}")
def delta_research_market_detail(
    product_id: str,
    response: Response,
    candle_limit: int = Query(default=200, ge=1, le=1000),
    _: UserSession = Depends(require_verified_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with _read_store() as store:
        result = store.product_detail(product_id, candle_limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Market product was not found.")
    return result | {"disclaimer": DISCLAIMER}


@router.get("/events")
def delta_research_events(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(default=None, gt=0),
    _: UserSession = Depends(require_verified_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with _read_store() as store:
        return {"items": store.list_events(limit, before_id), "disclaimer": DISCLAIMER}


@router.get("/strategies")
def delta_research_strategies(
    response: Response,
    _: UserSession = Depends(require_verified_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [],
        "state": "not_available",
        "reason": "Strategy evidence is not collected by this public-feed milestone.",
        "disclaimer": DISCLAIMER,
    }


@router.get("/evaluations")
def delta_research_evaluations(
    response: Response,
    _: UserSession = Depends(require_verified_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [],
        "state": "not_available",
        "reason": "No validated evaluation evidence is stored.",
        "disclaimer": DISCLAIMER,
    }


@router.get("/leaderboard")
def delta_research_leaderboard(
    response: Response,
    _: UserSession = Depends(require_verified_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [],
        "state": "not_available",
        "reason": "A leaderboard requires validated evaluation evidence, which is not stored.",
        "disclaimer": DISCLAIMER,
    }


@router.get("/stream")
async def delta_research_stream(
    _: UserSession = Depends(require_verified_session),
) -> StreamingResponse:
    collector = delta_research_manager.collector
    if not settings.delta_research_enabled or collector is None:
        raise HTTPException(status_code=503, detail="Delta public feed is not running.")
    try:
        subscription = collector.subscribe()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Delta research stream is at capacity.") from exc

    async def events() -> AsyncIterator[str]:
        async for message in subscription:
            payload = json.dumps(message["data"], default=_json_default, separators=(",", ":"))
            yield f"event: {message['event']}\ndata: {payload}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
