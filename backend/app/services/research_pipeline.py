"""Template-rendered, provenance-preserving instrument research briefs."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Instrument,
    InstrumentFundamental,
    InstrumentResearchBrief,
    InstrumentResearchRun,
    InstrumentSnapshot,
    MarketBar,
    MarketDataSync,
)
from app.research_service import is_stale as is_source_stale
from app.services import fundamentals as fundamentals_service
from app.services.indicators import compute_indicators
from app.services.source_registry import FetchedSource, fetch_instrument_sources

logger = logging.getLogger(__name__)
DISCLAIMER = "Decision support, not investment advice."


def _number(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _date_path(row: InstrumentFundamental) -> str:
    return f"instrument_fundamentals[{row.period_type}:{row.period_end.isoformat()}]"


def _fundamentals_section(
    db: Session, instrument_id: UUID, quality: list[dict[str, str]]
) -> tuple[dict[str, Any], list[InstrumentFundamental]]:
    rows = list(
        db.scalars(
            select(InstrumentFundamental)
            .where(InstrumentFundamental.instrument_id == instrument_id)
            .order_by(InstrumentFundamental.period_type, InstrumentFundamental.period_end)
        )
    )
    snapshot = db.scalar(
        select(InstrumentSnapshot).where(InstrumentSnapshot.instrument_id == instrument_id)
    )
    if snapshot is None:
        quality.append({
            "kind": "unavailable",
            "field_path": "instrument_snapshots",
            "reason": "No stored M5.3 snapshot is available.",
        })
        snapshot_section: dict[str, Any] = {
            "status": "unavailable",
            "field_path": "instrument_snapshots",
            "values": None,
        }
    else:
        snapshot_path = "instrument_snapshots"
        snapshot_values: dict[str, Any] = {}
        for field in (
            "market_cap",
            "pe",
            "pb",
            "dividend_yield",
            "sector",
            "industry",
        ):
            value = getattr(snapshot, field)
            field_path = f"{snapshot_path}.{field}"
            snapshot_values[field] = {
                "value": _number(value) if isinstance(value, Decimal) else value,
                "field_path": field_path,
                "source": snapshot.source,
                "fetched_at": snapshot.fetched_at.isoformat(),
            }
            if value is None:
                quality.append({
                    "kind": "unavailable",
                    "field_path": field_path,
                    "reason": "The provider did not return this field.",
                })
        stale = fundamentals_service.is_stale(snapshot.fetched_at)
        snapshot_section = {
            "status": "available",
            "values": snapshot_values,
            "source": snapshot.source,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "stale": stale,
        }
        if stale:
            quality.append({
                "kind": "stale",
                "field_path": snapshot_path,
                "reason": "The stored snapshot is older than 95 days.",
            })

    if not rows:
        quality.append({
            "kind": "unavailable",
            "field_path": "instrument_fundamentals",
            "reason": "No stored M5.3 financial periods are available.",
        })
    period_values: list[dict[str, Any]] = []
    for row in rows:
        row_path = _date_path(row)
        values: dict[str, Any] = {}
        for field in (
            "revenue",
            "net_income",
            "eps",
            "operating_margin",
            "total_assets",
            "total_liabilities",
            "total_equity",
            "operating_cash_flow",
            "free_cash_flow",
            "total_debt",
            "shares_outstanding",
        ):
            value = getattr(row, field)
            path = f"{row_path}.{field}"
            values[field] = {"value": _number(value), "field_path": path}
            if value is None:
                quality.append({
                    "kind": "unavailable",
                    "field_path": path,
                    "reason": "The stored provider row has no value for this field.",
                })
        stale = fundamentals_service.is_stale(row.fetched_at)
        period_values.append({
            "period_type": row.period_type,
            "period_end": row.period_end.isoformat(),
            "currency": row.currency,
            "values": values,
            "source": row.source,
            "fetched_at": row.fetched_at.isoformat(),
            "stale": stale,
        })
        if stale:
            quality.append({
                "kind": "stale",
                "field_path": row_path,
                "reason": "The stored financial period was fetched more than 95 days ago.",
            })
    return {
        "status": "available" if snapshot is not None or rows else "unavailable",
        "snapshot": snapshot_section,
        "periods": period_values,
    }, rows


def _financial_trend_section(
    rows: list[InstrumentFundamental], quality: list[dict[str, str]]
) -> dict[str, Any]:
    if not rows:
        quality.append({
            "kind": "unavailable",
            "field_path": "instrument_fundamentals.financial_trend",
            "reason": "No stored periods are available for year-on-year comparison.",
        })
        return {
            "status": "unavailable",
            "periods": [],
            "reason": "No stored financial periods are available.",
        }

    metrics = ("revenue", "net_income", "eps", "free_cash_flow")
    trends: list[dict[str, Any]] = []
    for row in rows:
        prior = [
            candidate
            for candidate in rows
            if candidate.period_type == row.period_type
            and 360 <= (row.period_end - candidate.period_end).days <= 370
        ]
        previous = min(
            prior,
            key=lambda item: abs((row.period_end - item.period_end).days - 365),
            default=None,
        )
        current_path = _date_path(row)
        values: dict[str, Any] = {}
        for metric in metrics:
            current_value = getattr(row, metric)
            prior_value = getattr(previous, metric) if previous else None
            prior_path = f"{_date_path(previous)}.{metric}" if previous else None
            growth = None
            if current_value is not None and prior_value is not None and prior_value > 0:
                growth = (current_value - prior_value) / abs(prior_value) * Decimal("100")
            field_path = f"{current_path}.{metric}"
            values[metric] = {
                "value": _number(current_value),
                "year_on_year_growth_pct": _number(growth),
                "field_paths": [field_path] + ([prior_path] if prior_path else []),
            }
            if previous is None:
                quality.append({
                    "kind": "unavailable",
                    "field_path": field_path,
                    "reason": "No stored comparable period exists for year-on-year growth.",
                })
            elif prior_value <= 0:
                quality.append({
                    "kind": "not_meaningful",
                    "field_path": field_path,
                    "reason": (
                        "Year-on-year growth from a non-positive prior-period base "
                        "is not meaningful."
                    ),
                })
            elif current_value is None or prior_value is None:
                quality.append({
                    "kind": "unavailable",
                    "field_path": field_path,
                    "reason": "A current or prior stored value is unavailable.",
                })
        trends.append({
            "period_type": row.period_type,
            "period_end": row.period_end.isoformat(),
            "currency": row.currency,
            "values": values,
        })
    return {"status": "available", "periods": trends}


def _technicals_section(
    db: Session, instrument_id: UUID, quality: list[dict[str, str]]
) -> dict[str, Any]:
    instrument = db.get(Instrument, instrument_id)
    if instrument is not None and instrument.is_synthetic:
        quality.append({
            "kind": "synthetic",
            "field_path": "instruments.is_synthetic",
            "reason": "This instrument is marked as synthetic in the instrument master.",
        })
    latest_sync = db.scalar(
        select(MarketDataSync)
        .where(
            MarketDataSync.instrument_id == instrument_id,
            MarketDataSync.interval == "1d",
        )
        .order_by(MarketDataSync.attempted_at.desc())
        .limit(1)
    )
    if latest_sync is not None and latest_sync.status != "success":
        quality.append({
            "kind": "failed_fetch",
            "field_path": "market_data_syncs[interval=1d]",
            "reason": (
                "The latest stored daily market-data sync was unavailable"
                + (
                    f" ({latest_sync.safe_error_code})."
                    if latest_sync.safe_error_code
                    else "."
                )
            ),
        })
    latest = db.scalar(
        select(MarketBar)
        .where(MarketBar.instrument_id == instrument_id, MarketBar.interval == "1d")
        .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
        .limit(1)
    )
    bars: list[MarketBar] = []
    if latest is not None:
        bars = list(
            db.scalars(
                select(MarketBar)
                .where(
                    MarketBar.instrument_id == instrument_id,
                    MarketBar.interval == "1d",
                    MarketBar.source == latest.source,
                )
                .order_by(MarketBar.timestamp.desc())
                .limit(settings.indicator_lookback_bars)
            )
        )
        bars.reverse()
    calculated = compute_indicators(bars)
    indicator_values: dict[str, Any] = {}
    for name, result in calculated.__dict__.items():
        field_path = f"market_bars[interval=1d].indicators.{name}"
        indicator_values[name] = {
            "value": _number(result.value),
            "is_synthetic": result.is_synthetic,
            "bars_required": result.bars_required,
            "bars_used": result.bars_used,
            "field_path": field_path,
        }
        if result.value is None:
            quality.append({
                "kind": "unavailable",
                "field_path": field_path,
                "reason": (
                    f"Insufficient stored daily bars: requires {result.bars_required}, "
                    f"has {result.bars_used}."
                ),
            })
        if result.is_synthetic:
            quality.append({
                "kind": "synthetic",
                "field_path": field_path,
                "reason": "The indicator includes synthetic stored market bars.",
            })
    if latest is None:
        quality.append({
            "kind": "unavailable",
            "field_path": "market_bars[interval=1d]",
            "reason": "No stored daily OHLCV bars are available.",
        })
    else:
        now = datetime.now(timezone.utc)
        freshness_limit = settings.daily_close_price_max_age_seconds
        latest_bar_timestamp = latest.timestamp
        if latest_bar_timestamp.tzinfo is None:
            latest_bar_timestamp = latest_bar_timestamp.replace(tzinfo=timezone.utc)
        close_age = (now - latest_bar_timestamp).total_seconds()
        if close_age < 0 or close_age > freshness_limit:
            quality.append({
                "kind": "stale",
                "field_path": "market_bars[interval=1d]",
                "reason": (
                    "The latest daily close exceeds the configured freshness "
                    f"limit of {freshness_limit} seconds."
                ),
            })
        if latest.is_synthetic:
            quality.append({
                "kind": "synthetic",
                "field_path": "market_bars[interval=1d]",
                "reason": "The latest stored daily bar is synthetic.",
            })
    return {
        "status": "available" if bars else "unavailable",
        "interval": "1d",
        "source": latest.source if latest else None,
        "latest_bar": latest.timestamp.isoformat() if latest else None,
        "bars_available": len(bars),
        "indicators": indicator_values,
    }


def _source_sections(
    fetched: list[FetchedSource], quality: list[dict[str, str]]
) -> dict[str, Any]:
    source_refs: list[dict[str, str]] = []
    news_items: list[dict[str, Any]] = []
    for item in fetched:
        source = item.source
        ref = {
            "source_id": str(source.id),
            "title": source.title or "Untitled source",
            "publisher": source.publisher or "Publisher not identified",
            "url": source.url,
            "fetched_at": source.fetched_at.isoformat(),
        }
        source_refs.append(ref)
        for news in item.news_items:
            news_items.append({
                "headline": news.get("headline") or "Headline unavailable",
                "publisher": news.get("publisher") or source.publisher or "Publisher not identified",
                "published_at": news.get("published_at"),
                "url": news.get("url") or source.url,
                "source_id": str(source.id),
            })
        if is_source_stale(source.fetched_at.date()):
            quality.append({
                "kind": "stale",
                "field_path": f"research_sources[{source.id}]",
                "reason": "The saved source was fetched more than 30 days ago.",
            })
    if not news_items:
        quality.append({
            "kind": "unavailable",
            "field_path": "research_sources.news",
            "reason": "No source returned a parseable news headline.",
        })
    return {
        "status": "available" if news_items else "unavailable",
        "items": news_items,
        "sources": source_refs,
    }


def render_brief(
    db: Session,
    instrument: Instrument,
    fetched: list[FetchedSource],
    failures: list[dict[str, str]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    quality: list[dict[str, str]] = []
    fundamentals, periods = _fundamentals_section(db, instrument.id, quality)
    technicals = _technicals_section(db, instrument.id, quality)
    financial_trend = _financial_trend_section(periods, quality)
    news = _source_sections(fetched, quality)
    for failure in failures:
        quality.append({
            "kind": "failed_fetch",
            "field_path": f"source_registry.{failure['source']}",
            "reason": failure["reason"],
            "stage": failure["stage"],
            "url": failure["url"],
        })
    sections = {
        "fundamentals": fundamentals,
        "technicals": technicals,
        "financials_trend": financial_trend,
        "news": news,
        "data_quality": {
            "status": "issues" if quality else "clear",
            "items": quality,
        },
        "disclaimer": DISCLAIMER,
    }
    return sections, quality


def run_instrument_research(
    db: Session, user_id: UUID, instrument: Instrument
) -> tuple[InstrumentResearchRun, InstrumentResearchBrief]:
    started_at = datetime.now(timezone.utc)
    run = InstrumentResearchRun(
        id=uuid4(),
        instrument_id=instrument.id,
        user_id=user_id,
        started_at=started_at,
        finished_at=None,
        status="running",
        sources_attempted=0,
        sources_succeeded=0,
        failures=[],
    )
    db.add(run)
    db.commit()
    try:
        fetched_result = fetch_instrument_sources(db, user_id, instrument)
        sections, quality = render_brief(
            db, instrument, fetched_result.sources, fetched_result.failures
        )
        succeeded = len(fetched_result.sources)
        if succeeded == 0:
            status = "failed"
        elif fetched_result.failures:
            status = "partial"
        else:
            status = "complete"
        run.sources_attempted = fetched_result.attempted
        run.sources_succeeded = succeeded
        run.failures = fetched_result.failures
        run.finished_at = datetime.now(timezone.utc)
        run.status = status
        brief = InstrumentResearchBrief(
            id=uuid4(),
            run_id=run.id,
            instrument_id=instrument.id,
            sections=sections,
            renderer="template",
            data_quality=quality,
            created_at=run.finished_at,
        )
        db.add(brief)
        db.commit()
        db.refresh(run)
        db.refresh(brief)
        return run, brief
    except Exception as exc:
        db.rollback()
        failed_run = db.get(InstrumentResearchRun, run.id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.finished_at = datetime.now(timezone.utc)
            db.commit()
        logger.error(
            "Instrument research pipeline failed (exception_class=%s)",
            type(exc).__name__,
        )
        raise


def latest_brief(
    db: Session, user_id: UUID, instrument_id: UUID
) -> tuple[InstrumentResearchRun, InstrumentResearchBrief] | None:
    result = db.execute(
        select(InstrumentResearchRun, InstrumentResearchBrief)
        .join(
            InstrumentResearchBrief,
            InstrumentResearchBrief.run_id == InstrumentResearchRun.id,
        )
        .where(
            InstrumentResearchRun.user_id == user_id,
            InstrumentResearchRun.instrument_id == instrument_id,
        )
        .order_by(InstrumentResearchRun.started_at.desc())
        .limit(1)
    ).first()
    return (result[0], result[1]) if result else None
