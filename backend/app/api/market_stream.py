from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal
from uuid import UUID

import redis
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.api.market_data import _validated_dhan_tokens
from app.config import settings
from app.db import get_db
from app.market_data import MarketDataProviderError
from app.market_stream.events import parse_timestamp
from app.market_stream.poller import DhanBarPoller
from app.market_stream.state import LatestStateStore, ValkeyLatestStateStore
from app.market_stream.transport import EventPublisher, KafkaEventPublisher, MarketStreamError
from app.models import BrokerCredential, Instrument, InstrumentProviderIdentifier, UserSession
from app.services.strategy_engine import DISCLAIMER

router = APIRouter(prefix="/market-stream", tags=["market-stream"])
MAX_SNAPSHOT_INSTRUMENTS = 50
Interval = Literal["1m"]


def require_enabled() -> None:
    if not settings.market_stream_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "market_stream_disabled",
                "message": "Market streaming is disabled.",
                "status": "unavailable",
                "bars": [],
            },
        )


def require_verified_session(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before using market streaming.",
            },
        )
    return active_session


@lru_cache(maxsize=1)
def get_latest_store() -> LatestStateStore:
    client = redis.Redis.from_url(settings.valkey_url, socket_timeout=2, socket_connect_timeout=2)
    return ValkeyLatestStateStore(client)


@lru_cache(maxsize=1)
def get_event_publisher() -> EventPublisher:
    return KafkaEventPublisher(settings.kafka_bootstrap_servers)


class PollRequest(BaseModel):
    instrument_id: UUID
    interval: Interval = "1m"


def _freshness(interval: str, bar_timestamp: str, now: datetime) -> tuple[int, str]:
    age_seconds = int((now - parse_timestamp(bar_timestamp)).total_seconds())
    threshold = (
        settings.market_data_daily_freshness_seconds
        if interval == "1d"
        else settings.market_data_intraday_freshness_seconds
    )
    return age_seconds, "fresh" if age_seconds <= threshold else "stale"


def _stored_snapshot(db: Session, ids: list[UUID], now: datetime) -> dict:
    """The newest REAL stored bar per instrument (1-minute first, else daily), used when the live state store is disabled or unreachable. Labelled as stored, never as live."""
    from app.models import MarketBar
    bars, missing = [], []
    for instrument_id in ids:
        bar = None
        for interval in ("1m", "1d"):
            bar = db.scalar(select(MarketBar).where(MarketBar.instrument_id == instrument_id, MarketBar.interval == interval, MarketBar.is_synthetic.is_(False)).order_by(MarketBar.timestamp.desc()).limit(1))
            if bar is not None:
                break
        if bar is None:
            missing.append(str(instrument_id))
            continue
        ts = bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=timezone.utc)
        age = int((now - ts).total_seconds())
        threshold = settings.market_data_daily_freshness_seconds if bar.interval == "1d" else settings.market_data_intraday_freshness_seconds
        bars.append({"instrument_id": str(instrument_id), "provider": bar.source, "interval": bar.interval, "bar_timestamp": ts.isoformat(), "open": str(bar.open), "high": str(bar.high), "low": str(bar.low),
                     "close": str(bar.close), "volume": str(bar.volume), "ingested_at": (bar.retrieved_at if bar.retrieved_at.tzinfo else bar.retrieved_at.replace(tzinfo=timezone.utc)).isoformat(),
                     "age_seconds": age, "freshness": "fresh" if age <= threshold else "stale", "is_synthetic": False, "source": "stored"})
    status = "no_data" if not bars else "stale" if all(b["freshness"] == "stale" for b in bars) else "ok"
    return {"bars": bars, "missing_instrument_ids": missing, "unreadable_instrument_ids": [], "status": status, "source": "stored",
            "note": "Live state store is not available on this deployment; these are the newest stored bars.", "disclaimer": DISCLAIMER}


@router.get("/latest")
def latest_bars(
    instrument_ids: str = Query(..., max_length=2000),
    _session: UserSession = Depends(require_verified_session),
    store: LatestStateStore = Depends(get_latest_store),
    db: Session = Depends(get_db),
) -> dict:
    try:
        ids = list(dict.fromkeys(UUID(item.strip()) for item in instrument_ids.split(",") if item.strip()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_instrument_id") from exc
    if not ids or len(ids) > MAX_SNAPSHOT_INSTRUMENTS:
        raise HTTPException(status_code=422, detail="instrument_count_out_of_range")
    now = datetime.now(timezone.utc)
    if not settings.market_stream_enabled:
        return _stored_snapshot(db, ids, now)            # no live stream configured (for example on Fly): answer from stored real bars instead of failing every page
    try:
        raw = store.get_many(ids)
    except redis.RedisError:
        return _stored_snapshot(db, ids, now) | {"note": "Live state store is unreachable; these are the newest stored bars."}
    bars = []
    unreadable: list[str] = []
    for instrument_id, payload in raw.items():
        try:
            age_seconds, freshness = _freshness(payload["interval"], payload["bar_timestamp"], now)
            record = {k: payload[k] for k in (
                "provider", "interval", "bar_timestamp", "open", "high", "low", "close", "volume", "ingested_at")}
        except (KeyError, ValueError, TypeError):
            unreadable.append(instrument_id)  # a corrupt stored tick is reported, never repaired or guessed
            continue
        bars.append(
            {
                "instrument_id": instrument_id,
                "provider": payload["provider"],
                "interval": payload["interval"],
                "bar_timestamp": payload["bar_timestamp"],
                "open": payload["open"],
                "high": payload["high"],
                "low": payload["low"],
                "close": payload["close"],
                "volume": payload["volume"],
                "ingested_at": payload["ingested_at"],
                "age_seconds": age_seconds,
                "freshness": freshness,
                "is_synthetic": False,
            }
        )
    missing = [str(item) for item in ids if str(item) not in raw]
    status = "no_data" if not bars else "stale" if all(b["freshness"] == "stale" for b in bars) else "ok"
    return {
        "bars": bars,
        "missing_instrument_ids": missing,
        "unreadable_instrument_ids": unreadable,
        "status": status,
        "disclaimer": DISCLAIMER,
    }


@router.post("/poll", status_code=202, dependencies=[Depends(require_enabled)])
def queue_poll(
    payload: PollRequest,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_event_publisher),
) -> dict:
    if db.get(Instrument, payload.instrument_id) is None:
        raise HTTPException(status_code=404, detail="instrument_not_found")
    identifier = db.scalar(
        select(InstrumentProviderIdentifier).where(
            InstrumentProviderIdentifier.instrument_id == payload.instrument_id,
            InstrumentProviderIdentifier.provider == "dhan",
            InstrumentProviderIdentifier.is_verified.is_(True),
        )
    )
    if identifier is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "dhan_identifier_unverified", "message": "No verified Dhan identifier."},
        )
    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == active_session.user_id,
            BrokerCredential.broker == "dhan",
        )
    )
    if credential is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "dhan_not_connected", "message": "Connect Dhan to poll market data."},
        )
    tokens, error = _validated_dhan_tokens(credential)
    if error:
        raise HTTPException(status_code=409, detail={"code": "dhan_credentials_invalid", "message": error})
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=settings.market_stream_poll_lookback_days)
    try:
        result = DhanBarPoller(publisher).poll(
            instrument_id=payload.instrument_id,
            provider_instrument_id=identifier.provider_instrument_id,
            tokens=tokens,
            interval=payload.interval,
            start=start,
            end=end,
        )
    except MarketStreamError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "event_bus_unavailable", "message": "Kafka publish failed."},
        ) from exc
    except MarketDataProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "provider_unavailable", "message": "Dhan market data request failed."},
        ) from exc
    return {
        "status": "queued",
        "fetched": result.fetched,
        "published": result.published,
        "rejected": result.rejected,
        "disclaimer": DISCLAIMER,
    }
