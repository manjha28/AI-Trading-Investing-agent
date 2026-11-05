"""Source adapters and persistence for public news and NSE filings."""
from __future__ import annotations

import hashlib
import json
import time
import tempfile
import re
import threading
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Instrument, InstrumentProviderIdentifier, MarketBar, NewsIngestionFailure, NewsItem, NewsSourcePoll, WatchlistInstrument
from app.services.source_registry import _reserve_host_request

IST = ZoneInfo("Asia/Kolkata")
_SOURCE_LOCK = threading.Lock()
_SOURCE_COOLDOWN_UNTIL: dict[str, float] = {}
_SOURCE_USER_AGENT = "TradingCopilotNews/1.0 (public RSS/API metadata; contact: local deployment)"


def _reserve_source_request(url: str) -> None:
    host = urlsplit(url).hostname or ""
    with _SOURCE_LOCK:
        now = time.monotonic()
        if _SOURCE_COOLDOWN_UNTIL.get(host, 0.0) > now:
            raise RuntimeError("rate_limited_cooldown_active")
    # Share the research source host queue so concurrent source polling follows
    # the same per-host request schedule.
    _reserve_host_request(host)


def _story_key(headline: str, published: datetime) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", headline.casefold()).strip()
    day = published.date().isoformat()
    return hashlib.sha256(f"{day}:{normalized}".encode()).hexdigest()


def _resolve_nse_instrument(db: Session, symbol: str | None) -> Instrument | None:
    if not symbol:
        return None
    matches = db.scalars(select(Instrument).where(
        func.upper(Instrument.symbol) == symbol.upper(), Instrument.venue == "NSE"
    )).all()
    return matches[0] if len(matches) == 1 else None


def _parse_rss(body: bytes, *, source: str) -> list[dict[str, Any]]:
    root = ET.fromstring(body)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    parsed = []
    for entry in entries:
        def value(name: str) -> str | None:
            node = entry.find(name)
            if node is None:
                node = entry.find(f"{{http://www.w3.org/2005/Atom}}{name}")
            return node.text.strip() if node is not None and node.text else None
        link_node = entry.find("{http://www.w3.org/2005/Atom}link")
        link = value("link") or (link_node.get("href") if link_node is not None else None)
        stamp = _stamp(value("pubDate") or value("published") or value("updated"))
        title = value("title")
        if title and link and stamp:
            source_node = entry.find("source")
            parsed.append({"title": title, "url": link, "published_at": stamp,
                           "description": value("description") or value("summary"),
                           "publisher": (source_node.text.strip() if source_node is not None and source_node.text else None),
                           "id": value("guid") or value("id"), "source": source})
    return parsed


def _get_source(url: str) -> bytes:
    _reserve_source_request(url)
    response = httpx.get(url, headers={"User-Agent": _SOURCE_USER_AGENT}, timeout=12.0,
                         follow_redirects=True)
    if response.status_code == 429:
        with _SOURCE_LOCK:
            _SOURCE_COOLDOWN_UNTIL[urlsplit(url).hostname or ""] = time.monotonic() + 900
        raise RuntimeError("rate_limited")
    if response.status_code in (401, 403):
        raise RuntimeError("access_denied")
    if response.status_code >= 400:
        raise RuntimeError(f"upstream_http_{response.status_code}")
    return response.content


def _fetch_google_news(query: str) -> list[dict[str, Any]]:
    url = "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    items = _parse_rss(_get_source(url), source="google_news")
    for item in items:
        item["publisher"] = item["publisher"] or "Google News"
        item["category"] = "market_news"
        item["raw_symbols"] = []
    return items


def _fetch_gdelt() -> list[dict[str, Any]]:
    query = '(stock OR company OR earnings OR merger OR regulation OR order) sourcecountry:india sourcelang:english'
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urlencode({"query": query, "mode": "ArtList", "format": "json", "sort": "DateDesc", "maxrecords": "50"})
    payload = json.loads(_get_source(url))
    items = []
    for row in payload.get("articles", []):
        stamp = _stamp(row.get("seendate"))
        title, link = row.get("title"), row.get("url")
        if not title or not link or not stamp:
            continue
        items.append({"id": row.get("url"), "title": title, "url": link, "published_at": stamp,
                      "description": row.get("snippet"), "publisher": row.get("domain"),
                      "category": row.get("tone") and "market_news" or "market_news",
                      "raw_symbols": [], "source": "gdelt", "published_time_kind": "gdelt_seen_at", "gdelt": row})
    return items


def _stamp(value: Any, *, ist: bool = False) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        parsed = None
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                pass
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(raw)
                except (TypeError, ValueError, OverflowError):
                    # GDELT uses compact UTC date strings such as 20261105T120000Z.
                    try:
                        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                    except ValueError:
                        return None
    if parsed.tzinfo is None:
        if not ist:
            return None
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def _external_id(source: str, value: Any, payload: dict) -> str:
    if value:
        return f"{source}:{value}"[:256]
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f"{source}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _store(db: Session, *, source: str, external_id: str, payload: dict, published: datetime,
           headline: str, symbol: str | None, url: str | None = None, publisher: str | None = None,
           category: str | None = None, body: str | None = None) -> int:
    instrument = _resolve_nse_instrument(db, symbol)
    payload = {**payload, "story_key": _story_key(headline, published),
               "full_text_status": "unavailable", "body_kind": "feed_description" if body else None}
    stmt = insert(NewsItem).values(
        external_id=external_id, source=source, source_url=url, instrument_id=instrument.id if instrument else None,
        symbol_raw=symbol, published_at=published, retrieved_at=datetime.now(timezone.utc), publisher=publisher,
        category=category, headline=headline[:1000], body=body, raw_payload=payload,
    ).on_conflict_do_nothing(index_elements=["source", "external_id"])
    result = db.execute(stmt.returning(NewsItem.id))
    return int(result.first() is not None)


def _failure(db: Session, source: str, detail: str, symbol: str | None = None) -> None:
    db.add(NewsIngestionFailure(source=source, symbol_raw=symbol, attempted_at=datetime.now(timezone.utc), detail=detail[:1000]))
    db.commit()


def _in_source_cooldown(db: Session, source: str, now: datetime) -> bool:
    poll = db.get(NewsSourcePoll, source)
    if poll is None or not poll.rate_limited:
        return False
    attempted = poll.last_attempted_at
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=timezone.utc)
    return now - attempted < timedelta(minutes=15)


def ingest(db: Session, filings_days: int = 30, news_days: int = 2) -> dict[str, Any]:
    """Ingest only source-dated items; source failures are persisted and isolated."""
    now = datetime.now(timezone.utc)
    counts = {"nse": 0, "yfinance": 0, "google_news": 0, "gdelt": 0}
    discovered = {source: 0 for source in counts}
    duplicates = {source: 0 for source in counts}
    successful = {source: False for source in counts}
    source_failures = {source: 0 for source in counts}
    source_attempted = {source: True for source in counts}
    failures = 0
    start = date.today() - timedelta(days=filings_days)
    instruments = db.scalars(select(Instrument).where(Instrument.id.in_(select(WatchlistInstrument.instrument_id)) | Instrument.id.in_(select(MarketBar.instrument_id).where(MarketBar.interval.in_(["1d", "1m"]))))).all()
    try:
        from nse import NSE
        from nse import MemoryCookieStore
        nse = NSE(download_folder=tempfile.gettempdir() + "/trading-copilot-nse-news", cookie_store=MemoryCookieStore())
        methods = (
            ("announcements", nse.announcements, {"from_date": start, "to_date": date.today()}, "seq_id", "exchdisstime", "attchmntText", "desc"),
            ("board_meetings", nse.board_meetings, {}, "bm_id", "bm_timestamp", "bm_desc", "bm_purpose"),
            ("financial_results", nse.financial_results, {"from_date": start, "to_date": date.today()}, "seqNumber", "exchdisstime", "desc", "subject"),
            ("actions", nse.actions, {}, "caBroadcastDate", "caBroadcastDate", "subject", "subject"),
        )
        for endpoint, method, kwargs, id_key, time_key, title_key, category_key in methods:
            try:
                records = method(**kwargs) or []
                successful["nse"] = True
                discovered["nse"] += len(records)
                for row in records:
                    data = dict(row)
                    symbol = data.get("symbol") or data.get("bm_symbol") or data.get("sm_symbol") or None
                    stamp = _stamp(data.get(time_key), ist=True)
                    headline = data.get(title_key) or data.get(category_key)
                    if stamp is None or not headline:
                        continue
                    if stamp < now - timedelta(days=filings_days):
                        continue
                    eid = _external_id("nse:" + endpoint, data.get(id_key), data)
                    inserted = _store(db, source="nse", external_id=eid, payload=data, published=stamp,
                        headline=str(headline), symbol=symbol, url=data.get("attchmntFile") or data.get("xbrl"), category=str(data.get(category_key) or endpoint))
                    counts["nse"] += inserted
                    duplicates["nse"] += int(not inserted)
                db.commit()
            except Exception as exc:
                failures += 1
                source_failures["nse"] += 1
                _failure(db, "nse", f"{endpoint}: {type(exc).__name__}: {exc}")
            time.sleep(1.0)
    except Exception as exc:
        failures += 1
        source_failures["nse"] += 1
        _failure(db, "nse", f"library unavailable: {type(exc).__name__}: {exc}")

    no_news: list[str] = []
    from app.provider_adapters import YahooFinanceProvider
    yahoo = YahooFinanceProvider()
    yahoo_rate_limited = False
    if _in_source_cooldown(db, "yfinance", now):
        source_attempted["yfinance"] = False
        yahoo_rate_limited = True
    for instrument in instruments if source_attempted["yfinance"] else []:
        ids = db.scalars(select(InstrumentProviderIdentifier).where(InstrumentProviderIdentifier.instrument_id == instrument.id)).all()
        provider_id = next((r for r in ids if r.provider.lower() in {"yahoo", "yfinance"}), None)
        ticker = provider_id.provider_symbol if provider_id else (instrument.symbol + (".NS" if instrument.venue.upper() == "NSE" else ".BO" if instrument.venue.upper() == "BSE" else ""))
        try:
            rows = yahoo.get_news(ticker)
            successful["yfinance"] = True
            if not rows:
                no_news.append(instrument.symbol)
            discovered["yfinance"] += len(rows)
            for item in rows:
                data = dict(item)
                content = data.get("content") if isinstance(data.get("content"), dict) else data
                stamp = _stamp(content.get("pubDate") or content.get("providerPublishTime") or content.get("displayTime") or content.get("published_at"))
                title = content.get("title") or content.get("headline")
                if not stamp or not title or stamp < now - timedelta(days=news_days):
                    continue
                related = content.get("relatedTickers") or data.get("relatedTickers") or []
                explicit = any(str(x).upper() == ticker.upper() for x in related) if isinstance(related, list) else False
                named_symbol = instrument.symbol if explicit else None
                url = (content.get("canonicalUrl") or {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else content.get("link") or data.get("link")
                publisher = content.get("publisher") or (content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else content.get("publisher")
                inserted = _store(db, source="yfinance", external_id=_external_id("yfinance", data.get("id") or content.get("id"), data), payload=data, published=stamp, headline=str(title), symbol=named_symbol, url=url, publisher=publisher)
                counts["yfinance"] += inserted
                duplicates["yfinance"] += int(not inserted)
            db.commit()
        except Exception as exc:
            failures += 1
            source_failures["yfinance"] += 1
            code = getattr(exc, "code", None) or str(exc)
            category = "rate_limited" if any(token in str(code).casefold() for token in ("rate", "429")) else "upstream_failure"
            _failure(db, "yfinance", f"{category}: {type(exc).__name__}: {exc}", instrument.symbol)
            # Yahoo is optional. Any per-ticker failure ends this source's poll;
            # continuing to other symbols would amplify throttling.
            yahoo_rate_limited = category == "rate_limited"
            break
        time.sleep(0.25)

    # One broad query plus a small deterministic company batch per poll keeps RSS
    # volume bounded. Subsequent polls naturally revisit the same stories safely.
    discovery_items: dict[str, list[dict[str, Any]]] = {"google_news": [], "gdelt": []}
    google_queries: list[Any] = ['India stock market earnings merger regulation corporate order']
    company_instruments = [row for row in instruments if row.country == "IN" and row.asset_class == "equity"]
    company_instruments.sort(key=lambda row: row.symbol)
    for instrument in company_instruments[:4]:
        name = instrument.display_name.strip()
        if len(name) >= 5:
            google_queries.append((f'"{name}" (India stock OR earnings OR merger OR order OR regulation)', instrument))
    normalized_queries = [(query, None) if isinstance(query, str) else query for query in google_queries]
    if _in_source_cooldown(db, "google_news", now):
        source_attempted["google_news"] = False
    else:
        for query, queried_instrument in normalized_queries:
            try:
                rows = _fetch_google_news(query)
                if queried_instrument is not None:
                    for row in rows:
                        row["company_refs"] = [queried_instrument.display_name]
                        row["raw_symbols"] = [queried_instrument.symbol]
                        text = f"{row['title']} {row.get('description') or ''}".casefold()
                        if queried_instrument.display_name.casefold() in text:
                            row["resolved_symbol"] = queried_instrument.symbol
                discovery_items["google_news"].extend(rows)
                successful["google_news"] = True
            except Exception as exc:
                failures += 1
                source_failures["google_news"] += 1
                _failure(db, "google_news", f"{type(exc).__name__}: {exc}")
                if "rate_limit" in str(exc).casefold() or "429" in str(exc):
                    break
    if _in_source_cooldown(db, "gdelt", now):
        source_attempted["gdelt"] = False
    else:
        try:
            discovery_items["gdelt"] = _fetch_gdelt()
            successful["gdelt"] = True
        except Exception as exc:
            failures += 1
            source_failures["gdelt"] += 1
            _failure(db, "gdelt", f"{type(exc).__name__}: {exc}")

    for source, items in discovery_items.items():
        seen_urls: set[str] = set()
        for item in items:
            url = item["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            stamp = item["published_at"]
            if stamp < now - timedelta(days=news_days):
                continue
            discovered[source] += 1
            payload = {key: value for key, value in item.items() if key != "published_at"}
            inserted = _store(db, source=source, external_id=_external_id(source, item.get("id") or url, payload),
                payload=payload, published=stamp, headline=item["title"], symbol=item.get("resolved_symbol"), url=url,
                publisher=item.get("publisher"), category=item.get("category"), body=item.get("description"))
            counts[source] += inserted
            duplicates[source] += int(not inserted)
        db.commit()
    # Persist the last observed state, including successful empty polls. Failures
    # remain append-only in news_ingestion_failures for audit history.
    for source in counts:
        poll = db.get(NewsSourcePoll, source)
        if poll is None:
            poll = NewsSourcePoll(source=source, last_attempted_at=now)
            db.add(poll)
        if source_attempted[source]:
            poll.last_attempted_at = now
        if successful[source]:
            poll.last_successful_at = now
        if source_attempted[source]:
            poll.articles_discovered = discovered[source]
            poll.articles_inserted = counts[source]
            poll.duplicates = duplicates[source]
            poll.failure_count = int(db.scalar(select(func.count()).select_from(NewsIngestionFailure).where(
                NewsIngestionFailure.source == source)) or 0)
        latest_failure = db.scalar(select(NewsIngestionFailure).where(
            NewsIngestionFailure.source == source).order_by(NewsIngestionFailure.attempted_at.desc()).limit(1))
        failure_time = latest_failure.attempted_at if latest_failure else None
        if failure_time is not None and failure_time.tzinfo is None:
            failure_time = failure_time.replace(tzinfo=timezone.utc)
        if source_attempted[source] and source_failures[source] and failure_time is not None:
            detail = latest_failure.detail.casefold()
            poll.rate_limited = "rate_limited" in detail or "429" in detail
            poll.last_error_category = ("rate_limited" if poll.rate_limited else
                "access_denied" if "access_denied" in detail else "parsing_error" if "parse" in detail else "upstream_failure")
        elif source_attempted[source]:
            poll.rate_limited = False
            poll.last_error_category = None
        if source_attempted[source]:
            poll.last_article_at = db.scalar(select(func.max(NewsItem.published_at)).where(NewsItem.source == source))
    db.commit()

    match_count = select(func.count(Instrument.id)).where(
        func.upper(Instrument.symbol) == func.upper(NewsItem.symbol_raw), Instrument.venue == "NSE"
    ).correlate(NewsItem).scalar_subquery()
    mapped_id = select(Instrument.id).where(
        func.upper(Instrument.symbol) == func.upper(NewsItem.symbol_raw),
        Instrument.venue == "NSE", match_count == 1,
    ).limit(1).scalar_subquery()
    db.execute(update(NewsItem).where(
        NewsItem.source == "nse", NewsItem.instrument_id.is_(None), NewsItem.symbol_raw.is_not(None), mapped_id.is_not(None)
    ).values(instrument_id=mapped_id))
    db.commit()
    unmapped = db.scalars(select(NewsItem.symbol_raw).where(
        NewsItem.symbol_raw.is_not(None), NewsItem.instrument_id.is_(None), NewsItem.source == "nse"
    ).distinct().order_by(NewsItem.symbol_raw)).all()
    return {"inserted": counts, "discovered": discovered, "duplicates": duplicates, "failures": failures,
            "yahoo_rate_limited": yahoo_rate_limited, "symbols_without_news": sorted(set(no_news)),
            "unmapped_nse_symbols": unmapped}
