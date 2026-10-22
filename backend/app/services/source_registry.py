"""Ordered, bounded, robots-aware discovery and fetching of research sources."""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import research_service
from app.models import (
    Instrument,
    InstrumentSourceLink,
    ResearchSource,
)

MAX_SOURCES_PER_RUN = 10
REQUESTS_PER_HOST_PER_SECOND = 1
USER_AGENT = (
    "TradingCopilotResearch/1.0 "
    "(+https://github.com/manjha28/AI-Trading-Investing-agent)"
)

_rate_lock = threading.Lock()
_next_host_request: dict[str, float] = {}


@dataclass(frozen=True)
class SourceCandidate:
    url: str
    label: str
    publisher: str
    kind: str


@dataclass(frozen=True)
class FetchedSource:
    source: ResearchSource
    news_items: list[dict[str, str | None]]


@dataclass(frozen=True)
class SourceFetchResult:
    sources: list[FetchedSource]
    failures: list[dict[str, str]]
    attempted: int


def _reserve_host_request(host: str) -> None:
    with _rate_lock:
        now = time.monotonic()
        available_at = _next_host_request.get(host, now)
        scheduled_at = max(now, available_at)
        _next_host_request[host] = scheduled_at + 1 / REQUESTS_PER_HOST_PER_SECOND
    delay = scheduled_at - now
    if delay > 0:
        time.sleep(delay)


def build_source_candidates(
    _instrument: Instrument, links: list[InstrumentSourceLink]
) -> list[SourceCandidate]:
    investor_links = [
        link for link in links
        if "investor" in link.label.casefold() or link.label.casefold() in {"ir", "ir site"}
    ]
    candidates = [
        SourceCandidate(link.url, link.label, "User-provided source", "investor_relations")
        for link in investor_links
    ]

    investor_ids = {link.id for link in investor_links}
    candidates.extend(
        SourceCandidate(link.url, link.label, "User-provided source", "custom")
        for link in links
        if link.id not in investor_ids
    )

    unique: list[SourceCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.url not in seen:
            seen.add(candidate.url)
            unique.append(candidate)
        if len(unique) >= MAX_SOURCES_PER_RUN:
            break
    return unique


def _safe_failure_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


def _failure(
    candidate: SourceCandidate, stage: str, error: str
) -> dict[str, str]:
    return {
        "source": candidate.label,
        "stage": stage,
        "url": _safe_failure_url(candidate.url),
        "reason": error[:300],
    }


def _robots_parser(url: str, host: str, cache: dict[str, RobotFileParser]) -> RobotFileParser:
    if host in cache:
        return cache[host]
    parts = urlsplit(url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    _reserve_host_request(host)
    rules = research_service.fetch_robots_txt(robots_url, user_agent=USER_AGENT)
    parser = RobotFileParser(robots_url)
    parser.parse(rules.splitlines())
    cache[host] = parser
    return parser


def _store_source(
    db: Session,
    user_id: UUID,
    instrument_id: UUID,
    candidate: SourceCandidate,
    document: research_service.FetchedDocument,
) -> ResearchSource:
    existing = db.scalar(
        select(ResearchSource).where(
            ResearchSource.user_id == user_id,
            ResearchSource.content_hash == document.content_hash,
        )
    )
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc)
    source = ResearchSource(
        user_id=user_id,
        instrument_id=instrument_id,
        url=document.url,
        publisher=candidate.publisher,
        title=document.title or candidate.label,
        fetched_at=document.fetched_at,
        content_hash=document.content_hash,
        excerpt=document.excerpt,
        created_at=now,
    )
    try:
        with db.begin_nested():
            db.add(source)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(ResearchSource).where(
                ResearchSource.user_id == user_id,
                ResearchSource.content_hash == document.content_hash,
            )
        )
        if existing is None:
            raise
        return existing
    return source


def fetch_instrument_sources(
    db: Session,
    user_id: UUID,
    instrument: Instrument,
) -> SourceFetchResult:
    links = list(
        db.scalars(
            select(InstrumentSourceLink)
            .where(
                InstrumentSourceLink.user_id == user_id,
                InstrumentSourceLink.instrument_id == instrument.id,
                InstrumentSourceLink.enabled.is_(True),
            )
            .order_by(InstrumentSourceLink.created_at, InstrumentSourceLink.id)
        )
    )
    candidates = build_source_candidates(instrument, links)
    sources: list[FetchedSource] = []
    failures: list[dict[str, str]] = []
    robots_cache: dict[str, RobotFileParser] = {}

    for candidate in candidates:
        try:
            host, _port, _normalized = research_service.validate_source_url(candidate.url)
            parser = _robots_parser(candidate.url, host, robots_cache)
        except research_service.ResearchError as exc:
            failures.append(_failure(candidate, "robots", str(exc)))
            continue
        if not parser.can_fetch(USER_AGENT, candidate.url):
            failures.append(
                _failure(candidate, "robots", "Source disallowed by robots.txt.")
            )
            continue

        try:
            _reserve_host_request(host)
            document = research_service.fetch_document(
                candidate.url, user_agent=USER_AGENT
            )
            source = _store_source(
                db, user_id, instrument.id, candidate, document
            )
        except research_service.ResearchError as exc:
            failures.append(_failure(candidate, "fetch", str(exc)))
            continue
        sources.append(FetchedSource(source=source, news_items=document.news_items))

    return SourceFetchResult(
        sources=sources,
        failures=failures,
        attempted=len(candidates),
    )
