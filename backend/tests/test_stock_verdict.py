"""Phase 7 roles, verdict order, evidence score, calibration display and regime caps (test data: hand-made lens dicts)."""
import itertools

import pytest

from app.services import stock_verdict as sv

CFG = sv.VerdictConfig()
LIQ_OK = {"pass": True, "status": "pass", "reason": "ok"}
NEWS_CLEAN = {"events_within_3_sessions": [], "material_headlines": [], "governance_events": [], "governance_known": True}


def mbres(klass, v=60.0, reasons=("ok",)):
    return {"class": klass, "scores": {"Q": 80, "G": 70, "V": v, "T": 40}, "reasons": list(reasons)}


def dec(h="swing", **kw):
    base = dict(tech_ok=True, liquidity=LIQ_OK, news=NEWS_CLEAN, mb=mbres("quality_compounder"), chart_ok=True, chart_reason="chart ok", grade="A", ready_to_buy=True, valuation_v=60.0, ipo_base_ok=None)
    base.update(kw)
    return sv.decide(h, **base)


# ───────────── verdict order: avoid -> not_now -> watch -> buy_on_trigger -> buy_now ─────────────
def test_first_match_wins_in_the_specified_order():
    assert dec(tech_ok=False)["verdict"] == "watch" and dec(tech_ok=False)["unknown_veto"] == ["stale_data"]
    assert dec(liquidity={"pass": False, "reason": "too thin"})["verdict"] == "avoid"
    assert dec(news={**NEWS_CLEAN, "events_within_3_sessions": [{"kind": "results", "date": "2026-11-25", "sessions_ahead": 2}]})["verdict"] == "avoid"      # veto beats a perfect chart
    assert dec(chart_ok=False, chart_reason="bearish")["verdict"] == "not_now"
    assert dec(grade="C")["verdict"] == "watch" and dec(grade=None)["verdict"] == "watch"
    assert dec(ready_to_buy=False)["verdict"] == "buy_on_trigger"
    assert dec(ready_to_buy=True)["verdict"] == "buy_now"
    assert dec(chart_ok=None, chart_reason="unknown")["verdict"] == "watch"                                    # a required input unknown: watch, not not_now


def test_swing_and_positional_vetoes():
    ev = {"kind": "ex_date", "date": "2026-11-25", "sessions_ahead": 2}
    assert dec("swing", news={**NEWS_CLEAN, "events_within_3_sessions": [ev]})["veto"] == "event"
    assert dec("positional", news={**NEWS_CLEAN, "events_within_3_sessions": [ev]})["verdict"] == "buy_now"      # the event veto is swing only
    for h in ("swing", "positional", "long_term"):
        assert dec(h, news={**NEWS_CLEAN, "material_headlines": ["Auditor resigns"]})["veto"] == "material_news"


# ───────────── long-term roles ─────────────
def test_long_term_needs_a_buy_class_and_sizes_emerging_and_new_listing():
    assert dec("long_term", mb=mbres("quality_compounder"))["size"] == 1.0
    assert dec("long_term", mb=mbres("emerging"))["size"] == 0.5 and dec("long_term", mb=mbres("emerging"))["verdict"] == "buy_now"
    nl = dec("long_term", mb=mbres("new_listing"), ipo_base_ok=True)
    assert nl["size"] == 0.25 and nl["verdict"] == "buy_now"
    assert dec("long_term", mb=mbres("new_listing"), ipo_base_ok=False)["verdict"] == "not_now"                # only on an IPO-base breakout
    for klass in ("watch", "not_a_fit", "not_tradable"):
        assert dec("long_term", mb=mbres(klass))["verdict"] == "not_now"
    assert dec("long_term", mb=mbres("avoid"))["verdict"] == "avoid" and dec("long_term", mb=mbres("avoid"))["veto"] == "fundamentals"
    assert dec("long_term", mb=mbres("not_assessable"))["verdict"] == "watch" and dec("long_term", mb=None)["verdict"] == "watch"
    assert dec("long_term", valuation_v=25.0)["verdict"] == "not_now" and "blocks a buy" in dec("long_term", valuation_v=25.0)["reasons"][0]
    assert dec("long_term", news={**NEWS_CLEAN, "governance_events": [{"kind": "auditor_resignation"}]})["veto"] == "governance"


def test_an_unknown_governance_feed_is_an_unknown_veto_input_for_long_term_only():
    unknown = {**NEWS_CLEAN, "governance_known": False}
    assert dec("long_term", news=unknown)["unknown_veto"] == ["governance"]
    assert dec("swing", news=unknown)["unknown_veto"] == [] and dec("positional", news=unknown)["unknown_veto"] == []


def test_fundamentals_never_affect_swing_or_positional_verdicts_property():
    """Property: for every fundamentals class, valuation score and growth shape, the swing and positional decisions are identical."""
    classes = [None, "multibagger_candidate", "quality_compounder", "emerging", "new_listing", "watch", "not_a_fit", "not_tradable", "avoid", "not_assessable"]
    for h, grade, ready, chart in itertools.product(("swing", "positional"), ("A", "B", "C", None), (True, False), (True, False, None)):
        seen = {(dec(h, mb=mbres(k) if k else None, valuation_v=v, grade=grade, ready_to_buy=ready, chart_ok=chart)["verdict"]) for k in classes for v in (None, 5.0, 90.0)}
        assert len(seen) == 1, (h, grade, ready, chart, seen)


# ───────────── evidence score ─────────────
def lens(score, direction, cov=1.0):
    return {"score": score, "direction": direction, "facts": [], "coverage": cov}


def test_evidence_formula_and_regime_penalty():
    lenses = {"chart": lens(80, "positive"), "market_regime": lens(80, "positive"), "liquidity_stale": lens(None, "positive"), "news_events": lens(None, "neutral")}
    setup = {"checks": [{"result": "pass"}] * 3 + [{"result": "fail"}] + [{"result": "not_evaluated"}]}
    e = sv.evidence("swing", lenses, setup, 90.0, "uptrend", [], CFG)
    assert (e["S"], e["A"], e["D"], e["L"], e["F"], e["P"]) == (75.0, 100.0, 100.0, 80.0, 90.0, 0.0)            # S ignores not_evaluated checks
    assert e["score"] == pytest.approx(0.30 * 75 + 0.25 * 100 + 0.20 * 100 + 0.15 * 80 + 0.10 * 90)
    assert sv.evidence("swing", lenses, setup, 90.0, "downtrend", [], CFG)["score"] == pytest.approx(e["score"] - 20)
    assert sv.evidence("swing", lenses, setup, 90.0, "range", [], CFG)["P"] == 10.0


def test_an_unknown_veto_input_caps_the_evidence_at_60_and_unknowns_never_help():
    lenses = {"chart": lens(95, "positive"), "market_regime": lens(80, "positive"), "liquidity_stale": lens(None, "positive"), "news_events": lens(None, "neutral")}
    setup = {"checks": [{"result": "pass"}] * 5}
    free = sv.evidence("swing", lenses, setup, 95.0, "uptrend", [], CFG)["score"]
    capped = sv.evidence("swing", lenses, setup, 95.0, "uptrend", ["governance"], CFG)
    assert free > 60 and capped["score"] == 60.0 and capped["capped_by_unknown_veto"] == ["governance"]
    blank = sv.evidence("swing", {}, None, None, None, [], CFG)
    assert blank["S"] == 0.0 and blank["A"] == 0.0 and blank["D"] == 0.0 and blank["score"] == 0.0               # nothing known: zero, never a favourable default
    assert "not yet validated" in sv.evidence("swing", lenses, setup, 95.0, "uptrend", [], CFG)["label"]


def test_the_setup_grade_is_a_pass_fail_check_not_extra_evidence():
    lenses = {"chart": lens(70, "positive")}
    a = sv.evidence("swing", lenses, {"checks": [{"result": "pass"}] * 4, "grade": "A"}, 50.0, "range", [], CFG)["score"]
    b = sv.evidence("swing", lenses, {"checks": [{"result": "pass"}] * 4, "grade": "C"}, 50.0, "range", [], CFG)["score"]
    assert a == b


# ───────────── calibration display ─────────────
TABLE = {"horizons": {"swing": {"validated": True, "buckets": [
    {"e_lo": 0, "e_hi": 50, "hit_rate": 0.30, "n": 500, "net_r_ci95": [-0.2, 0.1]},
    {"e_lo": 50, "e_hi": 70, "hit_rate": 0.58, "n": 418, "net_r_ci95": [0.05, 0.31]},
    {"e_lo": 70, "e_hi": 101, "hit_rate": 0.72, "n": 60, "net_r_ci95": [0.2, 0.5]}]},
    "positional": {"validated": False, "buckets": []}}}


def test_a_confidence_line_needs_a_validated_map_100_setups_and_a_net_r_interval_above_zero():
    ok = sv.confidence_line("swing", 61.0, TABLE)
    assert ok["line"] == "Confidence 58%: T1 before stop in 58% of 418 similar past setups" and ok["band"] == "Medium" and ok["calibrated"] == 58
    assert sv.confidence_line("swing", 30.0, TABLE)["line"] is None                       # the interval includes zero
    assert sv.confidence_line("swing", 85.0, TABLE)["line"] is None                       # only 60 setups
    assert sv.confidence_line("positional", 61.0, TABLE)["validated"] is False and sv.confidence_line("positional", 61.0, TABLE)["band"] is None
    assert sv.confidence_line("swing", 61.0, {})["band"] is None                          # no map stored: no High/Medium/Low at all


def test_no_high_medium_low_label_before_calibration_passes():
    e = sv.evidence("swing", {"chart": lens(90, "positive")}, {"checks": [{"result": "pass"}]}, 90.0, "uptrend", [], CFG)
    assert e["label"].startswith("Evidence score") and e["label"].endswith("(not yet validated)")
    c = sv.confidence_line("swing", e["score"], {})
    assert c["band"] is None and c["line"] is None and c["validated"] is False


# ───────────── regime caps (off by default) ─────────────
def _day(n):
    return [{"horizon": "swing", "verdict": "buy_now", "evidence": 90 - i, "grade": "A" if i < 3 else "B", "reasons": ["x"]} for i in range(n)]


def test_regime_caps_default_off_and_cap_when_on():
    assert CFG.regime_caps_enabled is False
    assert [v["verdict"] for v in sv.apply_regime_caps(_day(8), "downtrend", CFG)].count("buy_now") == 8              # off: nothing changes
    on = sv.VerdictConfig(regime_caps_enabled=True)
    assert [v["verdict"] for v in sv.apply_regime_caps(_day(12), "uptrend", on)].count("buy_now") == 10
    assert [v["verdict"] for v in sv.apply_regime_caps(_day(8), "range", on)].count("buy_now") == 5
    down = sv.apply_regime_caps(_day(8), "downtrend", on)
    assert [v["verdict"] for v in down].count("buy_now") == 2 and all(v["grade"] == "A" for v in down if v["verdict"] == "buy_now")
    assert "Regime cap" in down[-1]["reasons"][0]
    original = _day(3)
    sv.apply_regime_caps(original, "downtrend", on)
    assert all(v["verdict"] == "buy_now" for v in original)                                                           # the input is not modified


def test_roles_table_matches_the_specification():
    r = sv.ROLES
    assert r["chart"] == {"swing": "requirement", "positional": "requirement", "long_term": "context"}
    assert r["fundamentals"]["swing"] == "context" and r["fundamentals"]["positional"] == "context" and r["fundamentals"]["long_term"] == "veto_requirement"
    assert r["valuation"]["long_term"] == "score_block" and r["news_events"] == {"swing": "veto", "positional": "veto", "long_term": "veto"}
    assert r["financial_trends"] == {"swing": "context", "positional": "score", "long_term": "score"}
