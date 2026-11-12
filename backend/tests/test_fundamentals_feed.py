"""Authorised-feed adapter, throttling/retries, collection, coverage. No network: httpx.MockTransport only."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Instrument, InstrumentFundamental
from app.services import fundamentals_feed as ff
from test_research_pipeline import client, csrf, get_instrument_id, register, test_engine  # noqa: F401

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
D = Decimal


def payload(symbol="TESTCO", **over):
    data = {"symbol": symbol, "basis": "consolidated", "currency": "INR", "unit": "INR", "periods": [
        {"period_type": "annual", "period_end": "2025-03-31",
         "fields": {"revenue": "12000000000", "net_income": "1300000000", "operating_cash_flow": "1400000000", "free_cash_flow": "900000000",
                    "shares_outstanding": "100000000", "eps": "13.0", "total_debt": None},
         "extras": {"roce_pct": "18"}},
        {"period_type": "annual", "period_end": "2024-03-31", "fields": {"revenue": "10000000000"}}]}
    data.update(over)
    return data


# ───────────── contract validation ─────────────
def test_valid_payload_maps_to_canonical_fields_including_those_screener_cannot_supply():
    parsed = ff.parse_feed_payload(payload(), "testco")
    fields = parsed.periods[("annual", date(2025, 3, 31))].fields
    assert fields["free_cash_flow"] == D("900000000") and fields["shares_outstanding"] == D("100000000")
    assert "total_debt" not in fields  # null stays missing, never zero
    assert parsed.periods[("annual", date(2025, 3, 31))].extras["roce_pct"] == D("18") and parsed.basis == "consolidated"


@pytest.mark.parametrize("mutate,code", [
    (lambda p: {**p, "unit": "INR_CRORE"}, "feed_unit_not_inr"),
    (lambda p: {**p, "currency": "USD"}, "feed_unit_not_inr"),
    (lambda p: {**p, "basis": None}, "feed_basis_not_identified"),
    (lambda p: {**p, "symbol": "OTHER"}, "feed_symbol_mismatch"),
    (lambda p: {**p, "periods": [{"period_type": "weekly", "period_end": "2025-03-31", "fields": {"revenue": "1"}}]}, "feed_bad_period_type"),
    (lambda p: {**p, "periods": [{"period_type": "annual", "period_end": "2025-03-31", "fields": {"mystery": "1"}}]}, "feed_unknown_field"),
    (lambda p: {**p, "periods": [{"period_type": "annual", "period_end": "not-a-date", "fields": {"revenue": "1"}}]}, "feed_malformed"),
    (lambda p: {**p, "periods": [{"period_type": "annual", "period_end": "2025-03-31", "fields": {"revenue": "abc"}}]}, "feed_malformed"),
    (lambda p: {**p, "periods": []}, "feed_no_periods"),
])
def test_anything_off_contract_is_rejected_not_repaired(mutate, code):
    with pytest.raises(ff.FeedError) as error:
        ff.parse_feed_payload(mutate(payload()), "TESTCO")
    assert error.value.code == code
    with pytest.raises(ff.FeedError):
        ff.parse_feed_payload("not a dict", "TESTCO")


def test_implausible_margin_is_excluded():
    bad = payload(periods=[{"period_type": "annual", "period_end": "2025-03-31", "fields": {"revenue": "1", "operating_margin": "9999"}}])
    assert "operating_margin" not in ff.parse_feed_payload(bad, "TESTCO").periods[("annual", date(2025, 3, 31))].fields


# ───────────── client: auth header, throttle, retries ─────────────
def make_client(handler, sleeps, interval=2.0):
    return ff.FundamentalsFeedClient("https://feed.example/api", "secret-key", min_interval=interval,
                                     transport=httpx.MockTransport(handler), sleep=sleeps.append)


def test_client_sends_bearer_key_in_header_only_and_spaces_requests():
    seen, sleeps = [], []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload())

    feed = make_client(handler, sleeps)
    feed.fetch("TESTCO")
    feed.fetch("TESTCO")
    assert seen[0].headers["authorization"] == "Bearer secret-key" and "secret-key" not in str(seen[0].url)
    assert seen[0].url.path == "/api/fundamentals/TESTCO" and seen[0].url.params["basis"] == "consolidated"
    assert sleeps and 0 < sleeps[0] <= 2.0  # the second request waited out the minimum interval


def test_transient_errors_are_retried_with_backoff_then_succeed():
    statuses, sleeps = [503, 429, 200], []

    def handler(_r):
        code = statuses.pop(0)
        return httpx.Response(code, json=payload() if code == 200 else {}, headers={"Retry-After": "3"} if code == 429 else {})

    assert make_client(handler, sleeps, interval=0.5).fetch("TESTCO")["symbol"] == "TESTCO"
    waits = [s for s in sleeps if s >= 1]
    assert len(waits) == 2 and waits[1] >= 3  # Retry-After honoured, with jitter


@pytest.mark.parametrize("status,code", [(401, "feed_unauthorised"), (403, "feed_unauthorised"), (404, "feed_symbol_not_found"), (400, "feed_provider_error")])
def test_access_and_client_errors_are_never_retried_or_bypassed(status, code):
    calls = []
    feed = make_client(lambda r: calls.append(1) or httpx.Response(status, json={}), [])
    with pytest.raises(ff.FeedError) as error:
        feed.fetch("TESTCO")
    assert error.value.code == code and len(calls) == 1
    assert "secret-key" not in str(error.value)


def test_gives_up_after_bounded_retries_with_a_specific_reason():
    calls = []
    with pytest.raises(ff.FeedError) as error:
        make_client(lambda r: calls.append(1) or httpx.Response(503, json={}), []).fetch("TESTCO")
    assert error.value.code == "feed_unavailable" and len(calls) == ff.MAX_ATTEMPTS


# ───────────── collection and coverage ─────────────
@pytest.fixture
def db(test_engine):  # noqa: F811
    with Session(test_engine) as session:
        yield session


@pytest.fixture(autouse=True)
def feed_settings(monkeypatch):
    ff._LAST_ERRORS.clear()
    monkeypatch.setattr(settings, "fundamentals_feed_source_name", "licensed_feed")
    monkeypatch.setattr(settings, "fundamentals_feed_refresh_hours", 168)


def instrument(db, symbol="TESTCO"):
    row = Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN",
                     timezone="Asia/Kolkata", created_at=NOW)
    db.add(row)
    db.flush()
    return row


def yf(db, inst, end, **values):
    db.add(InstrumentFundamental(id=uuid4(), instrument_id=inst.id, period_type="annual", period_end=end, currency="INR",
                                 source="yfinance", fetched_at=NOW - timedelta(days=3), **values))
    db.flush()


def test_collection_fills_gaps_with_the_feeds_provenance_and_never_overwrites(db):
    inst = instrument(db)
    yf(db, inst, date(2025, 3, 31), revenue=D("12600000000"))  # 5% off the feed: a conflict, kept
    requests = []
    feed = make_client(lambda r: requests.append(r) or httpx.Response(200, json=payload()), [])
    summary = ff.collect_once(db, feed, now=NOW)
    assert summary["imported"] == 1 and len(requests) == 1
    row = db.scalar(select(InstrumentFundamental).where(InstrumentFundamental.period_end == date(2025, 3, 31)))
    assert row.revenue == D("12600000000") and row.free_cash_flow == D("900000000") and row.shares_outstanding == D("100000000")
    assert row.field_sources["free_cash_flow"]["source"] == "licensed_feed" and "revenue" not in row.field_sources
    assert row.extra_metrics["screener_conflicts"]["revenue"]["source"] == "licensed_feed"
    older = db.scalar(select(InstrumentFundamental).where(InstrumentFundamental.period_end == date(2024, 3, 31)))
    assert older.source == "licensed_feed" and older.statement_basis == "consolidated"  # missing history period created


def test_recently_collected_stocks_are_not_requested_again(db):
    inst = instrument(db)
    yf(db, inst, date(2025, 3, 31), revenue=D("1"))
    requests = []
    feed = make_client(lambda r: requests.append(r) or httpx.Response(200, json=payload()), [])
    ff.collect_once(db, feed, now=NOW)
    again = ff.collect_once(db, feed, now=NOW + timedelta(hours=1))
    assert len(requests) == 1 and again["attempted"] == 0 and again["skipped_complete_or_recent"] >= 1
    later = ff.collect_once(db, feed, now=NOW + timedelta(hours=200))  # refresh window elapsed: eligible again if gaps remain
    assert later["attempted"] in (0, 1)


def test_failures_are_recorded_per_stock_and_access_errors_stop_the_cycle(db):
    a, b = instrument(db, "AAA"), instrument(db, "BBB")
    yf(db, a, date(2025, 3, 31), revenue=D("1"))
    yf(db, b, date(2025, 3, 31), revenue=D("1"))
    db.commit()  # real data is committed; a failed import rolls back only its own work
    calls = []
    feed = make_client(lambda r: calls.append(1) or httpx.Response(401, json={}), [])
    summary = ff.collect_once(db, feed, now=NOW, limit=5)
    assert summary["failed"] == 1 and len(calls) == 1 and summary["errors"] == {"AAA": "feed_unauthorised"}  # BBB never attempted
    cov = ff.instrument_coverage(db, a)
    assert cov["last_error"]["code"] == "feed_unauthorised" and cov["status"] == "partial"


def test_coverage_reports_overall_and_per_stock_status(db):
    full, partial, empty = instrument(db, "FULL"), instrument(db, "PART"), instrument(db, "NONE")
    for year in range(2016, 2026):
        db.add(InstrumentFundamental(id=uuid4(), instrument_id=full.id, period_type="annual", period_end=date(year, 3, 31), currency="INR",
                                     source="yfinance", fetched_at=NOW, **{f: D("1") for f in ff.si.CANONICAL_FIELDS}))
    for q in range(12):
        db.add(InstrumentFundamental(id=uuid4(), instrument_id=full.id, period_type="quarterly", period_end=date(2023 + q // 4, [3, 6, 9, 12][q % 4], [31, 30, 30, 31][q % 4]),
                                     currency="INR", source="yfinance", fetched_at=NOW, **{f: D("1") for f in ff.si.CANONICAL_FIELDS}))
    yf(db, partial, date(2025, 3, 31), revenue=D("1"), net_income=D("1"))
    report = ff.coverage_report(db)
    by = {p["symbol"]: p for p in report["instruments"]}
    assert by["FULL"]["status"] == "complete" and by["FULL"]["field_completeness_pct"] == 100.0 and by["FULL"]["unresolved_gap_cells"] == 0
    assert by["PART"]["status"] == "partial" and by["PART"]["field_completeness_pct"] < 25 and by["PART"]["missing_history_periods"] > 0
    assert by["PART"]["last_retrieved_by_source"]["yfinance"]
    assert by["NONE"]["status"] == "none"
    assert report["overall"]["instruments"] == 3 and report["overall"]["complete"] == 1 and report["overall"]["none"] == 1
    assert report["feed"]["enabled"] is False and report["feed"]["configured"] is False


def test_coverage_endpoint_and_gaps_endpoint_include_coverage(client, test_engine):  # noqa: F811
    register(client, test_engine, "coverage@example.com")
    instrument_id = get_instrument_id(client)
    body = client.get("/api/v1/research/fundamentals-coverage").json()
    assert body["overall"]["instruments"] >= 1 and "Screener.in offers no authorised automated access" in body["feed"]["note"]
    gaps = client.get(f"/api/v1/research/instruments/{instrument_id}/fundamentals-gaps").json()
    assert gaps["coverage"]["instrument_id"] == instrument_id and "status" in gaps["coverage"]


# ───────────── safety: disabled by default, no Screener scraping ─────────────
def test_feed_is_off_by_default_and_requires_enable_url_and_key(monkeypatch):
    assert ff.feed_configured() is False
    monkeypatch.setattr(settings, "fundamentals_feed_enabled", True)
    monkeypatch.setattr(settings, "fundamentals_feed_base_url", "https://feed.example")
    assert ff.feed_configured() is False  # no key
    monkeypatch.setattr(settings, "fundamentals_feed_api_key", SecretStr("k"))
    assert ff.feed_configured() is True
    monkeypatch.setattr(settings, "fundamentals_feed_enabled", False)
    assert ff.feed_configured() is False


def test_unconfigured_loop_returns_immediately_and_nothing_targets_screener_in():
    asyncio.run(asyncio.wait_for(ff.fundamentals_collection_loop(), 1))
    for module in (ff, ff.si):
        source = Path(module.__file__).read_text()
        assert "https://www.screener.in" not in source and "https://screener.in" not in source
    assert "httpx" not in Path(ff.si.__file__).read_text()  # the HTML importer still has no network code at all
