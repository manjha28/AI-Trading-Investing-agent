"""Nexus v2 Phase 2: per-horizon setup grade from checks Nexus already has (flag ``confidence_v2_enabled``). Works on a levels card from research_levels.build_levels
(v1 or v2 shape). Vetoes make the horizon "Not ready"; soft checks set grade A (none failed), B (one failed), C (two or more). Every check returns pass | fail | unknown | not_evaluated
with a reason; an unknown soft check counts as failed (missing data is never favourable). Liquidity and corporate-event checks arrive with Phase 3 and are reported as not_evaluated.
Decision support, not investment advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VERSION = "setup-grade/1"


@dataclass
class GradeConfig:
    min_rr_t1: float = 1.5
    min_swing_stop_daily_atr: float = 1.0
    max_stop_pct: dict[str, float] = field(default_factory=lambda: {"positional": 12.0, "long_term": 20.0})
    bullish: tuple[str, ...] = ("BULLISH", "STRONG_BULLISH")
    high_rank_percentile: float = 70.0
    grade_points: dict[str, int] = field(default_factory=lambda: {"A": 3, "B": 2, "C": 1})


def _chk(key: str, label: str, result: str, reason: str, veto: bool) -> dict[str, Any]:
    return {"key": key, "label": label, "result": result, "reason": reason, "veto": veto}


def grade(horizon: str, card: dict[str, Any] | None, factor: dict[str, Any] | None, cfg: GradeConfig | None = None) -> dict[str, Any]:
    """-> {grade: 'A'|'B'|'C'|None, label, checks[], failed_vetoes[], failed_soft[]}. ``card`` is the horizon's levels dict; ``factor`` the factor_rank section (for relative strength)."""
    cfg = cfg or GradeConfig()
    checks: list[dict[str, Any]] = []
    if not card or card.get("status") != "available":
        reason = (card or {}).get("reason") or "No entry plan exists for this horizon."
        checks.append(_chk("plan", "An entry plan exists", "fail", reason, True))
        return _result(checks, cfg)
    close = float(card["reference_close"])
    entry = float(card["planned_entry"])
    stop = float(card["stop"])
    targets = card.get("targets") or []
    # vetoes
    d = card.get("direction")
    checks.append(_chk("direction", "Engine direction is bullish", "pass" if d in cfg.bullish else "fail", f"The {horizon.replace('_', '-')} engine reads {str(d).lower().replace('_', ' ')}.", True))
    t1 = float(targets[0]["price"]) if targets else None
    t1_rr = targets[0].get("risk_reward") if targets else None
    if t1 is None:
        checks.append(_chk("t1", "T1 is above the last close and at least 1.5R from the planned entry", "fail", "No target is available.", True))
    else:
        ok = t1 > close and t1_rr is not None and float(t1_rr) >= cfg.min_rr_t1 - 1e-9
        checks.append(_chk("t1", "T1 is above the last close and at least 1.5R from the planned entry", "pass" if ok else "fail", f"T1 {card['targets'][0]['price']} is {t1_rr}R from entry; last close {close:g}.", True))
    pct = float(card.get("stop_percent") or (entry - stop) / entry * 100)
    method = card.get("stop_method")
    atr_units = card.get("risk_atr")
    stop_ok, why = method == "STRUCTURAL", f"{method or 'unknown'} stop, {pct:.1f}% from entry."
    if stop_ok and horizon == "swing":
        stop_ok = atr_units is not None and float(atr_units) >= cfg.min_swing_stop_daily_atr
        why = f"Structural stop is {atr_units} ATR from entry (swing needs at least {cfg.min_swing_stop_daily_atr:g})."
    elif stop_ok:
        stop_ok = pct <= cfg.max_stop_pct[horizon]
        why = f"Structural stop is {pct:.1f}% from entry (limit {cfg.max_stop_pct[horizon]:g}%)."
    elif method != "STRUCTURAL":
        why = f"The stop is {str(method).lower()}-based, not a structural level with an ATR buffer."
    checks.append(_chk("stop", "Stop is structural with an ATR buffer and within the horizon's bounds", "pass" if stop_ok else "fail", why, True))
    if t1 is None:
        blocking = None
    else:
        blocking = next((z for z in (card.get("resistance_levels") or []) if float(z.get("strength", 0)) >= 2 and max(entry, close) < float(z["low"]) < t1 - 1e-9), None)
    checks.append(_chk("headroom", "No unbroken resistance between entry and T1", "fail" if blocking else "pass",
                       f"Resistance {blocking['low']}-{blocking['high']} sits between the entry and T1." if blocking else "No strong resistance zone lies between entry and T1.", True))
    # soft checks
    rs = (factor or {}).get("inputs") or {}
    rs20, rs60 = rs.get("rs_20"), rs.get("rs_60")
    if rs20 is None or rs60 is None:
        checks.append(_chk("rs", "Relative strength vs NIFTY 50 (20d and 60d) is not negative", "unknown", "NIFTY 50 history or the factor screen is unavailable.", False))
    else:
        checks.append(_chk("rs", "Relative strength vs NIFTY 50 (20d and 60d) is not negative", "pass" if rs20 >= 0 and rs60 >= 0 else "fail", f"Return minus NIFTY 50: {rs20 * 100:+.1f} points over 20 sessions, {rs60 * 100:+.1f} over 60.", False))
    lo, hi = float(card["entry_zone"]["low"]), float(card["entry_zone"]["high"])
    inside = lo <= close <= hi
    status = card.get("entry_status")
    kind = (card.get("v2") or {}).get("entry_kind")
    if kind == "breakout_trigger":
        consistent, why = status != "ENTER_NOW", "A band above the market is a breakout trigger and is shown as one."
    elif status == "ENTER_NOW":
        consistent, why = inside, "In buy band now" if inside else "Status says in the buy band but the close is outside the band."
    else:
        consistent = (not inside) or bool(card.get("v2"))          # v1 shows "wait" with the close inside the band (HONASA) or a band above the market (LALPATHLAB): inconsistent
        why = "Status matches the close's position against the band." if consistent else "Status says wait, but the last close is inside the band or the band is above the market."
        if not card.get("v2") and lo > close:
            consistent, why = False, "A 'wait' band above the last close would fill at once."
    checks.append(_chk("status", "Status matches price (in band, wait, or band above market)", "pass" if consistent else "fail", why, False))
    checks.append(_chk("liquidity_events", "Liquidity and corporate-event gates pass", "not_evaluated", "Arrives with Phase 3; not part of the grade yet.", False))
    return _result(checks, cfg)


def _result(checks: list[dict[str, Any]], cfg: GradeConfig) -> dict[str, Any]:
    vetoes = [c for c in checks if c["veto"] and c["result"] != "pass"]
    soft = [c for c in checks if not c["veto"] and c["result"] in ("fail", "unknown")]
    if vetoes:
        g, label = None, "Not ready"
    else:
        g = "A" if not soft else "B" if len(soft) == 1 else "C"
        label = f"Grade {g}"
    return {"version": VERSION, "grade": g, "label": label, "checks": checks, "failed_vetoes": [c["key"] for c in vetoes], "failed_soft": [c["key"] for c in soft]}


def combined_label(g: dict[str, Any], rank_percentile: float | None, cfg: GradeConfig | None = None) -> str:
    """"High" only when the setup grade is A and the factor-rank percentile is at least 70; otherwise the grade itself (or "Not ready")."""
    cfg = cfg or GradeConfig()
    if g["grade"] == "A" and rank_percentile is not None and rank_percentile >= cfg.high_rank_percentile:
        return "High"
    return g["grade"] or "Not ready"
