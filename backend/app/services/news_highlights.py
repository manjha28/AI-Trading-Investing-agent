"""Deterministic, rule-based highlights from stored news: what kind of event it is, and the amounts / percentages it quotes.

Nothing is generated: tags come from keyword rules, and every fact is a verbatim substring of the stored headline or summary (with its
character span so the UI can mark it). Works with AI_ENABLED=false. Articles with no rule match are still listed, untagged, as "other" -
absence of a tag is not a statement that the article is unimportant.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# (tag, label, weight, pattern). Weight orders importance; first matching tags are all kept, weight is the max of those matched.
_RULES: list[tuple[str, str, int, re.Pattern[str]]] = [(t, label, w, re.compile(p, re.I)) for t, label, w, p in [
    ("results", "Results", 5, r"\b(q[1-4]\s?(fy)?\d{0,4}|quarterly results?|results?|net profit|net loss|profit after tax|\bpat\b|ebitda|revenue|net sales|earnings)\b"),
    ("order", "Order / contract", 5, r"\b(order win|bags?|wins?|secures?|bagged|awarded|contract|letter of award|\bloa\b|orders? worth)\b"),
    ("rating", "Rating / target", 4, r"\b(upgrades?|downgrades?|target price|price target|initiates? coverage|buy rating|sell rating|overweight|underweight|outperform|underperform|reiterates?)\b"),
    ("corp_action", "Dividend / buyback", 4, r"\b(dividend|buyback|buy-back|bonus (issue|shares)|stock split|record date)\b"),
    ("deal", "M&A / stake", 5, r"\b(acquires?|acquisition|merger|demerger|amalgamation|takeover|stake (sale|buy)|block deal|bulk deal|open offer|divest)\b"),
    ("regulatory", "Regulatory / legal", 5, r"\b(sebi|nclt|penalty|fine[sd]?|probe|investigation|fraud|raid|show[- ]cause|tax demand|court|lawsuit|insolvency|default)\b"),
    ("management", "Management", 3, r"\b(resigns?|appoints?|steps down|ceo|cfo|managing director|chairman|promoter (pledge|stake))\b"),
    ("capital", "Fund-raising", 3, r"\b(qip|rights issue|preferential (issue|allotment)|ncd|fund[- ]?raise|raises?|ipo|fpo|debenture)\b"),
    ("expansion", "Capex / launch", 3, r"\b(capex|capacity|expansion|plant|launch(es|ed)?|new facility|commission(s|ed)?)\b"),
    ("guidance", "Guidance / outlook", 3, r"\b(guidance|outlook|forecast|expects?|projects?)\b"),
]]
_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)\s?\d[\d,]*(?:\.\d+)?\s?(?:crore|cr|lakh|lakhs|bn|billion|mn|million|trillion|tn|k)?\b", re.I)
_PERCENT = re.compile(r"[-+]?\d+(?:\.\d+)?\s?(?:%|per ?cent)", re.I)
_MAX_ITEMS = 8


def _span_facts(text: str) -> list[dict[str, Any]]:
    facts = [{"kind": "amount", "text": m.group(0).strip(), "start": m.start(), "end": m.end()} for m in _AMOUNT.finditer(text)]
    facts += [{"kind": "percent", "text": m.group(0).strip(), "start": m.start(), "end": m.end()} for m in _PERCENT.finditer(text)]
    return sorted(facts, key=lambda f: f["start"])


def extract(headline: str, summary: str | None = None) -> dict[str, Any]:
    """Tags and verbatim facts for one article. ``facts`` carry spans into ``headline`` (field=headline) or ``summary``."""
    tags: list[dict[str, Any]] = []
    for tag, label, weight, pattern in _RULES:
        match = pattern.search(headline) or (pattern.search(summary) if summary else None)
        if match:
            tags.append({"tag": tag, "label": label, "weight": weight, "matched": match.group(0)})
    facts = [{**f, "field": "headline"} for f in _span_facts(headline)]
    if summary:
        facts += [{**f, "field": "summary"} for f in _span_facts(summary)]
    return {"tags": tags, "facts": facts}


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u20b9\"'(])")
_WORD = re.compile(r"[a-z0-9]+")


def _overlap(a: str, b: str) -> float:
    wa, wb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


def key_line(headline: str, text: str | None, *, max_chars: int = 220) -> str | None:
    """The single most informative sentence of the stored text that is NOT just the headline again, verbatim; None when there is none.

    Feeds often carry only the headline (or the headline plus the publisher name): then there is no key line and the caller shows tags only.
    A sentence is chosen by rule score (event tags, quoted amounts/percentages); with no rule hit the first distinct sentence is used.
    """
    if not text:
        return None
    best: tuple[int, int, str] | None = None
    for position, raw in enumerate(_SENTENCE.split(text.strip())):
        sentence = raw.strip()
        if len(sentence) < 30 or _overlap(sentence, headline) >= 0.6:
            continue
        found = extract(sentence)
        score = max((t["weight"] for t in found["tags"]), default=0) + 3 * min(len(found["facts"]), 2)
        if best is None or score > best[0]:
            best = (score, -position, sentence)
    if best is None:
        return None
    line = best[2]
    if len(line) > max_chars:
        line = line[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"
    return line


_BOILERPLATE = re.compile(r"^.{3,140}?\s+has\s+(?:informed|intimated|submitted\s+to|updated|filed\s+with)\s+(?:the\s+)?(?:stock\s+)?exchange[s]?,?\s*"
                          r"(?:about|regarding|that|on|the|its)?\s+", re.I)


def display_title(headline: str) -> str:
    """Exchange filings repeat '<Company> has informed the Exchange about ...': drop that boilerplate so the subject leads. Else unchanged."""
    match = _BOILERPLATE.match(headline.strip())
    rest = headline.strip()[match.end():].strip() if match else ""
    if len(rest) < 12:
        return headline.strip()
    return rest[0].upper() + rest[1:]


def tag_labels(headline: str, text: str | None = None, limit: int = 2) -> list[str]:
    return [t["label"] for t in sorted(extract(headline, text)["tags"], key=lambda t: -t["weight"])[:limit]]


def _age_days(published_at: Any, now: datetime) -> float | None:
    if not published_at:
        return None
    try:
        when = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400)


def build(sentiment: dict[str, Any] | None, news_items: list[dict[str, Any]], instrument_id: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Ranked highlights for one company from analysed company-mapped events plus configured-feed headlines (duplicates by headline merged)."""
    now = now or datetime.now(timezone.utc)
    articles: dict[str, dict[str, Any]] = {}
    for event in (sentiment or {}).get("events", []) or []:
        for item in event.get("items", []):
            if str(item.get("instrument_id")) != str(instrument_id):
                continue
            key = " ".join(str(item.get("headline", "")).lower().split())
            articles.setdefault(key, {"headline": item.get("headline") or "", "summary": item.get("summary"), "publisher": item.get("publisher"),
                                      "url": item.get("url"), "published_at": item.get("published_at"), "sentiment": item.get("sentiment"),
                                      "impact": item.get("impact"), "event_category": item.get("event_category")})
    for item in news_items:
        key = " ".join(str(item.get("headline", "")).lower().split())
        articles.setdefault(key, {"headline": item.get("headline") or "", "summary": None, "publisher": item.get("publisher"), "url": item.get("url"),
                                  "published_at": item.get("published_at"), "sentiment": None, "impact": None, "event_category": None})
    ranked: list[dict[str, Any]] = []
    for article in articles.values():
        if not article["headline"] or article["headline"] == "Headline unavailable":
            continue
        found = extract(article["headline"], article.get("summary"))
        age = _age_days(article["published_at"], now)
        score = max((t["weight"] for t in found["tags"]), default=0) + (2 if found["facts"] else 0) + (2 if article.get("impact") == "high" else 1 if article.get("impact") == "medium" else 0)
        score += 2 if age is not None and age <= 2 else 1 if age is not None and age <= 7 else 0
        ranked.append({**article, **found, "importance_score": score, "importance": "high" if score >= 9 else "medium" if score >= 5 else "low"})
    ranked.sort(key=lambda a: str(a["published_at"] or ""), reverse=True)   # newest first, then a stable sort by importance
    ranked.sort(key=lambda a: -a["importance_score"])
    total = len(ranked)
    return {"status": "available" if ranked else "unavailable",
            "message": None if ranked else "No stored company-mapped news in the period. Missing news is not treated as neutral.",
            "method": "rule_based_v1", "total_articles": total, "items": ranked[:_MAX_ITEMS],
            "note": "Tags and quoted amounts come from keyword rules over the stored headline/summary; they are not a judgement of price impact."}
