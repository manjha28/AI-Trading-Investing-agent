"""Buy-conviction score (0-100) for a swing / positional / long-term BUY recommendation. Deterministic, from stored research only.

score = 0.40 x evidence quality + 0.25 x signal strength + 0.20 x horizon agreement + 0.15 x risk quality
  evidence quality   the assessment's own confidence (weight of scored components x their confidence)
  signal strength    how far the model score sits above the BUY threshold (0 at the threshold, 100 at a perfect score)
  horizon agreement  share of the three horizons that are BUY
  risk quality       how tight the protective stop is relative to the horizon's limit (0 when no valid level exists)
Bands: High >= 70, Medium >= 55, otherwise Low. Provisional and unvalidated: it ranks conviction, it is not a probability of profit.
"""
from __future__ import annotations

from typing import Any

WEIGHTS = {"evidence": 0.40, "strength": 0.25, "agreement": 0.20, "risk": 0.15}
HIGH, MEDIUM = 70.0, 55.0


def _clip(x: float) -> float:
    return max(0.0, min(100.0, x))


def band(score: float) -> str:
    return "High" if score >= HIGH else "Medium" if score >= MEDIUM else "Low"


def buy_confidence(horizon: dict[str, Any], levels: dict[str, Any] | None, buy_horizons: int, buy_threshold: float, max_risk_atr: float) -> dict[str, Any] | None:
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
    parts = {"evidence": evidence, "strength": strength, "agreement": agreement, "risk": risk}
    score = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
    return {"score": score, "band": band(score), "provisional": True,
            "parts": [{"key": k, "label": {"evidence": "Evidence quality", "strength": "Signal strength", "agreement": "Horizon agreement", "risk": "Risk quality"}[k],
                       "value": round(v, 1), "weight": WEIGHTS[k]} for k, v in parts.items()],
            "note": risk_note}
