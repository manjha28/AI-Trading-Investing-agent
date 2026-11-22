"""Nexus v2 Phase 4: Gate -> Quality (long-term only) -> Timing, then Rank (flag ``pipeline_v2_enabled``; with the flag off the same decision is computed in SHADOW and stored beside v1).

1. GATE (every horizon): technical data current, liquidity floor (unknown blocks), plus the corporate-event gate applied to the levels (services/gates_v2.py).
2. QUALITY (long-term ONLY, owner decision: swing and positional may be any company): hard gates, not averages. Fundamentals present (otherwise long-term is not assessable), positive equity,
   debt/equity under 2 (non-financials), positive operating cash flow (non-financials), at least 2 of the last 3 annual years profitable. Banks/NBFCs/insurers use only the ratios that apply to them.
3. TIMING (every horizon): the technical engine decides. A bearish engine read (or DOWNTREND regime) is never BUY (the bearish veto, unchanged); a neutral read is not BUY; the Phase 1 level rules
   (structural stop bounds, T1 >= 1.5R, nearest barrier, headroom) must produce a ladder. The engine's own per-horizon technical score replaces the generic technical component in the score, so the score
   and the levels agree. Sentiment is a context flag and a contradictory-news gate only (weight 0).
4. RANK: among cards that pass, the Phase 2 setup grade then the factor rank order them (the B06 overlay may later break ties; it never turns a non-BUY into BUY).
Decision vocabulary stays the existing one: BUY, WAIT (timing/news not ready), AVOID (bearish, quality failed, or not tradable), INSUFFICIENT_EVIDENCE (data missing/unknown). Decision support, not investment advice.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.services import gates_v2, levels_v2, research_confidence, research_scoring as rs, technical_engine as te
from app.services.research_levels import build_levels

logger = logging.getLogger(__name__)
VERSION = "pipeline-v2/1"
HORIZONS = te.HORIZONS


@dataclass
class QualityConfig:
    max_debt_to_equity: float = 2.0
    min_profitable_years: int = 2
    window_years: int = 3


def _chk(key: str, label: str, result: str, reason: str) -> dict[str, str]:
    return {"key": key, "label": label, "result": result, "reason": reason}


def quality_gate(fundamentals: dict[str, Any] | None, sector: str | None, industry: str | None, cfg: QualityConfig | None = None) -> dict[str, Any]:
    """Hard long-term quality gates. status: pass | fail | not_assessable (no statements) | unknown (a needed value is missing)."""
    cfg = cfg or QualityConfig()
    annual = [p for p in rs._annual((fundamentals or {}).get("periods") or []) if any(rs._field(p["values"], f) is not None for f in ("revenue", "net_income", "total_equity"))]
    if not annual:
        return {"status": "not_assessable", "financial": rs.is_financial_sector(sector, industry), "checks": [_chk("fundamentals_present", "Annual fundamentals are stored", "fail", "No annual statement period is stored: long-term is not assessable.")]}
    financial = rs.is_financial_sector(sector, industry)
    v = annual[-1]["values"]
    equity, debt, ocf = rs._field(v, "total_equity"), rs._field(v, "total_debt"), rs._field(v, "operating_cash_flow")
    checks = [_chk("fundamentals_present", "Annual fundamentals are stored", "pass", f"Latest annual period ends {annual[-1]['period_end']} ({len(annual)} annual period(s) stored).")]
    checks.append(_chk("equity", "Shareholders' equity is positive", "unknown" if equity is None else "pass" if equity > 0 else "fail", f"Equity {equity}."))
    if not financial:
        if debt is None or equity is None or equity <= 0:
            checks.append(_chk("debt_to_equity", f"Debt/equity is under {cfg.max_debt_to_equity:g}", "unknown", "Total debt or positive equity is not stored."))
        else:
            de = float(debt / equity)
            checks.append(_chk("debt_to_equity", f"Debt/equity is under {cfg.max_debt_to_equity:g}", "pass" if de < cfg.max_debt_to_equity else "fail", f"Debt/equity is {de:.2f}x."))
        checks.append(_chk("operating_cash_flow", "Operating cash flow is positive", "unknown" if ocf is None else "pass" if ocf > 0 else "fail", f"Operating cash flow {ocf}."))
    recent = annual[-cfg.window_years:]
    incomes = [rs._field(p["values"], "net_income") for p in recent]
    known = [i for i in incomes if i is not None]
    positives = sum(1 for i in known if i > 0)
    if positives >= cfg.min_profitable_years:
        res, why = "pass", f"{positives} of the last {len(recent)} annual period(s) were profitable."
    elif positives + (cfg.window_years - len(known)) >= cfg.min_profitable_years:
        res, why = "unknown", f"Only {len(known)} annual net-income figure(s) are stored; {cfg.min_profitable_years} of {cfg.window_years} profitable years cannot be confirmed yet."
    else:
        res, why = "fail", f"Only {positives} of the last {len(known)} annual period(s) were profitable (need {cfg.min_profitable_years} of {cfg.window_years})."
    checks.append(_chk("profitable_years", f"At least {cfg.min_profitable_years} of the last {cfg.window_years} years were profitable", res, why))
    status = "fail" if any(c["result"] == "fail" for c in checks) else "unknown" if any(c["result"] == "unknown" for c in checks) else "pass"
    return {"status": status, "financial": financial, "checks": checks}


def engine_quality(db: Any, user_id: Any, instrument_id: Any, phase4: dict[str, Any]) -> dict[str, Any]:
    """Phase 6 (flag ``multibagger_screen_enabled``): the long-term Quality stage is the multibagger engine's stored nightly class. Only multibagger_candidate and quality_compounder can be a long-term BUY;
    watch -> WAIT, avoid -> AVOID, not assessable / no result -> INSUFFICIENT_EVIDENCE. Phase 4's own gates still apply on top (a failed Phase 4 gate is never overridden), because the latest-year operating
    cash flow check is stricter than the engine's 3-year cash conversion veto."""
    from sqlalchemy import select

    from app.models.multibagger import MultibaggerAssessment
    row = db.scalar(select(MultibaggerAssessment).where(MultibaggerAssessment.instrument_id == instrument_id, MultibaggerAssessment.user_id == user_id).order_by(MultibaggerAssessment.as_of.desc()).limit(1))
    if phase4["status"] == "fail":
        return phase4
    if row is None:
        return {"status": "unknown", "financial": phase4.get("financial", False), "checks": [_chk("multibagger_engine", "Multibagger engine result", "unknown", "No nightly multibagger result is stored for this stock yet.")], "engine_class": None}
    klass = row.klass
    reasons = [r for r in ((row.checks or {}).get("reasons") or [])][:3] or [klass]
    status = {"multibagger_candidate": "pass", "quality_compounder": "pass", "watch": "watch", "avoid": "fail", "not_assessable": "not_assessable"}[klass]
    return {"status": status, "financial": phase4.get("financial", False), "engine_class": klass, "checks": [_chk("multibagger_engine", f"Multibagger engine class: {klass}", "pass" if status == "pass" else "fail" if status in ("fail", "watch") else "unknown", "; ".join(reasons))]}


def timing_stage(engine_card: dict[str, Any] | None, levels_card: dict[str, Any] | None) -> dict[str, Any]:
    if not engine_card or engine_card.get("status") != "available":
        return {"status": "unknown", "reason": (engine_card or {}).get("reason") or "The technical engine could not read this horizon."}
    direction, regime = engine_card.get("direction"), engine_card.get("market_regime")
    veto = levels_v2.bearish_veto(direction, regime)
    if veto:
        return {"status": "fail", "bearish": True, "reason": veto}
    if direction not in ("BULLISH", "STRONG_BULLISH"):
        return {"status": "fail", "bearish": False, "reason": f"The engine reads {str(direction).lower().replace('_', ' ')}: not a buy until the trend is bullish."}
    if not levels_card or levels_card.get("status") != "available":
        return {"status": "fail", "bearish": False, "reason": (levels_card or {}).get("reason") or "No valid ladder exists under the v2 level rules."}
    return {"status": "pass", "bearish": False, "reason": "Bullish engine read and a valid ladder (structural stop within bounds, T1 at least 1.5R, no barrier in the way)."}


def decide(horizon: str, technicals: dict[str, Any], gate_liquidity: dict[str, Any] | None, quality: dict[str, Any] | None, timing: dict[str, Any], contradictory_news: bool) -> dict[str, Any]:
    """-> {recommendation, stage_failed, reasons[]}. The order is Gate, Quality (long-term only), Timing, news."""
    if technicals.get("status") != "available":
        return {"recommendation": "INSUFFICIENT_EVIDENCE", "stage_failed": "gate", "reasons": [technicals.get("message") or "Technical data is not current."]}
    if gate_liquidity is None or not gate_liquidity.get("pass"):
        return {"recommendation": "AVOID", "stage_failed": "gate", "reasons": [(gate_liquidity or {}).get("reason") or "Liquidity could not be confirmed."]}
    if horizon == "long_term" and quality is not None:
        if quality["status"] == "fail":
            return {"recommendation": "AVOID", "stage_failed": "quality", "reasons": [c["reason"] for c in quality["checks"] if c["result"] == "fail"]}
        if quality["status"] == "watch":
            return {"recommendation": "WAIT", "stage_failed": "quality", "reasons": [c["reason"] for c in quality["checks"] if c["result"] != "pass"]}
        if quality["status"] in ("not_assessable", "unknown"):
            return {"recommendation": "INSUFFICIENT_EVIDENCE", "stage_failed": "quality", "reasons": [c["reason"] for c in quality["checks"] if c["result"] in ("fail", "unknown")]}
    if timing["status"] != "pass":
        return {"recommendation": "AVOID" if timing.get("bearish") else "WAIT" if timing["status"] == "fail" else "INSUFFICIENT_EVIDENCE", "stage_failed": "timing", "reasons": [timing["reason"]]}
    if contradictory_news:
        return {"recommendation": "WAIT", "stage_failed": "news", "reasons": ["Company news evidence is materially contradictory (sentiment is a gate and a context flag only)."]}
    return {"recommendation": "BUY", "stage_failed": None, "reasons": [timing["reason"]]}


def _summary_levels(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if not card or card.get("status") != "available":
        return None
    t = (card.get("targets") or [{}])[0]
    return {"entry_status": card.get("entry_status"), "entry_zone": card.get("entry_zone"), "planned_entry": card.get("planned_entry"), "stop": card.get("stop"), "stop_percent": card.get("stop_percent"),
            "t1": t.get("price"), "t1_rr": t.get("risk_reward"), "ready_to_buy": card.get("ready_to_buy"), "entry_label": card.get("entry_label")}


def evaluate(db: Any, user_id: Any, instrument: Any, sections: dict[str, Any], daily: Any, now: datetime | None = None, *, engine: dict[str, Any] | None = None,
             levels: dict[str, Any] | None = None, gates: dict[str, Any] | None = None, factor: dict[str, Any] | None = None, bench: Any = None,
             v1_recs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run Gate -> Quality -> Timing -> Rank for every horizon of one stock. Pass ``engine``/``levels``/``gates``/``factor`` to reuse what the brief already computed (flag on); otherwise they are
    computed here with explicit v2 settings (shadow mode), independent of the flags."""
    now = now or datetime.now(timezone.utc)
    technicals = sections.get("technicals") or {}
    v1 = ((sections.get("assessment") or {}).get("horizons") or {})
    frame = None
    if daily is not None and len(daily):
        frame = gates_v2.drop_forming_bar(daily, now)
    if engine is None:
        engine = te.analyze(frame, bench, cfg=te.EngineConfig(levels_v2=True, guardrails=False), as_of=pd.Timestamp(now.astimezone(gates_v2.IST).date())) if (technicals.get("status") == "available" and frame is not None) else {
            "status": "unavailable", "horizons": {}}
    if levels is None:
        all_buy = {"horizons": {h: {"recommendation": "BUY"} for h in HORIZONS}}
        levels = build_levels(technicals, all_buy, engine) if technicals.get("status") == "available" else {"horizons": {h: {"status": "stale_or_missing_data", "reason": technicals.get("message") or "Technical data is not current."} for h in HORIZONS}}
    if gates is None and frame is not None:
        gates = gates_v2.compute(db, instrument.id, user_id, frame, now)
        gates_v2.apply_to_levels(levels, gates)
    if factor is None:
        from app.services import factor_rank
        factor = factor_rank.rank_for(db, instrument.id)
    fundamentals = sections.get("fundamentals") or {}
    snap = (fundamentals.get("snapshot") or {}).get("values") or {}
    sector, industry = (snap.get("sector") or {}).get("value"), (snap.get("industry") or {}).get("value")
    quality = quality_gate(fundamentals, sector, industry)
    from app.config import settings
    if settings.multibagger_screen_enabled:
        quality = engine_quality(db, user_id, instrument.id, quality)
    news = ((sections.get("assessment") or {}).get("components") or {}).get("news_sentiment") or {}
    contradictory = bool(news.get("status") == "scored" and news.get("conflicts"))
    out_h: dict[str, Any] = {}
    for h in HORIZONS:
        card = (levels.get("horizons") or {}).get(h) or {}
        liq = ((gates or {}).get("liquidity") or {}).get(h)
        timing = timing_stage((engine.get("horizons") or {}).get(h), card)
        dec = decide(h, technicals, liq, quality if h == "long_term" else None, timing, contradictory)
        eng_card = (engine.get("horizons") or {}).get(h) or {}
        v2info = (eng_card.get("v2") or {})
        entry = {"v1_recommendation": (v1_recs or {}).get(h, (v1.get(h) or {}).get("recommendation")), "v2_recommendation": dec["recommendation"], "stage_failed": dec["stage_failed"], "reasons": dec["reasons"][:4],
                 "timing": timing["status"], "liquidity": None if liq is None else {"status": liq["status"], "median_traded_value_crore": liq.get("median_traded_value_crore"), "floor_crore": liq.get("floor_crore")},
                 "levels": _summary_levels(card), "blue_sky": {"near_high": bool(v2info.get("near_high_blue_sky")), "relaxation_dependent": bool(v2info.get("relaxation_dependent"))}}
        if h == "long_term":
            entry["quality"] = {"status": quality["status"], "failed": [c["key"] for c in quality["checks"] if c["result"] == "fail"], "unknown": [c["key"] for c in quality["checks"] if c["result"] == "unknown"]}
        if dec["recommendation"] == "BUY":
            conf = research_confidence.buy_confidence_v2(h, {"recommendation": "BUY"}, card, factor)
            entry["rank"] = {"label": conf["label"], "grade": conf["grade"], "sort_score": conf["score"], "factor_rank": conf["factor_rank"], "setup_failed_vetoes": conf["setup"]["failed_vetoes"]}
        out_h[h] = entry
    return {"version": VERSION, "as_of": now.isoformat(), "last_candle": (engine.get("last_daily_candle") if isinstance(engine, dict) else None), "quality": quality, "horizons": out_h,
            "engine": engine, "levels": levels, "gates": gates, "factor": factor}


def apply_to_assessment(sections: dict[str, Any], evaluation: dict[str, Any]) -> None:
    """Flag on: the v2 decision replaces the horizon recommendation (v1's kept beside it as ``v1_recommendation``), and the v2 levels/engine/gates/factor replace the v1 ones in the brief."""
    assessment = sections.get("assessment") or {}
    for h, dec in evaluation["horizons"].items():
        row = (assessment.get("horizons") or {}).get(h)
        if row is None:
            continue
        row["v1_recommendation"], row["recommendation"] = row.get("recommendation"), dec["v2_recommendation"]
        row["pipeline_v2"] = {k: dec[k] for k in ("stage_failed", "reasons", "timing", "blue_sky") if k in dec}
        if dec["v2_recommendation"] == "BUY":
            row["blockers"] = []
    sections["pipeline_v2"] = {"version": VERSION, "as_of": evaluation["as_of"], "quality": evaluation["quality"],
                               "horizons": {h: {k: v for k, v in d.items()} for h, d in evaluation["horizons"].items()},
                               "text": "Recommendation = Gate (data, liquidity) -> Quality (long-term only) -> Timing (technical engine and level rules). Sentiment is a context flag and a contradictory-news gate only; it carries no score weight."}


def shadow_payload(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {"version": evaluation["version"], "as_of": evaluation["as_of"], "quality": {k: evaluation["quality"][k] for k in ("status", "financial")}, "horizons": evaluation["horizons"],
            "event_feed": ((evaluation.get("gates") or {}).get("event_feed")), "universe_size": (evaluation.get("factor") or {}).get("universe_size")}


def record_shadow(db: Any, user_id: Any, instrument: Any, brief: Any, daily: Any, bench: Any = None, now: datetime | None = None) -> bool:
    """Store the v2 decision beside v1 for this research run (additive table). With the flag on the brief already carries the evaluation; with it off it is computed here. Never raises."""
    from app.models.pipeline_v2 import PipelineV2Shadow
    now = now or datetime.now(timezone.utc)
    try:
        sections = brief.sections or {}
        if sections.get("pipeline_v2"):
            payload = {"version": VERSION, "as_of": sections["pipeline_v2"]["as_of"], "quality": {k: sections["pipeline_v2"]["quality"][k] for k in ("status", "financial")}, "horizons": sections["pipeline_v2"]["horizons"],
                       "event_feed": (sections.get("gates_v2") or {}).get("event_feed"), "universe_size": (sections.get("factor_rank") or {}).get("universe_size")}
        else:
            instr_obj = instrument
            ev = evaluate(db, user_id, instr_obj, sections, daily, now, bench=bench)
            payload = shadow_payload(ev)
        horizons = payload["horizons"]
        db.add(PipelineV2Shadow(instrument_id=instrument.id, user_id=user_id, brief_id=brief.id, computed_at=now, as_of_candle=(daily.index[-1].date() if daily is not None and len(daily) else None),
                                v2_buy_horizons=sum(1 for d in horizons.values() if d.get("v2_recommendation") == "BUY"),
                                relaxation_dependent_ladders=sum(1 for d in horizons.values() if (d.get("blue_sky") or {}).get("relaxation_dependent") and d.get("v2_recommendation") == "BUY"), payload=payload))
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - shadow recording must never break a research run
        db.rollback()
        logger.warning("pipeline_v2_shadow_failed error_class=%s", type(exc).__name__)
        return False


def compare(db: Any, user_id: Any, *, limit: int = 300) -> dict[str, Any]:
    """v1 vs v2 for this owner's stocks from the latest shadow record of each (owner-scoped). Hidden from the UI; used to review before any flag is switched on."""
    from sqlalchemy import select

    from app.models import Instrument
    from app.models.pipeline_v2 import PipelineV2Shadow
    rows = db.execute(select(PipelineV2Shadow, Instrument.symbol).join(Instrument, Instrument.id == PipelineV2Shadow.instrument_id).where(PipelineV2Shadow.user_id == user_id).order_by(PipelineV2Shadow.computed_at.desc())).all()
    latest: dict[Any, tuple[Any, str]] = {}
    for rec, sym in rows:
        latest.setdefault(rec.instrument_id, (rec, sym))
    matrix: dict[str, dict[str, int]] = {h: {} for h in HORIZONS}
    changed, v2_buys, blue = [], [], []
    for rec, sym in latest.values():
        for h, d in (rec.payload.get("horizons") or {}).items():
            key = f"{d.get('v1_recommendation')} -> {d.get('v2_recommendation')}"
            matrix[h][key] = matrix[h].get(key, 0) + 1
            if d.get("v1_recommendation") != d.get("v2_recommendation"):
                changed.append({"symbol": sym, "horizon": h, "v1": d.get("v1_recommendation"), "v2": d.get("v2_recommendation"), "stage_failed": d.get("stage_failed"), "reason": (d.get("reasons") or [None])[0]})
            if d.get("v2_recommendation") == "BUY":
                row = {"symbol": sym, "horizon": h, "grade": (d.get("rank") or {}).get("grade"), "label": (d.get("rank") or {}).get("label"), "sort_score": (d.get("rank") or {}).get("sort_score"),
                       "levels": d.get("levels"), "blue_sky": d.get("blue_sky")}
                v2_buys.append(row)
                if (d.get("blue_sky") or {}).get("relaxation_dependent"):
                    blue.append(row)
    v2_buys.sort(key=lambda r: -(r["sort_score"] or 0))
    stamps = sorted(r.computed_at for r, _ in rows)
    return {"version": VERSION, "stocks": len(latest), "shadow_rows": len(rows), "first_shadow_at": stamps[0].isoformat() if stamps else None, "last_shadow_at": stamps[-1].isoformat() if stamps else None,
            "transitions": matrix, "changed": changed[:limit], "changed_total": len(changed), "v2_buy_cards": v2_buys[:limit], "v2_buy_total": len(v2_buys), "relaxation_dependent_ladders": blue,
            "note": "Decision support, not investment advice. v1 is what the daily report shows; v2 is computed beside it and shown only when pipeline_v2_enabled is switched on."}
