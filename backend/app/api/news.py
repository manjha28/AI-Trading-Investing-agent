from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.db import get_db
from app.models import Instrument, NewsIngestionFailure, NewsItem, NewsSourcePoll, UserSession, Watchlist, WatchlistInstrument
from app.news_ingestion import ingest
from app.services import news_sentiment

router = APIRouter(prefix="/news", tags=["news and filings"])


class SentimentAggregateResponse(BaseModel):
    status: str
    scope: str
    scope_id: str | None
    sentiment: Literal["positive", "negative", "neutral", "mixed"] | None
    interpretation: str | None = None
    score: float | None
    strength: float | None
    confidence_score: int | None
    event_count: int
    article_count: int
    counts: dict[str, int]
    contradictory_evidence: bool
    freshness: dict[str, Any]
    coverage: dict[str, Any]
    aggregation: dict[str, Any]
    events: list[dict[str, Any]]
    instrument: dict[str, str | None] | None = None


class SentimentBatchResponse(BaseModel):
    examined: int
    analyzed: int
    already_analyzed: int
    failed: int = 0
    skipped_locked: int = 0


class SentimentHistoryPoint(BaseModel):
    date: str
    score: float
    event_count: int
    article_count: int


def _verified(session: UserSession = Depends(get_active_session)) -> UserSession:
    if not session.user.email_verified:
        raise HTTPException(status_code=403, detail="Email verification required")
    return session


@router.post("/ingest")
def ingest_news(filings_days: int = Query(30, ge=1, le=90), news_days: int = Query(2, ge=1, le=30),
                db: Session = Depends(get_db), session: UserSession = Depends(_verified),
                _csrf: None = Depends(require_csrf)):
    return ingest(db, filings_days, news_days)


@router.get("/sources/health")
def news_source_health(db: Session = Depends(get_db), session: UserSession = Depends(_verified)):
    rows = db.scalars(select(NewsSourcePoll).order_by(NewsSourcePoll.source)).all()
    return [{"source": row.source,
             "status": "unavailable" if row.last_successful_at is None else
                      "partial" if row.last_error_category or row.rate_limited else
                      "successful_empty" if row.articles_discovered == 0 else "available",
             "last_attempted_at": row.last_attempted_at,
             "last_successful_poll_at": row.last_successful_at,
             "last_successful_article_at": row.last_article_at,
             "articles_discovered": row.articles_discovered,
             "articles_inserted": row.articles_inserted,
             "duplicate_count": row.duplicates,
             "failure_count": row.failure_count,
             "last_error_category": row.last_error_category or ("upstream_failure" if row.failure_count else None),
             "rate_limited": row.rate_limited} for row in rows]


@router.get("")
def read_news(instrument_id: UUID | None = None, watchlist_id: UUID | None = None,
              source: str | None = None, category: str | None = None,
              from_date: date | None = None, to_date: date | None = None,
              limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
              db: Session = Depends(get_db), session: UserSession = Depends(_verified)):
    stmt = select(NewsItem)
    if instrument_id:
        stmt = stmt.where(or_(NewsItem.instrument_id == instrument_id, NewsItem.instrument_id.is_(None)))
    if watchlist_id:
        owned = db.scalar(select(Watchlist.id).where(Watchlist.id == watchlist_id, Watchlist.user_id == session.user_id))
        if not owned:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        ids = select(WatchlistInstrument.instrument_id).where(WatchlistInstrument.watchlist_id == watchlist_id)
        stmt = stmt.where(or_(NewsItem.instrument_id.in_(ids), NewsItem.instrument_id.is_(None)))
    if source:
        stmt = stmt.where(NewsItem.source == source)
    if category:
        stmt = stmt.where(NewsItem.category.ilike(f"%{category}%"))
    if from_date:
        stmt = stmt.where(NewsItem.published_at >= datetime.combine(from_date, time.min, timezone.utc))
    if to_date:
        stmt = stmt.where(NewsItem.published_at < datetime.combine(to_date + timedelta(days=1), time.min, timezone.utc))
    rows = db.scalars(stmt.order_by(NewsItem.published_at.desc()).limit(limit).offset(offset)).all()
    return [{"external_id": r.external_id, "source": r.source, "source_url": r.source_url,
             "instrument_id": r.instrument_id, "symbol_raw": r.symbol_raw, "published_at": r.published_at,
             "published_at_ist": r.published_at.astimezone(ZoneInfo("Asia/Kolkata")).isoformat(),
             "retrieved_at": r.retrieved_at, "publisher": r.publisher, "category": r.category,
             "headline": r.headline, "body": r.body,
             "full_text_status": (r.raw_payload or {}).get("full_text_status", "restricted_or_unavailable"),
             "published_time_kind": (r.raw_payload or {}).get("published_time_kind", "publisher_publication_time"),
             "raw_payload": r.raw_payload} for r in rows]


@router.post("/sentiment/analyze", response_model=SentimentBatchResponse)
def analyze_news_sentiment(limit: int = Query(500, ge=1, le=5000),
                           period_days: int = Query(30, ge=1, le=365),
                           db: Session = Depends(get_db), session: UserSession = Depends(_verified),
                           _csrf: None = Depends(require_csrf)):
    return news_sentiment.analyze_news_items(db, limit=limit, period_days=period_days)


@router.get("/sentiment/market", response_model=SentimentAggregateResponse)
def market_sentiment(period_days: int = Query(7, ge=1, le=365), db: Session = Depends(get_db),
                     session: UserSession = Depends(_verified)):
    return news_sentiment.aggregate_market_sentiment(db, period_days=period_days)


@router.get("/sentiment/status")
def sentiment_processing_status(period_days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db),
                                session: UserSession = Depends(_verified)):
    return {"classifier_version": news_sentiment.CLASSIFIER_VERSION,
            "analysis": news_sentiment.analysis_state(db, period_days=period_days)}


@router.get("/sentiment/instruments", response_model=list[SentimentAggregateResponse])
def instrument_sentiments(period_days: int = Query(7, ge=1, le=365), limit: int = Query(100, ge=1, le=500),
                          db: Session = Depends(get_db), session: UserSession = Depends(_verified)):
    return news_sentiment.list_instrument_sentiment(db, period_days=period_days, limit=limit)


@router.get("/sentiment/instruments/{instrument_id}", response_model=SentimentAggregateResponse)
def one_instrument_sentiment(instrument_id: UUID, period_days: int = Query(7, ge=1, le=365),
                             db: Session = Depends(get_db), session: UserSession = Depends(_verified)):
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    return news_sentiment.aggregate_instrument_sentiment(db, instrument_id, period_days=period_days)


@router.get("/sentiment/sectors", response_model=list[SentimentAggregateResponse])
def sector_sentiments(period_days: int = Query(7, ge=1, le=365), db: Session = Depends(get_db),
                      session: UserSession = Depends(_verified)):
    return news_sentiment.list_sector_sentiment(db, period_days=period_days)


@router.get("/sentiment/sectors/{sector}", response_model=SentimentAggregateResponse)
def one_sector_sentiment(sector: str,
                         period_days: int = Query(7, ge=1, le=365), db: Session = Depends(get_db),
                         session: UserSession = Depends(_verified)):
    return news_sentiment.aggregate_sector_sentiment(db, sector, period_days=period_days)


@router.get("/sentiment/recent")
def recent_news_sentiment(limit: int = Query(50, ge=1, le=200), period_days: int = Query(30, ge=1, le=365),
                          instrument_id: UUID | None = None, db: Session = Depends(get_db),
                          session: UserSession = Depends(_verified)):
    return news_sentiment.recent_sentiment(db, period_days=period_days, limit=limit, instrument_id=instrument_id)


@router.get("/sentiment/history", response_model=list[SentimentHistoryPoint])
def sentiment_history(period_days: int = Query(90, ge=1, le=365), instrument_id: UUID | None = None,
                      db: Session = Depends(get_db), session: UserSession = Depends(_verified)):
    if instrument_id and db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    return news_sentiment.sentiment_history(db, period_days=period_days, instrument_id=instrument_id)


@router.get("/sentiment/context/{instrument_id}")
def levels_giver_sentiment_context(instrument_id: UUID, period_days: int = Query(7, ge=1, le=365),
                                   db: Session = Depends(get_db), session: UserSession = Depends(_verified)):
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    return news_sentiment.levels_sentiment_context(db, instrument_id, period_days=period_days)
