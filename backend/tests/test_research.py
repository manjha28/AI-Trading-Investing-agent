from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import research_service
from app.db import Base, get_db
from app.main import app
from app.models import ResearchNote, ResearchSource, User


@pytest.fixture
def test_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(test_engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def fake_fetch(monkeypatch):
    pages = {"body": "alpha"}

    def fetch(url: str):
        return research_service.FetchedDocument(
            url=url,
            content_hash=research_service.hashlib.sha256(pages["body"].encode()).hexdigest(),
            title="Quarterly results",
            excerpt=pages["body"],
            fetched_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(research_service, "fetch_document", fetch)
    return pages


def register(client: TestClient, test_engine, email: str, verified: bool = True) -> None:
    password = "correct-horse-battery"
    assert client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).status_code == 200
    if verified:
        with Session(test_engine) as db:
            user = db.scalar(select(User).where(User.email == email))
            user.email_verified = True
            db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def instrument_id(client: TestClient) -> str:
    return client.get("/api/v1/market-data/instruments?query=ALPHA").json()[0]["id"]


def add_source(client: TestClient, url: str = "https://example.com/report") -> dict:
    response = client.post("/api/v1/research/sources", headers=csrf(client), json={"url": url})
    assert response.status_code in (200, 201), response.text
    return response.json()


def note_body(instrument: str, source: str, **overrides) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    body = {
        "instrument_id": instrument,
        "as_of": today,
        "summary": "Margins improved.",
        "confidence": "medium",
        "key_facts": [{"statement": "Margin up 2pp", "as_of": today, "source_ids": [source]}],
    }
    body.update(overrides)
    return body


def test_sourced_note_round_trip(client, test_engine, fake_fetch) -> None:
    register(client, test_engine, "a@example.com")
    inst = instrument_id(client)
    source = add_source(client)
    created = client.post(
        "/api/v1/research/notes", headers=csrf(client), json=note_body(inst, source["id"])
    )
    assert created.status_code == 201, created.text
    note = created.json()
    assert note["source_ids"] == [source["id"]]
    assert note["stale"] is False
    assert "not investment advice" in note["disclaimer"]
    detail = client.get(f"/api/v1/research/instruments/{inst}").json()
    assert [n["id"] for n in detail["notes"]] == [note["id"]]
    assert [s["id"] for s in detail["sources"]] == [source["id"]]
    patched = client.patch(
        f"/api/v1/research/notes/{note['id']}", headers=csrf(client), json={"confidence": "high"}
    )
    assert patched.json()["confidence"] == "high"
    assert client.delete(f"/api/v1/research/notes/{note['id']}", headers=csrf(client)).status_code == 204
    assert client.get(f"/api/v1/research/notes/{note['id']}").status_code == 404


def test_unsourced_note_is_rejected(client, test_engine, fake_fetch) -> None:
    register(client, test_engine, "a@example.com")
    inst = instrument_id(client)
    source = add_source(client)
    today = datetime.now(timezone.utc).date().isoformat()
    no_sources = note_body(
        inst, source["id"], key_facts=[{"statement": "x", "as_of": today, "source_ids": []}]
    )
    assert client.post("/api/v1/research/notes", headers=csrf(client), json=no_sources).status_code == 422
    no_facts = note_body(inst, source["id"], key_facts=[])
    assert client.post("/api/v1/research/notes", headers=csrf(client), json=no_facts).status_code == 422
    unknown = note_body(
        inst, source["id"],
        key_facts=[{"statement": "x", "as_of": today, "source_ids": ["00000000-0000-0000-0000-000000000001"]}],
    )
    assert client.post("/api/v1/research/notes", headers=csrf(client), json=unknown).status_code == 422
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(ResearchNote)) == 0


def test_service_rejects_source_without_fetched_at(test_engine) -> None:
    with Session(test_engine) as db:
        user = User(email="s@example.com", password_hash="x", created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        source = ResearchSource(
            user_id=user.id, url="https://example.com", fetched_at=datetime.now(timezone.utc),
            content_hash="h", created_at=datetime.now(timezone.utc),
        )
        db.add(source)
        db.commit()
        source.fetched_at = None
        with db.no_autoflush, pytest.raises(research_service.ResearchError):
            research_service.resolve_fact_sources(
                db, user.id, [{"statement": "x", "as_of": "2026-01-01", "source_ids": [str(source.id)]}]
            )


def test_stale_research_is_flagged(client, test_engine, fake_fetch) -> None:
    register(client, test_engine, "a@example.com")
    inst = instrument_id(client)
    source = add_source(client)
    old = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    body = note_body(
        inst, source["id"], as_of=old,
        key_facts=[{"statement": "old", "as_of": old, "source_ids": [source["id"]]}],
    )
    note = client.post("/api/v1/research/notes", headers=csrf(client), json=body).json()
    assert note["stale"] is True
    assert note["key_facts"][0]["stale"] is True


def test_duplicate_content_hash_deduplicates(client, test_engine, fake_fetch) -> None:
    register(client, test_engine, "a@example.com")
    first = add_source(client, "https://example.com/a")
    second = client.post(
        "/api/v1/research/sources", headers=csrf(client), json={"url": "https://mirror.example.com/b"}
    )
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert second.json()["deduplicated"] is True
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(ResearchSource)) == 1


def test_cross_user_isolation(client, test_engine, fake_fetch) -> None:
    register(client, test_engine, "a@example.com")
    inst = instrument_id(client)
    source = add_source(client)
    note = client.post(
        "/api/v1/research/notes", headers=csrf(client), json=note_body(inst, source["id"])
    ).json()
    client.cookies.clear()
    register(client, test_engine, "b@example.com")
    assert client.get("/api/v1/research/sources").json() == []
    assert client.get("/api/v1/research/notes").json() == []
    detail = client.get(f"/api/v1/research/instruments/{inst}").json()
    assert detail["notes"] == [] and detail["sources"] == []
    assert client.get(f"/api/v1/research/notes/{note['id']}").status_code == 404
    assert client.patch(
        f"/api/v1/research/notes/{note['id']}", headers=csrf(client), json={"summary": "x"}
    ).status_code == 404
    assert client.delete(f"/api/v1/research/notes/{note['id']}", headers=csrf(client)).status_code == 404
    stolen = client.post(
        "/api/v1/research/notes", headers=csrf(client), json=note_body(inst, source["id"])
    )
    assert stolen.status_code == 422
    # B fetching identical content gets B's own row, not A's.
    own = add_source(client)
    assert own["id"] != source["id"]


def test_requires_verified_email_and_csrf(client, test_engine, fake_fetch) -> None:
    assert client.get("/api/v1/research/notes").status_code == 401
    register(client, test_engine, "a@example.com", verified=False)
    response = client.get("/api/v1/research/notes")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "email_verification_required"
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        user.email_verified = True
        db.commit()
    assert client.post("/api/v1/research/sources", json={"url": "https://example.com"}).status_code == 403


def public_dns(monkeypatch, mapping: dict[str, str] | None = None) -> None:
    table = mapping or {}

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table.get(host, "93.184.216.34"), port))]

    monkeypatch.setattr(research_service.socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "https://user:pw@example.com/x",
        "https://",
    ],
)
def test_ssrf_guard_rejects_bad_schemes(monkeypatch, url) -> None:
    public_dns(monkeypatch)
    with pytest.raises(research_service.ResearchError):
        research_service.validate_source_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "::1", "fd00::1"]
)
def test_ssrf_guard_rejects_private_addresses(monkeypatch, address) -> None:
    public_dns(monkeypatch, {"internal.example": address})
    with pytest.raises(research_service.ResearchError):
        research_service.validate_source_url("https://internal.example/x")


def test_redirect_to_private_host_is_blocked(monkeypatch) -> None:
    public_dns(monkeypatch, {"internal.example": "10.0.0.5"})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://internal.example/secret"})
    )
    with pytest.raises(research_service.ResearchError, match="non-public"):
        research_service.fetch_document("https://example.com/start", transport=transport)


def test_redirect_to_http_is_blocked(monkeypatch) -> None:
    public_dns(monkeypatch)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "http://example.com/plain"})
    )
    with pytest.raises(research_service.ResearchError, match="https"):
        research_service.fetch_document("https://example.com/start", transport=transport)


def test_fetch_hashes_body_and_bounds_size(monkeypatch) -> None:
    public_dns(monkeypatch)
    page = b"<html><title>Results</title><body>Hello world</body></html>"
    ok = httpx.MockTransport(
        lambda request: httpx.Response(200, content=page, headers={"content-type": "text/html"})
    )
    document = research_service.fetch_document("https://example.com/r", transport=ok)
    assert document.content_hash == research_service.hashlib.sha256(page).hexdigest()
    assert document.title == "Results"
    assert document.excerpt == "Results Hello world"

    big = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=b"x" * (research_service.MAX_RESPONSE_BYTES + 1),
            headers={"content-type": "text/plain"},
        )
    )
    with pytest.raises(research_service.ResearchError, match="too large"):
        research_service.fetch_document("https://example.com/big", transport=big)

    binary = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x", headers={"content-type": "image/png"})
    )
    with pytest.raises(research_service.ResearchError, match="content type"):
        research_service.fetch_document("https://example.com/i", transport=binary)


def test_api_rejects_private_source_url(client, test_engine, monkeypatch) -> None:
    public_dns(monkeypatch, {"internal.example": "127.0.0.1"})
    register(client, test_engine, "a@example.com")
    response = client.post(
        "/api/v1/research/sources", headers=csrf(client), json={"url": "https://internal.example/x"}
    )
    assert response.status_code == 422
    assert "127.0.0.1" not in response.text
    http = client.post(
        "/api/v1/research/sources", headers=csrf(client), json={"url": "http://example.com/x"}
    )
    assert http.status_code == 422
