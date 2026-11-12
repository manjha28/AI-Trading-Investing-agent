"""Verified Dhan Company Info integration. Fixtures are REAL Dhan responses captured during the audit (2026-11-05).
Nothing here calls Dhan: provider behaviour is faked, so these tests prove handling, not live provider success."""
import copy
import inspect
import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import market_data
from app.broker_adapters import BrokerAdapterError, DhanAdapter
from app.db import Base
from app.models import Instrument, InstrumentFundamental, InstrumentProviderIdentifier, InstrumentSnapshot
from app.services import dhan_company_info as dci
from app.services import research_pipeline as rp
from app.services import research_scoring as rs
from app.services import valuation as val
from test_research_pipeline import client, get_instrument_id, register, test_engine  # noqa: F401

FIX = Path(__file__).parent / "fixtures" / "dhan_company_info"
NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
D = Decimal


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())["response"]["data"]  # exactly what DhanAdapter.get_company_info returns


@pytest.fixture(autouse=True)
def fresh_cache():
    dci.clear_cache()
    yield
    dci.clear_cache()


# ───────────── approved fields ─────────────
def test_all_approved_fields_are_normalised_from_a_real_response():
    snap = dci.normalize(fixture("RELIANCE"), "2885", fetched_at=NOW)
    v = snap["values"]
    assert snap["source"] == "dhan" and snap["fetched_at"] == NOW.isoformat() and snap["schema_version"] == dci.SCHEMA_VERSION
    assert v["sector"]["value"] == "Energy" and v["industry"]["value"] == "Petroleum Products"
    assert D(v["market_cap"]["value"]) == D("1778175.5880708") * D("10000000") and v["market_cap"]["unit"] == "INR"  # source is INR crore
    assert v["book_value"]["value"] == "697.88" and v["book_value"]["unit"] == "INR/share"
    assert v["pb"]["value"] == "1.88"
    assert all(item["source"] == "dhan" and item["fetched_at"] == NOW.isoformat() for item in v.values())  # field-level provenance
    holdings = {h["key"]: h["percent"] for h in snap["shareholding"]["holdings"]}
    assert holdings == {"promoter": "50.44", "fii": "16.67", "dii": "21.72", "public": "8.67"}


def test_the_display_snapshot_keeps_only_approved_fields_and_every_other_usable_field_goes_to_valuation_inputs():
    snap = dci.normalize(fixture("RELIANCE"), "2885")
    assert set(snap["values"]) == {"market_cap", "book_value", "pb", "dividend_yield", "sector", "industry"}  # what the Research / Info tabs show is unchanged
    inputs = snap.pop("valuation_inputs")
    dumped = json.dumps(snap).lower()  # the stored brief JSON never carries the Decimal-valued inputs
    for field in ("pe_ratio", "ebitda", "debt_to_equity", "industry_", "pe_to_sector"):
        assert field not in dumped
    assert {"pe", "eps", "ebitda", "roce", "pe_to_sector_pe", "industry_pb", "industry_eps", "industry_dividend_yield", "industry_operating_margin"} <= set(inputs)
    assert "roe" not in inputs  # Dhan returns ROE as null for every security: not reported, never zero
    assert inputs["market_cap"]["value"] == D("1778175.5880708") * D("10000000") and inputs["market_cap"]["unit"] == "INR"
    assert inputs["ebitda"]["value"] == D("207911") * D("10000000") and inputs["ebitda"]["unit_assumed"] is True
    assert inputs["pe"]["value"] == D("18.6") and inputs["dividend_yield"]["value"] == D("0.46") and inputs["dividend_yield"]["unit"] == "percent"
    assert inputs["sector"]["text"] == "Energy" and inputs["industry"]["text"] == "Petroleum Products"


def test_percentages_are_percent_numbers_and_never_scaled_to_fractions():
    snap = dci.normalize(fixture("RELIANCE"), "2885")
    assert snap["shareholding"]["holdings"][0]["percent"] == "50.44"  # 50.44 means 50.44%, not 0.5044
    assert all(h["unit"] == "percent" for h in snap["shareholding"]["holdings"])
    data = {"SHP": {"PROMOTER_HOLDING": 15, "FII_HOLDING": 0.15, "DII_HOLDING": 100, "PUBLIC_HOLDING": 0}}
    holdings = {h["key"]: h["percent"] for h in dci.normalize(data, "1")["shareholding"]["holdings"]}
    assert holdings == {"promoter": "15", "fii": "0.15", "dii": "100", "public": "0"}  # 0.15 stays 0.15%; a real 0 stays 0, not missing


def test_dividend_yield_is_kept_as_before_but_flagged_unverified():
    v = dci.normalize({"CO": {"DIVIDEND_YIELD": 1.16}}, "1")["values"]["dividend_yield"]
    assert D(v["value"]) == D("0.0116") and v["verified"] is False and "unverified" in v["unit"]


@pytest.mark.parametrize("bad", [None, "", "  ", float("nan"), "abc", True])
def test_null_missing_and_empty_values_are_not_provided_never_zero(bad):
    snap = dci.normalize({"CO": {"SECTOR": bad if bad in ("", "  ") else None, "MARKET_CAP": bad, "BOOK_VALUE": bad, "INDUSTRY": "Banks"},
                          "RATIOS": {"PB_RATIO": bad}, "SHP": {"PROMOTER_HOLDING": bad}}, "1")
    assert snap["values"]["sector"]["value"] is None and snap["values"]["market_cap"]["value"] is None
    assert snap["values"]["book_value"]["value"] is None and snap["values"]["pb"]["value"] is None
    assert snap["shareholding"]["holdings"][0]["percent"] is None and snap["shareholding"]["status"] == "unavailable"


def test_real_empty_string_sector_and_missing_groups():
    jpl = dci.normalize(fixture("JPL"), "766830")
    assert jpl["values"]["sector"]["value"] is None and jpl["values"]["industry"]["value"] is None  # "" is not a classification
    assert jpl["values"]["market_cap"]["value"] is not None
    only_co = dci.normalize({"CO": {"SECTOR": "Energy"}}, "1")
    assert only_co["values"]["pb"]["value"] is None and only_co["shareholding"]["status"] == "unavailable"
    assert dci.normalize({}, "1") is None and dci.normalize({"CO": {}, "RATIOS": {}, "SHP": {}}, "1") is None


def test_invalid_numbers_are_discarded_not_repaired():
    snap = dci.normalize({"CO": {"MARKET_CAP": -5}, "SHP": {"PROMOTER_HOLDING": 140, "FII_HOLDING": -2, "DII_HOLDING": 10}}, "1")
    assert snap["values"]["market_cap"]["value"] is None
    percents = {h["key"]: h["percent"] for h in snap["shareholding"]["holdings"]}
    assert percents["promoter"] is None and percents["fii"] is None and percents["dii"] == "10"
    assert any("outside 0-100%" in w for w in snap["shareholding"]["warnings"])


# ───────────── shareholding is an undated snapshot ─────────────
def test_shareholding_is_undated_complete_with_warnings_and_never_infers_a_period():
    sh = dci.normalize(fixture("RELIANCE"), "2885", fetched_at=NOW)["shareholding"]
    assert sh["reporting_period"] is None and sh["reporting_period_note"] == "Not provided by source"
    assert sh["retrieved_at"] == NOW.isoformat() and sh["source"] == "dhan"
    assert D(sh["sum_percent"]) == D("97.50") and D(sh["not_itemised_percent"]) == D("2.50")
    assert any("incomplete" in w and "latest quarter" in w for w in sh["warnings"])
    assert "quarter" not in json.dumps({k: v for k, v in sh.items() if k not in {"warnings", "reporting_period_note"}}).lower()
    over = dci.normalize({"SHP": {"PROMOTER_HOLDING": 60, "FII_HOLDING": 30, "DII_HOLDING": 20, "PUBLIC_HOLDING": 5}}, "1")["shareholding"]
    assert any("exceeds 100%" in w for w in over["warnings"])


# ───────────── reconciliation ─────────────
def dhan_values(name="RELIANCE"):
    return dci.normalize(fixture(name), "2885")["values"]


def test_pb_reconciliation_agrees_within_tolerance_and_flags_discrepancy():
    v = dhan_values()
    agree = dci.reconcile(v, price=D("1312"), price_problem=None, shares=None)["pb"]  # 1312 / 697.88 = 1.880
    assert agree["status"] == "agrees" and agree["tolerance"] == "0.05"
    bad = dci.reconcile(v, price=D("1000"), price_problem=None, shares=None)["pb"]  # implied P/B 1.433
    assert bad["status"] == "discrepancy" and "differs from price / book value" in bad["reason"] and bad["reference_value"].startswith("1.43")
    assert dci.reconcile(v, price=D("1355"), price_problem=None, shares=None)["pb"]["status"] == "agrees"  # +3.3% price drift tolerated
    assert dci.reconcile(v, price=D("1420"), price_problem=None, shares=None)["pb"]["status"] == "discrepancy"  # +8.2%


def test_market_cap_reconciliation_against_price_times_shares():
    v = dhan_values()
    mcap = D("1778175.5880708") * D("10000000")
    shares = mcap / D("1312")
    agree = dci.reconcile(v, price=D("1312"), price_problem=None, shares=shares)["market_cap"]
    assert agree["status"] == "agrees"
    drift = dci.reconcile(v, price=D("1312"), price_problem=None, shares=shares * D("1.09"))["market_cap"]
    assert drift["status"] == "agrees"  # within the 10% audit-justified tolerance
    stale_shares = dci.reconcile(v, price=D("1312"), price_problem=None, shares=shares * D("2"))["market_cap"]
    assert stale_shares["status"] == "discrepancy" and "share count" in stale_shares["reason"]


def test_missing_or_stale_price_means_not_verified_never_a_silent_pass():
    v = dhan_values()
    out = dci.reconcile(v, price=None, price_problem="Latest stored daily bar is 2026-11-03", shares=D("1000"))
    assert out["pb"]["status"] == "not_verified" and "2026-11-03" in out["pb"]["reason"]
    assert out["market_cap"]["status"] == "not_verified"
    no_shares = dci.reconcile(v, price=D("1312"), price_problem=None, shares=None, shares_problem="no shares-outstanding figure is stored")
    assert no_shares["market_cap"]["status"] == "not_verified" and "share" in no_shares["market_cap"]["reason"]
    neg_book = dci.reconcile({**v, "book_value": {"value": "-5"}}, price=D("100"), price_problem=None, shares=None)
    assert neg_book["pb"]["status"] == "not_verified"
    assert dci.reconcile({}, price=D("1"), price_problem=None, shares=D("1"))["pb"]["status"] == "not_verified"


def reconcile_via_pipeline(technicals, shares="1000000"):
    snap = dci.normalize(fixture("RELIANCE"), "2885")
    fundamentals = {"snapshot": snap}
    rows = [SimpleNamespace(period_end=datetime(2026, 9, 30).date(), shares_outstanding=D(shares))]
    quality = []
    rp._reconcile_dhan_snapshot(fundamentals, technicals, rows, quality)
    return fundamentals["snapshot"], quality


def test_pipeline_flags_discrepancies_in_data_quality_and_does_not_change_values():
    before = dhan_values()
    snap, quality = reconcile_via_pipeline({"status": "available", "latest_close": "1000"}, shares="1000000")
    assert snap["reconciliation"]["pb"]["status"] == "discrepancy" and snap["reconciliation"]["market_cap"]["status"] == "discrepancy"
    assert {q["kind"] for q in quality} == {"discrepancy"} and len(quality) == 2
    assert snap["values"]["pb"]["value"] == before["pb"]["value"] and snap["values"]["market_cap"]["value"] == before["market_cap"]["value"]  # flagged, not overwritten
    assert snap["values"]["pb"]["reconciliation"] == "discrepancy"


def test_pipeline_with_stale_or_missing_price_marks_not_verified_without_raising_data_quality_noise():
    snap, quality = reconcile_via_pipeline({"status": "stale", "latest_close": "1312", "staleness": {"reason": "Latest stored daily bar is 2026-11-03."}})
    assert snap["reconciliation"]["pb"]["status"] == "not_verified" and "2026-11-03" in snap["reconciliation"]["pb"]["reason"]
    assert quality == []  # not being able to verify is not itself a discrepancy
    snap, _ = reconcile_via_pipeline({"status": "unavailable", "message": "No stored daily OHLCV bars are available."})
    assert snap["reconciliation"]["market_cap"]["status"] == "not_verified"


def test_non_dhan_snapshots_are_not_reconciled():
    fundamentals = {"snapshot": {"source": "yfinance", "values": {"pb": {"value": "1"}}}}
    quality = []
    rp._reconcile_dhan_snapshot(fundamentals, {"status": "available", "latest_close": "10"}, [], quality)
    assert "reconciliation" not in fundamentals["snapshot"] and quality == []


# ───────────── requests: shape, caching, duplicates, failures ─────────────
def test_adapter_requests_only_the_metric_groups_asked_for_and_default_is_unchanged(monkeypatch):
    sent = []
    from app import broker_adapters
    monkeypatch.setattr(broker_adapters, "_wait_for_dhan_market_data", lambda: None)
    monkeypatch.setattr(broker_adapters, "_request", lambda method, url, **kw: sent.append((url, kw["json"])) or {"status": "success", "data": {"CO": {"SECTOR": "x"}}})
    monkeypatch.setattr(DhanAdapter, "_check_market_data_payload", lambda self, payload: None)
    adapter = DhanAdapter()
    adapter.get_company_info("t", {"client_id": "c"}, "2885", "NSE_EQ")
    adapter.get_company_info("t", {"client_id": "c"}, "2885", "NSE_EQ", metrics=("CO", "RATIOS", "SHP"))
    assert sent[0][1]["metrics"] == ["CO", "RATIOS"] and sent[1][1]["metrics"] == ["CO", "RATIOS", "SHP"]
    assert sent[0][1]["securityId"] == "2885" and sent[0][1]["exchangeSegment"] == "NSE_EQ" and sent[0][1]["instrument"] == "EQUITY"
    for bad in ((), ("FOO",), ("CO", "ROE")):
        with pytest.raises(BrokerAdapterError):
            adapter.get_company_info("t", {"client_id": "c"}, "2885", "NSE_EQ", metrics=bad)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def instrument_with_dhan(db, symbol="RELIANCE", sid="2885"):
    inst = Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW)
    inst.provider_identifiers.append(InstrumentProviderIdentifier(provider="dhan", provider_instrument_id=f"{sid}|NSE_EQ", provider_symbol=symbol, is_verified=True, last_seen_at=NOW))
    db.add(inst)
    db.flush()
    return inst


def fake_provider(calls, payload=None, error=None):
    class Adapter:
        def get_company_info(self, token, tokens, security_id, segment, metrics=("CO", "RATIOS")):
            calls.append(metrics)
            if error:
                raise error
            return copy.deepcopy(payload or fixture("RELIANCE"))
    return SimpleNamespace(adapter=Adapter(), tokens={"access_token": "t", "client_id": "c"})


def test_research_fetch_requests_all_three_groups_once_and_reuses_the_result(db, monkeypatch):
    inst = instrument_with_dhan(db)
    calls = []
    monkeypatch.setattr(rp, "_market_data_credentials", lambda *_a: (None, fake_provider(calls), None))
    first, reason = rp._fetch_dhan_company_info(db, uuid4(), inst)
    second, _ = rp._fetch_dhan_company_info(db, uuid4(), inst)  # another run / another user within the cache window
    assert reason is None and first["values"]["sector"]["value"] == "Energy"
    assert calls == [("CO", "RATIOS", "SHP")]  # one request, all approved groups
    assert second["fetched_at"] == first["fetched_at"]  # the cached result keeps its ORIGINAL retrieval time


def test_simultaneous_requests_for_one_security_make_a_single_provider_call():
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return {"x": 1}

    results = []
    threads = [threading.Thread(target=lambda: results.append(dci.fetch_once("2885", slow))) for _ in range(5)]
    [t.start() for t in threads]
    [t.join(3) for t in threads]
    assert len(calls) == 1 and results == [{"x": 1}] * 5


def test_failures_are_not_cached_and_the_cache_expires():
    n = {"calls": 0}

    def fail():
        n["calls"] += 1
        return None

    assert dci.fetch_once("9", fail) is None and dci.fetch_once("9", fail) is None and n["calls"] == 2  # retried, not cached
    clock = {"t": 0.0}
    ok = lambda: {"v": 1}  # noqa: E731
    dci.fetch_once("10", ok, now=lambda: clock["t"])
    seen = []
    clock["t"] = dci.CACHE_SECONDS - 1
    dci.fetch_once("10", lambda: seen.append(1) or {"v": 2}, now=lambda: clock["t"])
    assert seen == []
    clock["t"] = dci.CACHE_SECONDS + 1
    assert dci.fetch_once("10", lambda: {"v": 3}, now=lambda: clock["t"]) == {"v": 3}


def test_provider_failure_is_reported_not_raised_and_not_cached(db, monkeypatch):
    inst = instrument_with_dhan(db)
    calls = []
    monkeypatch.setattr(rp, "_market_data_credentials", lambda *_a: (None, fake_provider(calls, error=BrokerAdapterError("boom", category="rate_limited")), None))
    snapshot, reason = rp._fetch_dhan_company_info(db, uuid4(), inst)
    assert snapshot is None and reason
    rp._fetch_dhan_company_info(db, uuid4(), inst)
    assert len(calls) == 2  # the failure was not cached
    monkeypatch.setattr(rp, "_market_data_credentials", lambda *_a: (None, None, None))
    assert rp._fetch_dhan_company_info(db, uuid4(), inst)[1] == "No authenticated Dhan connection is available."
    unmapped = Instrument(symbol="X", display_name="X", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW)
    db.add(unmapped)
    db.flush()
    assert "No verified Dhan instrument mapping" in rp._fetch_dhan_company_info(db, uuid4(), unmapped)[1]


def test_cached_snapshots_from_before_this_version_are_ignored(db, monkeypatch):
    current = dci.normalize(fixture("RELIANCE"), "2885", fetched_at=datetime.now(timezone.utc))
    legacy = {k: v for k, v in current.items() if k not in {"schema_version", "shareholding"}}
    for snap, expected in ((current, True), (legacy, False)):
        monkeypatch.setattr(rp, "latest_brief", lambda *_a, s=snap: (None, SimpleNamespace(sections={"fundamentals": {"snapshot": s}})))
        assert (rp._cached_dhan_snapshot(db, uuid4(), uuid4()) is not None) is expected


# ───────────── preservation of existing data; provider fallback ─────────────
def stored_snapshot(db, inst, **over):
    row = InstrumentSnapshot(id=uuid4(), instrument_id=inst.id, market_cap=D("17000000000000"), pe=D("20"), pb=D("1.9"), dividend_yield=D("0.5"), sector="Energy (Yahoo)",
                             industry="Oil & Gas Refining", source="yfinance", fetched_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    for k, v in over.items():
        setattr(row, k, v)
    db.add(row)
    db.flush()
    return row


def test_null_or_empty_dhan_fields_never_replace_existing_values(db):
    inst = instrument_with_dhan(db, "JPL", "766830")
    stored = stored_snapshot(db, inst)
    before = (stored.sector, stored.industry, stored.market_cap, stored.pb, stored.fetched_at)
    dhan = dci.normalize(fixture("JPL"), "766830", fetched_at=NOW)  # real response with "" sector and industry
    section, _rows = rp._fundamentals_section(db, inst.id, [], external_snapshot=dhan)
    values = section["snapshot"]["values"]
    assert values["sector"]["value"] == "Energy (Yahoo)" and values["sector"]["source"] == "yfinance" and values["sector"]["filled_from_existing"] is True
    assert values["industry"]["source"] == "yfinance" and values["market_cap"]["source"] == "dhan" and "filled_from_existing" not in values["market_cap"]
    assert (stored.sector, stored.industry, stored.market_cap, stored.pb, stored.fetched_at) == before  # the stored row is untouched


def test_dhan_values_are_primary_and_disagreements_are_reported_not_resolved(db):
    inst = instrument_with_dhan(db)
    stored_snapshot(db, inst, market_cap=D("17781755880708"), pb=D("3.0"))  # market cap matches Dhan; P/B is 60% apart; sector text differs
    dhan = dci.normalize(fixture("RELIANCE"), "2885", fetched_at=NOW)
    section, _ = rp._fundamentals_section(db, inst.id, [], external_snapshot=dhan)
    snap = section["snapshot"]
    assert snap["values"]["pb"]["value"] == "1.88" and snap["values"]["pb"]["source"] == "dhan"
    assert {c["field"] for c in snap["conflicts"]} == {"pb", "sector", "industry"} and "market_cap" not in {c["field"] for c in snap["conflicts"]}
    assert snap["alternate_sources"]["pb"]["value"] == "3.00000000" and snap["comparison_status"] == "conflicting_values"
    assert section["snapshot"]["shareholding"]["retrieved_at"] == NOW.isoformat()  # shareholding travels with the snapshot


def test_when_dhan_is_unavailable_the_existing_snapshot_is_used_unchanged(db):
    inst = instrument_with_dhan(db)
    stored_snapshot(db, inst)
    quality = []
    section, _ = rp._fundamentals_section(db, inst.id, quality, external_snapshot=None, external_reason="Dhan Company Info is unavailable")
    snap = section["snapshot"]
    assert snap["source"] == "yfinance" and snap["values"]["sector"]["value"] == "Energy (Yahoo)"
    assert any(q["field_path"] == "dhan.company_info" for q in quality)  # the failure is visible in Data Quality
    assert "shareholding" not in snap


def test_statements_and_other_records_are_never_written(db):
    inst = instrument_with_dhan(db)
    stored_snapshot(db, inst)
    db.add(InstrumentFundamental(id=uuid4(), instrument_id=inst.id, period_type="annual", period_end=datetime(2026, 3, 31).date(), currency="INR", source="yfinance",
                                 fetched_at=NOW, revenue=D("1"), total_equity=D("2")))
    db.flush()
    snapshot_rows = lambda: [(r.period_type, r.revenue, r.total_equity, r.source, r.field_sources, r.extra_metrics) for r in db.scalars(select(InstrumentFundamental))]  # noqa: E731
    before = snapshot_rows()
    rp._fundamentals_section(db, inst.id, [], external_snapshot=dci.normalize(fixture("RELIANCE"), "2885", fetched_at=NOW))
    assert snapshot_rows() == before and not db.dirty and not db.new


# ───────────── scoring and valuation boundaries ─────────────
def test_research_scoring_ignores_dhan_fields_and_valuation_never_reads_the_unreliable_ones():
    assert rs.HORIZONS == ("swing", "positional", "long_term") and "shareholding" not in rs.COMPONENTS
    source = inspect.getsource(rs)
    assert "shareholding" not in source and "dhan" not in source.lower()
    assert "valuation_scoring" not in inspect.getsource(rp)  # the pipeline only attaches the shared valuation evidence

    def sections(snapshot):
        return {"fundamentals": {"status": "available", "periods": [], "snapshot": {"values": snapshot}}, "technicals": {"status": "unavailable", "message": "x"},
                "financials_trend": {"status": "unavailable", "periods": []}, "news_sentiment": None}

    plain = rs.build_assessment(sections({}), instrument_id="I", now=NOW)
    with_dhan = rs.build_assessment(sections(dci.normalize(fixture("RELIANCE"), "2885")["values"]), instrument_id="I", now=NOW)
    for key in ("weights", "components", "horizons"):
        assert plain[key] == with_dhan[key]


def test_valuation_scoring_result_travels_with_the_evidence_and_uses_no_dhan_pe_or_benchmarks(db):
    inst = instrument_with_dhan(db)
    snap = dci.normalize(fixture("RELIANCE"), "2885", fetched_at=NOW)
    sections = {"fundamentals": {"periods": [], "snapshot": snap}, "technicals": {"status": "available", "latest_close": "1312", "latest_bar": "2026-11-04T00:00:00+00:00", "source": "dhan"}}
    result = val.build_valuation(db, inst, sections, NOW)
    scoring = result["scoring"]
    assert scoring["provisional"] is True and scoring["sector_family"] == "asset_heavy_cyclical"  # Dhan sector "Energy" drives the family
    assert scoring["score"] is None and scoring["status"] == "blocked"  # no peers, no cash/EBITDA
    assert result["metrics"]["pe_ttm"]["provider"] != "dhan"  # Dhan's unreliable P/E is never used
    assert "score_note" not in result


# ───────────── Info tab: every field, with its audit status ─────────────
def test_info_tab_lists_every_returned_field_with_presence_and_audit_status():
    info = dci.describe_all(fixture("RELIANCE"), fetched_at=NOW)
    fields = {f["key"]: f for g in info["groups"] for f in g["fields"]}
    assert len(fields) == 28 and info["retrieved_at"] == NOW.isoformat()
    assert info["reporting_period"] is None and "Not provided" in info["reporting_period_note"] and "unknown" in info["statement_basis"]
    assert fields["ROE"]["presence"] == "null" and fields["ROE"]["audit_status"] == "unreliable"
    assert fields["REPORTING_PERIOD"]["presence"] == "null"
    assert fields["PROMOTER_HOLDING"]["audit_status"] == "verified" and fields["PE_RATIO"]["audit_status"] == "unvalidated"
    assert fields["INDUSTRY_PB_RATIO"]["audit_status"] == "unreliable" and fields["PE_RATIO"]["value"] == 18.6  # shown exactly as received
    assert D(fields["MARKET_CAP"]["inr_value"]) == D("1778175.5880708") * D("10000000")
    assert {f["audit_status"] for f in fields.values()} == {"verified", "unvalidated", "unreliable"}


def test_info_tab_distinguishes_null_empty_string_missing_and_unknown_fields():
    info = dci.describe_all({"CO": {"SECTOR": "", "INDUSTRY": None, "MARKET_CAP": 10.5, "NEW_FIELD": 7}, "RATIOS": {}}, fetched_at=NOW)
    fields = {f["key"]: f for g in info["groups"] for f in g["fields"]}
    assert fields["SECTOR"]["presence"] == "empty_string" and fields["INDUSTRY"]["presence"] == "null" and fields["BOOK_VALUE"]["presence"] == "missing"
    assert fields["PB_RATIO"]["presence"] == "missing" and fields["PROMOTER_HOLDING"]["presence"] == "group_not_returned"
    assert fields["NEW_FIELD"]["label"] == "NEW_FIELD" and fields["NEW_FIELD"]["audit_text"] == "New field - not audited"  # never silently dropped
    assert not [g for g in info["groups"] if g["key"] == "SHP"][0]["returned"]


def test_company_profile_endpoint_returns_company_info_when_dhan_is_connected(client, test_engine, monkeypatch):  # noqa: F811
    register(client, test_engine, "info@example.com")
    instrument_id = get_instrument_id(client)
    with Session(test_engine) as session:
        session.add(InstrumentProviderIdentifier(instrument_id=uuid4() if False else __import__("uuid").UUID(instrument_id), provider="dhan",
                                                 provider_instrument_id="2885|NSE_EQ", provider_symbol="X", is_verified=True, last_seen_at=NOW))
        session.commit()
    calls = []
    monkeypatch.setattr(market_data, "_market_data_credentials", lambda *_a: (None, fake_provider(calls), None))
    monkeypatch.setattr(market_data, "_exclusive_selected_source", lambda *_a: "dhan")
    response = client.get(f"/api/v1/market-data/instruments/{instrument_id}/company-profile")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "available" and body["sector"] == "Energy" and D(body["market_cap"]) == D("1778175.5880708") * D("10000000")
    assert calls == [("CO", "RATIOS", "SHP")]
    keys = {f["key"] for g in body["company_info"]["groups"] for f in g["fields"]}
    assert len(keys) == 28 and "ROE" in keys and body["company_info"]["reporting_period"] is None
    assert body["fetched_at"] == body["company_info"]["retrieved_at"] or body["fetched_at"][:19] == body["company_info"]["retrieved_at"][:19]
    again = client.get(f"/api/v1/market-data/instruments/{instrument_id}/company-profile")
    assert again.status_code == 200 and calls == [("CO", "RATIOS", "SHP")]  # cached: no second provider request
