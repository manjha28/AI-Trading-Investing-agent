"""Buy-conviction score (0-100) for a swing / positional / long-term BUY recommendation. Deterministic, from stored research only.

score = 0.40 x evidence quality + 0.25 x signal strength + 0.20 x horizon agreement + 0.15 x risk quality
  evidence quality   the assessment's own confidence (weight of scored components x their confidence)
  signal strength    how far the model score sits above the BUY threshold (0 at the threshold, 100 at a perfect score)
  horizon agreement  share of the three horizons that are BUY
  risk quality       how tight the protective stop is relative to the horizon's limit (0 when no valid level exists)
Bands: High >= 70, Medium >= 55, otherwise Low. Provisional and unvalidated: it ranks conviction, it is not a probability of profit. Out-of-sample replays of the technical engine found no reliable link between higher technical scores and better forward returns.

When a universe rank is available (services/universe_rank.py) it REPLACES the formula above: the score is the stock's percentile on five factors that predicted forward returns
out of sample (swing rank correlation +0.11 vs +0.01 for the old score). Bands for the rank: High >= 70, Medium >= 40, Low below; replays showed the bottom bins clearly worse, the top bins only modestly better.
"""
from __future__ import annotations

from typing import Any

WEIGHTS = {"evidence": 0.40, "strength": 0.25, "agreement": 0.20, "risk": 0.15}
HIGH, MEDIUM = 70.0, 55.0


def _clip(x: float) -> float:
    return max(0.0, min(100.0, x))


def band(score: float) -> str:
    return "High" if score >= HIGH else "Medium" if score >= MEDIUM else "Low"


RANK_HIGH, RANK_MEDIUM = 70.0, 40.0


def _rank_confidence(rank: dict[str, Any], risk_note: str) -> dict[str, Any]:
    score = round(float(rank["percentile"]), 1)
    return {"score": score, "band": "High" if score >= RANK_HIGH else "Medium" if score >= RANK_MEDIUM else "Low", "provisional": True, "basis": "universe_rank", "as_of": rank.get("as_of"),
            "parts": [{"key": m["key"], "label": m["label"], "value": m["percentile"], "weight": round(1 / len(rank["members"]), 2)} for m in rank["members"]],
            "note": f"Percentile rank among {rank.get('universe_size')} NSE stocks (as of {rank.get('as_of')}); a ranking, not a probability of profit. {risk_note}"}


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def buy_confidence_v2(horizon_key: str, horizon: dict[str, Any], levels: dict[str, Any] | None, factor: dict[str, Any] | None) -> dict[str, Any] | None:
    """Nexus v2 (flag confidence_v2_enabled): a neutral factor rank plus a setup grade replace the High/Medium/Low confidence. "High" only for grade A and rank percentile >= 70."""
    from app.services import setup_grade
    if horizon.get("recommendation") != "BUY":
        return None
    g = setup_grade.grade(horizon_key, levels, factor)
    fh = ((factor or {}).get("horizons") or {}).get(horizon_key) or {}
    ok = fh.get("status") == "available"
    pct = fh.get("percentile") if ok else None
    label = setup_grade.combined_label(g, pct)
    points = setup_grade.GradeConfig().grade_points.get(g["grade"] or "", 0)
    parts = [{"key": x["key"], "label": x["label"], "value": x["percentile"], "weight": x.get("weight", 0)} for x in (fh.get("groups") or []) if x["percentile"] is not None]
    rank_text = f"Factor rank {_ordinal(fh['rank'])} of {fh['of']} (factor screen)" if ok else "Factor rank unavailable"
    return {"kind": "factor_v2", "score": points * 1000 + (pct or 0), "band": label, "label": label, "grade": g["grade"], "setup": g, "provisional": True, "basis": "factor_rank_v2",
            "factor_rank": ({"rank": fh["rank"], "of": fh["of"], "composite": fh["composite"], "percentile": fh["percentile"], "as_of": factor.get("as_of"), "universe_size": factor.get("universe_size"), "text": rank_text} if ok else None),
            "parts": parts, "note": (f"{rank_text}; composite {fh['composite']:g}/100, as of {factor.get('as_of')} across {factor.get('universe_size')} stocks. " if ok else "The factor rank could not be computed. ")
                                    + "A screen on trend quality, volume flow and relative strength, not a confidence and not a probability of profit."}


def buy_confidence(horizon: dict[str, Any], levels: dict[str, Any] | None, buy_horizons: int, buy_threshold: float, max_risk_atr: float, rank: dict[str, Any] | None = None,
                   horizon_key: str | None = None, factor: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from app.config import settings
    if settings.confidence_v2_enabled and horizon_key:
        return buy_confidence_v2(horizon_key, horizon, levels, factor)
    if horizon.get("recommendation") != "BUY" or horizon.get("score") is None or horizon.get("confidence") is None:
        return None
    evidence = _clip(float(horizon["confidence"]))
    strength = _clip((float(horizon["score"]) - buy_threshold) / max(1e-9, 100.0 - buy_threshold) * 100.0)
    agreement = _clip(buy_horizons / 3.0 * 100.0)
    risk, risk_note = 0.0, "No valid stop level, so risk quality scores 0."
    if levels and levels.get("status") == "available" and levels.get("risk_atr"):
        risk_atr = float(levels["risk_atr"])
        risk = _clip((1.0 - risk_atr / max_risk_atr) * 100.0)
        risk_note = f"Stop is {risk_atr:.1f} ATR away (limit {max_risk_atr:g})."
    if rank and rank.get("status") == "available":
        return _rank_confidence(rank, risk_note)
    parts = {"evidence": evidence, "strength": strength, "agreement": agreement, "risk": risk}
    score = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
    return {"score": score, "band": band(score), "provisional": True,
            "parts": [{"key": k, "label": {"evidence": "Evidence quality", "strength": "Signal strength", "agreement": "Horizon agreement", "risk": "Risk quality"}[k],
                       "value": round(v, 1), "weight": WEIGHTS[k]} for k, v in parts.items()],
            "basis": "legacy_formula", "note": risk_note + " Universe rank unavailable, so the older unvalidated formula is shown."}
