"""Evidence-aware, versioned sentiment over persisted news metadata."""
from __future__ import annotations

import hashlib
from html import unescape
from html.parser import HTMLParser
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Instrument, InstrumentSnapshot, NewsItem, NewsSentimentAnalysis, NewsSentimentFailure, NewsSourcePoll

_SUMMARY_LIMIT = 4000
_EVENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quarterly_results", (r"quarterly results?", r"q[1-4] results?", r"earnings? (?:results?|report)", r"results? for (?:the )?(?:quarter|q[1-4])")),
    ("revenue_profit_margin", (r"revenue", r"profit", r"margin", r"earnings per share", r"eps")),
    ("order_contract", (r"\b(?:wins?|secures?|bags?) (?:a |an )?(?:\w+ ){0,2}(?:order|contract)\b", r"new (?:order|contract|business win)", r"purchase order")),
    ("merger_acquisition_partnership", (r"merger", r"acqui(?:re|sition|res)", r"takeover", r"partnership", r"joint venture")),
    ("management_change", (r"appoint(?:s|ed|ment)?", r"resign(?:s|ed|ation)?", r"steps down", r"ceo", r"managing director", r"board changes?")),
    ("macroeconomic", (r"inflation", r"interest rate", r"repo rate", r"gdp", r"economic growth", r"budget deficit", r"monetary policy")),
    ("regulatory_government", (r"regulat(?:or|ory)", r"government (?:order|policy|approval|scheme)", r"sebi", r"rbi", r"ministry", r"compliance")),
    ("legal_investigation", (r"lawsuit", r"court case", r"investigat(?:ion|es|ed)", r"probe", r"fraud", r"penalt(?:y|ies)", r"legal action")),
    ("fundraising_debt_capex", (r"fundrais(?:e|es|ing)", r"raises? (?:funds|capital|debt)", r"debt", r"capex", r"capital expenditure", r"rights issue", r"qip")),
    ("dividend_buyback_corporate_action", (r"dividend", r"buyback", r"stock split", r"bonus issue", r"corporate action", r"record date")),
    ("product_launch_expansion", (r"launch(?:es|ed)? (?:a |an )?(?:new )?product", r"new (?:product|service) launch", r"expan(?:d|ds|ded|sion)", r"new facility", r"capacity addition", r"enters? (?:a |the )?market")),
    ("sector_development", (r"sector", r"industry-wide", r"across (?:the )?(?:banking|pharma|auto|technology|energy) industry")),
    ("global_market", (r"global markets?", r"wall street", r"u\.s\. markets?", r"oil prices?", r"global trade", r"foreign markets?")),
    ("geopolitical", (r"geopolitic", r"war in", r"sanctions", r"tariffs?", r"trade war", r"ceasefire", r"military conflict")),
)
_POSITIVE_CUES: tuple[tuple[str, float], ...] = (
    (r"\b(?:beats?|beat|exceeds?|exceeded|outperforms?) expectations\b", 1.0),
    (r"\b(?:beats?|beat) (?:analyst )?estimates?\b", 0.9),
    (r"\bprofit (?:rose|surged|grew|increased|soared)\b", 0.9),
    (r"\b(?:revenue|sales|earnings) (?:rose|grew|increased|surged)\b", 0.8),
    (r"\b(?:record|all-time high) (?:profit|revenue|earnings|sales)\b", 0.8),
    (r"\b(?:wins?|secures?|bags?) (?:a |an )?(?:\w+ ){0,2}(?:order|contract)\b", 0.7),
    (r"\bdebt (?:repaid|reduced|cut)\b", 0.7),
    (r"\b(?:upgrade|upgraded|raises? guidance|guidance raised)\b", 0.7),
)
_NEGATIVE_CUES: tuple[tuple[str, float], ...] = (
    (r"\b(?:misses?|missed|falls? short of) expectations\b", 1.0),
    (r"\b(?:misses?|missed|falls? short of) (?:analyst )?estimates?\b", 0.95),
    (r"\bprofit (?:fell|declined|dropped|slumped|plunged)\b", 0.9),
    (r"\b(?:revenue|sales|earnings) (?:fell|declined|dropped|slumped)\b", 0.8),
    (r"\bloss(?:es)? (?:widened|increased|surged)\b", 0.9),
    (r"\b(?:penalty|fine|default|bankruptcy|fraud probe|accounting fraud)\b", 0.9),
    (r"\b(?:downgrade|downgraded|cuts? guidance|guidance cut)\b", 0.7),
    (r"\b(?:suspends?|halts?|recalls?) (?:operations?|production|product|license)\b", 0.7),
)
_SOURCE_CREDIBILITY = {"nse": 1.0, "gdelt": 0.72, "google_news": 0.62, "yfinance": 0.68}
_HIGH_IMPACT_EVENTS = {"quarterly_results", "merger_acquisition_partnership", "regulatory_government", "legal_investigation", "macroeconomic", "geopolitical"}
_MEDIUM_IMPACT_EVENTS = {"revenue_profit_margin", "order_contract", "fundraising_debt_capex", "dividend_buyback_corporate_action", "product_launch_expansion", "sector_development", "global_market", "management_change"}

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    _VADER = SentimentIntensityAnalyzer()
except ImportError:  # A conservative deterministic fallback remains available offline.
    _VADER = None

CLASSIFIER_VERSION = "hybrid-v1.2-vader" if _VADER is not None else "hybrid-v1.2-rules"


class _TextContent(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _clean_summary(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextContent()
    try:
        parser.feed(value)
        content = " ".join(parser.parts)
    except Exception:
        content = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", unescape(content)).strip()[:_SUMMARY_LIMIT]


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _event_category(text: str) -> str:
    for category, patterns in _EVENT_PATTERNS:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return category
    return "unknown"


def _cue_score(text: str, patterns: tuple[tuple[str, float], ...]) -> tuple[float, list[str]]:
    matches: list[tuple[float, str]] = []
    for pattern, weight in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            phrase = match.group(0)
            prefix = text[max(0, match.start() - 18):match.start()]
            if re.search(r"\b(?:not|no|never|fails? to|failed to)\s*$", prefix, re.IGNORECASE):
                continue
            matches.append((weight, phrase))
    return (max((weight for weight, _ in matches), default=0.0), [phrase for _, phrase in matches[:3]])


def _event_relevance(item: NewsItem, category: str) -> str:
    if item.instrument_id:
        return "company"
    payload = item.raw_payload or {}
    if item.symbol_raw or payload.get("raw_symbols") or payload.get("company_refs"):
        return "unknown"
    if category == "geopolitical" or category == "global_market":
        return "global"
    if category == "macroeconomic":
        return "macro"
    if category == "sector_development":
        return "sector"
    company_events = {"quarterly_results", "revenue_profit_margin", "order_contract",
                      "merger_acquisition_partnership", "management_change", "regulatory_government",
                      "legal_investigation", "fundraising_debt_capex",
                      "dividend_buyback_corporate_action", "product_launch_expansion"}
    if category in company_events:
        return "unknown"
    text = f"{item.headline} {item.body or ''}"
    if item.source in {"google_news", "gdelt"} and re.search(
        r"\b(?:indian stock market|india(?:n)? markets?|sensex|nifty|nse index|bse index|market-wide)\b",
        text, re.IGNORECASE,
    ):
        return "market"
    return "unknown"


def _event_key(item: NewsItem) -> str:
    stored = (item.raw_payload or {}).get("story_key")
    if isinstance(stored, str) and re.fullmatch(r"[a-f0-9]{64}", stored):
        return stored
    title = re.sub(r"[^a-z0-9]+", " ", item.headline.casefold()).strip()
    stamp = _aware_utc(item.published_at).date().isoformat()
    return hashlib.sha256(f"{stamp}:{title}".encode()).hexdigest()


def classify_news_item(
    item: NewsItem,
    *,
    now: datetime | None = None,
    vader: Callable[[str], float] | None = None,
) -> dict[str, Any]:
    """Classify stored headline/summary only; no article fetch or number generation."""
    now = _aware_utc(now or datetime.now(timezone.utc))
    headline = item.headline.strip()
    summary = _clean_summary(item.body)
    text = " ".join(part for part in (headline, summary) if part)
    category = _event_category(text)
    positive, positive_phrases = _cue_score(text, _POSITIVE_CUES)
    negative, negative_phrases = _cue_score(text, _NEGATIVE_CUES)
    analyzer = vader
    if analyzer is None and _VADER is not None:
        analyzer = lambda value: float(_VADER.polarity_scores(value)["compound"])
    model_score = analyzer(text) if analyzer else None
    model_score = max(-1.0, min(1.0, model_score)) if model_score is not None else None
    rule_score = positive - negative
    if positive and negative:
        score = (0.45 * (positive - negative)) + (0.55 * (model_score or 0.0))
        label = "mixed"
    elif rule_score:
        cue_direction = 1.0 if rule_score > 0 else -1.0
        score = cue_direction * positive * 0.38 if rule_score > 0 else cue_direction * negative * 0.38
        if model_score is not None:
            score = 0.62 * model_score + 0.38 * (cue_direction * max(positive, negative))
            if model_score * rule_score < -0.12:
                label = "mixed"
            else:
                label = "positive" if score >= 0.12 else "negative" if score <= -0.12 else "neutral"
        else:
            label = "positive" if score >= 0.12 else "negative" if score <= -0.12 else "neutral"
    else:
        score = (model_score or 0.0) * 0.65
        label = "positive" if score >= 0.20 else "negative" if score <= -0.20 else "neutral"
    score = max(-1.0, min(1.0, score))
    direction_uncertain = label in {"neutral", "mixed"} or (model_score is None and not rule_score)
    evidence: list[dict[str, str]] = []
    for field, field_text, phrases in (("headline", headline, positive_phrases + negative_phrases),
                                        ("summary", summary, positive_phrases + negative_phrases)):
        if field_text:
            phrase = next((p for p in phrases if p.casefold() in field_text.casefold()), None)
            excerpt = phrase or field_text[:500]
            evidence.append({"field": field, "text": excerpt})
    confidence = 25
    if category != "unknown":
        confidence += 8
    if positive or negative:
        confidence += 9
    if summary:
        confidence += 7
    if model_score is not None and (not rule_score or model_score * rule_score > 0):
        confidence += 14
    elif model_score is not None and rule_score and model_score * rule_score < 0:
        confidence -= 12
    confidence += round(6 * _SOURCE_CREDIBILITY.get(item.source, 0.45))
    if label == "mixed":
        confidence -= 4
    if model_score is None:
        confidence = min(confidence, 58)
    confidence = max(0, min(100, confidence))
    if confidence < 55:
        direction_uncertain = True
    event_impact = "high" if category in _HIGH_IMPACT_EVENTS else "medium" if category in _MEDIUM_IMPACT_EVENTS else "low"
    if re.search(r"\b(?:₹|inr|\d+(?:\.\d+)?\s*%)\b", text, re.IGNORECASE) and category != "unknown":
        event_impact = "high"
    age_hours = max(0.0, (now - _aware_utc(item.published_at)).total_seconds() / 3600)
    explanation = f"Classified as {category.replace('_', ' ')} using the stored headline and {'feed summary' if summary else 'headline only'}; "
    if label == "mixed":
        explanation += "positive and negative cues conflict, so direction remains uncertain."
    elif direction_uncertain:
        explanation += "direction is uncertain because the available wording provides insufficient corroboration."
    elif model_score is None:
        explanation += "directional wording is present, but the local NLP model is unavailable; confidence is capped."
    else:
        explanation += "the financial context rules and local VADER signal were combined conservatively."
    if confidence < 55 and label not in {"neutral", "mixed"}:
        explanation += " Overall direction remains uncertain because evidence confidence is limited."
    raw_payload = item.raw_payload or {}
    text_basis = "headline_and_article" if raw_payload.get("full_text_status") == "available" else "headline_and_summary" if summary else "headline_only"
    return {
        "sentiment": label,
        "sentiment_score": round(score, 4),
        "strength": round(abs(score), 4),
        "impact": event_impact,
        "confidence_score": confidence,
        "event_category": category,
        "relevance_scope": _event_relevance(item, category),
        "interpretation": "uncertain" if direction_uncertain else "bullish" if label == "positive" else "bearish",
        "explanation": explanation,
        "evidence": evidence,
        "event_key": _event_key(item),
        "input_hash": hashlib.sha256(f"{item.source}\0{headline}\0{summary}\0{item.published_at.isoformat()}".encode()).hexdigest(),
        "text_basis": text_basis,
        "freshness_hours": round(age_hours, 2),
        "model_available": model_score is not None,
    }


def analyze_news_items(
    db: Session,
    *,
    limit: int = 500,
    period_days: int = 30,
    now: datetime | None = None,
    vader: Callable[[str], float] | None = None,
) -> dict[str, int]:
    now = _aware_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=period_days)
    if db.get_bind().dialect.name == "postgresql":
        locked = db.scalar(select(func.pg_try_advisory_xact_lock(73104291)))
        if not locked:
            return {"examined": 0, "analyzed": 0, "already_analyzed": 0, "failed": 0, "skipped_locked": 1}
    already_analyzed = int(db.scalar(select(func.count()).select_from(NewsSentimentAnalysis)
        .join(NewsItem, NewsItem.id == NewsSentimentAnalysis.news_item_id)
        .where(NewsSentimentAnalysis.classifier_version == CLASSIFIER_VERSION,
               NewsItem.published_at >= cutoff)) or 0)
    successful_ids = select(NewsSentimentAnalysis.news_item_id).where(
        NewsSentimentAnalysis.classifier_version == CLASSIFIER_VERSION)
    failed_rows = db.scalars(select(NewsSentimentFailure).where(
        NewsSentimentFailure.classifier_version == CLASSIFIER_VERSION,
        NewsSentimentFailure.news_item_id.in_(select(NewsItem.id).where(NewsItem.published_at >= cutoff)),
    )).all()
    failure_by_id = {failure.news_item_id: failure for failure in failed_rows}
    retryable_ids = [failure.news_item_id for failure in failed_rows if _aware_utc(failure.retry_after) <= now]
    pending_query = select(NewsItem).where(NewsItem.published_at >= cutoff, ~NewsItem.id.in_(successful_ids))
    if failure_by_id:
        pending_query = pending_query.where(
            (NewsItem.id.not_in(failure_by_id.keys())) | NewsItem.id.in_(retryable_ids)
        )
    items = db.scalars(pending_query.order_by(NewsItem.published_at.desc()).limit(limit)).all()
    records = []
    failed = 0
    for item in items:
        try:
            result = classify_news_item(item, now=now, vader=vader)
        except Exception as exc:
            failed += 1
            failure = failure_by_id.get(item.id)
            if failure is None:
                db.add(NewsSentimentFailure(news_item_id=item.id, classifier_version=CLASSIFIER_VERSION,
                    failed_at=now, retry_after=now + timedelta(hours=1),
                    error_category=type(exc).__name__[:64], attempts=1))
            else:
                failure.failed_at = now
                failure.retry_after = now + timedelta(hours=1)
                failure.error_category = type(exc).__name__[:64]
                failure.attempts += 1
            continue
        records.append(NewsSentimentAnalysis(
            news_item_id=item.id, classifier_version=CLASSIFIER_VERSION, input_hash=result["input_hash"],
            analyzed_at=now, sentiment=result["sentiment"], sentiment_score=Decimal(str(result["sentiment_score"])),
            strength=Decimal(str(result["strength"])), impact=result["impact"],
            confidence_score=result["confidence_score"], event_category=result["event_category"],
            relevance_scope=result["relevance_scope"], interpretation=result["interpretation"],
            explanation=result["explanation"], evidence=result["evidence"], event_key=result["event_key"],
        ))
        failure = failure_by_id.get(item.id)
        if failure is not None:
            db.delete(failure)
    if records:
        db.add_all(records)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = set(db.scalars(select(NewsSentimentAnalysis.news_item_id).where(
                NewsSentimentAnalysis.classifier_version == CLASSIFIER_VERSION,
                NewsSentimentAnalysis.news_item_id.in_([record.news_item_id for record in records]),
            )).all())
            records = [record for record in records if record.news_item_id not in existing]
            if records:
                db.add_all(records)
                db.commit()
    else:
        db.commit()
    return {"examined": len(items), "analyzed": len(records), "already_analyzed": already_analyzed,
            "failed": failed, "skipped_locked": 0}


def _source_coverage(db: Session, now: datetime) -> list[dict[str, Any]]:
    rows = db.scalars(select(NewsSourcePoll).order_by(NewsSourcePoll.source)).all()
    coverage = []
    for row in rows:
        attempted = _aware_utc(row.last_attempted_at)
        successful = _aware_utc(row.last_successful_at) if row.last_successful_at else None
        if successful is None:
            status = "unavailable" if row.last_error_category or row.rate_limited else "not_polled"
        elif row.rate_limited or row.last_error_category:
            status = "partial"
        elif now - successful > timedelta(hours=24):
            status = "stale"
        elif row.articles_discovered == 0:
            status = "successful_empty"
        else:
            status = "available"
        coverage.append({"source": row.source, "status": status, "last_attempted_at": attempted,
                         "last_successful_poll_at": successful, "last_successful_article_at": row.last_article_at,
                         "rate_limited": row.rate_limited, "last_error_category": row.last_error_category})
    return coverage


def analysis_state(db: Session, *, period_days: int = 30, now: datetime | None = None) -> dict[str, int]:
    now = _aware_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=period_days)
    total = int(db.scalar(select(func.count()).select_from(NewsItem).where(NewsItem.published_at >= cutoff)) or 0)
    analyzed = int(db.scalar(select(func.count()).select_from(NewsSentimentAnalysis)
        .join(NewsItem, NewsItem.id == NewsSentimentAnalysis.news_item_id)
        .where(NewsSentimentAnalysis.classifier_version == CLASSIFIER_VERSION,
               NewsItem.published_at >= cutoff)) or 0)
    failed = int(db.scalar(select(func.count()).select_from(NewsSentimentFailure)
        .join(NewsItem, NewsItem.id == NewsSentimentFailure.news_item_id)
        .where(NewsSentimentFailure.classifier_version == CLASSIFIER_VERSION,
               NewsItem.published_at >= cutoff)) or 0)
    return {"available_items": total, "analyzed_items": analyzed, "pending_items": max(0, total - analyzed - failed),
            "failed_items": failed}


def _load_analysis_rows(db: Session, cutoff: datetime) -> list[tuple[NewsItem, NewsSentimentAnalysis]]:
    return list(db.execute(select(NewsItem, NewsSentimentAnalysis)
        .join(NewsSentimentAnalysis, NewsSentimentAnalysis.news_item_id == NewsItem.id)
        .where(NewsSentimentAnalysis.classifier_version == CLASSIFIER_VERSION,
               NewsItem.published_at >= cutoff)
        .order_by(NewsItem.published_at.desc())).all())


def _item_response(item: NewsItem, analysis: NewsSentimentAnalysis) -> dict[str, Any]:
    payload = item.raw_payload or {}
    return {"news_item_id": str(item.id), "source": item.source, "publisher": item.publisher,
            "url": item.source_url, "headline": item.headline, "summary": _clean_summary(item.body) or None,
            "published_at": _aware_utc(item.published_at), "instrument_id": str(item.instrument_id) if item.instrument_id else None,
            "symbol_raw": item.symbol_raw, "category": item.category, "full_text_status": payload.get("full_text_status", "unavailable"),
            "sentiment": analysis.sentiment, "score": float(analysis.sentiment_score),
            "strength": float(analysis.strength), "impact": analysis.impact,
            "confidence_score": analysis.confidence_score, "event_category": analysis.event_category,
            "relevance_scope": analysis.relevance_scope, "interpretation": analysis.interpretation,
            "explanation": analysis.explanation, "evidence": analysis.evidence,
            "event_key": analysis.event_key, "classifier_version": analysis.classifier_version,
            "analyzed_at": _aware_utc(analysis.analyzed_at)}


def _base_weight(item: NewsItem, analysis: NewsSentimentAnalysis, now: datetime) -> float:
    age_days = max(0.0, (now - _aware_utc(item.published_at)).total_seconds() / 86400)
    freshness = math.exp(-age_days / 7.0)
    relevance = {"company": 1.0, "sector": 0.9, "market": 0.85, "macro": 0.8, "global": 0.75, "unknown": 0.45}.get(analysis.relevance_scope, 0.45)
    importance = {"high": 1.0, "medium": 0.75, "low": 0.5}.get(analysis.impact, 0.5)
    return max(0.0001, (analysis.confidence_score / 100) * _SOURCE_CREDIBILITY.get(item.source, 0.45) * freshness * relevance * importance)


def _aggregate_groups(rows: list[tuple[NewsItem, NewsSentimentAnalysis]], now: datetime) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[NewsItem, NewsSentimentAnalysis]]] = defaultdict(list)
    for item, analysis in rows:
        grouped[analysis.event_key].append((item, analysis))
    result = []
    for key, members in grouped.items():
        weights = [_base_weight(item, analysis, now) for item, analysis in members]
        total = sum(weights)
        score = sum(float(analysis.sentiment_score) * weight for (_, analysis), weight in zip(members, weights)) / total
        labels = {analysis.sentiment for _, analysis in members}
        if "positive" in labels and "negative" in labels:
            label = "mixed"
        elif "mixed" in labels:
            label = "mixed"
        elif score >= 0.12:
            label = "positive"
        elif score <= -0.12:
            label = "negative"
        else:
            label = "neutral"
        exemplar = max(members, key=lambda pair: _base_weight(pair[0], pair[1], now))
        confidence = round(sum(analysis.confidence_score * weight for (_, analysis), weight in zip(members, weights)) / total)
        # Duplicate coverage strengthens attribution, never event count or event weight.
        event_weight = max(weights)
        result.append({"event_key": key, "sentiment": label, "score": round(score, 4),
            "confidence_score": confidence, "impact": exemplar[1].impact,
            "event_category": exemplar[1].event_category, "relevance_scope": exemplar[1].relevance_scope,
            "event_weight": event_weight, "contradictory_sources": "positive" in labels and "negative" in labels,
            "news_item_ids": [str(item.id) for item, _ in members],
            "sources": sorted({item.source for item, _ in members}),
            "items": [_item_response(item, analysis) for item, analysis in members]})
    return sorted(result, key=lambda group: (group["event_weight"], group["confidence_score"]), reverse=True)


def _build_aggregate(
    db: Session,
    rows: list[tuple[NewsItem, NewsSentimentAnalysis]],
    *,
    scope: str,
    scope_id: str | None,
    now: datetime,
    period_days: int,
    unanalysed_count: int = 0,
) -> dict[str, Any]:
    coverage = _source_coverage(db, now)
    analysis_counts = analysis_state(db, period_days=period_days, now=now)
    groups = _aggregate_groups(rows, now)
    counts = {label: sum(group["sentiment"] == label for group in groups)
              for label in ("positive", "negative", "neutral", "mixed")}
    coverage_points = sum({"available": 1.0, "successful_empty": 1.0, "partial": 0.5,
                           "stale": 0.5}.get(row["status"], 0.0) for row in coverage)
    coverage_factor = max(0.25, coverage_points / max(1, len(coverage))) if coverage else 0.5
    if groups:
        total_weight = sum(group["event_weight"] for group in groups)
        score = sum(group["score"] * group["event_weight"] for group in groups) / total_weight
        positive_weight = sum(g["event_weight"] for g in groups if g["sentiment"] == "positive")
        negative_weight = sum(g["event_weight"] for g in groups if g["sentiment"] == "negative")
        contradictory = positive_weight > 0 and negative_weight > 0
        sentiment = "mixed" if counts["mixed"] or (positive_weight / total_weight >= 0.25 and negative_weight / total_weight >= 0.25) else "positive" if score >= 0.12 else "negative" if score <= -0.12 else "neutral"
        raw_confidence = sum(g["confidence_score"] * g["event_weight"] for g in groups) / total_weight
        event_coverage_factor = min(1.0, math.sqrt(len(groups) / 4.0))
        confidence = round(raw_confidence * coverage_factor * event_coverage_factor)
        articles = sum(len(g["items"]) for g in groups)
        newest = max(_aware_utc(item.published_at) for item, _ in rows)
        age_hours = max(0.0, (now - newest).total_seconds() / 3600)
        status = "stale" if age_hours > min(24 * 7, period_days * 24) else "available"
        score_result: float | None = round(score, 4)
        strength: float | None = round(abs(score), 4)
        confidence_result: int | None = max(0, min(100, confidence))
    else:
        articles = 0
        newest = None
        age_hours = None
        contradictory = False
        score_result = strength = None
        confidence_result = None
        if analysis_counts["pending_items"]:
            status = "not_analyzed"
        elif analysis_counts["failed_items"]:
            status = "analysis_failed"
        elif not coverage or not any(row["status"] in {"available", "successful_empty"} for row in coverage):
            status = "source_unavailable"
        else:
            status = "no_news_found"
        sentiment = None
    return {"status": status, "scope": scope, "scope_id": scope_id, "sentiment": sentiment,
        "interpretation": ("uncertain" if confidence_result is not None and confidence_result < 55 else sentiment),
        "score": score_result, "strength": strength, "confidence_score": confidence_result,
        "event_count": len(groups), "article_count": articles, "counts": counts,
        "contradictory_evidence": contradictory,
        "freshness": {"latest_published_at": newest, "age_hours": round(age_hours, 2) if age_hours is not None else None,
                      "is_stale": bool(age_hours is not None and age_hours > 24 * 7)},
        "coverage": {"period_days": period_days, "analyzed_events": len(groups),
                     "unanalysed_news_items": unanalysed_count, "available_source_fraction": round(coverage_factor, 3),
                     "analysis": analysis_counts, "sources": coverage},
        "aggregation": {"method": "confidence × source credibility × 7-day freshness decay × relevance × event importance; one weight per event group",
                        "classifier_version": CLASSIFIER_VERSION},
        "events": groups[:25]}


def aggregate_market_sentiment(db: Session, *, period_days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = _aware_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=period_days)
    rows = _load_analysis_rows(db, cutoff)
    instrument_ids = [item.instrument_id for item, _ in rows if item.instrument_id]
    instruments = {instrument.id: instrument for instrument in db.scalars(select(Instrument).where(
        Instrument.id.in_(set(instrument_ids)) if instrument_ids else False)).all()}
    filtered = [(item, analysis) for item, analysis in rows if
        (item.instrument_id is None and analysis.relevance_scope in {"market", "macro", "global", "sector"}) or
        (item.instrument_id is not None and instruments.get(item.instrument_id) is not None and instruments[item.instrument_id].country == "IN")]
    candidates = db.scalars(select(NewsItem).where(NewsItem.published_at >= cutoff)).all()
    analyzed_ids = {item.id for item, _ in rows}
    unanalysed = sum(item.id not in analyzed_ids for item in candidates)
    return _build_aggregate(db, filtered, scope="market", scope_id="IN", now=now, period_days=period_days, unanalysed_count=unanalysed)


def aggregate_instrument_sentiment(db: Session, instrument_id, *, period_days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = _aware_utc(now or datetime.now(timezone.utc))
    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        return _build_aggregate(db, [], scope="company", scope_id=str(instrument_id), now=now,
                                period_days=period_days, unanalysed_count=0)
    cutoff = now - timedelta(days=period_days)
    rows = _load_analysis_rows(db, cutoff)
    snapshot = db.scalar(select(InstrumentSnapshot).where(InstrumentSnapshot.instrument_id == instrument_id))
    sector = snapshot.sector if snapshot else None
    filtered = [(item, analysis) for item, analysis in rows if item.instrument_id == instrument_id or
        (item.instrument_id is None and analysis.relevance_scope in {"market", "macro", "global"})]
    candidate_items = db.scalars(select(NewsItem).where(NewsItem.published_at >= cutoff,
                                         NewsItem.instrument_id == instrument_id)).all()
    analyzed_ids = {item.id for item, _ in rows}
    return _build_aggregate(db, filtered, scope="company", scope_id=str(instrument_id), now=now,
        period_days=period_days, unanalysed_count=sum(item.id not in analyzed_ids for item in candidate_items)) | {
            "instrument": {"id": str(instrument.id), "symbol": instrument.symbol, "name": instrument.display_name,
                           "sector": sector, "industry": snapshot.industry if snapshot else None}}


def aggregate_sector_sentiment(db: Session, sector: str, *, period_days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = _aware_utc(now or datetime.now(timezone.utc))
    instrument_ids = set(db.scalars(select(InstrumentSnapshot.instrument_id).where(
        InstrumentSnapshot.sector.ilike(sector))).all())
    cutoff = now - timedelta(days=period_days)
    rows = _load_analysis_rows(db, cutoff)
    filtered = [(item, analysis) for item, analysis in rows if item.instrument_id in instrument_ids]
    candidate_items = db.scalars(select(NewsItem).where(NewsItem.published_at >= cutoff,
        NewsItem.instrument_id.in_(instrument_ids) if instrument_ids else False)).all()
    analyzed_ids = {item.id for item, _ in rows}
    return _build_aggregate(db, filtered, scope="sector", scope_id=sector, now=now,
        period_days=period_days, unanalysed_count=sum(item.id not in analyzed_ids for item in candidate_items))


def list_sector_sentiment(db: Session, *, period_days: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
    sectors = db.scalars(select(InstrumentSnapshot.sector).where(InstrumentSnapshot.sector.is_not(None)).distinct().order_by(InstrumentSnapshot.sector)).all()
    return [aggregate_sector_sentiment(db, sector, period_days=period_days, now=now) for sector in sectors]


def list_instrument_sentiment(db: Session, *, period_days: int = 7, limit: int = 100, now: datetime | None = None) -> list[dict[str, Any]]:
    now = _aware_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=period_days)
    ids = db.scalars(select(NewsItem.instrument_id).join(NewsSentimentAnalysis,
        NewsSentimentAnalysis.news_item_id == NewsItem.id).where(
            NewsItem.instrument_id.is_not(None), NewsItem.published_at >= cutoff,
            NewsSentimentAnalysis.classifier_version == CLASSIFIER_VERSION).distinct().limit(limit)).all()
    return [aggregate_instrument_sentiment(db, instrument_id, period_days=period_days, now=now) for instrument_id in ids]


def sentiment_history(db: Session, *, period_days: int = 90, instrument_id=None,
                      now: datetime | None = None) -> list[dict[str, Any]]:
    now = _aware_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=period_days)
    rows = _load_analysis_rows(db, cutoff)
    if instrument_id:
        rows = [(item, analysis) for item, analysis in rows if item.instrument_id == instrument_id]
    else:
        instrument_ids = {item.instrument_id for item, _ in rows if item.instrument_id}
        instruments = {instrument.id: instrument for instrument in db.scalars(select(Instrument).where(
            Instrument.id.in_(instrument_ids) if instrument_ids else False)).all()}
        rows = [(item, analysis) for item, analysis in rows if
            (item.instrument_id is None and analysis.relevance_scope in {"market", "macro", "global", "sector"}) or
            (item.instrument_id is not None and instruments.get(item.instrument_id) is not None and instruments[item.instrument_id].country == "IN")]
    by_day: dict[str, list[tuple[NewsItem, NewsSentimentAnalysis]]] = defaultdict(list)
    for pair in rows:
        by_day[_aware_utc(pair[0].published_at).date().isoformat()].append(pair)
    points = []
    for day, day_rows in sorted(by_day.items()):
        groups = _aggregate_groups(day_rows, now)
        total = sum(group["event_weight"] for group in groups)
        score = sum(group["score"] * group["event_weight"] for group in groups) / total if total else 0.0
        points.append({"date": day, "score": round(score, 4), "event_count": len(groups),
                       "article_count": sum(len(group["items"]) for group in groups)})
    return points


def recent_sentiment(db: Session, *, period_days: int = 30, limit: int = 50,
                     instrument_id=None) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
    stmt = select(NewsItem, NewsSentimentAnalysis).join(
        NewsSentimentAnalysis, NewsSentimentAnalysis.news_item_id == NewsItem.id
    ).where(NewsSentimentAnalysis.classifier_version == CLASSIFIER_VERSION,
            NewsItem.published_at >= cutoff)
    if instrument_id:
        stmt = stmt.where(NewsItem.instrument_id == instrument_id)
    rows = db.execute(stmt.order_by(NewsItem.published_at.desc()).limit(limit)).all()
    return [_item_response(item, analysis) for item, analysis in rows]


def levels_sentiment_context(db: Session, instrument_id, *, period_days: int = 7) -> dict[str, Any]:
    aggregate = aggregate_instrument_sentiment(db, instrument_id, period_days=period_days)
    items = [item for event in aggregate["events"] for item in event["items"]]
    direction = aggregate["sentiment"]
    if direction is not None and (aggregate["confidence_score"] or 0) < 55:
        direction = "uncertain"
    return {
        "interface_version": "news-sentiment-context-v1",
        "instrument_id": str(instrument_id),
        "direction": direction,
        "sentiment_score": aggregate["score"],
        "confidence": aggregate["confidence_score"],
        "event_importance": max((event["impact"] for event in aggregate["events"]),
                                 key=lambda impact: {"low": 0, "medium": 1, "high": 2}.get(impact, 0), default=None),
        "freshness": aggregate["freshness"],
        "supporting_news_item_ids": list(dict.fromkeys(item["news_item_id"] for item in items)),
        "source_attribution": [{"news_item_id": item["news_item_id"], "source": item["source"],
                                "publisher": item["publisher"], "url": item["url"]} for item in items],
        "availability_status": aggregate["status"],
        "contradictory_evidence": aggregate["contradictory_evidence"],
    }
