from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
from app.models import (
    Instrument,
    InstrumentFundamental,
    InstrumentResearchBrief,
    InstrumentResearchRun,
    MarketBar,
    ResearchSource,
    User,
)
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


def set_instrument_real(test_engine, instrument_id: str) -> None:
    with Session(test_engine) as db:
        instrument = db.get(Instrument, UUID(instrument_id))
        assert instrument is not None
        instrument.is_synthetic = False
        db.commit()


def add_daily_bars(test_engine, instrument_id: str, count: int, *, synthetic: bool) -> None:
    now = datetime.now(timezone.utc)
    with Session(test_engine) as db:
        for offset in range(count):
            close = Decimal("100") + Decimal(count - offset) / Decimal("10")
            db.add(MarketBar(
                instrument_id=UUID(instrument_id),
                interval="1d",
                timestamp=now - timedelta(days=offset),
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1000"),
                source="mock" if synthetic else "yfinance",
                retrieved_at=now,
                is_synthetic=synthetic,
            ))
        db.commit()


def fake_document(url: str, *, body: str = "recorded source", news_items=None):
    return research_service.FetchedDocument(
        url=url,
        content_hash=research_service.hashlib.sha256(body.encode()).hexdigest(),
        title="Recorded headline",
        excerpt="Recorded excerpt",
        fetched_at=datetime.now(timezone.utc),
        news_items=news_items or [],
    )


def test_atom_feed_preserves_article_link_and_publication_time() -> None:
    body = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Quarterly results published</title>
        <link rel="alternate" href="https://issuer.example/news/results"/>
        <published>2026-10-01T03:00:00+05:30</published>
        <source><title>Issuer IR</title></source>
      </entry>
    </feed>"""

    items = research_service._news_items(body, "application/atom+xml")

    assert items == [{
        "headline": "Quarterly results published",
        "publisher": "Issuer IR",
        "published_at": "2026-09-30T21:30:00+00:00",
        "url": "https://issuer.example/news/results",
    }]


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


def test_failed_news_source_keeps_usable_technical_report_partial(
    client, test_engine, monkeypatch, network_fixtures
) -> None:
    register(client, test_engine, "usable-partial@example.com")
    instrument_id = get_instrument_id(client)
    set_instrument_real(test_engine, instrument_id)
    add_daily_bars(test_engine, instrument_id, 30, synthetic=False)
    set_candidates(monkeypatch, "https://example.com/news.xml")
    monkeypatch.setattr(
        research_service,
        "fetch_document",
        lambda _url, **_kwargs: (_ for _ in ()).throw(
            research_service.ResearchError("The source responded with HTTP 503.")
        ),
    )

    response = client.post(
        f"/api/v1/research/instruments/{instrument_id}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    brief = response.json()
    assert brief["status"] == "partial"
    assert brief["sections"]["technicals"]["status"] == "available"
    assert brief["sections"]["news"]["status"] == "unavailable"
    assert brief["failures"][0]["reason"] == "The source responded with HTTP 503."


def test_synthetic_bars_are_not_used_for_real_instrument_technicals(
    client, test_engine, monkeypatch, network_fixtures
) -> None:
    register(client, test_engine, "synthetic-bars@example.com")
    instrument_id = get_instrument_id(client)
    set_instrument_real(test_engine, instrument_id)
    add_daily_bars(test_engine, instrument_id, 15, synthetic=True)
    with Session(test_engine) as db:
        instrument = db.get(Instrument, UUID(instrument_id))
        synthetic_count = db.scalar(
            select(func.count()).select_from(MarketBar).where(
                MarketBar.instrument_id == UUID(instrument_id),
                MarketBar.is_synthetic.is_(True),
            )
        )
        assert instrument is not None and not instrument.is_synthetic
        assert synthetic_count == 15
    set_candidates(monkeypatch)

    response = client.post(
        f"/api/v1/research/instruments/{instrument_id}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    technicals = response.json()["sections"]["technicals"]
    assert technicals["status"] == "unavailable"
    assert technicals["bars_available"] == 0
    assert "Only synthetic daily bars are stored" in technicals["message"]


def test_insufficient_real_history_is_reported_without_fabricating_bars(
    client, test_engine, monkeypatch, network_fixtures
) -> None:
    register(client, test_engine, "short-history@example.com")
    instrument_id = get_instrument_id(client)
    set_instrument_real(test_engine, instrument_id)
    add_daily_bars(test_engine, instrument_id, 1, synthetic=False)
    set_candidates(monkeypatch)

    response = client.post(
        f"/api/v1/research/instruments/{instrument_id}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    technicals = response.json()["sections"]["technicals"]
    assert technicals["status"] == "insufficient_history"
    assert technicals["bars_available"] == 1
    assert all(item["bars_used"] == 1 for item in technicals["indicators"].values())


def test_all_null_fundamentals_are_unavailable(
    client, test_engine, monkeypatch, network_fixtures
) -> None:
    register(client, test_engine, "null-fundamentals@example.com")
    instrument_id = get_instrument_id(client)
    set_instrument_real(test_engine, instrument_id)
    with Session(test_engine) as db:
        db.add(InstrumentFundamental(
            instrument_id=UUID(instrument_id),
            period_type="annual",
            period_end=date(2025, 12, 31),
            currency="USD",
            source="yfinance",
            fetched_at=datetime.now(timezone.utc),
        ))
        db.commit()
    set_candidates(monkeypatch)

    response = client.post(
        f"/api/v1/research/instruments/{instrument_id}/run",
        headers=csrf(client),
    )

    assert response.status_code == 200, response.text
    fundamentals = response.json()["sections"]["fundamentals"]
    assert fundamentals["status"] == "unavailable"
    assert any(
        item["reason"] == "No non-null provider fundamentals are stored."
        for item in response.json()["data_quality"]
    )


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
