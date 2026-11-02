from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel

from app.config import settings
from app.delta_research.collector import delta_research_manager
from app.delta_research.store import DeltaResearchStore

DISCLAIMER = "Decision support, not investment advice."

StoreStatus = Literal[
    "disabled", "not_configured", "not_found", "invalid", "present"
]


class DeltaResearchStatus(BaseModel):
    enabled: bool
    milestone: int
    mode: Literal["public_research_read_only"]
    store_status: StoreStatus
    store_modified_at: datetime | None
    store_age_seconds: int | None
    live_feeds: str
    account_monitor: Literal["disabled"]
    warnings: list[str]
    disclaimer: str
    feed: dict[str, Any]
    collection_interval_seconds: int


def get_status(now: datetime | None = None) -> DeltaResearchStatus:
    now = now or datetime.now(timezone.utc)
    modified_at: datetime | None = None
    warnings: list[str] = []
    feed = delta_research_manager.status()

    if not settings.delta_research_enabled:
        store_status: StoreStatus = "disabled"
        warnings.append("Delta research is disabled. Set DELTA_RESEARCH_ENABLED=true to enable it.")
    elif not settings.delta_research_db_path:
        store_status = "not_configured"
        warnings.append("No research data store is configured.")
    else:
        path = Path(settings.delta_research_db_path)
        try:
            stat = path.stat() if path.is_file() else None
        except OSError:
            stat = None
        if stat is None:
            store_status = "not_found"
            warnings.append("The configured research data store was not found.")
        else:
            modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            try:
                with DeltaResearchStore(path, read_only=True) as store:
                    store.summary()
                store_status = "present"
            except (OSError, RuntimeError, sqlite3.Error, ValueError):
                store_status = "invalid"
                warnings.append("The configured research store could not be read.")

    if settings.delta_research_enabled and feed["state"] in {
        "degraded", "reconnecting", "stopped_after_failures"
    }:
        warnings.append("The public market feed is not healthy; see feed health for details.")
    if settings.delta_research_enabled and feed.get("last_error"):
        warnings.append("The public research collector reports an error; see feed health for details.")

    return DeltaResearchStatus(
        enabled=settings.delta_research_enabled,
        milestone=2,
        mode="public_research_read_only",
        store_status=store_status,
        store_modified_at=modified_at,
        store_age_seconds=(
            max(0, int((now - modified_at).total_seconds())) if modified_at else None
        ),
        live_feeds=str(feed["state"]),
        account_monitor="disabled",
        warnings=warnings,
        disclaimer=DISCLAIMER,
        feed=feed,
        collection_interval_seconds=settings.delta_research_refresh_seconds,
    )
