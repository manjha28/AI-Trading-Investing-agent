"""Nexus v2 Phase 4: Gate -> Quality (long-term only) -> Timing -> Rank, sentiment as a gate, and the shadow record. Test data: real candle fixtures (tests/fixtures/level_guardrails, last candle
2026-11-20) and hand-made fundamentals/sections in an in-memory database."""
import json
from datetime import date, datetime, timezone
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument
from app.models.pipeline_v2 import PipelineV2Shadow
from app.services import gates_v2, pipeline_v2 as P, research_scoring as rs
from app.services import technical_engine as te

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
FIX = __import__("pathlib").Path(__file__).parent / "fixtures" / "level_guardrails"


def _period(year, **v):
    vals = {k: {"value": str(x)} for k, x in v.items()}
    return {"period_type": "annual", "period_end": f"{year}-03-31", "currency": "INR", "values": vals}


GOOD = [_period(2024, revenue=100, net_income=10, total_equity=50, total_debt=20, operating_cash_flow=12), _period(2025, revenue=110, net_income=12, total_equity=60, total_debt=20, operating_cash_flow=14),
        _period(2026, revenue=120, net_income=14, total_equity=70, total_debt=20, operating_cash_flow=15)]


# ───────────── quality gate (long-term only) ─────────────
def test_quality_passes_a_healthy_company():
    q = P.quality_gate({"periods": GOOD}, "Technology", "Software")
    assert q["status"] == "pass" and all(c["result"] == "pass" for c in q["checks"])


def test_no_statements_means_not_assessable_not_a_pass():
    q = P.quality_gate({"periods": []}, "Technology", None)
    assert q["status"] == "not_assessable" and "not assessable" in q["checks"][0]["reason"]


@pytest.mark.parametrize("patch,failing", [
    ({"total_equity": -5}, "equity"), ({"total_debt": 200}, "debt_to_equity"), ({"operating_cash_flow": -3}, "operating_cash_flow")])
def test_each_hard_gate_fails_on_its_own(patch, failing):
    bad = [dict(p) for p in GOOD]
    bad[-1] = _period(2026, revenue=120, net_income=14, **{**{"total_equity": 70, "total_debt": 20, "operating_cash_flow": 15}, **patch})
    q = P.quality_gate({"periods": bad}, "Technology", None)
    assert q["status"] == "fail" and failing in [c["key"] for c in q["checks"] if c["result"] == "fail"]


def test_two_of_three_profitable_years_is_the_bar_and_unknown_is_not_a_pass():
    loss_two = [_period(2024, net_income=-5, total_equity=50, total_debt=1, operating_cash_flow=1), _period(2025, net_income=-2, total_equity=50, total_debt=1, operating_cash_flow=1),
                _period(2026, net_income=9, total_equity=50, total_debt=1, operating_cash_flow=1)]
    assert P.quality_gate({"periods": loss_two}, "Tech", None)["status"] == "fail"
    one_loss = [dict(loss_two[0], values={**loss_two[0]["values"], "net_income": {"value": "8"}}), loss_two[1], loss_two[2]]
    assert P.quality_gate({"periods": one_loss}, "Tech", None)["status"] == "pass"          # 8, -2, 9: two of three profitable
    short = [_period(2026, net_income=9, total_equity=50, total_debt=1, operating_cash_flow=1)]
    q = P.quality_gate({"periods": short}, "Tech", None)
    assert q["status"] == "unknown" and [c["result"] for c in q["checks"] if c["key"] == "profitable_years"] == ["unknown"]


def test_financials_skip_debt_and_cash_flow_ratios():
    bank = [_period(y, net_income=10, total_equity=50, total_debt=900, operating_cash_flow=-40) for y in (2024, 2025, 2026)]       # leverage and negative OCF are normal for a bank
    q = P.quality_gate({"periods": bank}, "Financial Services", "Banks")
    assert q["financial"] is True and q["status"] == "pass" and {c["key"] for c in q["checks"]} == {"fundamentals_present", "equity", "profitable_years"}
    assert P.quality_gate({"periods": bank}, "Technology", None)["status"] == "fail"       # the same numbers fail for a non-financial


# ───────────── decision order ─────────────
TECH_OK = {"status": "available"}
LIQ_OK, LIQ_BAD = {"pass": True, "status": "pass", "reason": "ok"}, {"pass": False, "status": "fail", "reason": "below the floor"}
TIMING_OK = {"status": "pass", "reason": "bullish and a ladder"}


def test_decision_order_gate_quality_timing_news():
    d = P.decide
    assert d("swing", {"status": "stale"}, LIQ_OK, None, TIMING_OK, False)["recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert d("swing", TECH_OK, LIQ_BAD, None, TIMING_OK, False) == {"recommendation": "AVOID", "stage_failed": "gate", "reasons": ["below the floor"]}
    assert d("swing", TECH_OK, None, None, TIMING_OK, False)["stage_failed"] == "gate"                      # unknown liquidity blocks
    fail_q = {"status": "fail", "checks": [{"result": "fail", "reason": "debt too high"}]}
    assert d("long_term", TECH_OK, LIQ_OK, fail_q, TIMING_OK, False)["recommendation"] == "AVOID"
    assert d("swing", TECH_OK, LIQ_OK, fail_q, TIMING_OK, False)["recommendation"] == "BUY"                 # quality never applies to swing
    assert d("positional", TECH_OK, LIQ_OK, fail_q, TIMING_OK, False)["recommendation"] == "BUY"            # nor positional
    unk = {"status": "unknown", "checks": [{"result": "unknown", "reason": "needs data"}]}
    assert d("long_term", TECH_OK, LIQ_OK, unk, TIMING_OK, False)["recommendation"] == "INSUFFICIENT_EVIDENCE"
    bearish = {"status": "fail", "bearish": True, "reason": "bearish veto"}
    assert d("swing", TECH_OK, LIQ_OK, None, bearish, False)["recommendation"] == "AVOID"
    notyet = {"status": "fail", "bearish": False, "reason": "no ladder"}
    assert d("swing", TECH_OK, LIQ_OK, None, notyet, False)["recommendation"] == "WAIT"
    assert d("swing", TECH_OK, LIQ_OK, None, TIMING_OK, True)["stage_failed"] == "news"


def test_timing_requires_a_bullish_read_and_a_ladder_and_keeps_the_bearish_veto():
    ok_card = {"status": "available"}
    assert P.timing_stage({"status": "available", "direction": "BEARISH", "market_regime": "DOWNTREND"}, ok_card)["bearish"] is True
    assert P.timing_stage({"status": "available", "direction": "NEUTRAL", "market_regime": "SIDEWAYS"}, ok_card)["status"] == "fail"
    assert P.timing_stage({"status": "available", "direction": "BULLISH", "market_regime": "UPTREND"}, {"status": "no_valid_setup", "reason": "stop too wide"})["reason"] == "stop too wide"
    assert P.timing_stage({"status": "available", "direction": "STRONG_BULLISH", "market_regime": "STRONG_UPTREND"}, ok_card)["status"] == "pass"


# ───────────── assessment: technical score fed from the engine; sentiment as a gate only ─────────────
def _components(news_conflict=False):
    c = lambda score, conf=80.0, **k: {"status": "scored", "score": score, "confidence": conf, "conflicts": [], "risk_flags": [], **k}  # noqa: E731
    return {"fundamental": c(60.0), "technical": c(50.0), "financial_trends": c(60.0), "news_sentiment": c(90.0, conflicts=["x"] if news_conflict else []), "valuation": {"status": "blocked", "score": None, "confidence": None, "conflicts": [], "risk_flags": [], "blockers": ["no peers"]}}


def test_technical_override_replaces_the_score_per_horizon_and_defaults_change_nothing():
    base = rs.assess_horizons(_components())
    assert rs.assess_horizons(_components(), None, False) == base
    over = rs.assess_horizons(_components(), {"swing": 90.0}, False)
    assert over["swing"]["score"] > base["swing"]["score"] and over["positional"]["score"] == base["positional"]["score"]


def test_sentiment_has_no_weight_but_still_gates_contradictory_news():
    ctx = rs.assess_horizons(_components(), None, True)
    for h in rs.HORIZONS:
        w = ctx[h]["weights"]
        assert w["news_sentiment"] == 0.0 and sum(w.values()) == pytest.approx(1.0)
    assert ctx["swing"]["score"] < rs.assess_horizons(_components())["swing"]["score"]          # the 90-score news no longer lifts it
    conflicted = rs.assess_horizons(_components(news_conflict=True), None, True)
    assert any(g["code"] == "contradictory_evidence" for g in conflicted["long_term"]["gates"])    # gated even at zero weight (v1 gates only above a 20% weight)
    assert not any(g["code"] == "contradictory_evidence" for g in rs.assess_horizons(_components(news_conflict=True))["long_term"]["gates"])


# ───────────── end to end on real candles ─────────────
@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _frame(symbol):
    raw = json.loads((FIX / f"{symbol}.json").read_text())
    return te.frame_from_bars([(date.fromisoformat(b[0]), *b[1:]) for b in raw["bars"]])


def _instrument(db, sym="APARINDS"):
    i = Instrument(id=uuid4(), symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


FACTOR = {"status": "available", "as_of": "2026-11-20", "universe_size": 300, "inputs": {"rs_20": 0.02, "rs_60": 0.04},
          "horizons": {h: {"status": "available", "rank": 20, "of": 300, "composite": 80.0, "percentile": 93.0, "groups": []} for h in te.HORIZONS}}


def _sections(periods):
    return {"technicals": {"status": "available"}, "fundamentals": {"periods": periods, "snapshot": {"values": {"sector": {"value": "Industrials"}, "industry": {"value": "Cables"}}}}, "assessment": {"horizons": {}, "components": {}}}


def test_a_real_stock_goes_through_every_stage_and_is_ranked(db):
    i = _instrument(db)
    ev = P.evaluate(db, uuid4(), i, _sections(GOOD), _frame("APARINDS"), NOW, factor=FACTOR)
    lt = ev["horizons"]["long_term"]
    assert lt["v2_recommendation"] == "BUY" and lt["stage_failed"] is None and lt["levels"]["ready_to_buy"] is True and lt["levels"]["t1_rr"] >= 1.5
    assert lt["rank"]["grade"] in ("A", "B", "C") and lt["rank"]["factor_rank"]["rank"] == 20 and lt["quality"]["status"] == "pass"
    assert ev["horizons"]["swing"]["v2_recommendation"] in ("AVOID", "WAIT") and ev["horizons"]["swing"]["stage_failed"] == "timing"       # bearish swing read: the veto, unchanged
    assert ev["horizons"]["swing"]["timing"] == "fail" and ev["horizons"]["swing"]["levels"] is None


def test_fundamentals_decide_long_term_only_never_swing_or_positional(db):
    i = _instrument(db)
    a = P.evaluate(db, uuid4(), i, _sections(GOOD), _frame("APARINDS"), NOW, factor=FACTOR)
    b = P.evaluate(db, uuid4(), i, _sections([]), _frame("APARINDS"), NOW, factor=FACTOR)                       # no statements at all
    assert b["horizons"]["long_term"]["v2_recommendation"] == "INSUFFICIENT_EVIDENCE" and b["horizons"]["long_term"]["stage_failed"] == "quality"
    for h in ("swing", "positional"):
        assert {k: a["horizons"][h][k] for k in ("v2_recommendation", "stage_failed", "levels", "timing")} == {k: b["horizons"][h][k] for k in ("v2_recommendation", "stage_failed", "levels", "timing")}


def test_a_liquidity_failure_stops_the_stock_before_timing(db):
    i = _instrument(db)
    thin = _frame("APARINDS")
    thin["volume"] = 10.0
    ev = P.evaluate(db, uuid4(), i, _sections(GOOD), thin, NOW, factor=FACTOR)
    assert all(d["v2_recommendation"] == "AVOID" and d["stage_failed"] == "gate" for d in ev["horizons"].values())


# ───────────── apply and shadow ─────────────
def test_apply_replaces_the_recommendation_and_keeps_v1_beside_it(db):
    i = _instrument(db)
    sections = _sections(GOOD)
    sections["assessment"] = {"horizons": {h: {"recommendation": "HOLD", "blockers": ["x"]} for h in te.HORIZONS}, "components": {}}
    ev = P.evaluate(db, uuid4(), i, sections, _frame("APARINDS"), NOW, factor=FACTOR, v1_recs={h: "HOLD" for h in te.HORIZONS})
    P.apply_to_assessment(sections, ev)
    row = sections["assessment"]["horizons"]["long_term"]
    assert row["recommendation"] == "BUY" and row["v1_recommendation"] == "HOLD" and row["pipeline_v2"]["stage_failed"] is None and sections["pipeline_v2"]["version"] == P.VERSION


def test_shadow_record_is_written_with_v1_and_v2_side_by_side(db):
    i = _instrument(db)
    brief = type("B", (), {"id": uuid4(), "sections": {**_sections(GOOD), "assessment": {"horizons": {h: {"recommendation": "WAIT"} for h in te.HORIZONS}, "components": {}}}})()
    import app.services.factor_rank as fr
    fr.rank_for = lambda db_, iid: FACTOR                                    # the real loader uses Postgres-only SQL; this test is about the record
    try:
        assert P.record_shadow(db, uuid4(), i, brief, _frame("APARINDS"), None, NOW) is True
    finally:
        import importlib
        importlib.reload(fr)
    row = db.scalar(select(PipelineV2Shadow))
    assert row.v2_buy_horizons == 1 and row.as_of_candle == date(2026, 11, 20) and row.payload["horizons"]["long_term"]["v1_recommendation"] == "WAIT"
    assert row.payload["horizons"]["long_term"]["v2_recommendation"] == "BUY" and "blue_sky" in row.payload["horizons"]["long_term"]


# ───────────── the real brief, flag on and off ─────────────
def _store_bars(db, instrument, upto="2026-11-17"):
    from datetime import timedelta
    from decimal import Decimal

    from app.models import MarketBar
    for ts, r in _frame("APARINDS").loc[:upto].iterrows():
        t = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)                       # stored daily bars sit at 00:00 UTC of the session date
        db.add(MarketBar(id=uuid4(), instrument_id=instrument.id, interval="1d", timestamp=t, open=Decimal(str(r.open)), high=Decimal(str(r.high)), low=Decimal(str(r.low)), close=Decimal(str(r.close)),
                         volume=Decimal(str(r.volume)), source="dhan", retrieved_at=t + timedelta(hours=1), is_synthetic=False))
    db.commit()


def test_render_brief_with_the_v2_pipeline_replaces_the_decision_and_with_it_off_changes_nothing(db, trading_session_clock, monkeypatch):
    from app.config import settings
    from app.services import research_pipeline as rp
    i = _instrument(db)
    _store_bars(db, i)
    off, _ = rp.render_brief(db, i, [], [], user_id=uuid4())
    assert "pipeline_v2" not in off and all("v1_recommendation" not in r for r in off["assessment"]["horizons"].values()) and "error" not in off["levels"]
    monkeypatch.setattr(settings, "pipeline_v2_enabled", True)
    on, _ = rp.render_brief(db, i, [], [], user_id=uuid4())
    assert "error" not in on["levels"], on["levels"]
    assert on["pipeline_v2"]["version"] == P.VERSION and set(on["pipeline_v2"]["horizons"]) == set(te.HORIZONS)
    for h, row in on["assessment"]["horizons"].items():
        assert row["recommendation"] == on["pipeline_v2"]["horizons"][h]["v2_recommendation"] and "v1_recommendation" in row
    assert on["assessment"]["weights"]["swing"]["news_sentiment"] == 0.0                          # sentiment carries no weight under v2
    assert on["technical_engine"]["horizons"]["long_term"]["v2"]["version"] == "levels-v2/1"      # the engine ran with the v2 level rules because the pipeline needs them
