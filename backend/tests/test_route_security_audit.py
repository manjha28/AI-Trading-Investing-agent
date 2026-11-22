"""Route security audit (behavioural, version-independent).

Sweeps every OpenAPI route so a newly added endpoint is secure by default:
  1. no session            -> 401 (except the public allowlist)
  2. unverified session    -> 403 (except routes explicitly open to unverified users)
  3. verified, no CSRF     -> 403 on every mutation (except non-mutating POSTs)
Adding a route to an allowlist below is a conscious, reviewable decision.
"""
from collections.abc import Iterator
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import User

METHODS = ("get", "post", "put", "patch", "delete")
UUID_PLACEHOLDER = "00000000-0000-0000-0000-000000000001"

# Reachable without a session by design.
PUBLIC = {
    ("get", "/"),
    ("get", "/api/v1/health"),
    ("get", "/api/v1/healthz"),         # liveness only: no database, no data
    ("get", "/api/v1/ready"),
    ("post", "/api/v1/auth/register"),
    ("post", "/api/v1/auth/login"),
    ("post", "/api/v1/auth/verify-email"),
}
# Streaming routes answer 503 before auth when the stream feature is switched off.
FEATURE_GATED = {("get", "/api/v1/market-stream/latest"), ("post", "/api/v1/market-stream/poll")}

# Auth/session management has its own flow and is covered in test_auth_portfolios.py.
def _is_auth_route(path: str) -> bool:
    return path.startswith("/api/v1/auth")


# Routes that intentionally serve authenticated-but-unverified users (existing behaviour).
# NOTE: AGENTS.md says every route is email-verified; these are a documented exception
# pending an owner decision. Do not extend this list without that decision.
OPEN_TO_UNVERIFIED_PREFIXES = (
    "/api/v1/portfolios",
    "/api/v1/trades",
    "/api/v1/market-data/watchlist",
    "/api/v1/market-data/instruments/{instrument_id}",
    "/api/v1/market-data/history/sync-jobs/",
)

# POSTs that compute/preview only and persist nothing, so CSRF is not required.
NON_MUTATING_POSTS = {
    ("post", "/api/v1/portfolios/{portfolio_id}/transactions/import/preview"),
    ("post", "/api/v1/signals-lab/validate"),
}


def _routes() -> list[tuple[str, str]]:
    return [
        (method, path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if method in METHODS and not _is_auth_route(path)
    ]


def _call(client: TestClient, method: str, path: str, headers: dict[str, str] | None = None):
    url = re.sub(r"\{[^}]*\}", UUID_PLACEHOLDER, path)
    kwargs = {"headers": headers or {}}
    if method != "get":
        kwargs["json"] = {}
    return client.request(method.upper(), url, **kwargs)


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _login(client: TestClient) -> str:
    creds = {"email": "audit@example.com", "password": "correct-horse-battery"}
    assert client.post("/api/v1/auth/register", json=creds).status_code == 202
    assert client.post("/api/v1/auth/login", json=creds).status_code == 200
    return client.cookies.get("csrf_token", "")


def test_every_route_requires_a_session(client: TestClient) -> None:
    offenders = []
    for method, path in _routes():
        if (method, path) in PUBLIC:
            continue
        status = _call(client, method, path).status_code
        allowed = {401, 503} if (method, path) in FEATURE_GATED else {401}
        if status not in allowed:
            offenders.append((method, path, status))
    assert not offenders, f"routes reachable without a session: {offenders}"


def test_unverified_sessions_are_rejected_except_documented_routes(client: TestClient) -> None:
    csrf = _login(client)
    offenders = []
    for method, path in _routes():
        if (method, path) in PUBLIC or (method, path) in FEATURE_GATED:
            continue
        if path.startswith(OPEN_TO_UNVERIFIED_PREFIXES):
            continue
        status = _call(client, method, path, {"X-CSRF-Token": csrf}).status_code
        if status != 403:
            offenders.append((method, path, status))
    assert not offenders, f"routes serving unverified users: {offenders}"


def test_mutations_require_csrf(client: TestClient, engine) -> None:
    _login(client)
    with Session(engine) as session:
        user = session.scalar(select(User))
        user.email_verified = True
        session.commit()
    offenders = []
    for method, path in _routes():
        if method == "get" or (method, path) in PUBLIC or (method, path) in FEATURE_GATED:
            continue
        if (method, path) in NON_MUTATING_POSTS:
            continue
        status = _call(client, method, path).status_code  # no CSRF header
        if status != 403:
            offenders.append((method, path, status))
    assert not offenders, f"mutations accepted without CSRF: {offenders}"
