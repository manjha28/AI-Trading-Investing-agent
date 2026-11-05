from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.db import Base, get_db
from app.main import app
from app.models import NewsIngestionFailure, NewsItem, NewsSourcePoll
from app.news_ingestion import _fetch_gdelt, _parse_rss, _resolve_nse_instrument, _stamp, _store, _story_key
from app.api.news import _verified


def test_source_publication_time_is_utc_and_distinct_from_fetch_time():
    published = _stamp("04-Nov-2026 16:28:59", ist=True)
    assert published == datetime(2026, 11, 4, 10, 58, 59, tzinfo=timezone.utc)
    assert _stamp("not-a-publication-time", ist=True) is None


def test_missing_or_naive_source_time_is_not_replaced_with_fetch_time():
    assert _stamp(None) is None
    assert _stamp("2026-11-04 16:28:59") is None


def test_ingestion_persistence_uses_source_scoped_conflict_guard():
    import inspect

    source = inspect.getsource(_store)
    assert "on_conflict_do_nothing" in source
    assert 'index_elements=["source", "external_id"]' in source
    assert any(tuple(c.name for c in constraint.columns) == ("source", "external_id")
               for constraint in NewsItem.__table__.constraints if constraint.name == "uq_news_items_source_external_id")


def test_source_failure_is_persisted_and_ingestion_continues(monkeypatch):
    import nse
    from app.news_ingestion import ingest

    def broken_client(**_kwargs):
        raise RuntimeError("NSE unavailable")

    monkeypatch.setattr(nse, "NSE", broken_client)
    monkeypatch.setattr("app.news_ingestion._fetch_google_news", lambda _query: [])
    monkeypatch.setattr("app.news_ingestion._fetch_gdelt", lambda: [])
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            result = ingest(db)
            assert result["failures"] >= 1
            failures = db.scalars(select(NewsIngestionFailure)).all()
            assert any(f.source == "nse" and "NSE unavailable" in f.detail for f in failures)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_read_api_merges_and_filters_stored_news(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    owner = uuid4()

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_verified] = lambda: SimpleNamespace(user_id=owner)
    try:
        with Session(engine) as db:
            db.add_all([
                NewsItem(external_id="nse:1", source="nse", published_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
                         retrieved_at=datetime(2026, 11, 5, tzinfo=timezone.utc), category="Board Meeting",
                         headline="Board notice", raw_payload={}),
                NewsItem(external_id="yahoo:1", source="yfinance", published_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
                         retrieved_at=datetime(2026, 11, 5, tzinfo=timezone.utc), category="Market",
                         headline="Market story", raw_payload={}),
            ])
            db.commit()
        with TestClient(app) as client:
            response = client.get("/api/v1/news", params={"source": "nse", "category": "Board"})
        assert response.status_code == 200
        assert [row["headline"] for row in response.json()] == ["Board notice"]
        assert response.json()[0]["published_at_ist"].endswith("+05:30")
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_google_and_publisher_rss_parse_publication_and_metadata():
    body = b'''<rss><channel><item><title>Company wins order</title><link>https://publisher.example/story</link>
      <guid>story-1</guid><pubDate>Wed, 04 Nov 2026 10:00:00 GMT</pubDate>
      <description>Feed summary only</description><source>Publisher</source></item></channel></rss>'''
    rows = _parse_rss(body, source="google_news")
    assert rows[0]["title"] == "Company wins order"
    assert rows[0]["published_at"] == datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
    assert rows[0]["description"] == "Feed summary only"
    assert rows[0]["publisher"] == "Publisher"


def test_successful_empty_rss_is_distinct_from_failed_fetch(monkeypatch):
    assert _parse_rss(b"<rss><channel/></rss>", source="google_news") == []

    def unavailable(_url):
        raise RuntimeError("rate_limited")

    monkeypatch.setattr("app.news_ingestion._get_source", unavailable)
    with pytest.raises(RuntimeError, match="rate_limited"):
        from app.news_ingestion import _fetch_google_news
        _fetch_google_news("India stocks")


def test_gdelt_article_response_parser(monkeypatch):
    payload = {"articles": [{"url": "https://publisher.example/a", "title": "India company reports results",
        "seendate": "20261104T100000Z", "domain": "publisher.example", "snippet": "Metadata snippet"}]}
    monkeypatch.setattr("app.news_ingestion._get_source", lambda _url: __import__("json").dumps(payload).encode())
    rows = _fetch_gdelt()
    assert rows[0]["url"] == "https://publisher.example/a"
    assert rows[0]["published_at"] == datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
    assert rows[0]["description"] == "Metadata snippet"


def test_cross_source_story_identity_ignores_publisher_source():
    stamp = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
    assert _story_key("Company wins a major order!", stamp) == _story_key("Company wins a major order", stamp)


def test_company_matching_leaves_ambiguous_and_unmatched_symbols_unresolved():
    from app.models import Instrument

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            def instrument(symbol, venue, suffix):
                return Instrument(symbol=symbol, display_name=f"{symbol} {suffix}", asset_class="equity",
                    venue=venue, currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False,
                    relink_required=False, created_at=datetime.now(timezone.utc))
            unique = instrument("UNIQUE", "NSE", "Ltd")
            db.add_all([unique, instrument("AMBIG", "NSE", "One"), instrument("AMBIG", "NSE", "Two")])
            db.flush()
            assert _resolve_nse_instrument(db, "UNIQUE") == unique
            assert _resolve_nse_instrument(db, "AMBIG") is None
            assert _resolve_nse_instrument(db, "FOREIGN") is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_yahoo_news_fails_fast_after_one_request(monkeypatch):
    from app.market_data import MarketDataProviderError
    from app.provider_adapters import YahooFinanceProvider

    attempts = []

    def request(_function, *_args, _max_attempts=3, **_kwargs):
        attempts.append(_max_attempts)
        raise MarketDataProviderError("rate_limited")

    monkeypatch.setattr(YahooFinanceProvider, "_thread_call", staticmethod(request))
    with pytest.raises(MarketDataProviderError, match="rate_limited"):
        YahooFinanceProvider().get_news("TEST.NS")
    assert attempts == [1]


def test_yahoo_rate_limit_sets_shared_cooldown(monkeypatch):
    import time
    import app.provider_adapters as adapters
    from app.market_data import MarketDataProviderError

    class LimitedExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("HTTP 429 Too Many Requests")

    monkeypatch.setattr(adapters, "_YFINANCE_EXECUTOR", LimitedExecutor())
    monkeypatch.setattr(adapters, "_yfinance_cooldown_until", 0.0)
    with pytest.raises(MarketDataProviderError, match="rate_limited"):
        adapters.YahooFinanceProvider._thread_call(lambda: None, _max_attempts=1)
    assert adapters._yfinance_cooldown_until >= time.monotonic() + 899


def test_source_health_does_not_call_rate_limited_source_successful_empty():
    from app.api.news import news_source_health

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(NewsSourcePoll(source="gdelt", last_attempted_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
                articles_discovered=0, articles_inserted=0, duplicates=0, failure_count=1,
                last_error_category="rate_limited", rate_limited=True))
            db.commit()
            result = news_source_health(db=db, session=SimpleNamespace(user_id=uuid4()))
        assert result[0]["status"] == "unavailable"
        assert result[0]["rate_limited"] is True
        assert result[0]["last_error_category"] == "rate_limited"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_source_health_marks_partial_when_successful_poll_has_failures():
    from app.api.news import news_source_health

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(NewsSourcePoll(source="nse", last_attempted_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
                last_successful_at=datetime(2026, 11, 5, tzinfo=timezone.utc), articles_discovered=0,
                articles_inserted=0, duplicates=0, failure_count=2,
                last_error_category="upstream_failure", rate_limited=False))
            db.commit()
            result = news_source_health(db=db, session=SimpleNamespace(user_id=uuid4()))
        assert result[0]["status"] == "partial"
        assert result[0]["last_error_category"] == "upstream_failure"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
