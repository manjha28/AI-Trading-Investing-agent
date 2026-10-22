from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import research_service
from app.db import Base, get_db
from app.main import app
from app.models import InstrumentResearchBrief, InstrumentResearchRun, ResearchSource, User
from app.services import source_registry


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
def network_fixtures(monkeypatch):
    monkeypatch.setattr(
        research_service,
        "resolve_public_addresses",
        lambda _host, _port: ["93.184.216.34"],
    )
    monkeypatch.setattr(
        research_service,
        "fetch_robots_txt",
        lambda _url, *, user_agent: "User-agent: *\nAllow: /\n",
    )
    monkeypatch.setattr(source_registry, "_reserve_host_request", lambda _host: None)


def register(client: TestClient, test_engine, email: str) -> None:
    assert client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    ).status_code == 200
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.email_verified = True
        db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def get_instrument_id(client: TestClient) -> str:
    response = client.get("/api/v1/market-data/instruments?query=ALPHA")
    assert response.status_code == 200
    return response.json()[0]["id"]


def fake_document(url: str, *, body: str = "recorded source", news_items=None):
    return research_service.FetchedDocument(
        url=url,
        content_hash=research_service.hashlib.sha256(body.encode()).hexdigest(),
        title="Recorded headline",
        excerpt="Recorded excerpt",
        fetched_at=datetime.now(timezone.utc),
        news_items=news_items or [],
    )


def set_candidates(monkeypatch, *urls: str) -> None:
    monkeypatch.setattr(
        source_registry,
        "build_source_candidates",
        lambda _instrument, _links: [
            source_registry.SourceCandidate(
                url=url,
                label=f"Recorded source {index}",
                publisher="Recorded publisher",
                kind="news",
            )
            for index, url in enumerate(urls)
        ],
    )


def test_robots_disallow_blocks_fetch(client, test_engine, monkeypatch, network_fixtures) -> None:
    register(client, test_engine, "robots@example.com")
    instrument = get_instrument_id(client)
    url = "https://example.com/private/news.xml"
    set_candidates(monkeypatch, url)
    monkeypatch.setattr(
        research_service,
        "fetch_robots_txt",
        lambda _url, *, user_agent: "User-agent: *\nDisallow: /private\n",
    )
    fetched: list[str] = []
    monkeypatch.setattr(
        research_service,
        "fetch_document",
        lambda target, **_kwargs: fetched.append(target),
    )

    response = client.post(
        f"/api/v1/research/instruments/{instrument}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    assert fetched == []
    assert response.json()["failures"][0]["reason"] == "Source disallowed by robots.txt."


def test_per_host_rate_limit_enforces_one_request_each_second(monkeypatch) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(
        source_registry,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock["now"],
            sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        ),
    )
    source_registry._next_host_request.clear()

    source_registry._reserve_host_request("same.example")
    first_start = clock["now"]
    source_registry._reserve_host_request("same.example")

    assert clock["now"] - first_start >= 1
    source_registry._next_host_request.clear()


def test_duplicate_content_hash_reuses_existing_source(
    client, test_engine, monkeypatch, network_fixtures
) -> None:
    register(client, test_engine, "dedupe@example.com")
    instrument = get_instrument_id(client)
    set_candidates(
        monkeypatch,
        "https://example.com/first",
        "https://mirror.example.com/second",
    )
    monkeypatch.setattr(
        research_service,
        "fetch_document",
        lambda url, **_kwargs: fake_document(url),
    )

    response = client.post(
        f"/api/v1/research/instruments/{instrument}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["sources_succeeded"] == 2
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(ResearchSource)) == 1
    refs = response.json()["sections"]["news"]["sources"]
    assert refs[0]["source_id"] == refs[1]["source_id"]


def test_failed_source_is_recorded_and_run_is_partial(
    client, test_engine, monkeypatch, network_fixtures
) -> None:
    register(client, test_engine, "partial@example.com")
    instrument = get_instrument_id(client)
    set_candidates(
        monkeypatch,
        "https://success.example.com/news",
        "https://failure.example.com/news",
    )

    def fetch(url: str, **_kwargs):
        if "failure.example.com" in url:
            raise research_service.ResearchError("The source responded with HTTP 503.")
        return fake_document(url, news_items=[{
            "headline": "Recorded earnings headline",
            "publisher": "Recorded publisher",
            "published_at": None,
            "url": "https://success.example.com/article",
        }])

    monkeypatch.setattr(research_service, "fetch_document", fetch)
    response = client.post(
        f"/api/v1/research/instruments/{instrument}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "partial"
    assert run["sources_attempted"] == 2
    assert run["sources_succeeded"] == 1
    assert run["failures"][0]["reason"] == "The source responded with HTTP 503."
    with Session(test_engine) as db:
        saved = db.scalar(
            select(InstrumentResearchRun).where(
                InstrumentResearchRun.id == UUID(run["run_id"])
            )
        )
        assert saved is not None and saved.failures == run["failures"]


def test_empty_brief_marks_sections_unavailable(
    client, test_engine, monkeypatch, network_fixtures
) -> None:
    register(client, test_engine, "empty@example.com")
    instrument = get_instrument_id(client)
    set_candidates(monkeypatch)
    response = client.post(
        f"/api/v1/research/instruments/{instrument}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    brief = response.json()
    sections = brief["sections"]
    assert brief["status"] == "failed"
    assert sections["fundamentals"]["status"] == "unavailable"
    assert sections["technicals"]["status"] == "unavailable"
    assert sections["financials_trend"]["status"] == "unavailable"
    assert sections["news"]["status"] == "unavailable"
    assert sections["data_quality"]["items"]
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(InstrumentResearchBrief)) == 1


def test_source_links_are_owner_scoped(client, test_engine, monkeypatch) -> None:
    monkeypatch.setattr(
        research_service,
        "resolve_public_addresses",
        lambda _host, _port: ["93.184.216.34"],
    )
    register(client, test_engine, "owner@example.com")
    instrument = get_instrument_id(client)
    created = client.post(
        f"/api/v1/research/instruments/{instrument}/source-links",
        headers=csrf(client),
        json={"url": "https://example.com/investors", "label": "Investor relations"},
    )
    assert created.status_code == 201, created.text

    client.cookies.clear()
    register(client, test_engine, "other@example.com")
    assert client.get(
        f"/api/v1/research/instruments/{instrument}/source-links"
    ).json() == []
    assert client.delete(
        f"/api/v1/research/source-links/{created.json()['id']}",
        headers=csrf(client),
    ).status_code == 404
