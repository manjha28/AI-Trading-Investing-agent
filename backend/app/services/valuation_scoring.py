"""Valuation scoring: peer-relative, sector-family aware, provisional.

Feeds the production "valuation" Research component (research_scoring.score_valuation_component). It stays provisional:
the weights, thresholds and sector rules are unvalidated hypotheses documented in docs/valuation-scoring-methodology.md.

Principles: relative to peers (no universal cutoffs); missing / negative / stale / thin-peer inputs are NOT scored
(never zero or neutral); correlated metrics (P/E and PEG) form ONE group so they cannot double count; each metric
contributes ONE value (a provider-reported and a calculated figure are never two signals); market capitalisation is
context only and never scored; a peer set is used only when the peers' figures have the same origin as the subject's.
"""

from typing import Any

SCORING_VERSION = "valuation-score-0.2.0-provisional"
MIN_PEERS = 5                      # fewer comparable peers than this => the metric is not scored
MIN_GROUPS = 2                     # at least two independent evidence groups must be scored
MIN_SCORED_WEIGHT = 0.60           # share of the sector's group weights that must be backed by scored groups
ATTRACTIVE_AT = 65.0               # provisional label bands on the 0-100 scale (higher = cheaper vs peers)
EXPENSIVE_BELOW = 35.0

# metric -> evidence group. P/E and PEG share a group: PEG is derived from P/E, so they are one piece of evidence.
GROUP_OF = {"pe_ttm": "earnings", "peg": "earnings", "pb": "book", "price_to_sales": "sales", "ev_ebitda": "enterprise"}
# Provisional sector-family rules: which groups matter and how much. Weights within a family sum to 1.
SECTOR_RULES: dict[str, dict[str, float]] = {
    "financials": {"book": 0.50, "earnings": 0.50},                                  # banks/NBFC/insurers: P/B and P/E; EV and sales are not meaningful
    "asset_heavy_cyclical": {"book": 0.35, "earnings": 0.30, "enterprise": 0.35},    # metals, energy, utilities, real estate, autos...
    "growth_asset_light": {"earnings": 0.55, "sales": 0.25, "book": 0.20},           # technology, consumer, healthcare
    "default": {"earnings": 0.45, "book": 0.30, "sales": 0.25},
}
FAMILY_KEYWORDS = (
    ("financials", ("bank", "financ", "insurance", "nbfc", "lending", "asset management")),
    ("asset_heavy_cyclical", ("metal", "steel", "oil", "gas", "energy", "utilit", "power", "real estate", "cement", "mining", "auto", "chemical", "construction", "basic materials", "industrial")),
    ("growth_asset_light", ("technology", "software", "consumer", "healthcare", "pharma", "communication", "telecom", "retail", "internet")),
)


def sector_family(sector: str | None, industry: str | None) -> str:
    text = f"{sector or ''} {industry or ''}".casefold()
    for family, words in FAMILY_KEYWORDS:
        if any(word in text for word in words):
            return family
    return "default"


def _band(score: float) -> str:
    return "attractive" if score >= ATTRACTIVE_AT else "expensive" if score < EXPENSIVE_BELOW else "fair"


def score_valuation(valuation: dict[str, Any], *, sector: str | None, industry: str | None) -> dict[str, Any]:
    """``valuation`` is the dict from services.valuation.build_valuation (metrics + peer_comparison)."""
    family = sector_family(sector, industry)
    rules = SECTOR_RULES[family]
    metrics = valuation.get("metrics") or {}
    peers = (valuation.get("peer_comparison") or {}).get("metrics") or {}
    price_current = bool((valuation.get("price") or {}).get("current"))
    blockers: list[str] = []
    per_metric: dict[str, dict[str, Any]] = {}
    for key, group in GROUP_OF.items():
        if group not in rules:
            continue  # this sector family does not use the metric (e.g. EV/EBITDA for banks)
        metric, peer = metrics.get(key) or {}, peers.get(key) or {}
        if not price_current:
            per_metric[key] = {"group": group, "scored": False, "reason": "no current share price"}
        elif metric.get("status") not in {"calculated", "reported"}:
            per_metric[key] = {"group": group, "scored": False, "reason": metric.get("reason") or f"metric is {metric.get('status', 'missing')}"}
        elif metric.get("freshness") != "current":
            per_metric[key] = {"group": group, "scored": False, "reason": f"the {metric.get('label', key)} figure is not current ({metric.get('freshness')}); stale or non-TTM values are not scored"}
        elif peer.get("status") != "compared":
            per_metric[key] = {"group": group, "scored": False, "reason": peer.get("reason") or "no reliable peer comparison"}
        else:
            # cheaper than more peers => higher score. Percentile rank is the share of peers with a LOWER multiple.
            score = 100.0 - float(peer["percentile_rank"])
            per_metric[key] = {"group": group, "scored": True, "score": round(score, 1), "percentile_rank": peer["percentile_rank"], "peers": peer["peer_count"],
                               "value": metric.get("value"), "origin": metric.get("origin"), "provider": metric.get("provider"),
                               "reporting_period": metric.get("reporting_period"), "retrieved_at": metric.get("retrieved_at")}
    groups: dict[str, float] = {}
    for group in rules:
        scored = [m["score"] for m in per_metric.values() if m["group"] == group and m["scored"]]
        if scored:
            groups[group] = sum(scored) / len(scored)  # PEG and P/E average inside one group: no double weight
    scored_weight = sum(rules[g] for g in groups)
    result: dict[str, Any] = {
        "version": SCORING_VERSION, "provisional": True, "sector_family": family, "group_weights": rules, "metrics": per_metric,
        "groups": {g: round(s, 1) for g, s in groups.items()}, "scored_weight": round(scored_weight, 3),
        "thresholds": {"attractive_at": ATTRACTIVE_AT, "expensive_below": EXPENSIVE_BELOW, "min_peers": MIN_PEERS, "min_groups": MIN_GROUPS, "min_scored_weight": MIN_SCORED_WEIGHT},
    }
    if len(groups) < MIN_GROUPS:
        blockers.append(f"only {len(groups)} independent valuation group(s) could be scored (minimum {MIN_GROUPS})")
    if scored_weight < MIN_SCORED_WEIGHT:
        blockers.append(f"only {scored_weight:.0%} of the {family} valuation weight is backed by scored groups (minimum {MIN_SCORED_WEIGHT:.0%})")
    if blockers:
        reasons = list(dict.fromkeys(m["reason"] for m in per_metric.values() if not m["scored"]))[:3]
        if reasons:
            blockers.append("Why: " + "; ".join(reasons))
        return {**result, "status": "blocked", "score": None, "label": "insufficient_evidence", "confidence": None, "coverage_pct": round(100 * scored_weight, 1), "blockers": blockers}
    score = sum(rules[g] * s for g, s in groups.items()) / scored_weight
    peer_depth = min(1.0, min(m["peers"] for m in per_metric.values() if m["scored"]) / 15)
    confidence = 100 * (0.6 * scored_weight + 0.4 * peer_depth)
    return {**result, "status": "scored", "score": round(score, 1), "label": _band(score), "confidence": round(confidence, 1),
            "coverage_pct": round(100 * scored_weight, 1), "blockers": []}
