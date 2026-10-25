from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.api import health
from app.main import app


client = TestClient(app)


def test_health_is_live_without_database(monkeypatch) -> None:
    def fail_if_called():
        raise AssertionError("liveness must not depend on the database")

    monkeypatch.setattr(health, "engine", SimpleNamespace(connect=fail_if_called))

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "trading-copilot-api"}


def _sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_readiness_succeeds_when_migrations_current(monkeypatch) -> None:
    test_engine = _sqlite_engine()
    head = next(iter(health.expected_migration_heads()))
    with test_engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    monkeypatch.setattr(health, "engine", test_engine)

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "available",
        "migrations": "current",
    }
    test_engine.dispose()


def test_readiness_fails_when_migrations_behind(monkeypatch) -> None:
    test_engine = _sqlite_engine()
    with test_engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0013_app_two_factor')"))
    monkeypatch.setattr(health, "engine", test_engine)

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload == {
        "status": "not_ready",
        "database": "available",
        "migrations": "pending",
        "current_revisions": ["0013_app_two_factor"],
        "expected_revisions": sorted(health.expected_migration_heads()),
    }
    assert "0023_md_provider_settings" in payload["expected_revisions"]
    test_engine.dispose()


def test_readiness_fails_when_database_is_unmigrated(monkeypatch) -> None:
    test_engine = _sqlite_engine()
    monkeypatch.setattr(health, "engine", test_engine)

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["migrations"] == "pending"
    test_engine.dispose()


def test_expected_head_is_single() -> None:
    assert len(health.expected_migration_heads()) == 1


def test_readiness_reports_database_outage(monkeypatch) -> None:
    def fail_connect():
        raise health.SQLAlchemyError("private connection details")

    monkeypatch.setattr(health, "engine", SimpleNamespace(connect=fail_connect))

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}
    assert "private connection details" not in response.text