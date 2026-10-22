import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ResearchSource

MAX_RESPONSE_BYTES = 1_000_000
FETCH_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
EXCERPT_CHARS = 500
STALE_AFTER_DAYS = 30
ALLOWED_CONTENT_TYPES = ("text/", "application/json", "application/xhtml+xml", "application/xml")


class ResearchError(ValueError):
    """Raised for invalid research input; the message is safe to show to the user."""


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    content_hash: str
    title: str | None
    excerpt: str | None
    fetched_at: datetime
    news_items: list[dict[str, str | None]] = field(default_factory=list)


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_global
    except ValueError:
        return False


def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve a host and require every address to be publicly routable."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ResearchError("The source host could not be resolved.") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses or not all(_is_public(address) for address in addresses):
        raise ResearchError("The source URL resolves to a non-public address and was blocked.")
    return addresses


def validate_source_url(url: str) -> tuple[str, int, str]:
    """Return (host, port, normalized_url) for an acceptable https URL or raise."""
    if len(url) > 2048:
        raise ResearchError("The source URL is too long.")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ResearchError("Only https:// source URLs are allowed.")
    if parts.username or parts.password:
        raise ResearchError("Source URLs must not contain credentials.")
    host = parts.hostname
    if not host:
        raise ResearchError("The source URL has no host.")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise ResearchError("The source URL has an invalid port.") from exc
    resolve_public_addresses(host, port)
    return host, port, parts.geturl()


def _read_limited(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise ResearchError("The source response is too large.")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ResearchError("The source response is too large.")
    return bytes(body)


def _summarize(body: bytes, content_type: str) -> tuple[str | None, str | None]:
    text = body.decode("utf-8", errors="replace")
    title = None
    if "html" in content_type:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if match:
            title = re.sub(r"\s+", " ", match.group(1)).strip()[:500] or None
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    excerpt = re.sub(r"\s+", " ", text).strip()[:EXCERPT_CHARS]
    return title, excerpt or None


def _news_items(body: bytes, content_type: str) -> list[dict[str, str | None]]:
    text = body.decode("utf-8", errors="replace")
    items: list[dict[str, str | None]] = []
    if "json" in content_type:
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            return items
        for item in (payload.get("news") or [])[:10] if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:500]
            url = str(item.get("link") or item.get("url") or "").strip()
            if not title or urlsplit(url).scheme != "https":
                continue
            publisher_data = item.get("publisher") or item.get("provider")
            publisher = (
                str(publisher_data.get("name") or "").strip()[:200]
                if isinstance(publisher_data, dict)
                else str(publisher_data or "").strip()[:200] or None
            )
            published_at = None
            timestamp = item.get("providerPublishTime")
            if isinstance(timestamp, (int, float)):
                published_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            items.append({
                "headline": title,
                "publisher": publisher,
                "published_at": published_at,
                "url": url[:2048],
            })
    elif "xml" in content_type or "rss" in content_type:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return items
        if root.tag.rsplit("}", 1)[-1].casefold() == "feed":
            entries = root.findall(".//{*}entry")[:10]
            for entry in entries:
                title = (entry.findtext("{*}title") or "").strip()[:500]
                link = next(
                    (
                        item.get("href", "").strip()
                        for item in entry.findall("{*}link")
                        if item.get("rel", "alternate") == "alternate"
                    ),
                    "",
                )
                if not title or urlsplit(link).scheme != "https":
                    continue
                publisher = entry.findtext("{*}source/{*}title")
                published_at = (
                    entry.findtext("{*}published")
                    or entry.findtext("{*}updated")
                )
                if published_at:
                    try:
                        published_at = datetime.fromisoformat(
                            published_at.replace("Z", "+00:00")
                        ).astimezone(timezone.utc).isoformat()
                    except (TypeError, ValueError, OverflowError):
                        published_at = None
                items.append({
                    "headline": title,
                    "publisher": publisher.strip()[:200] if publisher else None,
                    "published_at": published_at,
                    "url": link[:2048],
                })
            return items
        for item in root.findall(".//item")[:10]:
            title = (item.findtext("title") or "").strip()[:500]
            url = (item.findtext("link") or "").strip()
            if not title or urlsplit(url).scheme != "https":
                continue
            source = item.findtext("source")
            published_at = item.findtext("pubDate")
            if published_at:
                try:
                    published_at = parsedate_to_datetime(published_at).astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError):
                    published_at = None
            items.append({
                "headline": title,
                "publisher": source.strip()[:200] if source and source.strip() else None,
                "published_at": published_at,
                "url": url[:2048],
            })
    return items


def _fetch_body(
    url: str,
    *,
    user_agent: str,
    transport: httpx.BaseTransport | None = None,
    allow_not_found: bool = False,
) -> tuple[str, str, bytes]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        host, port, normalized = validate_source_url(current)
        address = resolve_public_addresses(host, port)[0]
        parts = urlsplit(normalized)
        target = parts._replace(
            netloc=f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        )
        try:
            with httpx.Client(
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=False,
                transport=transport,
            ) as client:
                with client.stream(
                    "GET",
                    target.geturl(),
                    headers={
                        "Host": host,
                        "Accept": "text/html,text/plain,application/json,application/rss+xml,application/xml",
                        "User-Agent": user_agent,
                    },
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ResearchError("The source redirected without a location.")
                        current = urljoin(normalized, location)
                        continue
                    if response.status_code == 404 and allow_not_found:
                        return normalized, response.headers.get("content-type", "").lower(), b""
                    if response.status_code != 200:
                        raise ResearchError(
                            f"The source responded with HTTP {response.status_code}."
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type.startswith(ALLOWED_CONTENT_TYPES):
                        raise ResearchError("The source content type is not supported.")
                    return normalized, content_type, _read_limited(response)
        except httpx.TimeoutException as exc:
            raise ResearchError("The source request timed out.") from exc
        except httpx.ConnectError as exc:
            raise ResearchError("The source connection could not be established.") from exc
        except httpx.HTTPError as exc:
            raise ResearchError("The source could not be fetched.") from exc
    raise ResearchError("The source redirected too many times.")


def fetch_document(
    url: str,
    *,
    user_agent: str = "TradingCopilotResearch/1.0",
    transport: httpx.BaseTransport | None = None,
) -> FetchedDocument:
    """Fetch an https document with size, time and redirect limits.

    Each hop is re-validated, so a redirect to a private address is refused. The
    connection is pinned to the validated IP to defeat DNS rebinding. Only the
    hash and a short excerpt are returned; the body is never stored.
    """
    normalized, content_type, body = _fetch_body(url, user_agent=user_agent, transport=transport)
    title, excerpt = _summarize(body, content_type)
    return FetchedDocument(
        url=normalized,
        content_hash=hashlib.sha256(body).hexdigest(),
        title=title,
        excerpt=excerpt,
        fetched_at=datetime.now(timezone.utc),
        news_items=_news_items(body, content_type),
    )


def fetch_robots_txt(url: str, *, user_agent: str) -> str:
    """Read robots.txt using the same bounded, redirect-checked HTTPS fetcher."""
    _normalized, content_type, body = _fetch_body(
        url,
        user_agent=user_agent,
        allow_not_found=True,
    )
    if not body:
        return ""
    if not content_type.startswith("text/"):
        raise ResearchError("The robots.txt content type is not supported.")
    return body.decode("utf-8", errors="replace")


def is_stale(as_of: date, today: date | None = None) -> bool:
    return (today or datetime.now(timezone.utc).date()) - as_of > timedelta(days=STALE_AFTER_DAYS)


def resolve_fact_sources(
    db: Session, user_id: UUID, facts: list[dict]
) -> list[UUID]:
    """Require every fact to cite owned sources with a URL and fetched_at.

    Returns the de-duplicated union of cited source ids, in first-seen order.
    """
    if not facts:
        raise ResearchError("A research note needs at least one sourced fact.")
    cited: list[UUID] = []
    for fact in facts:
        ids = fact.get("source_ids") or []
        if not ids:
            raise ResearchError("Every research fact must cite at least one source.")
        for source_id in ids:
            if UUID(str(source_id)) not in cited:
                cited.append(UUID(str(source_id)))
    sources = {
        source.id: source
        for source in db.scalars(
            select(ResearchSource).where(
                ResearchSource.user_id == user_id, ResearchSource.id.in_(cited)
            )
        )
    }
    for source_id in cited:
        source = sources.get(source_id)
        if source is None:
            raise ResearchError("A cited source does not exist.")
        if not source.url or source.fetched_at is None:
            raise ResearchError("A cited source has no resolvable URL and fetch time.")
    return cited
