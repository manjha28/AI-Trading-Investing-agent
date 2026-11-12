"""Provider valuation inputs: extraction to canonical units for Dhan / Yahoo Finance / Screener, storage, the Dhan sector-peer universe refresh."""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.broker_adapters import BrokerAdapterError
from app.db import Base
from app.models import Instrument, InstrumentProviderIdentifier, ValuationProviderInput
from app.services import dhan_company_info as dci
from app.services import fundamentals as fundamentals_service
from app.services import peer_universe, screener_import as si, valuation_inputs as vi
from app.services import research_pipeline as rp

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
D = Decimal
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def instrument(db, symbol="RELIANCE", sid="2885", *, verified=True):
    inst = Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW)
    if sid:
        inst.provider_identifiers.append(InstrumentProviderIdentifier(provider="dhan", provider_instrument_id=f"{sid}|NSE_EQ", provider_symbol=symbol, is_verified=verified, last_seen_at=NOW))
    db.add(inst)
    db.flush()
    return inst


def dhan_payload(name="RELIANCE"):
    return json.loads((FIXTURES / "dhan_company_info" / f"{name}.json").read_text())["response"]["data"]


# ───────────── extraction ─────────────
def test_yahoo_inputs_are_converted_to_canonical_units_and_missing_values_are_omitted():
    out = vi.yahoo_inputs({"marketCap": 1_000_000_000, "trailingPE": 12.5, "priceToBook": 2.1, "dividendYield": 1.16, "returnOnEquity": 0.185, "trailingPegRatio": 1.4,
                           "enterpriseValue": 1_200_000_000, "enterpriseToEbitda": 8.1, "ebitda": 150_000_000, "totalCash": 5, "totalDebt": 7, "bookValue": 48.2,
                           "trailingEps": 8.0, "priceToSalesTrailing12Months": 2.6, "sector": "Financial Services", "industry": "Banks - Regional", "forwardPE": None, "pegRatio": None})
    assert out["roe"]["value"] == D("18.5") and out["roe"]["unit"] == "percent"  # a fraction becomes percent
    assert out["peg"]["value"] == D("1.4") and out["dividend_yield"]["value"] == D("1.16") and out["industry"]["text"] == "Banks - Regional"
    assert out["ev_ebitda"]["value"] == D("8.1") and out["market_cap"]["unit"] == "INR"
    sparse = vi.yahoo_inputs({"marketCap": None, "trailingPE": "", "priceToBook": float("nan"), "returnOnEquity": True})
    assert sparse == {}  # null / empty / non-finite / boolean values are "not reported", never zero
    assert vi.yahoo_inputs({"pegRatio": 2.2})["peg"]["value"] == D("2.2")  # the older key is the fallback


def test_every_dhan_field_is_extracted_in_canonical_units_and_empty_ones_are_not_zero():
    inputs = dci.normalize(dhan_payload(), "2885")["valuation_inputs"]
    assert inputs["market_cap"]["value"] == D("1778175.5880708") * D("10000000") and inputs["ebitda"]["unit_assumed"] is True
    assert inputs["pe"]["value"] == D("18.6") and inputs["industry_pb"]["value"] == D("1.17") and inputs["pe_to_sector_pe"]["value"] > 0
    assert "roe" not in inputs  # ROE is null in Dhan's response
    loss = dci.normalize(dhan_payload("EASEMYTRIP"), "2792")["valuation_inputs"]
    assert loss["pe"]["value"] == D("-40.8") and loss["eps"]["value"] == D("-0.11")  # a negative value is extracted as received; validation rejects it downstream


def test_screener_top_ratios_are_parsed_in_canonical_units_from_a_saved_page_only():
    parsed = si.parse_screener_html((FIXTURES / "screener_testco_consolidated.html").read_text())
    assert parsed.top_ratios["market_cap"]["value"] == D("9999") * D("10000000")  # Rs. Cr -> INR
    page = '<div id="top-ratios"><ul><li><span class="name">Stock P/E</span><span class="value"><span class="number">18.6</span></span></li>' \
           '<li><span class="name">ROE</span><span class="value"><span class="number">12.5</span> %</span></li>' \
           '<li><span class="name">Market Cap</span><span class="value">₹ <span class="number">500</span></span></li></ul></div>'
    ratios = si._top_ratios(page)
    assert ratios["pe"]["value"] == D("18.6") and ratios["roe"]["value"] == D("12.5")
    assert "market_cap" not in ratios  # no "Cr." marker: the scale is never guessed


def test_screener_import_stores_top_ratios_as_the_third_provider_but_a_licensed_feed_does_not(db):
    inst = instrument(db, sid=None)
    parsed = si.parse_screener_html((FIXTURES / "screener_testco_consolidated.html").read_text())
    si.apply_gap_fill(db, inst, parsed, NOW, source="licensed_feed")
    assert vi.load(db, inst.id) == {}
    si.apply_gap_fill(db, inst, parsed, NOW)
    assert vi.load(db, inst.id)["screener"]["market_cap"]["value"] == D("99990000000")


# ───────────── storage ─────────────
def test_store_upserts_removes_metrics_a_provider_stopped_reporting_and_rejects_unknown_providers(db):
    inst = instrument(db)
    vi.store(db, inst.id, "dhan", {"pe": {"value": D("10"), "unit": "x"}, "sector": {"text": "Energy"}}, NOW)
    vi.store(db, inst.id, "dhan", {"pe": {"value": D("11"), "unit": "x"}}, NOW + timedelta(hours=1))  # sector no longer reported
    loaded = vi.load(db, inst.id)["dhan"]
    assert loaded["pe"]["value"] == D("11") and "sector" not in loaded and loaded["pe"]["retrieved_at"].startswith("2026-11-05T06:00")
    assert db.scalar(select(ValuationProviderInput).where(ValuationProviderInput.metric == "sector")) is None
    with pytest.raises(ValueError):
        vi.store(db, inst.id, "mystery", {"pe": {"value": D("1")}}, NOW)


def test_dhan_research_fetch_persists_every_usable_field_but_keeps_the_brief_json_safe(db, monkeypatch):
    dci.clear_cache()
    inst = instrument(db)

    class Adapter:
        def get_company_info(self, *_a, **_k):
            return copy.deepcopy(dhan_payload())

    monkeypatch.setattr(rp, "_market_data_credentials", lambda *_a: (None, SimpleNamespace(adapter=Adapter(), tokens={"access_token": "t"}), None))
    snapshot, reason = rp._fetch_dhan_company_info(db, uuid4(), inst)
    assert reason is None and "valuation_inputs" not in snapshot and json.dumps(snapshot)  # Decimals never reach the stored brief
    stored = vi.load(db, inst.id)["dhan"]
    assert stored["pe"]["value"] == D("18.6") and stored["ebitda"]["unit_assumed"] and stored["industry"]["text"] == "Petroleum Products"
    dci.clear_cache()


def test_yahoo_valuation_inputs_refresh_stores_only_for_a_matching_currency(db, monkeypatch):
    inst = instrument(db, sid=None)
    monkeypatch.setattr(fundamentals_service.YahooFinanceProvider, "_thread_call", staticmethod(lambda fn: fn()))
    monkeypatch.setattr(fundamentals_service.yf, "Ticker", lambda _s: SimpleNamespace(get_info=lambda: {"currency": "INR", "marketCap": 5_000_000_000, "trailingPE": 12.0, "sector": "Energy"}))
    assert fundamentals_service.refresh_valuation_inputs(db, inst, "RELIANCE.NS", fetched_at=NOW) == 3
    assert vi.load(db, inst.id)["yfinance"]["pe"]["value"] == D("12.0")
    other = instrument(db, "USCO", sid=None)
    monkeypatch.setattr(fundamentals_service.yf, "Ticker", lambda _s: SimpleNamespace(get_info=lambda: {"currency": "USD", "marketCap": 1, "trailingPE": 9}))
    assert fundamentals_service.refresh_valuation_inputs(db, other, "USCO", fetched_at=NOW) == 0 and vi.load(db, other.id) == {}  # never stored under the wrong currency


# ───────────── Dhan sector-peer universe ─────────────
class FakeDhan:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def get_company_info(self, token, tokens, security_id, segment, metrics=("CO", "RATIOS")):
        self.calls.append((security_id, segment, metrics))
        if security_id in self.fail:
            raise BrokerAdapterError("no data", category="provider_request_failed")
        return copy.deepcopy(dhan_payload())


def test_refresh_fetches_each_verified_nse_equity_once_skips_fresh_ones_and_ignores_unverified_or_unmapped(db):
    first, second = instrument(db, "AAA", "111"), instrument(db, "BBB", "222")
    instrument(db, "UNVERIFIED", "333", verified=False)
    instrument(db, "UNMAPPED", None)
    fake = FakeDhan()
    summary = peer_universe.refresh_dhan_universe(db, fake, "tok", {}, now=NOW)
    assert summary["fetched"] == 2 and sorted(c[0] for c in fake.calls) == ["111", "222"] and all(c[1:] == ("NSE_EQ", ("CO", "RATIOS")) for c in fake.calls)
    assert vi.load(db, first.id)["dhan"]["industry"]["text"] == "Petroleum Products" and vi.load(db, second.id)["dhan"]["market_cap"]["value"] > 0
    again = peer_universe.refresh_dhan_universe(db, fake, "tok", {}, now=NOW + timedelta(hours=1))
    assert again["fetched"] == 0 and again["skipped_fresh"] == 2 and len(fake.calls) == 2  # inside the freshness window: no second request
    later = peer_universe.refresh_dhan_universe(db, fake, "tok", {}, now=NOW + timedelta(hours=13))
    assert later["fetched"] == 2
    assert peer_universe.peer_group_sizes(db)["industry"] == {"Petroleum Products": 2}


def test_refresh_reports_failures_and_stops_early_after_repeated_ones_so_a_revoked_token_cannot_hammer_the_api(db):
    for i in range(8):
        instrument(db, f"S{i}", str(100 + i))
    fake = FakeDhan(fail={str(100 + i) for i in range(8)})
    summary = peer_universe.refresh_dhan_universe(db, fake, "tok", {}, now=NOW)
    assert summary["failed"] == peer_universe.MAX_CONSECUTIVE_FAILURES and summary["stopped_early"] is True and summary["fetched"] == 0
    assert len(fake.calls) == peer_universe.MAX_CONSECUTIVE_FAILURES and summary["failures"][0]["category"] == "provider_request_failed"
