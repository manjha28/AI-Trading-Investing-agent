"""Provisional four-dimension research scoring over the serialized evidence pack.

Every figure is computed here in Python from the brief sections; the LLM and the
frontend never compute scores. Nothing in this module is empirically validated:
anchors, weights and thresholds are documented hypotheses (``MODEL_VERSION`` is
stored with each assessment). Missing data is never converted to zero or neutral:
a component without enough inputs is ``blocked`` with explicit reasons.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.config import (
    NSE_CALENDAR_YEARS,
    NSE_EQUITY_HOLIDAYS,
    NSE_FRESHNESS_SESSION_END,
    NSE_UNVERIFIED_HOLIDAYS,
)

MODEL_VERSION = "research-score-0.1.0-provisional"
IST = ZoneInfo("Asia/Kolkata")
# Daily bars are published some time after the 15:30 close; do not call a session
# "missing" until this grace has passed. Provisional assumption.
EOD_PUBLICATION_GRACE = timedelta(hours=2)

COMPONENTS = ("fundamental", "technical", "financial_trends", "news_sentiment")
COMPONENT_LABELS = {
    "fundamental": "Fundamental Intelligence",
    "technical": "Technical Intelligence",
    "financial_trends": "Financial Trends Intelligence",
    "news_sentiment": "News & Sentiment Intelligence",
}
HORIZONS = ("intraday", "swing", "positional", "long_term")
HORIZON_LABELS = {"intraday": "Intraday", "swing": "Swing", "positional": "Positional", "long_term": "Long-term"}
# Starting hypotheses, NOT validated weights.
HORIZON_WEIGHTS: dict[str, dict[str, float]] = {
    "intraday": {"fundamental": 0.05, "technical": 0.55, "financial_trends": 0.10, "news_sentiment": 0.30},
    "swing": {"fundamental": 0.15, "technical": 0.40, "financial_trends": 0.20, "news_sentiment": 0.25},
    "positional": {"fundamental": 0.25, "technical": 0.25, "financial_trends": 0.30, "news_sentiment": 0.20},
    "long_term": {"fundamental": 0.40, "technical": 0.10, "financial_trends": 0.35, "news_sentiment": 0.15},
}
# A horizon is not assessed unless this share of its weight is backed by scored
# components and each critical component is scored. Provisional.
MIN_SCORED_WEIGHT = 0.60
REQUIRED_COMPONENTS: dict[str, tuple[str, ...]] = {
    "intraday": ("technical",), "swing": ("technical",), "positional": (), "long_term": ("fundamental",),
}
BUY_THRESHOLD = 65.0
HOLD_THRESHOLD = 45.0
MIN_CONFIDENCE_FOR_BUY = 40.0
MIN_COMPANY_EVENTS = 3
# The technical engine scores stored DAILY bars only; intraday scoring needs
# intraday-bar indicators that the research pipeline does not compute.
INTRADAY_SUPPORTED = False
NOT_IMPLEMENTED_GATES = [
    "liquidity: no traded-value/liquidity measure is in the evidence pack",
    "corporate-event risk: corporate actions are not linked into the research pack",
]
FINANCIAL_SECTOR_WORDS = ("bank", "financ", "insurance", "nbfc", "lending", "asset management")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _field(values: dict[str, Any], name: str) -> Decimal | None:
    item = values.get(name)
    return _dec(item.get("value")) if isinstance(item, dict) else None


def _lerp(x: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear map to 0-100, clamped at the first/last anchor."""
    if x <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return anchors[-1][1]


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _interpretation(score: float | None) -> str | None:
    if score is None:
        return None
    return "strong" if score >= BUY_THRESHOLD else "mixed" if score >= HOLD_THRESHOLD else "weak"


def _component(key: str, *, score: float | None = None, confidence: float | None = None,
               coverage: float | None = None, metrics: list[dict[str, Any]] | None = None,
               missing: list[str] | None = None, conflicts: list[str] | None = None,
               blockers: list[str] | None = None, flags: list[str] | None = None,
               notes: list[str] | None = None) -> dict[str, Any]:
    scored = score is not None and not blockers
    return {
        "key": key, "label": COMPONENT_LABELS[key],
        "status": "scored" if scored else "blocked",
        "score": round(score, 1) if scored and score is not None else None,
        "interpretation": _interpretation(score) if scored else None,
        "confidence": round(confidence, 1) if scored and confidence is not None else None,
        "coverage_pct": round(coverage * 100, 1) if coverage is not None else None,
        "factors": metrics or [], "missing": missing or [], "conflicts": conflicts or [],
        "blockers": blockers or [], "risk_flags": flags or [], "notes": notes or [],
    }


def _metric(name: str, label: str, value: float, unit: str, sub_score: float, basis: str) -> dict[str, Any]:
    return {"key": name, "label": label, "value": round(value, 4), "unit": unit,
            "sub_score": round(sub_score, 1), "basis": basis}


def is_financial_sector(sector: str | None, industry: str | None) -> bool:
    text = f"{sector or ''} {industry or ''}".casefold()
    return any(word in text for word in FINANCIAL_SECTOR_WORDS)


def _annual(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((p for p in periods if p.get("period_type") == "annual"), key=lambda p: p["period_end"])


# ───────────────────────── Fundamental (levels / ratios) ─────────────────────────
def score_fundamental(fundamentals: dict[str, Any], sector: str | None, industry: str | None) -> dict[str, Any]:
    key = "fundamental"
    annual = [p for p in _annual(fundamentals.get("periods") or [])
              if any(_field(p["values"], f) is not None for f in ("revenue", "net_income", "total_equity"))]
    if not annual:
        return _component(key, blockers=["No annual statement period with revenue, net income or equity is stored."])
    currencies = {p.get("currency") for p in annual[-2:]}
    if len(currencies) > 1:
        return _component(key, blockers=[f"Latest annual periods report mixed currencies {sorted(map(str, currencies))}; no FX conversion is applied."])
    latest = annual[-1]
    v = latest["values"]
    prior = next((p for p in reversed(annual[:-1])
                  if 360 <= (date.fromisoformat(latest["period_end"]) - date.fromisoformat(p["period_end"])).days <= 370), None)
    financial = is_financial_sector(sector, industry)
    revenue, income, equity = _field(v, "revenue"), _field(v, "net_income"), _field(v, "total_equity")
    debt, liabilities, assets = _field(v, "total_debt"), _field(v, "total_liabilities"), _field(v, "total_assets")
    ocf, fcf = _field(v, "operating_cash_flow"), _field(v, "free_cash_flow")
    metrics: list[dict[str, Any]] = []
    missing: list[str] = []
    flags: list[str] = []
    notes = [f"Based on annual period ending {latest['period_end']} ({latest.get('currency')})."]
    applicable = 0

    def need(label: str, ok: bool, reason: str) -> bool:
        nonlocal applicable
        applicable += 1
        if not ok:
            missing.append(f"{label}: {reason}")
        return ok

    if not financial and need("Net margin", revenue is not None and income is not None and revenue > 0, "needs positive revenue and net income"):
        m = float(income / revenue * 100)  # type: ignore[operator]
        metrics.append(_metric("net_margin", "Net margin", m, "%", _lerp(m, [(-10, 0), (0, 25), (5, 50), (12, 75), (20, 100)]), "net income / revenue"))
    if not financial:
        om = _field(v, "operating_margin")
        if need("Operating margin", om is not None, "not stored"):
            if om is not None and not -500 <= om <= 100:
                missing.append(f"Operating margin: value {om} is outside the plausible percent range; excluded as a probable unit error.")
            elif om is not None:
                m = float(om)
                metrics.append(_metric("operating_margin", "Operating margin", m, "%", _lerp(m, [(-10, 0), (0, 25), (8, 50), (15, 75), (25, 100)]), "stored operating margin (percent)"))
    if need("Return on equity", income is not None and equity is not None and equity > 0, "needs net income and positive equity"):
        m = float(income / equity * 100)  # type: ignore[operator]
        metrics.append(_metric("roe", "Return on equity", m, "%", _lerp(m, [(-10, 0), (0, 20), (8, 45), (15, 70), (25, 100)]), "net income / year-end equity (not average equity)"))
    elif equity is not None and equity <= 0:
        flags.append("Non-positive shareholder equity.")
    if not financial:
        if need("Debt to equity", debt is not None and equity is not None and equity > 0, "needs total debt and positive equity"):
            m = float(debt / equity)  # type: ignore[operator]
            metrics.append(_metric("debt_to_equity", "Debt to equity", m, "x", _lerp(m, [(0.1, 100), (0.5, 80), (1, 60), (2, 30), (3, 0)]), "total debt / equity"))
            if m >= 3:
                flags.append(f"Debt to equity is {m:.1f}x.")
        if need("Liabilities to assets", liabilities is not None and assets is not None and assets > 0, "needs total liabilities and assets"):
            m = float(liabilities / assets)  # type: ignore[operator]
            metrics.append(_metric("liabilities_to_assets", "Liabilities to assets", m, "x", _lerp(m, [(0.4, 100), (0.6, 70), (0.8, 40), (1.0, 0)]), "total liabilities / total assets"))
        if income is not None and income <= 0:
            notes.append("Cash conversion is not meaningful with non-positive net income; excluded from the applicable set (not counted as missing data).")
        elif need("Cash conversion", ocf is not None and income is not None, "needs operating cash flow and net income"):
            m = float(ocf / income)  # type: ignore[operator]
            metrics.append(_metric("cash_conversion", "Cash conversion", m, "x", _lerp(m, [(0, 0), (0.5, 45), (0.8, 75), (1.0, 100)]), "operating cash flow / net income"))
        if need("Free-cash-flow margin", fcf is not None and revenue is not None and revenue > 0, "needs free cash flow and positive revenue"):
            m = float(fcf / revenue * 100)  # type: ignore[operator]
            metrics.append(_metric("fcf_margin", "Free-cash-flow margin", m, "%", _lerp(m, [(-5, 0), (0, 35), (5, 65), (12, 100)]), "free cash flow / revenue"))
    shares_now = _field(v, "shares_outstanding")
    shares_prior = _field(prior["values"], "shares_outstanding") if prior else None
    if need("Share dilution", bool(shares_now and shares_prior and shares_now > 0 and shares_prior > 0), "needs two comparable annual share counts"):
        m = float((shares_now - shares_prior) / shares_prior * 100)  # type: ignore[operator]
        if abs(m) >= SHARE_DISCONTINUITY_PCT:
            missing.append(f"Share dilution: share count changed {m:+.0f}% year-on-year, which is more likely a split/bonus/consolidation than dilution; excluded (verify against corporate actions).")
        else:
            metrics.append(_metric("share_change", "Share count change", m, "%", _lerp(m, [(-1, 100), (0, 85), (2, 60), (5, 30), (10, 0)]), "year-on-year shares outstanding"))
    missing.append("Interest coverage: interest expense is not stored.")
    if financial:
        notes.append("Bank/NBFC/insurer: leverage, margin and cash-flow ratios are not comparable and are excluded; asset quality, NIM and capital adequacy are not stored, so confidence is capped at 50.")
    min_metrics = 2 if financial else 3
    coverage = len(metrics) / applicable if applicable else 0.0
    blockers = []
    if len(metrics) < min_metrics or coverage < 0.5:
        blockers.append(f"Only {len(metrics)} of {applicable} applicable metrics could be computed (need at least {min_metrics} and 50% coverage).")
    score = sum(m["sub_score"] for m in metrics) / len(metrics) if metrics else None
    freshness = 0.4 if latest.get("stale") else 1.0
    if latest.get("stale"):
        notes.append("Latest stored period was fetched more than 95 days ago; confidence reduced.")
    depth = min(1.0, len(annual) / 3)
    confidence = 100 * (0.5 * coverage + 0.3 * depth + 0.2 * freshness)
    if financial:
        confidence = min(confidence, 50.0)
    return _component(key, score=score, confidence=confidence, coverage=coverage, metrics=metrics,
                      missing=missing, blockers=blockers, flags=flags, notes=notes)


# ───────────────────────── Financial trends (growth / direction) ─────────────────────────
# A share-count move this large between comparable periods is treated as a split/bonus/consolidation,
# not dilution, and per-share growth across it is not comparable. Provisional heuristic.
SHARE_DISCONTINUITY_PCT = 25.0
GROWTH_ANCHORS = [(-25, 0), (0, 35), (10, 60), (20, 80), (35, 100)]
TREND_METRICS = (("revenue", "Revenue"), ("net_income", "Net profit"), ("eps", "EPS"), ("free_cash_flow", "Free cash flow"))


def _share_discontinuities(fund_periods: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """(period_type, period_end) pairs whose share count moved >= SHARE_DISCONTINUITY_PCT vs the comparable prior period."""
    result: set[tuple[str, str]] = set()
    for p in fund_periods:
        now = _field(p["values"], "shares_outstanding")
        end = date.fromisoformat(p["period_end"])
        for q in fund_periods:
            if q.get("period_type") != p.get("period_type") or not 360 <= (end - date.fromisoformat(q["period_end"])).days <= 370:
                continue
            before = _field(q["values"], "shares_outstanding")
            if now and before and now > 0 and before > 0 and abs(float((now - before) / before * 100)) >= SHARE_DISCONTINUITY_PCT:
                result.add((p["period_type"], p["period_end"]))
    return result


def score_financial_trends(trend: dict[str, Any], fundamentals: dict[str, Any]) -> dict[str, Any]:
    key = "financial_trends"
    periods = trend.get("periods") or []
    if trend.get("status") != "available" or not periods:
        return _component(key, blockers=["No comparable annual or quarterly periods (360-370 days apart) are stored."])
    currencies = {p.get("currency") for p in periods}
    if len(currencies) > 1:
        return _component(key, blockers=[f"Trend periods report mixed currencies {sorted(map(str, currencies))}; growth across them is not comparable and no FX conversion is applied."])
    metrics: list[dict[str, Any]] = []
    missing: list[str] = []
    conflicts: list[str] = []
    notes = ["Growth is year-on-year against a positive prior-period base only; growth from a zero or negative base (including loss-to-profit turnarounds) is undefined and excluded rather than scored."]
    share_jump = _share_discontinuities(fundamentals.get("periods") or [])
    for name, label in TREND_METRICS:
        growth: dict[str, list[float]] = {"annual": [], "quarterly": []}
        for period in sorted(periods, key=lambda p: p["period_end"]):
            raw = _dec(((period.get("values") or {}).get(name) or {}).get("year_on_year_growth_pct"))
            if raw is None or period.get("period_type") not in growth:
                continue
            if name == "eps" and (period["period_type"], period["period_end"]) in share_jump:
                missing.append(f"EPS {period['period_type']} {period['period_end']}: share count changed sharply (split/bonus?); per-share growth is not comparable and is excluded.")
                continue
            if abs(raw) > 1000:
                notes.append(f"{label} {period['period_type']} {period['period_end']}: growth of {raw}% is from a very small base; the sub-score saturates.")
            growth[period["period_type"]].append(float(raw))
        legs = [g[-1] for g in growth.values() if g]
        if not legs:
            missing.append(f"{label}: no meaningful year-on-year growth (prior base missing, zero or negative, e.g. loss-to-profit).")
            continue
        base = sum(_lerp(g, GROWTH_ANCHORS) for g in legs) / len(legs)
        observed = (growth["annual"] or growth["quarterly"])[-3:]
        sub = base
        basis = "latest " + "/".join(t for t in ("annual", "quarterly") if growth[t]) + " YoY growth"
        if len(observed) >= 2:
            consistency = 100 * sum(g > 0 for g in observed) / len(observed)
            sub = 0.7 * base + 0.3 * consistency
            basis += f"; consistency {sum(g > 0 for g in observed)}/{len(observed)} periods positive"
        if growth["annual"] and growth["quarterly"] and (growth["annual"][-1] > 0) != (growth["quarterly"][-1] > 0):
            conflicts.append(f"{label}: annual and quarterly growth point in opposite directions.")
        metrics.append(_metric(f"{name}_growth", f"{label} growth", legs[-1], "%", sub, basis))
    annual = _annual(fundamentals.get("periods") or [])
    if len(annual) >= 2:
        def margin(p: dict[str, Any]) -> float | None:
            rev, ni = _field(p["values"], "revenue"), _field(p["values"], "net_income")
            return float(ni / rev * 100) if rev and rev > 0 and ni is not None else None
        now_m, before_m = margin(annual[-1]), margin(annual[-2])
        gap = (date.fromisoformat(annual[-1]["period_end"]) - date.fromisoformat(annual[-2]["period_end"])).days
        if now_m is not None and before_m is not None and 360 <= gap <= 370:
            delta = now_m - before_m
            metrics.append(_metric("net_margin_change", "Net margin change", delta, "pp", _lerp(delta, [(-5, 0), (0, 50), (3, 75), (6, 100)]), "latest vs prior annual net margin"))
        else:
            missing.append("Net margin change: needs two comparable annual net margins.")
    total = len(TREND_METRICS) + 1
    coverage = len(metrics) / total
    blockers = [] if len(metrics) >= 2 and coverage >= 0.4 else [f"Only {len(metrics)} of {total} trend metrics could be computed (need at least 2 and 40% coverage)."]
    score = sum(m["sub_score"] for m in metrics) / len(metrics) if metrics else None
    depth = min(1.0, len(_annual(trend.get("periods") or [])) / 3)
    confidence = 100 * (0.6 * coverage + 0.4 * depth)
    if conflicts:
        confidence *= 0.85
    return _component(key, score=score, confidence=confidence, coverage=coverage, metrics=metrics,
                      missing=missing, conflicts=conflicts, blockers=blockers, notes=notes)


# ───────────────────────── Technical ─────────────────────────
def score_technical(technicals: dict[str, Any]) -> dict[str, Any]:
    key = "technical"
    status = technicals.get("status")
    if status != "available":
        reason = technicals.get("message") or f"Technical data status is {status}."
        stale = technicals.get("staleness") or {}
        if stale.get("reason"):
            reason = stale["reason"]
        return _component(key, blockers=[f"{reason} No technical score is produced from stale or unavailable data."])
    ind = technicals.get("indicators") or {}

    def val(name: str) -> float | None:
        item = _dec((ind.get(name) or {}).get("value"))
        return float(item) if item is not None else None

    close = _dec(technicals.get("latest_close"))
    close_f = float(close) if close is not None else None
    metrics: list[dict[str, Any]] = []
    missing: list[str] = []
    applicable = 0
    for ma, label in (("sma_50", "Price vs 50-day SMA"), ("sma_200", "Price vs 200-day SMA")):
        applicable += 1
        level = val(ma)
        if close_f and level:
            pct = (close_f / level - 1) * 100
            metrics.append(_metric(f"price_vs_{ma}", label, pct, "%", _lerp(pct, [(-15, 0), (0, 45), (8, 75), (15, 90), (30, 70)]), f"close vs {ma}"))
        else:
            missing.append(f"{label}: needs the latest close and {ma} ({(ind.get(ma) or {}).get('bars_used', 0)}/{(ind.get(ma) or {}).get('bars_required', '?')} bars).")
    applicable += 1
    e20, e50, e200 = val("ema_20"), val("ema_50"), val("ema_200")
    if e20 is not None and e50 is not None:
        checks = [e20 > e50] + ([e50 > e200] if e200 is not None else [])
        s = 100 * sum(checks) / len(checks)
        metrics.append(_metric("ema_alignment", "EMA alignment", sum(checks), f"of {len(checks)}", s, "20>50" + (">200" if e200 is not None else "") + " ordering"))
    else:
        missing.append("EMA alignment: needs EMA 20 and EMA 50.")
    applicable += 1
    rsi = val("rsi_14")
    if rsi is not None:
        metrics.append(_metric("rsi_14", "RSI 14", rsi, "", _lerp(rsi, [(20, 20), (30, 35), (50, 60), (60, 75), (70, 65), (80, 35), (90, 15)]), "momentum with overbought/oversold penalty"))
    else:
        missing.append("RSI 14: insufficient history.")
    applicable += 1
    high = val("distance_to_52w_high")
    if high is not None:
        metrics.append(_metric("distance_to_52w_high", "Distance to 52-week high", high, "%", _lerp(high, [(-50, 15), (-25, 45), (-10, 70), (0, 85)]), "proximity to 52-week high"))
    else:
        missing.append("52-week high distance: needs 253 daily bars.")
    flags: list[str] = []
    vol = val("realized_volatility_20d")
    if vol is not None and vol >= 60:
        flags.append(f"20-day annualised realised volatility is {vol:.0f}%.")
    if technicals.get("bars_available", 0) < 200:
        missing.append("Fewer than 200 daily bars: long-term trend context is limited.")
    coverage = len(metrics) / applicable
    blockers = [] if len(metrics) >= 3 else [f"Only {len(metrics)} of {applicable} technical metrics could be computed (need at least 3)."]
    score = sum(m["sub_score"] for m in metrics) / len(metrics) if metrics else None
    depth = min(1.0, technicals.get("bars_available", 0) / 200)
    confidence = 100 * (0.6 * coverage + 0.4 * depth)
    return _component(key, score=score, confidence=confidence, coverage=coverage, metrics=metrics, missing=missing, flags=flags,
                      notes=["Uses the existing stored-daily-bar indicators unchanged; Levels Giver output is not part of this score (not present in this repository)."])


# ───────────────────────── News & sentiment (company-specific only) ─────────────────────────
def score_news_sentiment(sentiment: dict[str, Any] | None, instrument_id: str, now: datetime) -> dict[str, Any]:
    key = "news_sentiment"
    if not sentiment:
        return _component(key, blockers=["No sentiment aggregate is available."])
    events = sentiment.get("events") or []
    company = [e for e in events if any(str(i.get("instrument_id")) == instrument_id for i in e.get("items") or [])]
    market_wide = len(events) - len(company)
    notes = [f"{len(company)} company-mapped event group(s); {market_wide} market/sector/macro event group(s) are context only and excluded from this score. Duplicate publisher articles count once per event."]
    if len(company) < MIN_COMPANY_EVENTS:
        status = sentiment.get("status")
        why = {"not_analyzed": "News exists but has not been analysed.", "analysis_failed": "Analysis failed for available news.",
               "source_unavailable": "News sources are unavailable."}.get(str(status), "")
        return _component(key, blockers=[f"Only {len(company)} company-specific event(s) in the period (need at least {MIN_COMPANY_EVENTS}). Missing company news is not treated as neutral. {why}".strip()], notes=notes,
                          coverage=min(1.0, len(company) / MIN_COMPANY_EVENTS))
    weights = [max(float(e.get("event_weight") or 0), 1e-6) for e in company]
    total = sum(weights)
    mean = sum(float(e["score"]) * w for e, w in zip(company, weights)) / total
    score = 50 + 50 * mean
    base_conf = sum(float(e["confidence_score"]) * w for e, w in zip(company, weights)) / total
    confidence = base_conf * min(1.0, (len(company) / 6) ** 0.5)
    conflicts: list[str] = []
    pos = sum(w for e, w in zip(company, weights) if e.get("sentiment") == "positive")
    neg = sum(w for e, w in zip(company, weights) if e.get("sentiment") == "negative")
    contradictory = (pos / total >= 0.25 and neg / total >= 0.25) or any(e.get("contradictory_sources") for e in company)
    if contradictory:
        conflicts.append("Company events carry both positive and negative evidence (or publishers disagree on the same event).")
        confidence *= 0.8
    publishers = {i.get("publisher") or i.get("source") for e in company for i in e.get("items") or []}
    if len(publishers) < 2:
        confidence *= 0.6
        notes.append("All company evidence comes from a single publisher/source; confidence reduced.")
    newest = max((_parse_dt(i.get("published_at")) for e in company for i in e.get("items") or []), default=None, key=lambda d: d or datetime.min.replace(tzinfo=timezone.utc))
    flags: list[str] = []
    if newest is not None and (now - newest) > timedelta(days=7):
        confidence *= 0.5
        flags.append("Newest company event is more than 7 days old.")
    notes.append(f"Classifier {sentiment.get('classifier_version')}; sentiment is a heuristic, provisional signal.")
    metrics = [_metric("company_event_sentiment", "Company event sentiment", mean, "(-1..1)", score, f"weighted mean of {len(company)} event groups"),
               _metric("positive_negative_weight", "Positive vs negative weight", (pos - neg) / total, "", 50 + 50 * (pos - neg) / total, "event weights")]
    return _component(key, score=score, confidence=confidence, coverage=min(1.0, len(company) / 6), metrics=metrics,
                      conflicts=conflicts, flags=flags, notes=notes)


# ───────────────────────── Horizons and recommendation ─────────────────────────
def assess_horizons(components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    technical_ok = components["technical"]["status"] == "scored"
    result: dict[str, Any] = {}
    for horizon in HORIZONS:
        weights = HORIZON_WEIGHTS[horizon]
        scored = {k: c for k, c in components.items() if c["status"] == "scored"}
        scored_weight = sum(weights[k] for k in scored)
        blockers: list[str] = []
        if horizon == "intraday" and not INTRADAY_SUPPORTED:
            blockers.append("Intraday scoring needs intraday-bar indicators and a current intraday feed; the research pipeline scores stored daily bars only.")
        for required in REQUIRED_COMPONENTS[horizon]:
            if required not in scored:
                blockers.append(f"{COMPONENT_LABELS[required]} is required for {HORIZON_LABELS[horizon]} and is blocked: {'; '.join(components[required]['blockers'])}")
        if scored_weight < MIN_SCORED_WEIGHT:
            blockers.append(f"Only {scored_weight:.0%} of the {HORIZON_LABELS[horizon]} weight is backed by scored components (minimum {MIN_SCORED_WEIGHT:.0%}).")
        base = {"horizon": horizon, "label": HORIZON_LABELS[horizon], "weights": weights,
                "scored_weight": round(scored_weight, 3), "blockers": blockers}
        if blockers:
            result[horizon] = {**base, "score": None, "recommendation": "INSUFFICIENT_EVIDENCE", "confidence": None,
                               "components_used": [], "components_missing": [k for k in COMPONENTS if k not in scored],
                               "raw_recommendation": None, "gates": [], "contributions": []}
            continue
        # Weights of unscored components are redistributed over scored ones only
        # after the coverage check above; unscored components are never zero/neutral.
        contributions = [{"component": k, "weight": weights[k], "effective_weight": round(weights[k] / scored_weight, 4),
                          "score": scored[k]["score"]} for k in scored]
        score = sum(weights[k] * scored[k]["score"] for k in scored) / scored_weight
        # Unscored components contribute zero confidence, so thin coverage lowers confidence.
        confidence = sum(weights[k] * scored[k]["confidence"] for k in scored)
        raw = "BUY" if score >= BUY_THRESHOLD else "HOLD" if score >= HOLD_THRESHOLD else "AVOID"
        gates: list[dict[str, str]] = []
        if not technical_ok:
            gates.append({"code": "price_not_current", "detail": "Technical data is stale or unavailable, so entry-style output is suppressed (refresh market data)."})
        if confidence < MIN_CONFIDENCE_FOR_BUY:
            gates.append({"code": "low_confidence", "detail": f"Confidence {confidence:.0f} is below {MIN_CONFIDENCE_FOR_BUY:.0f}."})
        news = components["news_sentiment"]
        if news["status"] == "scored" and news["conflicts"] and weights["news_sentiment"] >= 0.20:
            gates.append({"code": "contradictory_evidence", "detail": "News evidence is materially contradictory."})
        fundamental = components["fundamental"]
        if fundamental["status"] == "scored" and fundamental["risk_flags"]:
            gates.append({"code": "balance_sheet_risk", "detail": " ".join(fundamental["risk_flags"])})
        final = "WAIT" if raw == "BUY" and gates else raw
        result[horizon] = {**base, "score": round(score, 1), "recommendation": final, "raw_recommendation": raw,
                           "confidence": round(confidence, 1), "components_used": list(scored),
                           "components_missing": [k for k in COMPONENTS if k not in scored],
                           "gates": gates, "contributions": contributions}
    return result


def build_assessment(sections: dict[str, Any], *, instrument_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    fundamentals = sections["fundamentals"]
    snap_values = (fundamentals.get("snapshot") or {}).get("values") or {}
    sector = (snap_values.get("sector") or {}).get("value")
    industry = (snap_values.get("industry") or {}).get("value")
    components = {
        "fundamental": score_fundamental(fundamentals, sector, industry),
        "technical": score_technical(sections["technicals"]),
        "financial_trends": score_financial_trends(sections["financials_trend"], fundamentals),
        "news_sentiment": score_news_sentiment(sections.get("news_sentiment"), instrument_id, now),
    }
    return {
        "model_version": MODEL_VERSION,
        "validation": {"status": "provisional", "backtested": False,
                       "statement": "Weights, anchors and thresholds are unvalidated hypotheses. Scores are not validated predictions of future returns."},
        "generated_at": now.isoformat(),
        "weights": HORIZON_WEIGHTS,
        "thresholds": {"buy": BUY_THRESHOLD, "hold": HOLD_THRESHOLD, "min_confidence_for_buy": MIN_CONFIDENCE_FOR_BUY,
                       "min_scored_weight": MIN_SCORED_WEIGHT, "min_company_events": MIN_COMPANY_EVENTS},
        "sector": sector, "financial_sector": is_financial_sector(sector, industry),
        "components": components,
        "horizons": assess_horizons(components),
        "not_implemented_gates": NOT_IMPLEMENTED_GATES,
        "default_horizon": "positional",
    }


# ───────────────────────── Session-aware daily-bar staleness ─────────────────────────
def expected_last_completed_session(now: datetime) -> tuple[date | None, str]:
    """Latest NSE session whose daily bar should exist at ``now``, or (None, reason).

    Uses the repository's NSE calendar (only years in NSE_CALENDAR_YEARS). Unverified
    holidays and uncovered years return None so the caller falls back to the age rule.
    """
    local = now.astimezone(IST)
    cutoff = datetime.combine(local.date(), NSE_FRESHNESS_SESSION_END, tzinfo=IST) + EOD_PUBLICATION_GRACE
    day = local.date() if local >= cutoff else local.date() - timedelta(days=1)
    for _ in range(14):
        if day.year not in NSE_CALENDAR_YEARS:
            return None, "calendar_not_covered"
        if day in NSE_UNVERIFIED_HOLIDAYS:
            return None, "holiday_unverified"
        if day.weekday() >= 5 or day in NSE_EQUITY_HOLIDAYS:
            day -= timedelta(days=1)
            continue
        return day, "nse_session_calendar"
    return None, "calendar_not_covered"


def daily_bar_staleness(latest_bar_timestamp: datetime, now: datetime, *, country: str | None,
                        continuous: bool, age_limit_seconds: int) -> dict[str, Any]:
    """Classify the latest stored 1d bar. Stored daily keys are UTC midnight of the exchange-local session date."""
    ts = latest_bar_timestamp if latest_bar_timestamp.tzinfo else latest_bar_timestamp.replace(tzinfo=timezone.utc)
    latest_date = ts.astimezone(timezone.utc).date()
    if (country or "").upper() == "IN" and not continuous:
        expected, basis = expected_last_completed_session(now)
        if expected is not None:
            if latest_date > now.astimezone(IST).date():
                return {"stale": True, "basis": basis, "latest_session": latest_date.isoformat(), "expected_session": expected.isoformat(),
                        "reason": f"Latest stored daily bar {latest_date} is dated in the future."}
            stale = latest_date < expected
            return {"stale": stale, "basis": basis, "latest_session": latest_date.isoformat(), "expected_session": expected.isoformat(),
                    "reason": (f"Latest stored daily bar is {latest_date}; the last completed NSE session is {expected} "
                               f"(bars missing since). The history refresh did not store it.") if stale else None}
    age = (now - ts).total_seconds()
    stale = age < 0 or age > age_limit_seconds
    return {"stale": stale, "basis": "age_limit", "latest_session": latest_date.isoformat(), "expected_session": None,
            "reason": f"The latest daily close exceeds the configured freshness limit of {age_limit_seconds} seconds." if stale else None}
