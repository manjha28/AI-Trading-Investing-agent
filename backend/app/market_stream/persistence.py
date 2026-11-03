from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.market_stream.events import MarketBarEvent
from app.market_stream.state import LatestStateStore
from app.models import MarketBar


def persist_event(db: Session, event: MarketBarEvent) -> bool:
    """Idempotently store one real bar. Returns True only when a new row was written."""
    values = {
        "id": uuid4(),
        "instrument_id": event.instrument_id,
        "interval": event.interval,
        "timestamp": event.bar_timestamp,
        "open": event.open,
        "high": event.high,
        "low": event.low,
        "close": event.close,
        "volume": event.volume,
        "source": event.provider,
        "retrieved_at": event.ingested_at,
        "is_synthetic": False,
    }
    dialect = db.get_bind().dialect.name
    conflict_columns = ["instrument_id", "interval", "timestamp", "source"]
    if dialect == "postgresql":
        statement = postgres_insert(MarketBar).values(**values).on_conflict_do_nothing(
            index_elements=conflict_columns
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(MarketBar).values(**values).on_conflict_do_nothing(
            index_elements=conflict_columns
        )
    else:
        exists = db.scalar(
            select(MarketBar.id).where(
                MarketBar.instrument_id == event.instrument_id,
                MarketBar.interval == event.interval,
                MarketBar.timestamp == event.bar_timestamp,
                MarketBar.source == event.provider,
            )
        )
        if exists is not None:
            return False
        db.add(MarketBar(**values))
        db.commit()
        return True
    result = db.execute(statement)
    db.commit()
    return max(0, result.rowcount or 0) > 0


def apply_event(db: Session, store: LatestStateStore, event: MarketBarEvent) -> dict[str, bool]:
    """Persist first (source of truth), then refresh the live latest-state cache."""
    inserted = persist_event(db, event)
    updated_latest = store.upsert_if_newer(event)
    return {"inserted": inserted, "latest_updated": updated_latest}
