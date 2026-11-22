"""NSE shareholding / results adapter: polite client, XBRL parsing, conflict rule, storage. Test data: tiny hand-written XBRL documents and a fake NSE client; no network."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, InstrumentFundamental
from app.models.fundamental_snapshots import ShareholdingSnapshot
from app.services import nse_data as nd

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def shp_xml(promoter="0.24", public="0.76", fii="0.07", dii="0.10", pledged_flag="true", pledged_shares="1200", promoter_shares="2400"):
    return f"""<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:s="x">
<xbrli:context id="ShareholdingOfPromoterAndPromoterGroup_ContextI"><xbrli:entity/><xbrli:period><xbrli:instant>2026-09-30</xbrli:instant></xbrli:period></xbrli:context>
<s:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI">{promoter}</s:ShareholdingAsAPercentageOfTotalNumberOfShares>
<s:NumberOfFullyPaidUpEquityShares contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI">{promoter_shares}</s:NumberOfFullyPaidUpEquityShares>
<s:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="PublicShareholding_ContextI">{public}</s:ShareholdingAsAPercentageOfTotalNumberOfShares>
<s:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="InstitutionsForeign_ContextI">{fii}</s:ShareholdingAsAPercentageOfTotalNumberOfShares>
<s:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="InstitutionsDomestic_ContextI">{dii}</s:ShareholdingAsAPercentageOfTotalNumberOfShares>
<s:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged contextRef="x">{pledged_flag}</s:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged>
<s:NumberOfSharesEncumberedUnderPledged contextRef="ShareholdingPattern_ContextI">{pledged_shares}</s:NumberOfSharesEncumberedUnderPledged></xbrli:xbrl>""".encode()


def results_xml(*, revenue="1000", pat="120", pbt="160", fin="10", other="20", dep="30", eps="12.5", start="2026-07-01", end="2026-09-30"):
    return f"""<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:i="x" xmlns:d="http://xbrl.org/2006/xbrldi">
<xbrli:context id="OneD"><xbrli:entity/><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="Seg"><xbrli:entity><xbrli:segment><d:explicitMember dimension="a">b</d:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>
<i:RevenueFromOperations contextRef="OneD">{revenue}</i:RevenueFromOperations><i:RevenueFromOperations contextRef="Seg">999999</i:RevenueFromOperations>
<i:OtherIncome contextRef="OneD">{other}</i:OtherIncome><i:FinanceCosts contextRef="OneD">{fin}</i:FinanceCosts><i:ProfitBeforeTax contextRef="OneD">{pbt}</i:ProfitBeforeTax>
<i:ProfitLossForPeriod contextRef="OneD">{pat}</i:ProfitLossForPeriod><i:DepreciationDepreciationAndAmortisationExpense contextRef="OneD">{dep}</i:DepreciationDepreciationAndAmortisationExpense>
<i:BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations contextRef="OneD">{eps}</i:BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations></xbrli:xbrl>""".encode()


# ───────────── parsing ─────────────
def test_shareholding_parse_gives_percentages_and_pledge_of_the_promoter_holding():
    p = nd.parse_shareholding_xbrl(shp_xml())
    assert p == {"promoter_pct": Decimal("24.0000"), "fii_pct": Decimal("7.0000"), "dii_pct": Decimal("10.0000"), "public_pct": Decimal("76.0000"), "pledged_pct_of_promoter": Decimal("50.0000")}   # 1200 of 2400 promoter shares


def test_no_pledge_disclosed_is_a_known_zero_and_a_missing_disclosure_is_unknown():
    assert nd.parse_shareholding_xbrl(shp_xml(pledged_flag="false"))["pledged_pct_of_promoter"] == Decimal("0")
    xml = shp_xml().decode().replace("WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged", "Other").encode()
    assert nd.parse_shareholding_xbrl(xml)["pledged_pct_of_promoter"] is None


def test_percentages_already_stated_as_0_to_100_are_normalised_and_inconsistent_ones_rejected():
    assert nd.parse_shareholding_xbrl(shp_xml(promoter="24", public="76", fii="7", dii="10"))["promoter_pct"] == Decimal("24.0000")
    bad = nd.parse_shareholding_xbrl(shp_xml(promoter="0.5", public="0.2"))
    assert bad["promoter_pct"] is None and bad["public_pct"] is None


def test_results_parse_takes_the_dimension_free_period_and_derives_operating_profit():
    r = nd.parse_results_xbrl(results_xml(), date(2026, 9, 30))["quarterly"]
    assert r["revenue"] == 1000 and r["net_income"] == 120 and r["eps"] == Decimal("12.5") and r["interest"] == 10 and r["depreciation"] == 30
    assert r["operating_profit"] == 150 and r["operating_margin"] == Decimal("15.00000000")                      # PBT 160 + finance costs 10 - other income 20
    assert nd.parse_results_xbrl(results_xml(), date(2026, 6, 30)) is None                                      # no context ends on that date
    annual = nd.parse_results_xbrl(results_xml(start="2025-10-01", end="2026-09-30"), date(2026, 9, 30))
    assert set(annual) == {"annual"}


# ───────────── polite client ─────────────
class FakeNse:
    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def shareholding(self, symbol):
        self.calls.append(("shp", symbol))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("blocked")
        return [{"date": "30-SEP-2026"}]

    def download_document(self, url):
        raise AssertionError("not used")


def test_the_client_spaces_requests_caches_responses_and_keeps_the_raw_file(tmp_path):
    t = {"now": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t["now"] += s
    fake = FakeNse()
    c = nd.PoliteNse(tmp_path, min_interval=2.2, sleep=sleep, clock=lambda: t["now"], nse=fake)
    assert c.shareholding("TCS") == [{"date": "30-SEP-2026"}]
    c.shareholding("INFY")
    assert sleeps and min(sleeps) >= 2.2 - 1e-9 and c.requests == 2                                             # at least 2.2 s between two requests
    c.shareholding("TCS")                                                                                       # cached: no new request
    assert c.requests == 2 and c.cache_hits == 1 and len(fake.calls) == 2
    blob = json.loads((tmp_path / "json" / "shp_TCS.json").read_text())
    assert blob["downloaded_at"] and blob["data"] == [{"date": "30-SEP-2026"}]                                  # raw response stored with its download date


def test_errors_back_off_and_repeated_errors_stop_the_client(tmp_path):
    sleeps = []
    c = nd.PoliteNse(tmp_path, min_interval=0.0, max_consecutive_errors=4, backoff=(5.0, 15.0, 45.0), sleep=sleeps.append, clock=lambda: 0.0, nse=FakeNse(fail=99))
    with pytest.raises(nd.NseBlocked):
        c.shareholding("TCS")
    assert 5.0 in sleeps and 15.0 in sleeps                                                                     # exponential backoff between attempts
    with pytest.raises(nd.NseBlocked):
        c.shareholding("INFY")                                                                                  # stays stopped: no more requests
    assert c.requests == 4
    ok = nd.PoliteNse(tmp_path / "b", min_interval=0.0, max_consecutive_errors=5, backoff=(1.0,), sleep=lambda s: None, clock=lambda: 0.0, nse=FakeNse(fail=1))
    assert ok.shareholding("TCS") and ok.consecutive_errors == 0                                                 # one failure then success resets the counter


def test_the_request_budget_stops_a_nightly_run(tmp_path):
    c = nd.PoliteNse(tmp_path, min_interval=0.0, budget=1, sleep=lambda s: None, clock=lambda: 0.0, nse=FakeNse())
    c.shareholding("A")
    with pytest.raises(nd.NseBlocked):
        c.shareholding("B")


# ───────────── storage ─────────────
@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _inst(db):
    i = Instrument(id=uuid4(), symbol="ABC", display_name="ABC", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


def test_shareholding_rows_carry_quarter_end_and_filing_date_and_are_never_duplicated(db):
    i = _inst(db)
    p = nd.parse_shareholding_xbrl(shp_xml())
    assert nd.store_shareholding(db, i.id, date(2026, 9, 30), date(2026, 10, 19), p, NOW) is True
    db.commit()
    assert nd.store_shareholding(db, i.id, date(2026, 9, 30), date(2026, 10, 25), p, NOW) is False             # same quarter: left alone
    row = db.scalar(select(ShareholdingSnapshot))
    assert row.quarter_end == date(2026, 9, 30) and row.filed_on == date(2026, 10, 19) and row.captured_on == date(2026, 10, 19) and row.pledged_pct_of_promoter == Decimal("50.0000")


def test_results_conflicts_over_two_percent_keep_nse_and_log_yahoo_in_provenance(db):
    i = _inst(db)
    db.add(InstrumentFundamental(instrument_id=i.id, period_type="quarterly", period_end=date(2026, 9, 30), currency="INR", revenue=Decimal("1000"), net_income=Decimal("100"), eps=Decimal("10"), source="yfinance", fetched_at=NOW,
                                 field_sources=None, extra_metrics=None))
    db.commit()
    vals = nd.parse_results_xbrl(results_xml(revenue="1010", pat="130", eps="10.1"), date(2026, 9, 30))["quarterly"]
    log: list = []
    res = nd.store_results(db, i.id, "quarterly", date(2026, 9, 30), vals, date(2026, 10, 8), "consolidated", NOW, log)
    db.commit()
    row = db.scalar(select(InstrumentFundamental))
    assert res == "updated" and row.net_income == Decimal("130") and row.revenue == Decimal("1000")             # revenue +1% is within tolerance (existing value kept), PAT +30% conflicts: NSE wins
    assert [c["field"] for c in log] == ["net_income"] and log[0]["previous"] == "100.00000000" and log[0]["previous_source"] == "yfinance"
    assert row.field_sources["net_income"]["source"] == "nse_results" and row.field_sources["net_income"]["basis"] == "consolidated" and row.filed_on == date(2026, 10, 8) and row.extra_metrics["conflicts"][0]["nse"] == "130" and row.extra_metrics["interest_expense"]["source"] == "nse_results"


def test_provenance_has_the_shape_the_statement_readers_expect(db):
    """Regression: a bare string per field made the research pipeline crash ('str' object has no attribute 'get') for every stock the NSE backfill touched."""
    i = _inst(db)
    vals = nd.parse_results_xbrl(results_xml(), date(2026, 9, 30))["quarterly"]
    nd.store_results(db, i.id, "quarterly", date(2026, 9, 30), vals, date(2026, 10, 8), "consolidated", NOW)
    db.commit()
    for field, prov in db.scalar(select(InstrumentFundamental)).field_sources.items():
        assert isinstance(prov, dict) and prov["source"] == "nse_results" and prov["fetched_at"] and prov["derived"] is False, field


def test_new_nse_periods_are_inserted_with_provenance_and_filing_date(db):
    i = _inst(db)
    vals = nd.parse_results_xbrl(results_xml(), date(2026, 9, 30))["quarterly"]
    assert nd.store_results(db, i.id, "quarterly", date(2026, 9, 30), vals, date(2026, 10, 8), "consolidated", NOW) == "inserted"
    db.commit()
    row = db.scalar(select(InstrumentFundamental))
    assert row.source == "nse_results" and row.filed_on == date(2026, 10, 8) and row.statement_basis == "consolidated" and row.operating_margin == Decimal("15.00000000")


def test_pick_filings_prefers_consolidated_then_the_latest_revision():
    old = [{"toDate": "30-Sep-2024", "period": "Quarterly", "consolidated": "Standalone", "broadCastDate": "10-Oct-2024 18:59:44", "xbrl": "u1"},
           {"toDate": "30-Sep-2024", "period": "Quarterly", "consolidated": "Consolidated", "broadCastDate": "10-Oct-2024 19:00:00", "xbrl": "u2"}]
    new = [{"qe_Date": "30-SEP-2026", "consolidated": "Consolidated", "broadcast_Date": "08-Oct-2026 22:07:33", "xbrl": "u3"}, {"qe_Date": "30-SEP-2026", "consolidated": "Consolidated", "broadcast_Date": "20-Oct-2026 10:00:00", "xbrl": "u4"}]
    picked = nd.pick_filings(old, new)
    assert [(f["period_end"], f["xbrl"]) for f in picked] == [(date(2026, 9, 30), "u4"), (date(2024, 9, 30), "u2")]


def test_a_missing_old_document_is_skipped_without_counting_toward_a_block(tmp_path):
    class Gone(Exception):
        pass
    Gone.__name__ = "NSEFileUnavailableError"

    class Nse:
        def download_document(self, url):
            raise Gone("404 Not Found")
    c = nd.PoliteNse(tmp_path, min_interval=0.0, max_consecutive_errors=2, sleep=lambda s: None, clock=lambda: 0.0, nse=Nse())
    for _ in range(5):                                                                                     # five missing files in a row must not stop the client
        with pytest.raises(nd.NseDocumentUnavailable):
            c.document("https://example.invalid/old.xml")
    assert c.consecutive_errors == 0
