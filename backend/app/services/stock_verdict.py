"""Nexus v2 Phase 7, Steps 2-3: horizon roles, the verdict per horizon, and the evidence score. Composes the earlier phases and rebuilds none of them: the v2 levels, gates and setup checks come from
``pipeline_v2.evaluate`` (Phases 1-4), the fundamentals/growth/valuation lenses from the Phase 6 engine result, the chart lens from ``stock_lenses``. Swing and positional are NEVER filtered by fundamentals
(property-tested). Roles live in the ROLES table, not in code. Decision support, not investment advice; a verdict is never an order.

Verdict, first match wins: avoid (a veto fails) -> not_now (the chart requirement fails, or the long-term class is watch / not a fit) -> watch (setup grade C or a required input unknown) ->
buy_on_trigger (all requirements pass, grade A/B, waiting for the band or breakout close) -> buy_now (all pass, grade A/B, in the band or the entry fired).
Evidence E_h = 0.30 S + 0.25 A + 0.20 D + 0.15 L + 0.10 F - P_regime, capped at 60 when a veto input is unknown. Until calibration passes it is shown as "Evidence score N (not yet validated)"."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "stock-verdict-0.1.0-provisional"
HORIZONS = ("swing", "positional", "long_term")
VERDICTS = ("buy_now", "buy_on_trigger", "watch", "not_now", "avoid")

# lens -> horizon -> role (veto | requirement | score | context | veto_requirement | score_block). A table, not code.
ROLES: dict[str, dict[str, str]] = {
    "liquidity_stale": {"swing": "veto", "positional": "veto", "long_term": "veto"},
    "chart": {"swing": "requirement", "positional": "requirement", "long_term": "context"},
    "financial_trends": {"swing": "context", "positional": "score", "long_term": "score"},
    "fundamentals": {"swing": "context", "positional": "context", "long_term": "veto_requirement"},
    "growth": {"swing": "context", "positional": "context", "long_term": "score"},
    "valuation": {"swing": "context", "positional": "context", "long_term": "score_block"},
    "news_events": {"swing": "veto", "positional": "veto", "long_term": "veto"},
    "market_regime": {"swing": "score", "positional": "score", "long_term": "score"},
}
# weight of each lens in D (data coverage of the horizon's lens weight)
LENS_WEIGHTS: dict[str, dict[str, float]] = {
    "swing": {"chart": 0.55, "market_regime": 0.20, "liquidity_stale": 0.15, "news_events": 0.10},
    "positional": {"chart": 0.35, "financial_trends": 0.20, "market_regime": 0.15, "liquidity_stale": 0.15, "news_events": 0.15},
    "long_term": {"fundamentals": 0.25, "growth": 0.15, "valuation": 0.15, "financial_trends": 0.15, "market_regime": 0.10, "liquidity_stale": 0.10, "news_events": 0.10},
}
SIZE = {"multibagger_candidate": 1.0, "quality_compounder": 1.0, "emerging": 0.5, "new_listing": 0.25}


@dataclass
class VerdictConfig:
    e_weights: tuple[float, float, float, float, float] = (0.30, 0.25, 0.20, 0.15, 0.10)        # S, A, D, L, F
    regime_penalty: dict[str, float] = field(default_factory=lambda: {"uptrend": 0.0, "range": 10.0, "downtrend": 20.0, "unknown": 0.0})
    unknown_veto_cap: float = 60.0
    min_valuation: float = 30.0                                                                  # V below this blocks a long-term buy
    news_event_sessions: int = 3                                                                 # swing: results / ex / record date within 3 sessions
    news_material_days: dict[str, int] = field(default_factory=lambda: {"swing": 5, "positional": 10})
    governance_months: int = 24
    regime_caps_enabled: bool = False                                                           # OFF by default: the Phase 4c holdout showed the caps did not remove the 2022 losses
    regime_caps: dict[str, int] = field(default_factory=lambda: {"uptrend": 10, "range": 5, "downtrend": 2})
    bands: tuple[float, float] = (65.0, 50.0)                                                    # High >= 65%, Medium 50-65%, Low below (calibrated confidence only)
    min_bucket_setups: int = 100


def _lens(lenses: dict[str, Any], name: str) -> dict[str, Any]:
    return lenses.get(name) or {"score": None, "direction": "unknown", "facts": [], "coverage": 0.0}


# ───────────────────────── the evidence score ─────────────────────────
def evidence(horizon: str, lenses: dict[str, Any], setup: dict[str, Any] | None, factor_pct: float | None, regime: str | None, unknown_veto: list[str], cfg: VerdictConfig | None = None) -> dict[str, Any]:
    """E_h = 0.30 S + 0.25 A + 0.20 D + 0.15 L + 0.10 F - P. S is the share of Phase 2 setup checks passed (the grade is a pass/fail check, never extra evidence); A lens agreement; D data coverage of the
    horizon's lens weight; L the mean score of its score lenses; F the factor-rank percentile; P the regime penalty. Unknown pieces count as 0 here AND lower D; they are never favourable."""
    cfg = cfg or VerdictConfig()
    checks = (setup or {}).get("checks") or []
    ev = [c for c in checks if c.get("result") in ("pass", "fail")]
    s = 100.0 * sum(1 for c in ev if c["result"] == "pass") / len(ev) if ev else 0.0
    scored = [n for n, role in ((n, ROLES[n][horizon]) for n in ROLES) if role in ("requirement", "score", "score_block", "veto_requirement") and n in lenses]
    dirs = [_lens(lenses, n)["direction"] for n in scored if _lens(lenses, n)["direction"] != "unknown"]
    a = ((sum(1 for d in dirs if d == "positive") - sum(1 for d in dirs if d == "negative")) / len(dirs) + 1) / 2 * 100 if dirs else 0.0
    weights = LENS_WEIGHTS[horizon]
    known = sum(w for n, w in weights.items() if _lens(lenses, n)["coverage"] > 0 or (n == "liquidity_stale" and lenses.get("liquidity_stale") is not None))
    d = 100.0 * known / sum(weights.values())
    scores = [_lens(lenses, n)["score"] for n in scored if _lens(lenses, n)["score"] is not None]
    l_ = sum(scores) / len(scores) if scores else 0.0
    f = float(factor_pct) if factor_pct is not None else 0.0
    p = cfg.regime_penalty.get(regime or "unknown", 0.0)
    w = cfg.e_weights
    raw = w[0] * s + w[1] * a + w[2] * d + w[3] * l_ + w[4] * f - p
    e = max(0.0, min(100.0, raw))
    capped = bool(unknown_veto)
    if capped:
        e = min(e, cfg.unknown_veto_cap)
    return {"score": round(e, 1), "S": round(s, 1), "A": round(a, 1), "D": round(d, 1), "L": round(l_, 1), "F": round(f, 1), "P": p, "capped_by_unknown_veto": unknown_veto if capped else [],
            "label": f"Evidence score {e:.0f} (not yet validated)"}


# ───────────────────────── one horizon ─────────────────────────
def decide(horizon: str, *, tech_ok: bool, liquidity: dict[str, Any] | None, news: dict[str, Any], mb: dict[str, Any] | None, chart_ok: bool | None, chart_reason: str, grade: str | None,
           ready_to_buy: bool | None, valuation_v: float | None, ipo_base_ok: bool | None = None, cfg: VerdictConfig | None = None) -> dict[str, Any]:
    """The verdict for one horizon, first match wins. Returns {verdict, reasons[], veto, size, unknown_veto[]}. ``chart_ok`` is the Phase 4 timing result (bullish engine and a valid v2 ladder)."""
    cfg = cfg or VerdictConfig()
    reasons: list[str] = []
    unknown: list[str] = []
    size = 1.0
    # --- vetoes (all horizons) ---
    if not tech_ok:
        return {"verdict": "watch", "reasons": ["Technical data is stale or missing: no call until it is current."], "veto": None, "size": 0.0, "unknown_veto": ["stale_data"]}
    if liquidity is None or not liquidity.get("pass"):
        why = (liquidity or {}).get("reason") or "Liquidity could not be confirmed."
        return {"verdict": "avoid", "reasons": [why], "veto": "liquidity", "size": 0.0, "unknown_veto": [] if liquidity else ["liquidity"]}
    if horizon == "swing" and news.get("events_within_3_sessions"):
        e = news["events_within_3_sessions"][0]
        return {"verdict": "avoid", "reasons": [f"{e['kind'].replace('_', ' ')} on {e['date']} ({e.get('sessions_ahead')} session(s) ahead): no new swing entry."], "veto": "event", "size": 0.0, "unknown_veto": []}
    if horizon in ("swing", "positional") and news.get("material_headlines"):
        return {"verdict": "avoid", "reasons": [f"Material negative headline: {news['material_headlines'][0][:100]}"], "veto": "material_news", "size": 0.0, "unknown_veto": []}
    if horizon == "long_term":
        if news.get("governance_events"):
            return {"verdict": "avoid", "reasons": [f"{len(news['governance_events'])} governance event(s) in the last {cfg.governance_months} months."], "veto": "governance", "size": 0.0, "unknown_veto": []}
        if not news.get("governance_known", False):
            unknown.append("governance")
        if news.get("material_headlines"):
            return {"verdict": "avoid", "reasons": [f"Material negative headline: {news['material_headlines'][0][:100]}"], "veto": "material_news", "size": 0.0, "unknown_veto": []}
        klass = (mb or {}).get("class")
        if klass == "avoid":
            return {"verdict": "avoid", "reasons": [str(r) for r in (mb or {}).get("reasons", [])[:2]] or ["The fundamentals engine flags a red flag."], "veto": "fundamentals", "size": 0.0, "unknown_veto": unknown}
        if klass is None or klass == "not_assessable":
            return {"verdict": "watch", "reasons": ["Fundamentals cannot be assessed yet (data missing): a required input is unknown."], "veto": None, "size": 0.0, "unknown_veto": unknown + ["fundamentals"]}
    if horizon == "long_term":
        klass = (mb or {}).get("class")
        if klass in ("watch", "not_a_fit", "not_tradable"):
            return {"verdict": "not_now", "reasons": [f"Long-term class is {klass.replace('_', ' ')}: {((mb or {}).get('reasons') or [''])[0]}"], "veto": None, "size": 0.0, "unknown_veto": unknown}
        if klass not in SIZE:
            return {"verdict": "not_now", "reasons": [f"Long-term class {klass} is not a buy class."], "veto": None, "size": 0.0, "unknown_veto": unknown}
        size = SIZE[klass]
        if klass == "new_listing" and not ipo_base_ok:
            return {"verdict": "not_now", "reasons": ["A new listing is only bought on an IPO-base breakout (quarter size)."], "veto": None, "size": 0.0, "unknown_veto": unknown}
        if valuation_v is not None and valuation_v < cfg.min_valuation:
            return {"verdict": "not_now", "reasons": [f"Valuation score {valuation_v:.0f} is under {cfg.min_valuation:g}: valuation blocks a buy."], "veto": None, "size": 0.0, "unknown_veto": unknown}
    # --- chart requirement (swing and positional only; long-term chart is a timing tag) ---
    if horizon in ("swing", "positional") and chart_ok is False:
        return {"verdict": "not_now", "reasons": [chart_reason], "veto": None, "size": 0.0, "unknown_veto": unknown}
    if grade is None or grade == "C" or (horizon in ("swing", "positional") and chart_ok is None):
        return {"verdict": "watch", "reasons": [chart_reason if chart_ok is None else f"Setup grade {grade or 'unknown'}: not all setup checks pass yet."], "veto": None, "size": 0.0, "unknown_veto": unknown}
    if horizon == "long_term" and chart_ok is False:
        reasons.append("The chart is not in a buy position yet (timing only): wait for a valid entry.")
        return {"verdict": "buy_on_trigger", "reasons": reasons, "veto": None, "size": size, "unknown_veto": unknown}
    return {"verdict": "buy_now" if ready_to_buy else "buy_on_trigger", "reasons": [chart_reason], "veto": None, "size": size, "unknown_veto": unknown}


# ───────────────────────── calibration map ─────────────────────────
CALIBRATION_PATH = Path(__file__).with_name("stock_verdict_calibration.json")


def load_calibration(path: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads((path or CALIBRATION_PATH).read_text())
    except (OSError, ValueError):
        return {}


def confidence_line(horizon: str, e: float, table: dict[str, Any] | None = None, cfg: VerdictConfig | None = None) -> dict[str, Any]:
    """Calibrated confidence for an evidence score, only from a validated map and only for buckets with at least 100 past setups AND a net-R 95% CI that excludes zero. Otherwise the evidence score is shown
    as "not yet validated" with no High/Medium/Low."""
    cfg = cfg or VerdictConfig()
    table = table if table is not None else load_calibration()
    hz = (table.get("horizons") or {}).get(horizon) or {}
    if not hz.get("validated"):
        return {"calibrated": None, "band": None, "line": None, "validated": False}
    for b in hz.get("buckets") or []:
        if b["e_lo"] <= e < b["e_hi"]:
            ci = b.get("net_r_ci95") or [None, None]
            if b["n"] >= cfg.min_bucket_setups and ci[0] is not None and ci[0] > 0:
                p = float(b["hit_rate"])
                band = "High" if p >= cfg.bands[0] / 100 else "Medium" if p >= cfg.bands[1] / 100 else "Low"
                return {"calibrated": round(p * 100), "band": band, "validated": True, "line": f"Confidence {p:.0%}: T1 before stop in {p:.0%} of {b['n']} similar past setups"}
            return {"calibrated": None, "band": None, "validated": True, "line": None}
    return {"calibrated": None, "band": None, "validated": True, "line": None}


def apply_regime_caps(day_verdicts: list[dict[str, Any]], regime: str | None, cfg: VerdictConfig | None = None) -> list[dict[str, Any]]:
    """Optional (default OFF): at most N buy verdicts per horizon on a day by regime (uptrend 10, range 5, downtrend 2 and grade A only), best evidence first; the rest become 'watch' with the reason.
    Returned as new dicts; the input is not modified."""
    cfg = cfg or VerdictConfig()
    out = [dict(v) for v in day_verdicts]
    if not cfg.regime_caps_enabled or regime not in cfg.regime_caps:
        return out
    for h in HORIZONS:
        buys = sorted([v for v in out if v["horizon"] == h and v["verdict"] in ("buy_now", "buy_on_trigger")], key=lambda v: -v["evidence"])
        keep = {id(v) for v in buys[: cfg.regime_caps[regime]] if regime != "downtrend" or v.get("grade") == "A"}
        for v in buys:
            if id(v) not in keep:
                v["verdict"], v["reasons"] = "watch", [f"Regime cap: only {cfg.regime_caps[regime]} buys a day in a {regime}" + (", grade A only" if regime == "downtrend" else "")] + list(v["reasons"])
    return out


# ───────────────────────── composition (loads stored data, builds the lenses, decides, scores) ─────────────────────────
_RANK = {"buy_now": 0, "buy_on_trigger": 1, "watch": 2, "not_now": 3, "avoid": 4}


def _recent_headlines(sections: dict[str, Any], now: Any, days: int) -> list[str]:
    from datetime import timedelta
    out = []
    for it in ((sections.get("news") or {}).get("items") or []):
        try:
            when = it.get("published_at") or it.get("published")
            if when and (now - __import__("datetime").datetime.fromisoformat(str(when).replace("Z", "+00:00"))) > timedelta(days=days):
                continue
        except (ValueError, TypeError):
            pass
        out.append(str(it.get("headline") or it.get("title") or ""))
    return out


def _win_confidence(h: str, card: dict[str, Any] | None, daily: Any, index_close: Any, regime: str | None, required_unknown: bool, mb: dict[str, Any] | None) -> dict[str, Any]:
    """The measured win probability (win_confidence) for one horizon; the old evidence-score map (confidence_line) is no longer the confidence. Never raises: an error is an unavailable confidence, not a number."""
    import logging

    from app.services import gates_v2, win_confidence as wc
    try:
        has = daily is not None and len(daily) >= 200
        med = None if not has else gates_v2.median_traded_value_crore(daily)[0]
        out = wc.confidence_for_card(h, card=card, close=daily["close"] if has else None, high=daily["high"] if has else None, bench_close=index_close, regime=regime, median_traded_value_cr=med,
                                     required_unknown=required_unknown, mb=mb)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("win_confidence_failed horizon=%s error_class=%s", h, type(exc).__name__)
        out = {"available": False, "reason": "Confidence could not be computed.", "band": None, "line": None, "calibrated": None}
    out.setdefault("validated", False)
    return out


def compose(*, sections: dict[str, Any], ev: dict[str, Any], mb: dict[str, Any] | None, daily: Any, bench: Any, regime: dict[str, Any], governance: list[dict[str, Any]] | None, now: Any,
            delivery: Any = None, calibration: dict[str, Any] | None = None, cfg: VerdictConfig | None = None, index_close: Any = None) -> dict[str, Any]:
    """One stock, three horizons. ``ev`` is pipeline_v2.evaluate's output (levels, gates, timing, factor); ``mb`` the stored Phase 6 result (checks JSON)."""
    from app.services import research_confidence, stock_lenses as sl
    cfg = cfg or VerdictConfig()
    comps = (sections.get("assessment") or {}).get("components") or {}
    technicals = sections.get("technicals") or {}
    tech_ok = technicals.get("status") == "available" and (ev.get("engine") or {}).get("status", "available") != "unavailable"
    gates = ev.get("gates") or {}
    events = gates.get("events") or []
    contradictory = bool((comps.get("news_sentiment") or {}).get("conflicts"))
    shared = {
        "financial_trends": sl.financial_trends_lens(comps.get("financial_trends"), ((mb or {}).get("scores") or {}).get("T"), next((c["reason"] for c in (mb or {}).get("checks", []) if c["id"] == "turning_point"), None)),
        "fundamentals": sl.fundamentals_lens(mb), "growth": sl.growth_lens(mb), "valuation": sl.valuation_lens(mb),
        "market_regime": regime,
    }
    out_h: dict[str, Any] = {}
    unknown_any: dict[str, list[str]] = {}
    for h in HORIZONS:
        card = ((ev.get("levels") or {}).get("horizons") or {}).get(h) or {}
        have_card = card.get("status") == "available"
        tgt = (card.get("targets") or [{}])[0] if have_card else {}
        head_r = tgt.get("risk_reward") if isinstance(tgt, dict) else None
        news = sl.news_events_lens(headlines=_recent_headlines(sections, now, cfg.news_material_days.get(h, 3650)), events=events, governance=governance, contradictory=contradictory, now=now.date())
        chart = sl.chart_lens(daily, h, bench, delivery_pct=delivery, headroom_r=head_r if isinstance(head_r, (int, float)) else None)
        liq = (gates.get("liquidity") or {}).get(h)
        lenses = {**shared, "chart": chart, "news_events": news,
                  "liquidity_stale": {"score": None, "direction": "unknown" if liq is None else ("positive" if liq["pass"] else "negative"), "facts": [liq["reason"]] if liq else ["Liquidity not computed."], "coverage": 0.0 if liq is None else 1.0}}
        factor = ev.get("factor") or {}
        fh = (factor.get("horizons") or {}).get("positional" if h == "long_term" else h) or {}
        factor_pct = fh.get("percentile")
        conf = research_confidence.buy_confidence_v2(h, {"recommendation": "BUY"}, card, factor) if have_card else None
        setup = (conf or {}).get("setup")
        grade = (conf or {}).get("grade")
        timing = (ev["horizons"].get(h) or {}).get("timing")
        chart_ok = True if timing == "pass" else False if timing == "fail" else None
        entry = ev["horizons"].get(h) or {}
        chart_reason = (f"Bullish engine read and a valid v2 ladder; setup grade {grade or 'unknown'}." if timing == "pass"
                        else (entry.get("reasons") or ["The chart read is not available."])[0] if entry.get("stage_failed") in ("timing", None) else "The chart read does not support a buy yet.")
        ipo_ok = bool((((mb or {}).get("new_listing") or {}).get("horizons") or {}).get("swing", {}).get("allowed")) if (mb or {}).get("class") == "new_listing" else None
        d = decide(h, tech_ok=tech_ok, liquidity=liq, news=news, mb=mb, chart_ok=chart_ok, chart_reason=chart_reason, grade=grade, ready_to_buy=card.get("ready_to_buy") if have_card else None,
                   valuation_v=((mb or {}).get("scores") or {}).get("V"), ipo_base_ok=ipo_ok, cfg=cfg)
        e = evidence(h, lenses, setup, factor_pct, regime.get("regime"), d["unknown_veto"], cfg)
        calib = _win_confidence(h, card if have_card else None, daily, index_close if index_close is not None else bench, regime.get("regime"), bool(d["unknown_veto"]), mb)
        reasons = (d["reasons"] + [f for lens in ("chart", "fundamentals", "financial_trends") for f in lenses[lens].get("facts", [])[:1]])[:3]
        out_h[h] = {"verdict": d["verdict"], "veto": d["veto"], "reasons": reasons, "size": d["size"], "evidence": e, "confidence": calib, "grade": grade,
                    "levels": ({"entry_status": card.get("entry_status"), "entry_zone": card.get("entry_zone"), "planned_entry": card.get("planned_entry"), "stop": card.get("stop"), "targets": card.get("targets"),
                                "ready_to_buy": card.get("ready_to_buy"), "entry_label": card.get("entry_label")} if have_card and d["verdict"] in ("buy_now", "buy_on_trigger", "watch") else None),
                    "lenses": {k: {kk: vv for kk, vv in v.items() if kk != "detectors"} for k, v in lenses.items()}}
        unknown_any[h] = d["unknown_veto"]
    headline_h = min(HORIZONS, key=lambda h: (_RANK[out_h[h]["verdict"]], -out_h[h]["evidence"]["score"]))
    risks = [f for h in HORIZONS for f in (out_h[h]["lenses"]["news_events"].get("facts") or [])[:2]][:4]
    return {"version": VERSION, "as_of": now.isoformat(), "headline": {"horizon": headline_h, "verdict": out_h[headline_h]["verdict"], "evidence": out_h[headline_h]["evidence"]["score"]}, "horizons": out_h,
            "multibagger": None if not mb else {"class": mb.get("class"), "rank": mb.get("rank"), "scores": mb.get("scores"), "checklist": mb.get("checklist"), "timing": mb.get("timing"),
                                                  "failed_or_unknown": [f"{c['id']}: {c['reason']}" for c in mb.get("checks", []) if c["result"] in ("fail", "unknown")][:8]},
            "regime": {"regime": regime.get("regime"), "facts": regime.get("facts")}, "risks_and_events": risks, "disclaimer": "Decision support, not investment advice. A verdict is never an order."}


def _breadth(frames: dict[Any, Any]) -> float | None:
    above = []
    for f in frames.values():
        if f is not None and len(f) >= 60:
            above.append(float(f["close"].iloc[-1] > f["close"].iloc[-50:].mean()))
    return sum(above) / len(above) if len(above) >= 30 else None


def run_universe(db: Any, user_id: Any, *, now: Any = None, store: bool = True, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Nightly shadow run: a verdict for every stock of the owner's research universe from the latest stored research. Flag-independent; never raises per stock."""
    import logging
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.models.multibagger import MultibaggerAssessment
    from app.research_daily import user_universe
    from app.services import benchmark_index, gates_v2, pipeline_v2, research_pipeline as rp, stock_lenses as sl
    log = logging.getLogger(__name__)
    now = now or datetime.now(timezone.utc)
    universe = [i for i in user_universe(db, user_id) if not symbols or i.symbol in symbols]
    frames = {i.id: rp._daily_frame(db, i.id, None) for i in universe}
    try:
        nifty, _ = benchmark_index.index_series(db)
    except Exception:  # noqa: BLE001
        nifty = None
    bench = rp._benchmark_series(db)
    regime = sl.market_regime(nifty, _breadth(frames))
    calibration = load_calibration()
    results = []
    for inst in universe:
        try:
            latest = rp.latest_brief(db, user_id, inst.id)
            if latest is None:
                continue
            sections = latest[1].sections or {}
            frame = frames[inst.id]
            ev = pipeline_v2.evaluate(db, user_id, inst, sections, frame, now, bench=bench)
            row = db.scalar(select(MultibaggerAssessment).where(MultibaggerAssessment.instrument_id == inst.id, MultibaggerAssessment.user_id == user_id).order_by(MultibaggerAssessment.as_of.desc()).limit(1))
            mb = None if row is None else {**(row.checks or {}), "class": row.klass, "scores": {"Q": row.q, "G": row.g, "V": row.v, "T": row.t, "X": row.x}, "rank": row.rank}
            res = compose(sections=sections, ev=ev, mb=mb, daily=gates_v2.drop_forming_bar(frame, now) if frame is not None and len(frame) else frame, bench=bench, regime=regime, governance=None, now=now, calibration=calibration, index_close=nifty)
            res["symbol"] = inst.symbol
            results.append(res)
            if store:
                _store(db, user_id, inst, res, now)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.warning("stock_verdict_failed symbol=%s error_class=%s", inst.symbol, type(exc).__name__)
    if store:
        db.commit()
    return results


def _store(db: Any, user_id: Any, inst: Any, res: dict[str, Any], now: Any) -> None:
    from sqlalchemy import select

    from app.models.stock_verdict import StockVerdict
    for h, d in res["horizons"].items():
        row = db.scalar(select(StockVerdict).where(StockVerdict.instrument_id == inst.id, StockVerdict.user_id == user_id, StockVerdict.as_of == now.date(), StockVerdict.horizon == h))
        fields = dict(computed_at=now, verdict=d["verdict"], evidence_score=d["evidence"]["score"], calibrated_confidence=d["confidence"].get("calibrated"), lens=_clean(d["lenses"]),
                      reasons=_clean({"reasons": d["reasons"], "veto": d["veto"], "size": d["size"], "levels": d["levels"], "evidence": d["evidence"], "confidence": d["confidence"], "grade": d["grade"],
                                      "headline": res["headline"], "multibagger": res["multibagger"], "regime": res["regime"], "risks_and_events": res["risks_and_events"]}), model_version=VERSION)
        if row is None:
            db.add(StockVerdict(instrument_id=inst.id, user_id=user_id, as_of=now.date(), horizon=h, **fields))
        else:
            for k, v in fields.items():
                setattr(row, k, v)


def _clean(o: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, float) and o != o:
        return None
    return o


def log_nightly(db: Any, user_id: Any) -> None:
    import logging
    from collections import Counter
    try:
        res = run_universe(db, user_id)
        logging.getLogger(__name__).info("stock_verdict_nightly %s", dict(Counter(r["headline"]["verdict"] for r in res)))
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logging.getLogger(__name__).warning("stock_verdict_nightly_failed error_class=%s", type(exc).__name__)
