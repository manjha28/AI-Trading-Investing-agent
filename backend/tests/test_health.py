from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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


def test_readiness_succeeds_with_database(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(health, "engine", test_engine)

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "available"}
    test_engine.dispose()


def test_readiness_reports_database_outage(monkeypatch) -> None:
    def fail_connect():
        raise health.SQLAlchemyError("private connection details")

    monkeypatch.setattr(health, "engine", SimpleNamespace(connect=fail_connect))

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}
    assert "private connection details" not in response.text