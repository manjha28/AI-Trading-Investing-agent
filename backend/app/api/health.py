from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import engine

router = APIRouter(tags=["health"])

BACKEND_DIR = Path(__file__).resolve().parents[2]


def expected_migration_heads() -> set[str]:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return set(ScriptDirectory.from_config(config).get_heads())


@router.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "trading-copilot-api"}


@router.get("/ready")
def readiness_check() -> JSONResponse:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(connection).get_current_heads())
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
        )

    if current != expected_migration_heads():
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "available",
                "migrations": "pending",
                "current_revisions": sorted(current),
                "expected_revisions": sorted(expected_migration_heads()),
            },
        )

    return JSONResponse(
        content={"status": "ready", "database": "available", "migrations": "current"},
    )
