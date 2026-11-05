from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.auth import require_csrf
from app.api.news import _verified
from app.db import Base, get_db
from app.main import app
from app.models import Instrument, InstrumentSnapshot, NewsItem, NewsSentimentAnalysis, NewsSentimentFailure, NewsSourcePoll
from app.services.news_sentiment import (
    CLASSIFIER_VERSION,
    _base_weight,
    aggregate_instrument_sentiment,
    aggregate_market_sentiment,
    aggregate_sector_sentiment,
    analysis_state,
    analyze_news_items,
    classify_news_item,
    _clean_summary,
    levels_sentiment_context,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def _news(source="google_news", headline="Company reports update", *, body=None, stamp=NOW,
          instrument_id=None, payload=None):
    return NewsItem(external_id=f"{source}:{uuid4()}", source=source,
        source_url="https://publisher.example/story", instrument_id=instrument_id,
        symbol_raw=None, published_at=stamp, retrieved_at=NOW, publisher="Publisher",
        category="market_news", headline=headline, body=body,
        raw_payload=payload or {})


def _instrument(symbol="TEST"):
    return Instrument(symbol=symbol, display_name=f"{symbol} Limited", asset_class="equity",
        venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata",
        is_synthetic=False, relink_required=False, created_at=NOW)


def _source(db, source, *, success=True, rate_limited=False):
    db.add(NewsSourcePoll(source=source, last_attempted_at=NOW,
        last_successful_at=NOW if success else None, last_article_at=NOW if success else None,
        articles_discovered=1 if success else 0, articles_inserted=1 if success else 0,
        duplicates=0, failure_count=0 if success else 1,
        last_error_category=None if success else "rate_limited", rate_limited=rate_limited))


def test_financial_event_and_polarity_are_separate():
    positive = classify_news_item(_news(headline="Q2 results: profit rose 20%, beats estimates"), now=NOW,
                                  vader=lambda _text: 0.7)
    negative = classify_news_item(_news(headline="Quarterly results: profit fell 20%"), now=NOW,
                                  vader=lambda _text: -0.7)
    neutral_fundraising = classify_news_item(_news(headline="Company announces fundraising through QIP"), now=NOW,
                                             vader=lambda _text: 0.0)
    assert positive["event_category"] == "quarterly_results"
    assert positive["sentiment"] == "positive" and positive["sentiment_score"] > 0
    assert negative["sentiment"] == "negative" and negative["sentiment_score"] < 0
    assert neutral_fundraising["event_category"] == "fundraising_debt_capex"
    assert neutral_fundraising["sentiment"] == "neutral"


def test_mixed_ambiguous_and_missing_summary_stay_conservative():
    mixed = classify_news_item(_news(headline="Revenue rose but profit fell"), now=NOW, vader=lambda _text: 0.0)
    ambiguous = classify_news_item(_news(headline="Company makes announcement"), now=NOW, vader=lambda _text: 0.0)
    headline_only = classify_news_item(_news(headline="Company reports results"), now=NOW, vader=lambda _text: None)
    assert mixed["sentiment"] == "mixed"
    assert ambiguous["sentiment"] == "neutral"
    assert ambiguous["interpretation"] == "uncertain"
    assert headline_only["text_basis"] == "headline_only"
    assert headline_only["confidence_score"] < 50
    assert headline_only["interpretation"] == "uncertain"
    assert all(evidence["text"] in "Revenue rose but profit fell" for evidence in mixed["evidence"])


def test_feed_html_is_cleaned_before_sentiment_and_evidence():
    item = _news(headline="Company reports results", body='<a href="https://example.test">Profit rose 10%</a>&nbsp; Publisher')
    result = classify_news_item(item, now=NOW, vader=lambda text: 0.3)
    assert _clean_summary(item.body) == "Profit rose 10% Publisher"
    assert all("<a" not in evidence["text"] for evidence in result["evidence"])


def test_persisted_validation_dataset_is_reviewable_and_not_mislabeled_as_ground_truth():
    fixture = Path(__file__).parent / "fixtures" / "news_sentiment_validation.json"
    dataset = json.loads(fixture.read_text())
    assert dataset["dataset_status"] == "unlabelled_for_human_review"
    assert dataset["genuine_manual_labels"] == 0
    assert dataset["sample_size"] == len(dataset["examples"]) > 0
    assert all(example["expected_sentiment"] is None and example["expected_event_category"] is None
               for example in dataset["examples"])
    assert all(example["classifier_version"] == CLASSIFIER_VERSION for example in dataset["examples"])


def test_event_taxonomy_covers_financial_and_market_developments():
    examples = {
        "New purchase order awarded": "order_contract",
        "Company announces acquisition and partnership": "merger_acquisition_partnership",
        "CEO resigns from board": "management_change",
        "SEBI regulatory penalty issued": "regulatory_government",
        "Court opens fraud investigation": "legal_investigation",
        "Debt fundraising and capex plan announced": "fundraising_debt_capex",
        "Dividend buyback record date announced": "dividend_buyback_corporate_action",
        "New product launch and capacity expansion": "product_launch_expansion",
        "Banking sector outlook changes": "sector_development",
        "Inflation and RBI interest rate decision": "macroeconomic",
        "Global markets react to oil prices": "global_market",
        "Geopolitical sanctions announced": "geopolitical",
    }
    for headline, expected in examples.items():
        assert classify_news_item(_news(headline=headline), now=NOW, vader=lambda _text: 0.0)["event_category"] == expected


def test_company_sector_and_unmatched_relevance_use_existing_mapping():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            instrument = _instrument()
            db.add(instrument)
            db.flush()
            db.add(InstrumentSnapshot(instrument_id=instrument.id, sector="Industrials", industry="Engineering",
                                      source="test", fetched_at=NOW))
            company_news = _news(headline="Q1 results: profit rose", instrument_id=instrument.id)
            market_news = _news(headline="Indian inflation outlook changes", source="google_news")
            unmatched = _news(headline="A company announces an order", source="nse", payload={"raw_symbols": ["AMBIG"]})
            db.add_all([company_news, market_news, unmatched])
            db.commit()
            analyze_news_items(db, now=NOW, vader=lambda _text: 0.0)
            assert classify_news_item(company_news, now=NOW)["relevance_scope"] == "company"
            assert classify_news_item(market_news, now=NOW)["relevance_scope"] == "macro"
            assert classify_news_item(unmatched, now=NOW)["relevance_scope"] == "unknown"
            summary = aggregate_instrument_sentiment(db, instrument.id, now=NOW)
            sector = aggregate_sector_sentiment(db, "Industrials", now=NOW)
            assert summary["instrument"]["sector"] == "Industrials"
            assert summary["article_count"] == 2  # company item plus broad macro context
            assert sector["article_count"] == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_unmatched_discovery_news_remains_market_wide():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            item = _news(headline="Indian markets react to inflation data")
            db.add(item)
            _source(db, "google_news")
            db.commit()
            analyze_news_items(db, now=NOW, vader=lambda _text: 0.0)
            aggregate = aggregate_market_sentiment(db, now=NOW)
        assert aggregate["article_count"] == 1
        assert aggregate["status"] == "available"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_duplicate_publishers_count_once_and_keep_contradictory_attribution():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            story_key = "a" * 64
            positive = _news("google_news", "Indian stock market celebrates strong gains", payload={"story_key": story_key})
            negative = _news("gdelt", "Indian stock market sees severe losses", payload={"story_key": story_key})
            db.add_all([positive, negative])
            _source(db, "google_news")
            _source(db, "gdelt")
            db.commit()
            analyze_news_items(db, now=NOW, vader=lambda text: 0.7 if "gains" in text else -0.7)
            aggregate = aggregate_market_sentiment(db, now=NOW)
        assert aggregate["event_count"] == 1
        assert aggregate["article_count"] == 2
        assert aggregate["events"][0]["sentiment"] == "mixed"
        assert aggregate["events"][0]["contradictory_sources"] is True
        assert aggregate["events"][0]["sources"] == ["gdelt", "google_news"]
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_stale_news_is_downweighted_and_low_confidence_is_exposed():
    fresh = _news(stamp=NOW)
    stale = _news(stamp=NOW - timedelta(days=20))
    fresh_analysis = classify_news_item(fresh, now=NOW, vader=lambda _text: 0.6)
    stale_analysis = classify_news_item(stale, now=NOW, vader=lambda _text: 0.6)
    fresh_record = SimpleNamespace(confidence_score=fresh_analysis["confidence_score"],
        relevance_scope="market", impact=fresh_analysis["impact"])
    stale_record = SimpleNamespace(confidence_score=stale_analysis["confidence_score"],
        relevance_scope="market", impact=stale_analysis["impact"])
    assert _base_weight(stale, stale_record, NOW) < _base_weight(fresh, fresh_record, NOW)


def test_analysis_persistence_is_versioned_and_idempotent():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(_news(headline="Profit rose after new contract win"))
            db.commit()
            first = analyze_news_items(db, now=NOW, vader=lambda _text: 0.5)
            second = analyze_news_items(db, now=NOW, vader=lambda _text: -0.5)
            records = db.scalars(select(NewsSentimentAnalysis)).all()
        assert first == {"examined": 1, "analyzed": 1, "already_analyzed": 0, "failed": 0, "skipped_locked": 0}
        assert second == {"examined": 0, "analyzed": 0, "already_analyzed": 1, "failed": 0, "skipped_locked": 0}
        assert len(records) == 1
        assert records[0].classifier_version == CLASSIFIER_VERSION
        assert len(records[0].input_hash) == 64
        assert records[0].evidence
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_background_batch_reuses_bounded_idempotent_analyzer(monkeypatch):
    from app import main
    captured = {}
    monkeypatch.setattr(main, "analyze_news_items", lambda _db, **kwargs: captured.update(kwargs))
    main._process_sentiment_batch()
    assert captured == {"limit": 500, "period_days": 3650}


def test_failed_analysis_is_distinct_and_retried_after_backoff():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(_news(headline="Quarterly results reported"))
            db.commit()

            def broken_model(_text):
                raise RuntimeError("mock classification failure")

            first = analyze_news_items(db, now=NOW, vader=broken_model)
            state = analysis_state(db, now=NOW)
            retry_window = analyze_news_items(db, now=NOW + timedelta(minutes=10), vader=lambda _text: 0.0)
            retry = analyze_news_items(db, now=NOW + timedelta(hours=2), vader=lambda _text: 0.0)
            failures = db.scalars(select(NewsSentimentFailure)).all()
        assert first["failed"] == 1 and state["failed_items"] == 1 and state["pending_items"] == 0
        assert retry_window["examined"] == 0
        assert retry["analyzed"] == 1 and failures == []
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_separate_company_developments_are_not_merged_by_entity():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            instrument = _instrument()
            db.add(instrument)
            db.flush()
            db.add_all([
                _news(headline="Q1 results: profit rose 12%", instrument_id=instrument.id),
                _news(headline="Company wins a new order contract", instrument_id=instrument.id),
            ])
            _source(db, "nse")
            db.commit()
            analyze_news_items(db, now=NOW, vader=lambda _text: 0.4)
            aggregate = aggregate_instrument_sentiment(db, instrument.id, now=NOW)
        assert aggregate["event_count"] == 2
        assert len({event["event_key"] for event in aggregate["events"]}) == 2
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_missing_news_and_unavailable_sources_are_not_neutral():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            _source(db, "gdelt", success=False, rate_limited=True)
            db.commit()
            unavailable = aggregate_market_sentiment(db, now=NOW)
            db.add(NewsSourcePoll(source="google_news", last_attempted_at=NOW, last_successful_at=NOW,
                                  articles_discovered=0, articles_inserted=0, duplicates=0,
                                  failure_count=0, rate_limited=False))
            db.commit()
            empty = aggregate_market_sentiment(db, now=NOW)
        assert unavailable["status"] == "source_unavailable"
        assert unavailable["sentiment"] is None and unavailable["score"] is None
        assert empty["status"] == "no_news_found"
        assert empty["sentiment"] is None and empty["confidence_score"] is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_low_confidence_aggregate_keeps_direction_but_marks_it_uncertain():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(_news(headline="Indian stock market celebrates strong gains"))
            db.commit()
            analyze_news_items(db, now=NOW, vader=lambda _text: 0.9)
            aggregate = aggregate_market_sentiment(db, now=NOW)
        assert aggregate["sentiment"] == "positive"
        assert aggregate["interpretation"] == "uncertain"
        assert aggregate["confidence_score"] < 35
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_levels_giver_context_is_read_only_stable_and_attributed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            instrument = _instrument()
            db.add(instrument)
            db.flush()
            item = _news(headline="Q1 results beat estimates; profit rose", instrument_id=instrument.id)
            db.add(item)
            _source(db, "google_news")
            db.commit()
            analyze_news_items(db, now=NOW, vader=lambda _text: 0.7)
            context = levels_sentiment_context(db, instrument.id)
        assert context["interface_version"] == "news-sentiment-context-v1"
        assert context["availability_status"] == "available"
        assert context["supporting_news_item_ids"] == [str(item.id)]
        assert context["source_attribution"][0]["source"] == "google_news"
        assert context["direction"] == "uncertain"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_market_sentiment_api_is_authenticated_and_returns_no_score_for_empty_coverage():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    owner = uuid4()

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_verified] = lambda: SimpleNamespace(user_id=owner)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        with Session(engine) as db:
            _source(db, "google_news")
            db.commit()
        with TestClient(app) as client:
            response = client.get("/api/v1/news/sentiment/market")
        assert response.status_code == 200
        assert response.json()["status"] == "no_news_found"
        assert response.json()["sentiment"] is None
        assert response.json()["score"] is None
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_sentiment_analysis_api_requires_normal_authenticated_route_and_returns_counts():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    owner = uuid4()

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_verified] = lambda: SimpleNamespace(user_id=owner)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        with Session(engine) as db:
            db.add(_news(headline="Quarterly results: profit rose 25%"))
            db.commit()
        with TestClient(app) as client:
            response = client.post("/api/v1/news/sentiment/analyze?limit=10&period_days=30")
        assert response.status_code == 200
        assert response.json() == {"examined": 1, "analyzed": 1, "already_analyzed": 0, "failed": 0, "skipped_locked": 0}
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()
