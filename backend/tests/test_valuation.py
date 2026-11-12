"""Valuation Intelligence metrics: calculations, negative earnings, missing data, share-count / currency / basis mismatches,
stale prices, peers. Display-only: no test here involves any score."""
import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, InstrumentFundamental, InstrumentSnapshot, MarketBar
from app.services import valuation as v

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
D = Decimal
QUARTERS = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31"]


def fv(value):
    return {"value": None if value is None else str(value), "field_path": "x"}


def quarter(end, *, ni=2_500_000, rev=10_000_000, equity=50_000_000, shares=1_000_000, basis=None, currency="INR", extras=None, source="yfinance"):
    return {"period_type": "quarterly", "period_end": end, "currency": currency, "source": source, "fetched_at": "2026-11-04T00:00:00+00:00",
            "statement_basis": basis, "extra_metrics": extras or {},
            "values": {"net_income": fv(ni), "revenue": fv(rev), "total_equity": fv(equity), "shares_outstanding": fv(shares)}}


def periods(**over):
    rows = [quarter(end, **over) for end in QUARTERS[:4]]
    # the year-earlier quarters (for trailing growth / PEG): TTM net income 8,000,000 vs 10,000,000 now => +25%
    rows += [quarter(end, ni=2_000_000, equity=None, shares=None) for end in QUARTERS[4:]]
    return rows


def technicals(**over):
    base = {"status": "available", "latest_close": "100", "latest_bar": "2026-11-04T00:00:00+00:00", "source": "dhan", "staleness": None, "message": None}
    base.update(over)
    return base


def fundamentals(periods_=None, *, mcap="100000000", age_days=1, pe=None, pb=None, dy=None, source="yfinance"):
    fetched = datetime(2026, 11, 5, tzinfo=timezone.utc) - __import__("datetime").timedelta(days=age_days)
    return {"periods": periods() if periods_ is None else periods_, "snapshot": {
        "source": source, "fetched_at": fetched.isoformat(),
        "values": {"market_cap": fv(mcap), "pe": fv(pe), "pb": fv(pb), "dividend_yield": fv(dy)}}}


def run(tech=None, fund=None, currency="INR"):
    return v.compute_valuation(currency=currency, technicals=tech or technicals(), fundamentals=fund or fundamentals(), now=NOW)


# ───────────── calculations ─────────────
def test_provider_market_cap_is_used_and_calculated_ratios_use_ttm_earnings_with_provenance():
    m = run()["metrics"]
    assert m["market_cap"]["status"] == "reported" and m["market_cap"]["origin"] == "provider_reported" and m["market_cap"]["provider"] == "yfinance"
    assert D(m["market_cap"]["value"]) == D("100000000") and m["market_cap"]["cross_check"]["disagrees"] is False  # price x shares agrees
    assert m["market_cap"]["retrieved_at"]
    assert D(m["pe_ttm"]["value"]) == D("10.00") and m["pe_ttm"]["origin"] == "calculated" and "TTM (sum of the latest 4 consecutive quarters)" in m["pe_ttm"]["basis"]
    assert D(m["pb"]["value"]) == D("2.00") and D(m["price_to_sales"]["value"]) == D("2.50") and D(m["book_value_per_share"]["value"]) == D("50.00")
    assert m["pe_ttm"]["reporting_period"] == "2026-09-30, 2026-06-30, 2026-03-31, 2025-12-31" and m["pb"]["reporting_period"] == "2026-09-30"
    pe_inputs = {i["name"]: i for i in m["pe_ttm"]["inputs"]}
    ni = next(i for n, i in pe_inputs.items() if n.startswith("Net income"))
    assert ni["value"] == "10000000" and "2026-09-30" in ni["period_end"] and ni["source"] == "yfinance" and ni["fetched_at"]
    assert m["pe_ttm"]["freshness"] == "current"


def test_pe_is_not_computed_from_annual_eps_and_annual_fallback_is_labelled_not_ttm():
    only_annual = [{"period_type": "annual", "period_end": "2026-03-31", "currency": "INR", "source": "yfinance", "fetched_at": "2026-11-01T00:00:00+00:00",
                    "statement_basis": None, "extra_metrics": {}, "values": {"net_income": fv(9_000_000), "revenue": fv(40_000_000), "total_equity": fv(50_000_000),
                                                                                 "shares_outstanding": fv(1_000_000), "eps": fv(9)}}]
    pe = run(fund=fundamentals(only_annual))["metrics"]["pe_ttm"]
    assert pe["status"] == "calculated" and D(pe["value"]) == D("11.11") and "NOT TTM" in pe["basis"]
    assert any("not TTM" in w for w in pe["warnings"]) and pe["freshness"] == "stale"


def test_peg_is_trailing_pe_over_ttm_growth_and_cannot_use_a_pe_that_is_unavailable():
    peg = run()["metrics"]["peg"]
    assert peg["status"] == "calculated" and D(peg["value"]) == D("0.40") and "trailing" in peg["warnings"][0]
    assert {i["name"]: i["value"] for i in peg["inputs"]} == {"P/E (TTM)": "10.00", "TTM net income growth": "25.0"}
    no_prior = run(fund=fundamentals(periods()[:4]))["metrics"]["peg"]
    assert no_prior["status"] == "unavailable" and "eight consecutive quarters" in no_prior["reason"]
    shrinking = [quarter(e, ni=1_000_000) for e in QUARTERS[:4]] + [quarter(e, ni=2_000_000, equity=None, shares=None) for e in QUARTERS[4:]]
    assert run(fund=fundamentals(shrinking))["metrics"]["peg"]["status"] == "not_meaningful"


def test_dividend_yield_units_are_normalised_per_source_and_unknown_sources_are_ignored():
    yf = run(fund=fundamentals(dy="1.16", source="yfinance"))["metrics"]["dividend_yield"]
    dhan = run(fund=fundamentals(dy="0.0116", source="dhan"))["metrics"]["dividend_yield"]  # the snapshot mapping stores Dhan as a fraction
    assert D(yf["value"]) == D(dhan["value"]) == D("1.16") and yf["unit"] == "%" and yf["status"] == "reported"
    other = run(fund=fundamentals(dy="1.16", source="mystery"))["metrics"]["dividend_yield"]
    assert other["status"] == "unavailable"  # not one of Dhan / Yahoo Finance / Screener: never read


def test_provider_reported_pe_and_pb_take_precedence_and_the_calculation_is_only_a_cross_check():
    m = run(fund=fundamentals(pe="11.5", pb="2.1"))["metrics"]  # consistent with the calculated 10.00x / 2.00x
    assert m["pe_ttm"]["status"] == "reported" and m["pe_ttm"]["value"] == "11.50" and m["pe_ttm"]["origin"] == "provider_reported" and m["pe_ttm"]["provider"] == "yfinance"
    assert m["pe_ttm"]["cross_check"]["origin"] == "calculated" and m["pe_ttm"]["cross_check"]["value"] == "10.00" and m["pe_ttm"]["cross_check"]["disagrees"] is False
    assert m["pb"]["status"] == "reported" and m["pb"]["value"] == "2.10"
    assert not any("calculated" in i["name"].lower() for i in m["pe_ttm"]["inputs"])  # the displayed value is the provider's, nothing else


def test_a_provider_value_that_contradicts_the_statements_is_rejected_recorded_and_replaced_by_the_calculation():
    m = run(fund=fundamentals(pe="20"))["metrics"]["pe_ttm"]  # 100% above market cap / TTM net income
    assert m["status"] == "calculated" and m["value"] == "10.00" and m["origin"] == "calculated"
    assert m["discrepancies"][0]["provider"] == "yfinance" and "differs from market cap / TTM net income" in m["discrepancies"][0]["reason"]
    assert [(c["provider"], c["outcome"]) for c in m["source_chain"]] == [("dhan", "not_reported"), ("yfinance", "rejected"), ("screener", "not_reported"), ("calculated", "used")]


# ───────────── negative / zero / missing ─────────────
def test_negative_earnings_negative_equity_and_zero_denominators_are_explicit_never_a_ratio():
    loss = run(fund=fundamentals(periods(ni=-500_000)))["metrics"]
    assert loss["pe_ttm"]["status"] == "not_meaningful" and loss["pe_ttm"]["value"] is None and "not positive" in loss["pe_ttm"]["reason"]
    assert loss["peg"]["status"] == "unavailable" and loss["price_to_sales"]["status"] == "calculated"  # other metrics still shown
    neg_equity = run(fund=fundamentals(periods(equity=-1_000_000)))["metrics"]
    assert neg_equity["pb"]["status"] == "not_meaningful" and neg_equity["book_value_per_share"]["status"] == "not_meaningful"
    zero = run(fund=fundamentals(periods(ni=0, rev=0)))["metrics"]
    assert zero["pe_ttm"]["status"] == "not_meaningful" and zero["price_to_sales"]["status"] == "not_meaningful"
    assert all(m["value"] is None for m in (loss["pe_ttm"], neg_equity["pb"], zero["pe_ttm"]))


def test_missing_inputs_leave_metrics_unavailable_with_the_exact_reason():
    empty = run(fund=fundamentals([], mcap=None))["metrics"]
    assert empty["market_cap"]["status"] == "unavailable" and "no share count" in empty["market_cap"]["reason"]
    assert empty["pb"]["status"] == "unavailable" and empty["pe_ttm"]["status"] == "unavailable"
    three_quarters = run(fund=fundamentals([quarter(e) for e in QUARTERS[:3]]))["metrics"]["pe_ttm"]
    assert three_quarters["status"] in {"unavailable", "calculated"}
    if three_quarters["status"] == "unavailable":
        assert "4 needed for TTM" in three_quarters["reason"]
    gap = [quarter(QUARTERS[0]), quarter(QUARTERS[1]), quarter(QUARTERS[3]), quarter(QUARTERS[4])]  # a missing quarter
    assert "not consecutive" in run(fund=fundamentals(gap))["metrics"]["pe_ttm"]["reason"] or run(fund=fundamentals(gap))["metrics"]["pe_ttm"]["status"] == "calculated"


def test_market_cap_pe_and_pb_survive_when_every_cash_and_ebitda_based_metric_is_impossible():
    m = run()["metrics"]
    assert {m[k]["status"] for k in ("market_cap", "pe_ttm", "pb")} == {"reported", "calculated"}
    for key in ("enterprise_value", "ev_ebitda", "net_debt_ebitda"):
        assert m[key]["status"] == "unavailable" and "cash and equivalents" in m[key]["reason"] and m[key]["value"] is None and m[key]["origin"] is None
    assert "EBITDA" in m["ev_ebitda"]["reason"]


# ───────────── stale prices ─────────────
@pytest.mark.parametrize("tech", [
    technicals(status="stale", staleness={"reason": "Latest stored daily bar is 2026-11-03; the last completed NSE session is 2026-11-04"}),
    technicals(status="unavailable", latest_close=None, message="No stored daily OHLCV bars are available."),
])
def test_a_stale_or_missing_price_is_never_used_as_current(tech):
    result = run(tech=tech, fund=fundamentals(mcap=None))
    assert result["price"]["current"] is False and result["price"]["value"] is None
    m = result["metrics"]
    assert m["market_cap"]["status"] == "unavailable" and ("stale" in m["market_cap"]["reason"] or "no current price" in m["market_cap"]["reason"])
    assert m["pe_ttm"]["status"] == m["pb"]["status"] == "unavailable"
    assert m["book_value_per_share"]["status"] == "calculated"  # needs no price: equity / shares


def test_stale_price_with_a_fresh_provider_market_cap_still_gives_market_cap_pe_pb_but_flags_the_source():
    m = run(tech=technicals(status="stale", latest_close="100"), fund=fundamentals(mcap="100000000", age_days=1))["metrics"]
    assert m["market_cap"]["status"] == "reported" and m["market_cap"]["freshness"] == "current"
    assert "not cross-checked" in m["market_cap"]["warnings"][0]
    assert D(m["pe_ttm"]["value"]) == D("10.00")


def test_an_old_provider_market_cap_is_not_current():
    m = run(tech=technicals(status="stale"), fund=fundamentals(age_days=30))["metrics"]
    assert m["market_cap"]["status"] == "unavailable" and "stale" in m["market_cap"]["reason"]


# ───────────── share-count / currency / basis mismatches ─────────────
def test_share_count_mismatch_with_the_provider_market_cap_blocks_every_market_cap_ratio():
    # price 100 x 1.0M shares = 100M, but the provider says 400M (e.g. a bonus/split not reflected in the stored share count)
    result = run(fund=fundamentals(mcap="400000000"))
    m = result["metrics"]
    assert m["market_cap"]["status"] == "not_meaningful" and "share-count mismatch" in m["market_cap"]["reason"] and "75.0%" in m["market_cap"]["reason"]
    assert {i["name"] for i in m["market_cap"]["inputs"]} >= {"Shares outstanding", "Yahoo Finance market cap"}
    assert m["market_cap"]["discrepancies"][0]["provider"] == "yfinance"
    for key in ("pe_ttm", "pb", "price_to_sales"):
        assert m[key]["status"] == "unavailable" and "share-count mismatch" in m[key]["reason"]
    assert m["book_value_per_share"]["status"] == "calculated"


def test_small_differences_between_computed_and_provider_market_cap_are_accepted_and_the_provider_value_is_kept():
    m = run(fund=fundamentals(mcap="104000000"))["metrics"]["market_cap"]
    assert m["status"] == "reported" and D(m["value"]) == D("104000000")  # not replaced by the calculated 100,000,000
    assert m["cross_check"]["origin"] == "calculated" and m["cross_check"]["disagrees"] is False


def test_old_share_counts_are_not_matched_to_a_current_price():
    old = [{**p, "period_end": e} for p, e in zip(periods(), ["2024-09-30", "2024-06-30", "2024-03-31", "2023-12-31"] + QUARTERS[4:])]
    m = run(fund=fundamentals(old, mcap=None))["metrics"]
    assert m["market_cap"]["status"] == "unavailable" and "more than 400 days old" in m["market_cap"]["reason"]


def test_currency_mismatch_refuses_ratios_without_converting():
    m = run(fund=fundamentals(periods(currency="USD")))["metrics"]
    for key in ("pb", "pe_ttm", "price_to_sales"):
        assert m[key]["status"] == "unavailable" and "no FX conversion" in m[key]["reason"]
    assert m["book_value_per_share"]["status"] == "unavailable"


def test_mixed_consolidated_and_standalone_inputs_are_refused_and_unstated_basis_is_flagged():
    mixed = periods()
    mixed[0]["statement_basis"] = "standalone"
    for p in mixed[1:4]:
        p["statement_basis"] = "consolidated"
    pe = run(fund=fundamentals(mixed))["metrics"]["pe_ttm"]
    assert pe["status"] == "unavailable" and "mix consolidated and standalone" in pe["reason"]
    unstated = run()["metrics"]["pe_ttm"]
    assert unstated["statement_basis"].startswith("unknown") and any("not stated" in w for w in unstated["warnings"])
    standalone = run(fund=fundamentals(periods(basis="standalone")))["metrics"]["pe_ttm"]
    assert standalone["statement_basis"] == "standalone" and any("standalone earnings compared with whole-company" in w for w in standalone["warnings"])
    half_stated = periods()
    half_stated[0]["statement_basis"] = "consolidated"
    assert run(fund=fundamentals(half_stated))["metrics"]["pe_ttm"]["status"] == "unavailable"


# ───────────── quality metrics from Screener-sourced extras ─────────────
def extras(op, dep, interest, source="screener"):
    return {n: {"value": str(x), "source": source, "fetched_at": "2026-11-01T00:00:00+00:00", "basis": "consolidated"}
            for n, x in (("operating_profit", op), ("depreciation", dep), ("interest_expense", interest))}


def test_interest_coverage_is_ebit_over_interest_and_roce_is_passed_through_with_its_source():
    ps = periods(basis="consolidated", extras=extras(3_000_000, 500_000, 250_000))
    ps[0]["extra_metrics"]["roce_pct"] = {"value": "18", "source": "screener", "fetched_at": "2026-11-01T00:00:00+00:00", "basis": "consolidated"}
    ps.append({"period_type": "annual", "period_end": "2026-03-31", "currency": "INR", "source": "screener", "fetched_at": "2026-11-01T00:00:00+00:00", "statement_basis": "consolidated",
               "values": {}, "extra_metrics": {"roce_pct": {"value": "18", "source": "screener", "fetched_at": "2026-11-01T00:00:00+00:00", "basis": "consolidated"}}})
    m = run(fund=fundamentals(ps))["metrics"]
    assert m["interest_coverage"]["status"] == "calculated" and D(m["interest_coverage"]["value"]) == D("10.00")  # (4 x 3.0M - 4 x 0.5M) / (4 x 0.25M)
    assert m["roce"]["status"] == "reported" and m["roce"]["value"] == "18.00" and m["roce"]["inputs"][0]["source"] == "screener"


def test_interest_coverage_edge_cases():
    zero_interest = run(fund=fundamentals(periods(basis="consolidated", extras=extras(3_000_000, 500_000, 0))))["metrics"]["interest_coverage"]
    assert zero_interest["status"] == "not_meaningful" and zero_interest["value"] is None
    negative_ebit = run(fund=fundamentals(periods(basis="consolidated", extras=extras(100_000, 500_000, 250_000))))["metrics"]["interest_coverage"]
    assert negative_ebit["status"] == "calculated" and D(negative_ebit["value"]) < 0 and "not covered" in negative_ebit["warnings"][0]
    missing = run()["metrics"]
    assert missing["interest_coverage"]["status"] == missing["roce"]["status"] == "unavailable"
    assert "not stored" in missing["roce"]["reason"]


def test_no_metric_is_a_score_and_the_evidence_layer_carries_the_scoring_result_separately(db):
    result = run()
    assert not any("score" in key for key in result["metrics"])
    assert result["scoring"] is None  # compute_valuation is evidence only; build_valuation attaches the scoring result
    inst = add_company(db, "SCORED")
    built = v.build_valuation(db, inst, {"technicals": technicals(), "fundamentals": fundamentals()}, NOW)
    assert built["scoring"]["version"].startswith("valuation-score-") and built["scoring"]["provisional"] is True and built["scoring"]["status"] == "blocked"


# ───────────── peers (real tables) ─────────────
@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_company(db, symbol, *, pe_ni=2_500_000, price="100", industry="Banks - Regional", fresh=True, snapshot_age_days=0):
    inst = Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW)
    db.add(inst)
    db.flush()
    db.add(InstrumentSnapshot(id=uuid4(), instrument_id=inst.id, market_cap=D(price) * 1_000_000, sector="Financial Services", industry=industry, source="yfinance", fetched_at=NOW - __import__("datetime").timedelta(days=snapshot_age_days)))
    day = date(2026, 11, 4) if fresh else date(2026, 10, 1)
    db.add(MarketBar(instrument_id=inst.id, interval="1d", timestamp=datetime(day.year, day.month, day.day, tzinfo=timezone.utc), open=D(price), high=D(price) + 1, low=D(price) - 1,
                     close=D(price), volume=D(1), source="dhan", retrieved_at=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc), is_synthetic=False))
    for end in QUARTERS[:4]:
        y, m, d = map(int, end.split("-"))
        db.add(InstrumentFundamental(id=uuid4(), instrument_id=inst.id, period_type="quarterly", period_end=date(y, m, d), currency="INR", source="yfinance", fetched_at=NOW,
                                     net_income=D(pe_ni), revenue=D(10_000_000), total_equity=D(50_000_000), shares_outstanding=D(1_000_000)))
    db.flush()
    return inst


def test_peer_comparison_needs_enough_comparable_peers_with_current_prices(db):
    subject = add_company(db, "SUBJ", price="100")
    sections_ = lambda: {"technicals": technicals(), "fundamentals": fundamentals()}  # noqa: E731
    assert v.build_valuation(db, subject, sections_(), NOW)["peer_comparison"]["metrics"]["pe_ttm"]["status"] == "insufficient_peers"
    for i, price in enumerate(["60", "80", "100", "120", "140"]):
        add_company(db, f"PEER{i}", price=price)
    add_company(db, "STALEPEER", price="10", fresh=False, snapshot_age_days=30)  # stale price AND stale provider market cap: excluded
    add_company(db, "OTHERIND", price="50", industry="Software")  # different industry: excluded
    result = v.build_valuation(db, subject, sections_(), NOW)["peer_comparison"]
    pe = result["metrics"]["pe_ttm"]
    assert result["peer_group"].startswith("industry: Banks - Regional") and pe["status"] == "compared" and pe["peer_count"] == 5
    assert pe["peer_median"] == "10.00" and pe["subject"] == "10.00" and pe["percentile_rank"] == "40"  # P/E of peers: 6, 8, 10, 12, 14
    assert "not index-wide medians" in result["note"] and result["peer_group_level"] == "industry"


def test_peer_comparison_unavailable_without_a_sector_or_industry(db):
    inst = Instrument(symbol="NOSECT", display_name="x", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW)
    db.add(inst)
    db.flush()
    peers = v.peer_comparison(db, inst, run(), NOW)
    assert peers["status"] == "unavailable" and "no sector or industry" in peers["reason"]


def test_a_peer_failure_never_removes_the_instruments_own_metrics(db, monkeypatch):
    subject = add_company(db, "SUBJ2")
    monkeypatch.setattr(v, "peer_comparison", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = v.build_valuation(db, subject, {"technicals": technicals(), "fundamentals": fundamentals()}, NOW)
    assert result["metrics"]["pe_ttm"]["status"] == "calculated" and "peer lookup failed" in result["peer_comparison"]["reason"]


def test_valuation_is_an_optional_scoring_component_with_the_documented_weights():
    from app.services import research_scoring as rs
    assert "valuation" in rs.COMPONENTS and rs.OPTIONAL_COMPONENTS == ("valuation",)
    assert {h: rs.HORIZON_WEIGHTS[h]["valuation"] for h in rs.HORIZONS} == {"swing": 0.05, "positional": 0.15, "long_term": 0.20}
    for horizon, weights in rs.HORIZON_WEIGHTS.items():
        assert abs(sum(weights.values()) - 1) < 1e-9
        for component, old in rs.HORIZON_WEIGHTS_V0_1_0[horizon].items():  # proportional rescale: the old mix is preserved exactly
            assert abs(weights[component] / (1 - weights["valuation"]) - old) < 1e-9

# ───────────── provider-first sourcing hierarchy ─────────────
def test_dhan_pb_is_displayed_and_never_replaced_by_the_calculation():
    fund = fundamentals(pb="1.88", source="yfinance")
    fund["snapshot"]["values"]["pb"].update(source="dhan", reconciliation="agrees")
    fund["snapshot"]["values"]["book_value"] = {"value": "53.19", "source": "dhan", "fetched_at": fund["snapshot"]["fetched_at"]}  # 100 / 53.19 = 1.88
    pb = run(fund=fund)["metrics"]["pb"]
    assert pb["status"] == "reported" and pb["value"] == "1.88" and pb["provider"] == "dhan" and pb["origin"] == "provider_reported" and pb["freshness"] == "current"
    assert pb["cross_check"]["origin"] == "calculated" and pb["cross_check"]["value"] == "2.00" and pb["cross_check"]["disagrees"] is False
    assert pb["warnings"] == [] and pb["retrieved_at"]


def test_dhan_book_value_per_share_is_displayed_when_reported_and_calculated_only_otherwise():
    fund = fundamentals()
    fund["snapshot"]["values"]["book_value"] = {"value": "47.5", "source": "dhan", "fetched_at": "2026-11-05T00:00:00+00:00"}
    bv = run(fund=fund)["metrics"]["book_value_per_share"]
    assert bv["status"] == "reported" and bv["value"] == "47.50" and bv["provider"] == "dhan" and bv["unit"] == "INR/share"
    assert bv["cross_check"]["value"] == "50.00" and bv["cross_check"]["disagrees"] is False
    assert run()["metrics"]["book_value_per_share"]["origin"] == "calculated"
    stale = fundamentals()
    stale["snapshot"]["values"]["book_value"] = {"value": "47.5", "source": "dhan", "fetched_at": "2026-01-01T00:00:00+00:00"}
    assert run(fund=stale)["metrics"]["book_value_per_share"]["origin"] == "calculated"  # older than 95 days: not trusted


def test_a_provider_pb_that_fails_reconciliation_or_is_stale_falls_back_to_the_calculation():
    fund = fundamentals(pb="1.88")
    fund["snapshot"]["values"]["pb"].update(source="dhan", reconciliation="discrepancy")
    pb = run(fund=fund)["metrics"]["pb"]
    assert pb["origin"] == "calculated" and pb["value"] == "2.00" and "disagrees with price / book value" in pb["discrepancies"][0]["reason"]
    old = fundamentals(pb="1.88")
    old["snapshot"]["values"]["pb"].update(source="dhan", fetched_at="2026-10-20T00:00:00+00:00")
    stale = run(fund=old)["metrics"]["pb"]
    assert stale["origin"] == "calculated" and stale["source_chain"][0]["outcome"] == "stale"  # price-linked: older than 3 days is not current
    assert run(fund=fundamentals(pb="-3"))["metrics"]["pb"]["origin"] == "calculated"


def test_dhan_pe_is_used_first_when_valid_then_yahoo_then_screener_and_stale_or_negative_values_are_skipped():
    pm = lambda **by: {p: {"pe": {"value": D(val), "retrieved_at": NOW.isoformat()}} for p, val in by.items()}  # noqa: E731
    result = lambda provider_metrics: v.compute_valuation(currency="INR", technicals=technicals(), fundamentals=fundamentals(), now=NOW, provider_metrics=provider_metrics)["metrics"]["pe_ttm"]  # noqa: E731
    first = result(pm(dhan="10.4", yfinance="10.8", screener="9.9"))
    assert first["provider"] == "dhan" and first["value"] == "10.40"
    assert [c["outcome"] for c in first["source_chain"]] == ["used", "not_checked", "not_checked", "not_needed"]  # Yahoo / Screener are not even consulted
    second = result(pm(yfinance="10.8", screener="9.9"))
    assert second["provider"] == "yfinance" and [c["outcome"] for c in second["source_chain"]][:3] == ["not_reported", "used", "not_checked"]
    third = result(pm(screener="9.9"))
    assert third["provider"] == "screener" and [c["outcome"] for c in third["source_chain"]][:3] == ["not_reported", "not_reported", "used"]
    skipped = result({"dhan": {"pe": {"value": D("-40.8"), "retrieved_at": NOW.isoformat()}}, "yfinance": {"pe": {"value": D("10.8"), "retrieved_at": "2026-10-01T00:00:00+00:00"}},
                      "screener": {"pe": {"value": D("10.1"), "retrieved_at": NOW.isoformat()}}})
    assert skipped["provider"] == "screener"
    assert [c["outcome"] for c in skipped["source_chain"]][:3] == ["rejected", "stale", "used"] and "loss-making" in skipped["discrepancies"][0]["reason"]


def test_calculation_happens_only_when_every_provider_is_unavailable_or_invalid():
    for provider_fund, expected in ((fundamentals(pe="11", pb="2.1"), "provider_reported"), (fundamentals(), "calculated")):
        m = run(fund=provider_fund)["metrics"]
        assert m["pe_ttm"]["origin"] == expected and m["pb"]["origin"] == expected
    assert run(fund=fundamentals(pe="11"))["metrics"]["price_to_sales"]["origin"] == "calculated"  # no snapshot provider reports P/S
    assert run(fund=fundamentals(pe="11"))["metrics"]["peg"]["origin"] == "calculated"


def test_market_cap_hierarchy_provider_then_calculation_then_unavailable():
    assert run()["metrics"]["market_cap"]["origin"] == "provider_reported"
    stale = run(fund=fundamentals(age_days=10))["metrics"]["market_cap"]
    assert stale["origin"] == "calculated" and stale["value"] == "100000000" and any("no usable provider market cap" in w for w in stale["warnings"])
    none = run(tech=technicals(status="stale"), fund=fundamentals(age_days=10, mcap=None))["metrics"]["market_cap"]
    assert none["status"] == "unavailable" and none["value"] is None and none["origin"] is None


def test_every_displayed_metric_keeps_field_level_provenance():
    m = run(fund=fundamentals(pe="18.5", dy="1.16"))["metrics"]
    for key in ("market_cap", "pe_ttm", "pb", "book_value_per_share", "price_to_sales", "dividend_yield"):
        metric = m[key]
        assert metric["origin"] in {"provider_reported", "calculated"} and metric["retrieved_at"] and metric["freshness"] == "current", key
        if metric["origin"] == "provider_reported":
            assert metric["provider"] == "yfinance"
        else:
            assert metric["reporting_period"] and metric["statement_basis"]
    assert D(m["dividend_yield"]["value"]) == D("1.16") and m["dividend_yield"]["unit"] == "%"  # source-specific normalisation kept
    for key in ("enterprise_value", "ev_ebitda"):
        assert m[key]["origin"] is None and m[key]["value"] is None


def test_roe_is_calculated_from_ttm_earnings_and_year_end_equity_and_handles_invalid_equity():
    assert run()["metrics"]["roe"]["value"] == "20.00" and run()["metrics"]["roe"]["origin"] == "calculated"  # 10M / 50M
    assert run(fund=fundamentals(periods(ni=-500_000)))["metrics"]["roe"]["value"] == "-4.00"  # a loss is a real (negative) ROE
    assert run(fund=fundamentals(periods(equity=-1_000_000)))["metrics"]["roe"]["status"] == "not_meaningful"
    assert run(fund=fundamentals(periods(currency="USD")))["metrics"]["roe"]["status"] == "unavailable"
    dhan = fundamentals()
    dhan["snapshot"]["values"]["roe"] = {"value": "99", "source": "dhan", "fetched_at": NOW.isoformat()}
    assert run(fund=dhan)["metrics"]["roe"]["value"] == "20.00"  # Dhan ROE is never read


def test_mismatch_between_provider_market_cap_and_price_times_shares_blocks_dependent_ratios_but_pb_from_the_provider_survives():
    fund = fundamentals(mcap="400000000", pb="2.4")
    fund["snapshot"]["values"]["pb"]["source"] = "dhan"
    m = run(fund=fund)["metrics"]
    assert m["market_cap"]["status"] == "not_meaningful" and m["pe_ttm"]["status"] == "unavailable"
    assert m["pb"]["status"] == "reported" and m["pb"]["warnings"]  # an independent provider ratio is still shown, flagged as not cross-checked


def test_normalized_evidence_is_the_shared_view_of_the_same_metrics():
    result = run(fund=fundamentals(pe="12.2"))  # 22% from the calculated 10.00x: used, but flagged as differing from the calculation? no: within the 25% cross-check band
    evidence = v.normalized_evidence(result)
    for key, m in result["metrics"].items():
        assert evidence["metrics"][key]["value"] == m["value"] and evidence["metrics"][key]["origin"] == m["origin"] and evidence["metrics"][key]["status"] == m["status"]
    assert "provider_vs_calculated_disagreements" in evidence and evidence["provider_vs_calculated_disagreements"] == []
    assert v.normalized_evidence(None) is None and v.normalized_evidence({"error": "x"}) is None

# ───────────── Dhan -> Yahoo Finance -> Screener -> calculation, for the metrics beyond P/E ─────────────
def table(**by_provider):
    """{provider: {metric: record}} in canonical units, retrieved now."""
    return {p: {m: {"value": D(str(val)), "retrieved_at": NOW.isoformat(), "unit_assumed": m == "ebitda" and p == "dhan"} for m, val in metrics.items()} for p, metrics in by_provider.items()}


def with_table(provider_metrics, *, fund=None, tech=None):
    return v.compute_valuation(currency="INR", technicals=tech or technicals(), fundamentals=fund or {"periods": periods(), "snapshot": {}}, now=NOW, provider_metrics=provider_metrics)


def test_every_dhan_field_is_used_when_it_is_valid_including_eps_ebitda_roe_and_pe():
    result = with_table(table(dhan={"market_cap": 100_000_000, "pe": 10.5, "pb": 2.05, "book_value": 49, "dividend_yield": 1.2, "roe": 21, "roce": 15, "eps": 9.8, "ebitda": 18_000_000}))
    m = result["metrics"]
    for key, expected in (("market_cap", "100000000"), ("pe_ttm", "10.50"), ("pb", "2.05"), ("book_value_per_share", "49.00"), ("dividend_yield", "1.20"), ("roe", "21.00"), ("roce", "15.00")):
        assert m[key]["provider"] == "dhan" and m[key]["value"] == expected and m[key]["origin"] == "provider_reported", key
    assert result["supporting_metrics"]["eps"]["provider"] == "dhan" and result["supporting_metrics"]["ebitda"]["provider"] == "dhan"
    assert any("INR crore is assumed" in w for w in result["supporting_metrics"]["ebitda"]["warnings"])  # the unit Dhan does not state is flagged
    assert m["pe_ttm"]["source_chain"][0] == {"provider": "dhan", "outcome": "used", "value": "10.5"}


def test_yahoo_supplies_what_dhan_lacks_and_screener_what_both_lack():
    result = with_table(table(dhan={"pe": 10.5}, yfinance={"price_to_sales": 2.4, "peg": 0.9, "enterprise_value": 120_000_000, "ev_ebitda": 7.5, "roe": 19, "ebitda": 16_000_000, "pb": 2.0},
                              screener={"roce": 14, "dividend_yield": 1.0, "roe": 25}))
    m = result["metrics"]
    assert {k: m[k]["provider"] for k in ("pe_ttm", "price_to_sales", "peg", "enterprise_value", "ev_ebitda", "roe", "pb", "roce", "dividend_yield")} == {
        "pe_ttm": "dhan", "price_to_sales": "yfinance", "peg": "yfinance", "enterprise_value": "yfinance", "ev_ebitda": "yfinance", "roe": "yfinance", "pb": "yfinance",
        "roce": "screener", "dividend_yield": "screener"}
    assert [c["provider"] + ":" + c["outcome"] for c in m["roce"]["source_chain"]] == ["dhan:not_reported", "yfinance:not_reported", "screener:used", "calculated:not_needed"]
    assert m["roe"]["source_chain"][2]["outcome"] == "not_checked"  # Screener ROE is never consulted once Yahoo is usable


def test_dhan_values_that_fail_validation_fall_through_with_the_discrepancy_recorded():
    result = with_table(table(dhan={"pb": 2.05, "book_value": 20, "roe": 90, "pe": 400, "market_cap": 100_000_000, "dividend_yield": 40}, yfinance={"pb": 2.02, "book_value": 50, "roe": 19.5, "dividend_yield": 1.1}))
    m = result["metrics"]
    assert m["pb"]["provider"] == "yfinance" and "differs from price / book value" in m["pb"]["discrepancies"][0]["reason"]  # Dhan P/B 2.05 vs price / BVPS 5.0
    assert m["roe"]["provider"] == "yfinance" and m["roe"]["discrepancies"][0]["provider"] == "dhan"  # 90% vs TTM net income / equity 20%
    assert m["pe_ttm"]["origin"] == "calculated" and "differs from market cap / TTM net income" in m["pe_ttm"]["discrepancies"][0]["reason"]
    assert m["dividend_yield"]["provider"] == "yfinance" and "outside 0-25%" in m["dividend_yield"]["discrepancies"][0]["reason"]
    assert m["book_value_per_share"]["provider"] == "yfinance"


def test_calculation_fills_ebitda_enterprise_value_and_ev_ebitda_only_when_no_provider_has_them():
    extras = {"operating_profit": {"value": "5000000"}, "depreciation": {"value": "500000"}, "interest_expense": {"value": "250000"}}
    rows = [quarter(end, extras=extras, basis="consolidated") for end in QUARTERS[:4]] + [quarter(end, ni=2_000_000, equity=None, shares=None, basis="consolidated") for end in QUARTERS[4:]]
    balance = {"total_debt": 10_000_000, "total_cash": 4_000_000}
    result = with_table(table(yfinance=balance), fund={"periods": rows, "snapshot": {}})
    ebitda, ev, ratio = result["supporting_metrics"]["ebitda"], result["metrics"]["enterprise_value"], result["metrics"]["ev_ebitda"]
    assert ebitda["origin"] == "calculated" and ebitda["value"] == "20000000" and [c["outcome"] for c in ebitda["source_chain"]][-1] == "used"
    assert ev["origin"] == "calculated" and ev["value"] == "106000000" and ratio["origin"] == "calculated" and ratio["value"] == "5.30"
    assert result["metrics"]["net_debt_ebitda"]["value"] == "0.30" and result["metrics"]["interest_coverage"]["value"] == "18.00"  # EBIT 20M - D&A 2M = 18M over interest 1M; net debt 6M / EBITDA 20M
    # a provider EBITDA that is wildly different from the stored operating profit is rejected as a unit / period mismatch
    bad = with_table(table(dhan={"ebitda": 2_000_000_000}, yfinance=balance), fund={"periods": rows, "snapshot": {}})["supporting_metrics"]["ebitda"]
    assert bad["origin"] == "calculated" and "unit or period mismatch" in bad["discrepancies"][0]["reason"]


def test_a_provider_market_cap_that_disagrees_with_price_times_shares_is_accepted_only_when_another_provider_corroborates_it():
    alone = with_table(table(dhan={"market_cap": 400_000_000}))["metrics"]["market_cap"]
    assert alone["status"] == "not_meaningful" and alone["discrepancies"][0]["provider"] == "dhan"
    corroborated = with_table(table(dhan={"market_cap": 400_000_000}, yfinance={"market_cap": 410_000_000}))["metrics"]["market_cap"]
    assert corroborated["status"] == "reported" and corroborated["provider"] == "dhan" and any("corroborated by Yahoo Finance" in w for w in corroborated["warnings"])


def test_dhan_industry_benchmarks_are_kept_as_context_only_and_never_become_metrics_or_inputs():
    result = with_table(table(dhan={"pe": 10.5, "pe_to_sector_pe": 0.75, "industry_pb": 1.4, "industry_eps": 12, "industry_operating_margin": 11}))
    b = result["provider_benchmarks"]
    assert b["industry_pe_implied"]["value"] == "14.00" and b["industry_pb"]["value"] == "1.40" and b["industry_pb"]["provider"] == "dhan"
    assert not any(k.startswith("industry") for k in result["metrics"])
    assert "provider_benchmarks" not in v.normalized_evidence(result)  # the AI payload shape is unchanged
    negative = with_table(table(dhan={"pe": 10.5, "pe_to_sector_pe": -2.8}))["provider_benchmarks"]
    assert "industry_pe_implied" not in negative and "not positive" in negative["industry_pe_implied_rejected"]["reason"]


# ───────────── sector peers from the stored provider classification ─────────────
def add_dhan_peer(db, symbol, *, pe, pb, industry="Banks", sector="Financial Services", mcap=1_000_000_000, age_days=0):
    from app.services import valuation_inputs as vi
    inst = Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW)
    db.add(inst)
    db.flush()
    inputs = {"market_cap": {"value": D(mcap), "unit": "INR"}, "pe": {"value": D(str(pe)), "unit": "x"}, "pb": {"value": D(str(pb)), "unit": "x"}, "sector": {"text": sector}, "industry": {"text": industry}}
    vi.store(db, inst.id, "dhan", inputs, NOW - __import__("datetime").timedelta(days=age_days))
    db.flush()
    return inst


def test_sector_peers_come_from_the_stored_dhan_classification_and_are_compared_on_the_same_provider_basis(db):
    subject = add_dhan_peer(db, "SUBJ", pe=10, pb=2.0)
    for i, (pe, pb) in enumerate([(6, 1.0), (8, 1.5), (12, 2.5), (14, 3.0), (16, 3.5)]):
        add_dhan_peer(db, f"P{i}", pe=pe, pb=pb)
    add_dhan_peer(db, "OTHER", pe=30, pb=9, industry="Software", sector="Technology")  # different industry and sector: excluded
    add_dhan_peer(db, "STALE", pe=99, pb=9, age_days=30)  # stale provider values are not comparable
    sections_ = {"technicals": technicals(status="stale"), "fundamentals": {"periods": [], "snapshot": {}}}
    result = v.build_valuation(db, subject, sections_, NOW)
    peers = result["peer_comparison"]
    assert peers["peer_group"].startswith("industry: Banks (Dhan classification)") and peers["peer_group_level"] == "industry" and peers["level_note"] is None
    pe, pb = peers["metrics"]["pe_ttm"], peers["metrics"]["pb"]
    assert pe["status"] == pb["status"] == "compared" and pe["peer_count"] == 5 and pe["peer_median"] == "12.00" and pe["percentile_rank"] == "40"
    assert result["metrics"]["pe_ttm"]["provider"] == "dhan" and result["metrics"]["pb"]["provider"] == "dhan"


def test_peers_widen_from_industry_to_sector_only_when_the_industry_is_too_small_and_say_so(db):
    subject = add_dhan_peer(db, "SUBJ", pe=10, pb=2.0, industry="Banks")
    add_dhan_peer(db, "B1", pe=9, pb=1.8, industry="Banks")
    for i, industry in enumerate(["Insurance", "Finance", "Finance", "Insurance", "Finance"]):
        add_dhan_peer(db, f"F{i}", pe=8 + i, pb=1.5 + i / 4, industry=industry)
    result = v.build_valuation(db, subject, {"technicals": technicals(status="stale"), "fundamentals": {"periods": [], "snapshot": {}}}, NOW)["peer_comparison"]
    assert result["peer_group_level"] == "sector" and "too few comparable peers" in result["level_note"] and result["metrics"]["pe_ttm"]["peer_count"] == 6
    small = v.build_valuation(db, add_dhan_peer(db, "LONELY", pe=10, pb=2.0, industry="Niche", sector="Niche"), {"technicals": technicals(status="stale"), "fundamentals": {"periods": [], "snapshot": {}}}, NOW)["peer_comparison"]
    assert small["status"] == "insufficient_peers" and small["metrics"]["pe_ttm"]["peer_count"] == 0  # nothing is invented for an empty group


def test_the_same_symbol_listed_twice_is_never_its_own_peer_or_counted_twice(db):
    subject = add_dhan_peer(db, "DUP", pe=10, pb=2.0)
    add_dhan_peer(db, "DUP", pe=10, pb=2.0)  # a second listing row for the same symbol
    for i in range(3):
        add_dhan_peer(db, f"Q{i}", pe=9 + i, pb=1.8)
        add_dhan_peer(db, f"Q{i}", pe=9 + i, pb=1.8)
    peers = v.build_valuation(db, subject, {"technicals": technicals(status="stale"), "fundamentals": {"periods": [], "snapshot": {}}}, NOW)["peer_comparison"]
    assert peers["metrics"]["pe_ttm"]["peer_count"] == 3 and peers["status"] == "insufficient_peers"
