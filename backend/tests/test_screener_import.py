"""Screener gap-fill: parsing, units, gap detection, source priority, conflicts, provenance, API.
Uses a saved illustrative HTML fixture only; nothing here (or in the app) contacts screener.in."""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument, InstrumentFundamental
from app.services import fundamentals as fundamentals_service
from app.services import research_pipeline
from app.services import research_scoring as rs
from app.services import screener_import as si
from test_research_pipeline import client, csrf, get_instrument_id, register, test_engine  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)
CR = Decimal("10000000")
D = Decimal


def page(name="screener_testco_consolidated.html"):
    return (FIXTURES / name).read_text()


# ───────────── parsing and unit normalisation ─────────────
def test_parser_reads_periods_basis_and_converts_rs_crores_to_inr():
    parsed = si.parse_screener_html(page())
    assert parsed.basis == "consolidated"
    annual = parsed.periods[("annual", date(2025, 3, 31))]
    assert annual.fields["revenue"] == D("1200") * CR and annual.fields["net_income"] == D("130") * CR
    assert annual.fields["eps"] == D("13.00")  # per-share: never scaled to Crores
    assert annual.fields["operating_margin"] == D("15")  # percent, existing convention
    assert annual.fields["operating_cash_flow"] == D("140") * CR
    assert annual.fields["total_debt"] == D("100") * CR and annual.fields["total_assets"] == D("1090") * CR
    assert ("annual", date(2022, 3, 31)) in parsed.periods and ("annual", date(2026, 3, 31)) not in parsed.periods  # TTM skipped
    assert set(p[0] for p in parsed.periods) == {"annual", "quarterly"}


def test_derived_fields_are_flagged_and_unfillable_fields_never_appear():
    annual = si.parse_screener_html(page()).periods[("annual", date(2025, 3, 31))]
    assert annual.fields["total_equity"] == D("730") * CR and "total_equity" in annual.derived  # capital + reserves
    assert annual.fields["total_liabilities"] == D("360") * CR and "total_liabilities" in annual.derived  # borrowings + other
    assert "free_cash_flow" not in annual.fields and "shares_outstanding" not in annual.fields


def test_extras_cover_interest_working_capital_roce_and_shareholding():
    parsed = si.parse_screener_html(page())
    annual = parsed.periods[("annual", date(2025, 3, 31))].extras
    assert annual["interest_expense"] == D("26") * CR and annual["roce_pct"] == D("18") and annual["cash_conversion_cycle_days"] == D("41")
    assert annual["cash_from_investing"] == D("-90") * CR
    quarter = parsed.periods[("quarterly", date(2025, 6, 30))].extras
    assert quarter["promoter_holding_pct"] == D("59.80") and quarter["fii_holding_pct"] == D("11.20")


def test_blank_cells_are_missing_not_zero():
    quarter = si.parse_screener_html(page()).periods[("quarterly", date(2025, 6, 30))]
    assert "interest_expense" not in quarter.extras  # the Jun 2025 'Interest' cell is blank


@pytest.mark.parametrize("text,expected", [("1,23,456", D("123456")), ("-1,234", D("-1234")), ("12.5%", D("12.5")),
                                           ("", None), ("-", None), ("abc", None), ("₹ 9,999", D("9999"))])
def test_number_normalisation(text, expected):
    assert si._number(text) == expected


@pytest.mark.parametrize("mutate,code", [
    (lambda h: "<html><body>nothing</body></html>", "no_screener_financial_sections_found"),
    (lambda h: h.replace("Rs. Crores", "USD Millions"), "unit_not_rs_crores"),
    (lambda h: h.replace("Consolidated Figures in Rs. Crores", "Figures in Rs. Crores", 1), "statement_basis_not_identified"),
    (lambda h: h + "x" * (si.MAX_HTML_BYTES + 1), "page_too_large"),
])
def test_unrecognised_or_unsafe_pages_are_rejected_not_guessed(mutate, code):
    with pytest.raises(si.ScreenerParseError) as error:
        si.parse_screener_html(mutate(page()))
    assert str(error.value) == code


def test_implausible_operating_margin_is_excluded_not_repaired():
    mutated = page().replace("<td>12%</td><td>13%</td><td>14%</td><td>15%</td><td>15%</td>", "<td>12%</td><td>13%</td><td>14%</td><td>1500%</td><td>15%</td>")
    assert mutated != page()
    parsed = si.parse_screener_html(mutated)
    assert "operating_margin" not in parsed.periods[("annual", date(2025, 3, 31))].fields
    assert parsed.periods[("annual", date(2024, 3, 31))].fields["operating_margin"] == D("14")


# ───────────── gap detection + gap filling ─────────────
def make_instrument(db, **over):
    data = dict(symbol="TESTCO", display_name="Test Co", asset_class="equity", venue="NSE", currency="INR", country="IN",
                timezone="Asia/Kolkata", created_at=NOW)
    data.update(over)
    instrument = Instrument(**data)
    db.add(instrument)
    db.flush()
    return instrument


def yf_row(db, instrument, kind, end, **values):
    row = InstrumentFundamental(id=uuid4(), instrument_id=instrument.id, period_type=kind, period_end=end, currency="INR",
                                source="yfinance", fetched_at=EARLIER, **values)
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def db(test_engine):  # noqa: F811
    with Session(test_engine) as session:
        yield session


def test_gap_audit_reports_missing_fields_history_depth_and_what_screener_can_fill(db):
    instrument = make_instrument(db)
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR, net_income=D("130") * CR)
    gaps = si.fundamentals_gaps(db, instrument.id)
    annual = gaps["annual"]
    assert annual["stored_periods"] == 1 and annual["missing_history_periods"] == 9
    assert "operating_cash_flow" in annual["missing_fields_by_period"]["2025-03-31"]
    assert "free_cash_flow" not in annual["screener_fillable"]["2025-03-31"]  # only unfillable fields are excluded
    assert gaps["unfillable_from_screener"] == ["free_cash_flow", "shares_outstanding"]
    assert gaps["quarterly"]["stored_periods"] == 0


def test_fills_only_nulls_keeps_existing_values_and_records_field_provenance(db):
    instrument = make_instrument(db)
    row = yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR, net_income=D("130") * CR, shares_outstanding=D("0"))
    result = si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), NOW)
    db.refresh(row)
    assert row.revenue == D("1200") * CR and row.source == "yfinance" and row.fetched_at.replace(tzinfo=None) == EARLIER.replace(tzinfo=None)
    assert row.operating_cash_flow == D("140") * CR and row.total_debt == D("100") * CR and row.eps == D("13.00")
    assert row.free_cash_flow is None and row.shares_outstanding == 0  # unfillable gaps stay gaps; never invented
    prov = row.field_sources["operating_cash_flow"]
    assert prov["source"] == "screener" and prov["basis"] == "consolidated" and prov["unit"] == "INR" and prov["derived"] is False
    assert prov["fetched_at"] == NOW.isoformat()
    assert row.field_sources["total_equity"]["derived"] is True
    assert "revenue" not in row.field_sources  # existing provider values carry no screener provenance
    assert "operating_cash_flow" in result.filled["annual 2025-03-31"] and "revenue" not in result.filled["annual 2025-03-31"]
    assert row.extra_metrics["roce_pct"]["source"] == "screener" and row.extra_metrics["interest_expense"]["value"] == str(D("26") * CR)


def test_missing_historical_periods_are_created_with_screener_as_source_and_basis(db):
    instrument = make_instrument(db)
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR)
    result = si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), NOW)
    rows = {r.period_end: r for r in db.scalars(select(InstrumentFundamental).where(
        InstrumentFundamental.instrument_id == instrument.id, InstrumentFundamental.period_type == "annual"))}
    assert set(rows) == {date(2022, 3, 31), date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31)}
    created = rows[date(2022, 3, 31)]
    assert created.source == "screener" and created.statement_basis == "consolidated" and created.currency == "INR"
    assert created.revenue == D("800") * CR and created.total_debt is None  # no balance sheet that year: NULL, not zero
    assert "annual 2022-03-31" in result.created_periods
    quarterly = db.scalars(select(InstrumentFundamental).where(InstrumentFundamental.period_type == "quarterly")).all()
    assert len(quarterly) == 3 and all(q.source == "screener" for q in quarterly)


def test_conflicting_values_keep_the_existing_value_and_are_recorded(db):
    instrument = make_instrument(db)
    row = yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1260") * CR, net_income=D("130.5") * CR)  # revenue +5%, net income +0.4%
    result = si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), NOW)
    db.refresh(row)
    assert row.revenue == D("1260") * CR and row.net_income == D("130.5") * CR  # nothing overwritten
    conflict = row.extra_metrics["screener_conflicts"]["revenue"]
    assert conflict["screener"] == str(D("1200") * CR) and conflict["existing_source"] == "yfinance" and D(conflict["relative_diff"]) > D("0.04")
    assert "net_income" not in row.extra_metrics["screener_conflicts"]  # within tolerance
    assert [c["field"] for c in result.conflicts] == ["revenue"]


def test_conflict_surfaces_as_a_data_quality_item_in_the_brief(db):
    instrument = make_instrument(db)
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1260") * CR)
    si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), NOW)
    quality: list = []
    section, _rows = research_pipeline._fundamentals_section(db, instrument.id, quality)
    items = [q for q in quality if q["kind"] == "conflicting"]
    assert items and "Screener reports" in items[0]["reason"] and "existing value was kept" in items[0]["reason"]
    period = next(p for p in section["periods"] if p["period_end"] == "2025-03-31")
    filled = period["values"]["operating_cash_flow"]
    assert filled["source"] == "screener" and filled["basis"] == "consolidated" and filled["fetched_at"] == NOW.isoformat()
    assert "source" not in period["values"]["revenue"]  # provider-sourced values carry only the row source
    assert period["extra_metrics"]["roce_pct"]["value"] == "18" and "screener_conflicts" not in period["extra_metrics"]


def test_reimport_is_idempotent(db):
    instrument = make_instrument(db)
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR)
    parsed = si.parse_screener_html(page())
    si.apply_gap_fill(db, instrument, parsed, EARLIER)
    count = len(db.scalars(select(InstrumentFundamental)).all())
    again = si.apply_gap_fill(db, instrument, parsed, NOW)
    assert again.created_periods == [] and again.filled == {} and again.conflicts == [] and again.extras_written == 0
    assert len(db.scalars(select(InstrumentFundamental)).all()) == count


def test_near_identical_period_end_is_matched_not_duplicated(db):
    instrument = make_instrument(db)
    row = yf_row(db, instrument, "annual", date(2025, 3, 30), revenue=D("1200") * CR)
    si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), NOW)
    db.refresh(row)
    assert row.operating_cash_flow == D("140") * CR
    assert len([r for r in db.scalars(select(InstrumentFundamental)) if r.period_type == "annual" and r.period_end.year == 2025]) == 1


def test_standalone_page_never_mixes_into_consolidated_provider_rows(db):
    instrument = make_instrument(db)
    row = yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR)
    result = si.apply_gap_fill(db, instrument, si.parse_screener_html(page("screener_testco_standalone.html")), NOW)
    db.refresh(row)
    assert row.operating_cash_flow is None
    assert any(s["reason"].startswith("statement_basis_mismatch") for s in result.skipped)
    created = db.scalar(select(InstrumentFundamental).where(InstrumentFundamental.period_end == date(2022, 3, 31)))
    assert created.statement_basis == "standalone"  # genuinely new periods keep their own basis


def test_non_inr_rows_and_non_indian_instruments_are_never_filled_or_converted(db):
    instrument = make_instrument(db)
    row = yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1"))
    row.currency = "USD"
    db.flush()
    result = si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), NOW)
    db.refresh(row)
    assert row.operating_cash_flow is None and any(s["reason"] == "currency_mismatch_no_conversion" for s in result.skipped)
    foreign = make_instrument(db, symbol="AAPL", country="US", currency="USD", venue="NASDAQ")
    with pytest.raises(si.ScreenerParseError):
        si.apply_gap_fill(db, foreign, si.parse_screener_html(page()), NOW)


# ───────────── source priority against the primary provider refresh ─────────────
def yf_payload(**annual):
    base = dict(period_end=date(2025, 3, 31), currency="INR", revenue=D("1200") * CR, net_income=D("130") * CR, eps=None,
                operating_margin=None, total_assets=None, total_liabilities=None, total_equity=None, operating_cash_flow=None,
                free_cash_flow=None, total_debt=None, shares_outstanding=None)
    base.update(annual)
    return {"annual": [base], "quarterly": [], "snapshot": {"market_cap": None, "pe": None, "pb": None, "dividend_yield": None, "sector": None, "industry": None}}


def test_provider_refresh_keeps_screener_gap_fills_when_it_still_has_a_gap(db, monkeypatch):
    instrument = make_instrument(db)
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR)
    si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), EARLIER)
    monkeypatch.setattr(fundamentals_service, "fetch_yfinance_fundamentals", lambda _s: yf_payload())
    fundamentals_service.sync_instrument_fundamentals(db, instrument, "TESTCO.NS", fetched_at=NOW)
    row = db.scalar(select(InstrumentFundamental))
    assert row.operating_cash_flow == D("140") * CR and row.field_sources["operating_cash_flow"]["source"] == "screener"
    assert row.fetched_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)  # the primary provider's freshness advanced


def test_provider_value_replaces_a_screener_fill_and_provenance_reverts(db, monkeypatch):
    instrument = make_instrument(db)
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR)
    si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), EARLIER)
    monkeypatch.setattr(fundamentals_service, "fetch_yfinance_fundamentals", lambda _s: yf_payload(operating_cash_flow=D("141") * CR))
    fundamentals_service.sync_instrument_fundamentals(db, instrument, "TESTCO.NS", fetched_at=NOW)
    row = db.scalar(select(InstrumentFundamental))
    assert row.operating_cash_flow == D("141") * CR and "operating_cash_flow" not in (row.field_sources or {})
    assert row.total_debt == D("100") * CR  # unrelated screener fill untouched


# ───────────── scoring sees filled data; missing stays missing ─────────────
def test_scoring_uses_filled_values_and_leaves_unfilled_metrics_blocked_not_zero(db):
    instrument = make_instrument(db, symbol="SCORECO")
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1200") * CR, net_income=D("130") * CR)
    sections_before = {"periods": research_pipeline._fundamentals_section(db, instrument.id, [])[0]["periods"]}
    before = rs.score_fundamental(sections_before, "Industrials", None)
    si.apply_gap_fill(db, instrument, si.parse_screener_html(page()), NOW)
    section, _ = research_pipeline._fundamentals_section(db, instrument.id, [])
    after = rs.score_fundamental(section, "Industrials", None)
    before_keys = {f["key"] for f in before["factors"]}
    after_keys = {f["key"] for f in after["factors"]}
    assert {"roe", "debt_to_equity", "cash_conversion"} <= after_keys and not {"roe", "debt_to_equity"} & before_keys
    assert "fcf_margin" not in after_keys and any("Free-cash-flow margin" in m for m in after["missing"])  # FCF still unavailable


# ───────────── API ─────────────
def test_import_route_requires_csrf_validates_input_and_reports_before_after(client, test_engine):  # noqa: F811
    register(client, test_engine, "screener@example.com")
    instrument_id = get_instrument_id(client)
    url = f"/api/v1/research/instruments/{instrument_id}/screener-import"
    assert client.post(url, json={"html": page()}).status_code == 403
    bad = client.post(url, json={"html": "<html>" + "x" * 300 + "</html>"}, headers=csrf(client))
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "no_screener_financial_sections_found"
    with Session(test_engine) as session:
        yf_row(session, session.get(Instrument, UUID(instrument_id)), "annual", date(2025, 3, 31), revenue=D("1200") * CR)
        session.commit()
    before = client.get(f"/api/v1/research/instruments/{instrument_id}/fundamentals-gaps").json()
    assert "operating_cash_flow" in before["annual"]["missing_fields_by_period"]["2025-03-31"]
    ok = client.post(url, json={"html": page()}, headers=csrf(client))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["basis"] == "consolidated" and body["source"] == "screener" and "annual 2025-03-31" in body["filled_fields"]
    assert "operating_cash_flow" not in body["gaps_after"]["annual"]["missing_fields_by_period"].get("2025-03-31", [])
    assert body["gaps_after"]["annual"]["stored_periods"] > body["gaps_before"]["annual"]["stored_periods"]
    assert "Decision support" in body["disclaimer"]
    client.cookies.clear()
    assert client.post(url, json={"html": page()}).status_code in {401, 403}


def test_no_code_path_in_the_module_performs_network_requests():
    source = Path(si.__file__).read_text()
    assert "httpx" not in source and "requests" not in source and "urllib" not in source and "socket" not in source


def test_reimport_with_an_unresolved_conflict_leaves_stored_data_identical(db):
    instrument = make_instrument(db)
    yf_row(db, instrument, "annual", date(2025, 3, 31), revenue=D("1260") * CR)
    parsed = si.parse_screener_html(page())
    si.apply_gap_fill(db, instrument, parsed, EARLIER)
    row = db.scalar(select(InstrumentFundamental).where(InstrumentFundamental.period_end == date(2025, 3, 31)))
    snapshot = (dict(row.field_sources), dict(row.extra_metrics))
    again = si.apply_gap_fill(db, instrument, parsed, NOW)
    db.refresh(row)
    assert (dict(row.field_sources), dict(row.extra_metrics)) == snapshot  # same retrieved_at, same record
    assert [c["field"] for c in again.conflicts] == ["revenue"]  # still reported to the caller
