"""Valuation scoring: provisional, peer-relative; feeds the production valuation component."""
import inspect

import pytest

from app.services import research_pipeline, research_scoring
from app.services import valuation_scoring as vs


def metric(status="calculated", reason=None):
    return {"status": status, "reason": reason, "value": "10.00", "freshness": "current", "origin": "calculated", "provider": None}


def peer(rank="30", n=12, status="compared"):
    return {"status": status, "percentile_rank": rank, "peer_count": n, "reason": None if status == "compared" else "only 2 comparable peer(s) (minimum 5)"}


def valuation(metrics=None, peers=None, current=True):
    return {"price": {"current": current}, "metrics": metrics or {}, "peer_comparison": {"metrics": peers or {}}}


FULL = lambda **o: valuation(  # noqa: E731
    {"pe_ttm": metric(), "peg": metric(), "pb": metric(), "price_to_sales": metric(), "ev_ebitda": metric(), **o.get("metrics", {})},
    {"pe_ttm": peer("20"), "peg": peer("40"), "pb": peer("30"), "price_to_sales": peer("50"), "ev_ebitda": peer("70"), **o.get("peers", {})}, o.get("current", True))


def test_sector_families_pick_different_metrics_and_weights():
    assert vs.sector_family("Financial Services", "Banks - Regional") == "financials"
    assert vs.sector_family("Basic Materials", "Steel") == "asset_heavy_cyclical"
    assert vs.sector_family("Technology", "Software - Infrastructure") == "growth_asset_light"
    assert vs.sector_family(None, None) == "default"
    for rules in vs.SECTOR_RULES.values():
        assert abs(sum(rules.values()) - 1) < 1e-9
    bank = vs.score_valuation(FULL(), sector="Financial Services", industry="Banks")
    assert set(bank["metrics"]) == {"pe_ttm", "peg", "pb"}  # EV/EBITDA and P/S are not used for banks
    tech = vs.score_valuation(FULL(), sector="Technology", industry="Software")
    assert "ev_ebitda" not in tech["metrics"] and "price_to_sales" in tech["metrics"]


def test_cheaper_than_peers_scores_higher_and_labels_follow_the_provisional_bands():
    cheap = vs.score_valuation(valuation({"pe_ttm": metric(), "pb": metric(), "price_to_sales": metric()}, {"pe_ttm": peer("10"), "pb": peer("20"), "price_to_sales": peer("15")}), sector=None, industry=None)
    dear = vs.score_valuation(valuation({"pe_ttm": metric(), "pb": metric(), "price_to_sales": metric()}, {"pe_ttm": peer("90"), "pb": peer("85"), "price_to_sales": peer("95")}), sector=None, industry=None)
    mid = vs.score_valuation(valuation({"pe_ttm": metric(), "pb": metric(), "price_to_sales": metric()}, {"pe_ttm": peer("50"), "pb": peer("50"), "price_to_sales": peer("50")}), sector=None, industry=None)
    assert (cheap["label"], mid["label"], dear["label"]) == ("attractive", "fair", "expensive") and cheap["score"] > mid["score"] > dear["score"]
    assert cheap["provisional"] is True and cheap["version"].endswith("provisional")


def test_pe_and_peg_form_one_group_and_cannot_double_count():
    both = vs.score_valuation(FULL(), sector="Technology", industry="Software")
    pe_only = vs.score_valuation(FULL(metrics={"peg": metric("unavailable", "x")}), sector="Technology", industry="Software")
    assert both["groups"]["earnings"] == 70.0  # mean of P/E 80 and PEG 60: one earnings evidence group
    assert pe_only["groups"]["earnings"] == 80.0
    assert both["scored_weight"] == pe_only["scored_weight"]  # adding PEG does not add weight


def test_negative_earnings_negative_equity_and_missing_metrics_are_not_scored_never_zero():
    v = FULL(metrics={"pe_ttm": metric("not_meaningful", "net income is not positive"), "peg": metric("unavailable", "no growth"), "pb": metric("not_meaningful", "equity is not positive")})
    result = vs.score_valuation(v, sector="Technology", industry="Software")
    assert result["metrics"]["pe_ttm"] == {"group": "earnings", "scored": False, "reason": "net income is not positive"}
    assert "earnings" not in result["groups"] and "book" not in result["groups"]
    assert result["status"] == "blocked" and result["score"] is None and result["label"] == "insufficient_evidence"


def test_unreliable_or_thin_peer_data_blocks_rather_than_falling_back_to_universal_cutoffs():
    thin = FULL(peers={k: peer(status="insufficient_peers") for k in ("pe_ttm", "peg", "pb", "price_to_sales", "ev_ebitda")})
    result = vs.score_valuation(thin, sector=None, industry=None)
    assert result["status"] == "blocked" and result["score"] is None and result["coverage_pct"] == 0.0
    assert all("peer" in m["reason"] for m in result["metrics"].values())


def test_stale_price_blocks_everything():
    result = vs.score_valuation(FULL(current=False), sector=None, industry=None)
    assert result["status"] == "blocked" and all(m["reason"] == "no current share price" for m in result["metrics"].values())


def test_two_independent_groups_and_sixty_percent_weight_are_required():
    only_one = vs.score_valuation(valuation({"pe_ttm": metric()}, {"pe_ttm": peer("10")}), sector=None, industry=None)
    assert only_one["status"] == "blocked" and "independent valuation group" in only_one["blockers"][0]
    # default family: earnings .45 + sales .25 = .70 >= .60 and 2 groups => scored
    ok = vs.score_valuation(valuation({"pe_ttm": metric(), "price_to_sales": metric()}, {"pe_ttm": peer("10"), "price_to_sales": peer("30")}), sector=None, industry=None)
    assert ok["status"] == "scored" and ok["scored_weight"] == pytest.approx(0.70)
    # financials: book .5 + earnings .5; only book scored => 1 group => blocked
    bank = vs.score_valuation(valuation({"pb": metric()}, {"pb": peer("10")}), sector="Financial Services", industry="Banks")
    assert bank["status"] == "blocked"


def test_confidence_reflects_coverage_and_peer_depth_separately_from_the_score():
    keys = ("pe_ttm", "pb", "price_to_sales", "peg", "ev_ebitda")
    deep = vs.score_valuation(FULL(peers={k: peer("30", n=15) for k in keys}), sector=None, industry=None)
    shallow = vs.score_valuation(FULL(peers={k: peer("30", n=5) for k in keys}), sector=None, industry=None)
    assert deep["score"] == shallow["score"] and deep["confidence"] > shallow["confidence"]


def test_market_cap_is_context_only_and_the_module_feeds_the_production_component():
    result = vs.score_valuation(FULL(metrics={"market_cap": metric()}), sector=None, industry=None)
    assert "market_cap" not in result["metrics"]
    assert "valuation" in research_scoring.COMPONENTS and "valuation_scoring" in inspect.getsource(research_scoring)
    assert "valuation_scoring" not in inspect.getsource(research_pipeline)  # the pipeline only attaches the shared evidence


def test_stale_or_non_ttm_values_are_not_scored():
    stale = FULL(metrics={"pe_ttm": {**metric(), "freshness": "stale"}, "peg": metric("unavailable", "no growth")})
    result = vs.score_valuation(stale, sector=None, industry=None)
    assert result["metrics"]["pe_ttm"]["scored"] is False and "not current" in result["metrics"]["pe_ttm"]["reason"]
