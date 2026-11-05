"""Template-rendered, provenance-preserving instrument research briefs."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from app.api.market_data import (
    _exclusive_selected_source,
    _market_data_credentials,
    _providers,
    _resolve_provider_candidate,
    create_market_data_provider_chain,
)
from app.broker_adapters import BrokerAdapterError
from app.config import settings
from app.db import engine
from app.market_data import MarketDataProviderError
from app.models import (
    Instrument,
    InstrumentFundamental,
    InstrumentResearchBrief,
    InstrumentResearchRun,
    InstrumentSnapshot,
    MarketBar,
    MarketDataSync,
    NewsItem,
    NewsSourcePoll,
)
from app.research_service import is_stale as is_source_stale
from app.services import fundamentals as fundamentals_service
from app.services.indicators import compute_indicators
from app.services.news_sentiment import CLASSIFIER_VERSION, aggregate_instrument_sentiment
from app.services.research_scoring import build_assessment, daily_bar_staleness
from app.services.source_registry import FetchedSource, fetch_instrument_sources

logger = logging.getLogger(__name__)
DISCLAIMER = "Decision support, not investment advice."


def _number(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _date_path(row: InstrumentFundamental) -> str:
    return f"instrument_fundamentals[{row.period_type}:{row.period_end.isoformat()}]"


def _company_info_snapshot(
    data: dict[str, Any], security_id: str
) -> dict[str, Any] | None:
    entry = data.get(security_id)
    if isinstance(entry, list):
        entry = entry[0] if entry else None
    if not isinstance(entry, dict):
        return None
    overview = entry.get("CO") if isinstance(entry.get("CO"), dict) else {}
    ratios = entry.get("RATIOS") if isinstance(entry.get("RATIOS"), dict) else {}

    def decimal_field(value: Any, *, crore: bool = False, percentage: bool = False) -> str | None:
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not parsed.is_finite():
            return None
        if crore:
            parsed *= Decimal("10000000")
        if percentage:
            parsed /= Decimal("100")
        return str(parsed)

    fetched_at = datetime.now(timezone.utc).isoformat()
    fields = {
        "market_cap": decimal_field(overview.get("MARKET_CAP"), crore=True),
        "pe": decimal_field(ratios.get("PE_RATIO")),
        "pb": decimal_field(ratios.get("PB_RATIO")),
        "dividend_yield": decimal_field(overview.get("DIVIDEND_YIELD"), percentage=True),
        "eps": decimal_field(overview.get("EPS")),
        "ebitda": decimal_field(overview.get("EBITDA"), crore=True),
        "book_value": decimal_field(overview.get("BOOK_VALUE")),
        "fifty_two_week_high": decimal_field(overview.get("FIFTY_TWO_WEEK_HIGH")),
        "fifty_two_week_low": decimal_field(overview.get("FIFTY_TWO_WEEK_LOW")),
        "debt_to_equity": decimal_field(ratios.get("DEBT_TO_EQUITY")),
        "roe": decimal_field(ratios.get("ROE"), percentage=True),
        "roce": decimal_field(ratios.get("ROCE"), percentage=True),
        "sector": overview.get("SECTOR") or None,
        "industry": overview.get("INDUSTRY") or None,
    }
    return {
        "status": "available" if any(value is not None for value in fields.values()) else "unavailable",
        "values": {
            name: {
                "value": value,
                "field_path": f"dhan.company_info.{name}",
                "source": "dhan",
                "fetched_at": fetched_at,
            }
            for name, value in fields.items()
        },
        "source": "dhan",
        "fetched_at": fetched_at,
        "stale": False,
    }


def _fetch_dhan_company_info(
    db: Session, user_id: UUID, instrument: Instrument
) -> tuple[dict[str, Any] | None, str | None]:
    mapping = next((
        item for item in instrument.provider_identifiers
        if item.provider == "dhan" and item.is_verified
    ), None)
    if mapping is None:
        return None, "No verified Dhan instrument mapping is available."
    parts = mapping.provider_instrument_id.split("|", maxsplit=1)
    security_id = parts[0]
    segment = parts[1] if len(parts) == 2 else ""
    if not security_id.isdigit() or segment not in {"NSE_EQ", "BSE_EQ"}:
        return None, "Dhan Company Info supports only verified NSE/BSE equity mappings."
    try:
        _, provider, _ = _market_data_credentials(db, user_id, "dhan")
        if provider is None:
            return None, "No authenticated Dhan connection is available."
        data = provider.adapter.get_company_info(
            provider.tokens["access_token"], provider.tokens, security_id, segment
        )
        snapshot = _company_info_snapshot(data, security_id)
        if snapshot is None or snapshot["status"] == "unavailable":
            return None, "Dhan Company Info returned no usable company-overview or ratio fields."
        return snapshot, None
    except HTTPException:
        return None, "Dhan Company Info is unavailable; verify the Dhan connection and its data access."
    except BrokerAdapterError:
        return None, "Dhan Company Info could not provide this instrument's fundamentals."


def _fundamentals_section(
    db: Session,
    instrument_id: UUID,
    quality: list[dict[str, str]],
    *,
    selected_source: str | None = None,
    external_snapshot: dict[str, Any] | None = None,
    external_reason: str | None = None,
) -> tuple[dict[str, Any], list[InstrumentFundamental]]:
    rows_query = select(InstrumentFundamental).where(
        InstrumentFundamental.instrument_id == instrument_id
    )
    if selected_source is not None:
        rows_query = rows_query.where(InstrumentFundamental.source == selected_source)
    rows = list(db.scalars(rows_query.order_by(
        InstrumentFundamental.period_type, InstrumentFundamental.period_end
    )))
    snapshot_query = select(InstrumentSnapshot).where(
        InstrumentSnapshot.instrument_id == instrument_id
    )
    if selected_source is not None:
        snapshot_query = snapshot_query.where(InstrumentSnapshot.source == selected_source)
    snapshot = db.scalar(snapshot_query)
    snapshot_has_values = snapshot is not None and any(
        getattr(snapshot, field) is not None
        for field in ("market_cap", "pe", "pb", "dividend_yield", "sector", "industry")
    )
    if external_snapshot is not None:
        snapshot_section = dict(external_snapshot)
        alternate_fields: dict[str, dict[str, str]] = {}
        conflicts: list[dict[str, str]] = []
        if snapshot is not None and snapshot.source != external_snapshot.get("source"):
            stored_values = external_snapshot.get("values", {})
            for field in ("market_cap", "pe", "pb", "dividend_yield", "sector", "industry"):
                stored_value = getattr(snapshot, field)
                current_value = stored_values.get(field, {}).get("value") if isinstance(stored_values.get(field), dict) else None
                if stored_value is None:
                    continue
                alternate_fields[field] = {
                    "value": str(stored_value), "source": snapshot.source,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                }
                if current_value is not None:
                    try:
                        if Decimal(str(current_value)) != Decimal(str(stored_value)):
                            conflicts.append({"field": field, "source_a": str(external_snapshot.get("source")),
                                              "value_a": str(current_value), "source_b": snapshot.source,
                                              "value_b": str(stored_value)})
                    except (InvalidOperation, ValueError):
                        if str(current_value).casefold() != str(stored_value).casefold():
                            conflicts.append({"field": field, "source_a": str(external_snapshot.get("source")),
                                              "value_a": str(current_value), "source_b": snapshot.source,
                                              "value_b": str(stored_value)})
        snapshot_section["alternate_sources"] = alternate_fields
        snapshot_section["conflicts"] = conflicts
        snapshot_section["comparison_status"] = "conflicting_values" if conflicts else "no_conflict_detected"
        snapshot_has_values = external_snapshot["status"] == "available"
    elif snapshot is None:
        quality.append({
            "kind": "unavailable",
            "field_path": "instrument_snapshots",
            "reason": (
                f"No {selected_source} company snapshot is stored."
                if selected_source is not None
                else "No stored M5.3 snapshot is available."
            ),
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
            "status": "available" if snapshot_has_values else "unavailable",
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
            "reason": (
                "Dhan Company Info does not provide annual or quarterly statements; "
                "no Dhan financial periods are stored."
                if selected_source == "dhan"
                else f"No {selected_source} financial periods are stored."
                if selected_source is not None
                else "No stored M5.3 financial periods are available."
            ),
        })
    if external_reason:
        quality.append({
            "kind": "unavailable",
            "field_path": "dhan.company_info",
            "reason": external_reason,
        })
    period_values: list[dict[str, Any]] = []
    period_fields = (
        "revenue", "net_income", "eps", "operating_margin", "total_assets",
        "total_liabilities", "total_equity", "operating_cash_flow",
        "free_cash_flow", "total_debt", "shares_outstanding",
    )
    periods_have_values = any(
        getattr(row, field) is not None for row in rows for field in period_fields
    )
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
            # Yahoo's statement API stores a zero sentinel when its shares-outstanding
            # field is absent. A listed company's outstanding share count is not a
            # meaningful zero, so expose this sentinel as unavailable.
            if field == "shares_outstanding" and value == Decimal("0"):
                value = None
            values[field] = {"value": _number(value), "field_path": path}
            if value is None:
                quality.append({
                    "kind": "unavailable",
                    "field_path": path,
                    "reason": (
                        "The provider returned a zero placeholder for an absent shares-outstanding value."
                        if field == "shares_outstanding"
                        else "The stored provider row has no value for this field."
                    ),
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
    has_fundamental_values = snapshot_has_values or periods_have_values
    if not has_fundamental_values:
        quality.append({
            "kind": "unavailable",
            "field_path": "instrument_fundamentals",
            "reason": "No non-null provider fundamentals are stored.",
        })
    return {
        "status": "available" if has_fundamental_values else "unavailable",
        "snapshot": snapshot_section,
        "periods": period_values,
    }, rows


def _financial_trend_section(
    rows: list[InstrumentFundamental], quality: list[dict[str, str]]
) -> dict[str, Any]:
    metrics = ("revenue", "net_income", "eps", "free_cash_flow")
    if not rows or not any(
        getattr(row, metric) is not None for row in rows for metric in metrics
    ):
        quality.append({
            "kind": "unavailable",
            "field_path": "instrument_fundamentals.financial_trend",
            "reason": "No non-null stored values are available for year-on-year comparison.",
        })
        return {
            "status": "unavailable",
            "periods": [],
            "reason": "No non-null stored financial values are available.",
        }

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
            elif current_value is None or prior_value is None:
                quality.append({
                    "kind": "unavailable",
                    "field_path": field_path,
                    "reason": "A current or prior stored value is unavailable.",
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
        trends.append({
            "period_type": row.period_type,
            "period_end": row.period_end.isoformat(),
            "currency": row.currency,
            "values": values,
        })
    return {"status": "available", "periods": trends}


def _technicals_section(
    db: Session,
    instrument_id: UUID,
    quality: list[dict[str, str]],
    *,
    selected_source: str | None = None,
) -> dict[str, Any]:
    instrument = db.get(Instrument, instrument_id)
    if instrument is not None and instrument.is_synthetic:
        quality.append({
            "kind": "synthetic",
            "field_path": "instruments.is_synthetic",
            "reason": "This instrument is marked as synthetic in the instrument master.",
        })
    sync_query = select(MarketDataSync).where(
            MarketDataSync.instrument_id == instrument_id,
            MarketDataSync.interval == "1d",
        )
    if selected_source is not None:
        sync_query = sync_query.where(MarketDataSync.source == selected_source)
    if instrument is not None:
        sync_query = sync_query.where(
            MarketDataSync.source == "mock"
            if instrument.is_synthetic
            else MarketDataSync.source != "mock"
        )
    latest_sync = db.scalar(
        sync_query.order_by(MarketDataSync.attempted_at.desc()).limit(1)
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
    bar_query = select(MarketBar).where(
        MarketBar.instrument_id == instrument_id,
        MarketBar.interval == "1d",
    )
    if selected_source is not None:
        bar_query = bar_query.where(MarketBar.source == selected_source)
    if instrument is not None:
        bar_query = bar_query.where(
            MarketBar.is_synthetic.is_(instrument.is_synthetic)
        )
    else:
        bar_query = bar_query.where(MarketBar.is_synthetic.is_(False))
    latest = db.scalar(
        bar_query
        .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
        .limit(1)
    )
    bars: list[MarketBar] = []
    synthetic_bars_excluded = 0
    if instrument is not None and not instrument.is_synthetic:
        synthetic_bars_excluded = int(
            db.scalar(
                select(func.count(MarketBar.id)).where(
                    MarketBar.instrument_id == instrument_id,
                    MarketBar.interval == "1d",
                    MarketBar.is_synthetic.is_(True),
                )
            ) or 0
        )
        if synthetic_bars_excluded:
            quality.append({
                "kind": "synthetic",
                "field_path": "market_bars[interval=1d].is_synthetic",
                "reason": (
                    f"Excluded {synthetic_bars_excluded} synthetic daily bars from "
                    "technical indicators."
                ),
            })
    if latest is not None:
        bars_query = select(MarketBar).where(
            MarketBar.instrument_id == instrument_id,
            MarketBar.interval == "1d",
            MarketBar.source == latest.source,
            MarketBar.is_synthetic.is_(latest.is_synthetic),
        )
        if selected_source is not None:
            bars_query = bars_query.where(MarketBar.source == selected_source)
        bars = list(db.scalars(
            bars_query.order_by(MarketBar.timestamp.desc())
            .limit(settings.indicator_lookback_bars)
        ))
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
                    f"Insufficient stored daily bars from "
                    f"{latest.source if latest else selected_source or 'unavailable'} "
                    f"(interval=1d): requires {result.bars_required}, has {result.bars_used}."
                ),
            })
        if result.is_synthetic:
            quality.append({
                "kind": "synthetic",
                "field_path": field_path,
                "reason": "The indicator includes synthetic stored market bars.",
            })
    latest_is_stale = False
    staleness: dict[str, Any] | None = None
    if latest is None:
        quality.append({
            "kind": "unavailable",
            "field_path": "market_bars[interval=1d]",
            "reason": (
                "Only synthetic daily bars are stored; they were excluded."
                if synthetic_bars_excluded
                else (
                    f"No stored daily OHLCV bars from {selected_source} for interval=1d."
                    if selected_source is not None
                    else "No stored daily OHLCV bars are available."
                )
            ),
        })
    else:
        staleness = daily_bar_staleness(
            latest.timestamp, datetime.now(timezone.utc),
            country=instrument.country if instrument is not None else None,
            continuous=instrument is not None and (instrument.asset_class or "").lower() == "crypto",
            age_limit_seconds=settings.daily_close_price_max_age_seconds,
        )
        latest_is_stale = staleness["stale"]
        if latest_is_stale:
            quality.append({
                "kind": "stale",
                "field_path": "market_bars[interval=1d]",
                "reason": staleness["reason"],
            })
        if latest.is_synthetic:
            quality.append({
                "kind": "synthetic",
                "field_path": "market_bars[interval=1d]",
                "reason": "The latest stored daily bar is synthetic.",
            })
    has_indicator_values = any(item.value is not None for item in calculated.__dict__.values())
    technical_status = (
        "unavailable"
        if not bars
        else "illustrative"
        if instrument is not None and instrument.is_synthetic
        else "stale"
        if latest_is_stale
        else "available"
        if has_indicator_values
        else "insufficient_history"
    )
    return {
        "status": technical_status,
        "message": (
            "Illustrative synthetic data; not a real market signal."
            if technical_status == "illustrative"
            else "Stored daily bars are stale; do not use for current decisions."
            if technical_status == "stale"
            else (
                "Only synthetic daily bars are stored; they were excluded. "
                "Sync history from a configured provider."
                if synthetic_bars_excluded
                else "No real daily bars are available. Sync history from a configured provider."
            )
            if technical_status == "unavailable"
            else "Real stored bars do not yet meet the minimum history for any indicator."
            if technical_status == "insufficient_history"
            else None
        ),
        "interval": "1d",
        "source": latest.source if latest else None,
        "latest_bar": latest.timestamp.isoformat() if latest else None,
        "latest_close": _number(bars[-1].close) if bars else None,
        "staleness": staleness,
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
            "reason": (
                "No authorized configured source returned a parseable headline. "
                "Add an issuer investor-relations or exchange feed URL."
            ),
        })
    return {
        "status": "available" if news_items else "unavailable",
        "message": (
            None
            if news_items
            else "No authorized news feeds are configured. Add an issuer investor-relations or exchange feed URL."
        ),
        "items": news_items,
        "sources": source_refs,
    }


def render_brief(
    db: Session,
    instrument: Instrument,
    fetched: list[FetchedSource],
    failures: list[dict[str, str]],
    *,
    selected_source: str | None = None,
    external_snapshot: dict[str, Any] | None = None,
    external_reason: str | None = None,
    sentiment_section: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    quality: list[dict[str, str]] = []
    fundamentals, periods = _fundamentals_section(
        db,
        instrument.id,
        quality,
        external_snapshot=external_snapshot,
        external_reason=external_reason,
    )
    technicals = _technicals_section(
        db, instrument.id, quality, selected_source=selected_source
    )
    financial_trend = _financial_trend_section(periods, quality)
    news = _source_sections(fetched, quality)
    sentiment = sentiment_section or aggregate_instrument_sentiment(db, instrument.id, period_days=30)
    sentiment["validation_status"] = "provisional_unlabelled"
    sentiment["classifier_version"] = CLASSIFIER_VERSION
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
        "news_sentiment": sentiment,
        "data_quality": {
            "status": "issues" if quality else "clear",
            "items": quality,
        },
        "disclaimer": DISCLAIMER,
    }
    # Scoring failure must never discard the evidence pack; it is reported, not swallowed.
    try:
        sections["assessment"] = build_assessment(sections, instrument_id=str(instrument.id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("research_assessment_failed error_class=%s", type(exc).__name__)
        sections["assessment"] = {"error": f"Scoring failed ({type(exc).__name__}); evidence is unaffected."}
    return sections, quality


def _progress_event(
    db: Session,
    run: InstrumentResearchRun,
    component: str,
    state: str,
    detail: str,
    **metadata: Any,
) -> bool:
    db.refresh(run)
    if run.status == "cancelled":
        return False
    if run.cancel_requested:
        run.status = "cancelled"
        run.finished_at = datetime.now(timezone.utc)
        events = list(run.progress or [])
        events.append({"component": component, "state": "cancelled",
                       "detail": "The run stopped at a safe component boundary.",
                       "updated_at": datetime.now(timezone.utc).isoformat()})
        run.progress = events[-40:]
        db.commit()
        return False
    events = list(run.progress or [])
    events.append({
        "component": component,
        "state": state,
        "detail": detail,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **metadata,
    })
    run.progress = events[-40:]
    db.commit()
    db.refresh(run)
    return True


def start_instrument_research(
    db: Session, user_id: UUID, instrument: Instrument
) -> tuple[InstrumentResearchRun, bool]:
    """Create one active per-user/instrument run or reuse the current one."""
    now = datetime.now(timezone.utc)
    active = db.scalar(select(InstrumentResearchRun).where(
        InstrumentResearchRun.user_id == user_id,
        InstrumentResearchRun.instrument_id == instrument.id,
        InstrumentResearchRun.status == "running",
    ).order_by(InstrumentResearchRun.started_at.desc()).limit(1))
    if active is not None:
        started = active.started_at.replace(tzinfo=timezone.utc) if active.started_at.tzinfo is None else active.started_at
        if now - started < timedelta(minutes=15):
            return active, False
        active.status = "failed"
        active.finished_at = now
        active.failures = [*(active.failures or []), {
            "source": "orchestrator", "stage": "recovery", "url": "",
            "reason": "The previous research worker stopped before completion; this run is safe to retry.",
        }]
        active.progress = [*(active.progress or []), {
            "component": "recovery", "state": "failed",
            "detail": "The prior run exceeded the recovery window.", "updated_at": now.isoformat(),
        }]
        db.commit()
    recent = db.scalar(select(InstrumentResearchRun).where(
        InstrumentResearchRun.user_id == user_id,
        InstrumentResearchRun.instrument_id == instrument.id,
        InstrumentResearchRun.status.in_(["complete", "partial"]),
    ).order_by(InstrumentResearchRun.started_at.desc()).limit(1))
    if recent is not None and recent.finished_at is not None:
        finished = recent.finished_at.replace(tzinfo=timezone.utc) if recent.finished_at.tzinfo is None else recent.finished_at
        # Debounce repeated clicks briefly after completion as well as while a
        # run is active; source collection can legitimately take over a minute.
        if now - finished < timedelta(minutes=5):
            return recent, False
    run = InstrumentResearchRun(
        id=uuid4(), instrument_id=instrument.id, user_id=user_id, started_at=now,
        finished_at=None, status="running", sources_attempted=0, sources_succeeded=0,
        failures=[], progress=[{
            "component": "orchestrator", "state": "queued",
            "detail": "Research run accepted for this instrument.", "updated_at": now.isoformat(),
        }],
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active = db.scalar(select(InstrumentResearchRun).where(
            InstrumentResearchRun.user_id == user_id,
            InstrumentResearchRun.instrument_id == instrument.id,
            InstrumentResearchRun.status == "running",
        ).order_by(InstrumentResearchRun.started_at.desc()).limit(1))
        if active is None:
            raise
        return active, False
    db.refresh(run)
    return run, True


def _refresh_yahoo_fundamentals(
    db: Session, instrument: Instrument
) -> tuple[str, str | None, str | None]:
    snapshot = db.scalar(select(InstrumentSnapshot).where(
        InstrumentSnapshot.instrument_id == instrument.id,
        InstrumentSnapshot.source == "yfinance",
    ))
    now = datetime.now(timezone.utc)
    snapshot_has_values = snapshot is not None and any(
        getattr(snapshot, field) is not None
        for field in ("market_cap", "pe", "pb", "dividend_yield", "sector", "industry")
    )
    periods_have_values = bool(db.scalar(select(func.count()).select_from(InstrumentFundamental).where(
        InstrumentFundamental.instrument_id == instrument.id,
        InstrumentFundamental.source == "yfinance",
        (InstrumentFundamental.revenue.is_not(None)) | (InstrumentFundamental.net_income.is_not(None)) |
        (InstrumentFundamental.eps.is_not(None)) | (InstrumentFundamental.total_assets.is_not(None)),
    )))
    if snapshot is not None and (snapshot_has_values or periods_have_values) and not fundamentals_service.is_stale(snapshot.fetched_at, now):
        return "cached", None, snapshot.fetched_at.isoformat()
    try:
        providers = _providers(create_market_data_provider_chain(("yfinance",)))
        provider = next((item for item in providers if item.name == "yfinance"), None)
        if provider is None:
            return "unavailable", "Yahoo Finance collector is not configured.", None
        candidate, _mapping = _resolve_provider_candidate(db, instrument, provider)
        _rows, snapshot = fundamentals_service.sync_instrument_fundamentals(
            db, instrument, candidate.provider_symbol
        )
        return "refreshed", None, snapshot.fetched_at.isoformat()
    except (BrokerAdapterError, HTTPException, MarketDataProviderError, TimeoutError, ValueError) as exc:
        db.rollback()
        category = getattr(exc, "code", type(exc).__name__)
        return "failed", str(category)[:120], None


def _cached_dhan_snapshot(
    db: Session, user_id: UUID, instrument_id: UUID
) -> dict[str, Any] | None:
    previous = latest_brief(db, user_id, instrument_id)
    if previous is None:
        return None
    section = (previous[1].sections or {}).get("fundamentals", {})
    snapshot = section.get("snapshot") if isinstance(section, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("source") != "dhan":
        return None
    fetched = snapshot.get("fetched_at")
    if not fetched:
        return None
    try:
        timestamp = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
        timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
    except (AttributeError, ValueError):
        return None
    if datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc) <= timedelta(hours=24):
        return snapshot
    return None


def _execute_instrument_research(
    db: Session, run: InstrumentResearchRun
) -> tuple[InstrumentResearchRun, InstrumentResearchBrief | None]:
    instrument = db.get(Instrument, run.instrument_id)
    if instrument is None:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.failures = [{"source": "instrument_master", "stage": "resolve", "url": "",
                         "reason": "The selected instrument no longer exists."}]
        db.commit()
        return run, None
    if not _progress_event(db, run, "checking_existing_data", "running", "Inspecting stored market, financial, news and sentiment data."):
        return run, None
    latest_sync = db.scalar(select(MarketDataSync).where(
        MarketDataSync.instrument_id == instrument.id, MarketDataSync.interval == "1d"
    ).order_by(MarketDataSync.attempted_at.desc()).limit(1))
    latest_bar = db.scalar(select(MarketBar).where(
        MarketBar.instrument_id == instrument.id, MarketBar.interval == "1d",
        MarketBar.is_synthetic.is_(instrument.is_synthetic),
    ).order_by(MarketBar.timestamp.desc()).limit(1))
    if not _progress_event(
        db, run, "checking_existing_data", "completed", "Stored data inspection completed.",
        daily_bar_at=latest_bar.timestamp.isoformat() if latest_bar else None,
        daily_bar_source=latest_bar.source if latest_bar else None,
        last_market_sync_status=latest_sync.status if latest_sync else None,
    ):
        return run, None
    bar_age = datetime.now(timezone.utc) - (
        latest_bar.timestamp.replace(tzinfo=timezone.utc)
        if latest_bar and latest_bar.timestamp.tzinfo is None
        else latest_bar.timestamp.astimezone(timezone.utc) if latest_bar else datetime.now(timezone.utc)
    )
    technical_state = "missing" if latest_bar is None else "stale" if bar_age > timedelta(days=7) else "cached"
    _progress_event(
        db, run, "technical_intelligence", technical_state,
        "Using existing daily bars; the existing market-data history-sync endpoint handles missing bars before this run.",
        source=latest_bar.source if latest_bar else None,
        retrieved_at=latest_bar.retrieved_at.isoformat() if latest_bar else None,
        validation_status="stored" if latest_bar else "missing",
    )

    external_snapshot = None
    external_reason = None
    yahoo_state = "not_checked"
    yahoo_timestamp = None
    yahoo_reason = None
    try:
        yahoo_state, yahoo_reason, yahoo_timestamp = _refresh_yahoo_fundamentals(db, instrument)
    except Exception as exc:  # Collector failure remains isolated from other components.
        logger.warning("Yahoo fundamentals refresh failed error_class=%s", type(exc).__name__)
        yahoo_state, yahoo_reason, yahoo_timestamp = "failed", type(exc).__name__, None
    if not _progress_event(
        db, run, "yahoo_fundamentals", yahoo_state,
        yahoo_reason or "Independent supplementary financial collector; stale after 95 days.",
        source="yfinance", retrieved_at=yahoo_timestamp,
    ):
        return run, None

    dhan_state = "unsupported"
    if not instrument.is_synthetic:
        external_snapshot = _cached_dhan_snapshot(db, run.user_id, instrument.id)
        if external_snapshot is not None:
            dhan_state = "cached"
        else:
            external_snapshot, external_reason = _fetch_dhan_company_info(db, run.user_id, instrument)
            dhan_state = "refreshed" if external_snapshot else "unavailable"
    if not _progress_event(
        db, run, "dhan_company_info", dhan_state,
        "Dhan company overview is an independent optional financial source; it does not replace filings or statements.",
        source="dhan" if external_snapshot else None,
        retrieved_at=external_snapshot.get("fetched_at") if external_snapshot else None,
    ):
        return run, None

    fetched_result = fetch_instrument_sources(db, run.user_id, instrument)
    news_state = "refreshed" if fetched_result.sources else "unavailable"
    if not _progress_event(
        db, run, "company_ir_news", news_state,
        "Checked enabled, permitted company and publisher source links for this instrument.",
        sources_attempted=fetched_result.attempted, sources_succeeded=len(fetched_result.sources),
    ):
        return run, None

    try:
        sentiment = aggregate_instrument_sentiment(db, instrument.id, period_days=30)
        sentiment_state = sentiment["status"]
        sentiment_detail = f"Existing classifier {CLASSIFIER_VERSION}; sentiment remains provisional and does not drive a recommendation."
    except Exception as exc:
        sentiment = {"status": "unavailable", "sentiment": None, "score": None,
                     "confidence_score": None, "events": [], "article_count": 0, "event_count": 0,
                     "coverage": {}, "classifier_version": CLASSIFIER_VERSION}
        sentiment_state = "failed"
        sentiment_detail = f"Sentiment lookup failed ({type(exc).__name__}); other components continue."
    if not _progress_event(
        db, run, "news_sentiment", sentiment_state, sentiment_detail,
        classifier_version=CLASSIFIER_VERSION,
        event_count=sentiment.get("event_count", 0), article_count=sentiment.get("article_count", 0),
    ):
        return run, None

    selected_source = _exclusive_selected_source(db, run.user_id)
    sections, quality = render_brief(
        db, instrument, fetched_result.sources, fetched_result.failures,
        selected_source=selected_source,
        external_snapshot=external_snapshot, external_reason=external_reason,
        sentiment_section=sentiment,
    )
    nse_filings_count = db.scalar(select(func.count()).select_from(NewsItem).where(
        NewsItem.instrument_id == instrument.id, NewsItem.source == "nse"
    )) or 0
    poll_rows = {row.source: row for row in db.scalars(select(NewsSourcePoll).where(
        NewsSourcePoll.source.in_(["nse", "google_news", "gdelt"])
    )).all()}

    def poll_status(source: str) -> dict[str, Any]:
        row = poll_rows.get(source)
        if row is None:
            return {"state": "not_polled"}
        if row.last_successful_at is None:
            state = "unavailable" if row.last_error_category or row.rate_limited else "not_polled"
        elif row.rate_limited or row.last_error_category:
            state = "partial"
        elif row.articles_discovered == 0:
            state = "successful_empty"
        else:
            state = "available"
        return {"state": state, "last_attempted_at": row.last_attempted_at.isoformat(),
                "last_successful_at": row.last_successful_at.isoformat() if row.last_successful_at else None,
                "last_article_at": row.last_article_at.isoformat() if row.last_article_at else None,
                "articles_discovered": row.articles_discovered,
                "last_error_category": row.last_error_category, "rate_limited": row.rate_limited}

    collector_status = {
        "nse_filings": {**poll_status("nse"), "mapped_records": int(nse_filings_count)},
        "bse_filings": {"state": "unavailable", "detail": "No BSE filing collector is currently installed."},
        "screener_fundamentals": {"state": "unavailable", "detail": "No Screener.in collector is currently installed."},
        "yahoo_fundamentals": {"state": yahoo_state, "source": "yfinance", "retrieved_at": yahoo_timestamp},
        "dhan_company_info": {"state": dhan_state, "source": "dhan" if external_snapshot else None},
        "company_ir_feeds": {"state": news_state, "source_count": len(fetched_result.sources)},
        "google_news": poll_status("google_news"),
        "gdelt": poll_status("gdelt"),
    }
    sections["collector_status"] = collector_status
    if not _progress_event(db, run, "validating_evidence", "completed",
                           "Component values retain their source and freshness; missing sources remain unavailable."):
        return run, None
    usable_sections = (
        sections["technicals"]["status"] == "available"
        or sections["fundamentals"]["status"] == "available"
        or sections["news"]["status"] == "available"
        or sentiment.get("status") in {"available", "stale"}
    )
    all_sections_available = all(sections[name]["status"] == "available"
                                 for name in ("technicals", "fundamentals", "news"))
    if not usable_sections:
        status = "failed"
    elif fetched_result.failures or not all_sections_available or quality:
        status = "partial"
    else:
        status = "complete"
    run = db.scalar(select(InstrumentResearchRun).where(
        InstrumentResearchRun.id == run.id,
    ).with_for_update())
    if run is None or run.status != "running":
        return run, None
    if run.cancel_requested:
        run.status = "cancelled"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return run, None
    if not _progress_event(db, run, "preparing_snapshot", "running", "Persisting the immutable research brief and provenance."):
        return run, None
    run = db.get(InstrumentResearchRun, run.id)
    if run is None or run.status == "cancelled":
        return run, None
    run.sources_attempted = fetched_result.attempted
    run.sources_succeeded = len(fetched_result.sources)
    run.failures = fetched_result.failures
    run.finished_at = datetime.now(timezone.utc)
    run.status = status
    brief = InstrumentResearchBrief(
        id=uuid4(), run_id=run.id, instrument_id=instrument.id,
        sections=jsonable_encoder(sections), renderer="template",
        data_quality=jsonable_encoder(quality), created_at=run.finished_at,
    )
    db.add(brief)
    db.commit()
    db.refresh(run)
    db.refresh(brief)
    _progress_event(db, run, "preparing_snapshot", "completed", "Immutable research brief and provenance persisted.")
    return run, brief


def fail_instrument_research_run(db: Session, run_id: UUID) -> None:
    """Persist a consistent terminal failure after logging the active exception."""
    db.rollback()
    failed = db.get(InstrumentResearchRun, run_id)
    if failed is not None and failed.status == "running":
        failed_at = datetime.now(timezone.utc)
        failed_events = list(failed.progress or [])
        for event in failed_events:
            if event.get("state") == "running":
                event["state"] = "failed"
                event["detail"] = "Component failed before its result could be saved."
                event["updated_at"] = failed_at.isoformat()
        failed_events.append({
            "component": "orchestrator", "state": "failed",
            "detail": "Research run failed before a complete snapshot was persisted.",
            "updated_at": failed_at.isoformat(),
        })
        failed.status = "cancelled" if failed.cancel_requested else "failed"
        failed.finished_at = failed_at
        if not failed.cancel_requested:
            failed.failures = [*(failed.failures or []), {
                "source": "orchestrator", "stage": "execute", "url": "",
                "reason": "Research orchestration failed; see backend traceback for details.",
            }]
            failed_events[-1]["state"] = "failed"
        else:
            failed_events[-1]["state"] = "cancelled"
            failed_events[-1]["detail"] = "Run cancelled after the current source request."
        failed.progress = failed_events[-40:]
        db.commit()
    logger.exception("Instrument research run failed run_id=%s", run_id)


def process_instrument_research(run_id: UUID) -> None:
    from sqlalchemy.orm import Session as DatabaseSession

    with DatabaseSession(engine) as db:
        run = db.get(InstrumentResearchRun, run_id)
        if run is None or run.status != "running":
            return
        try:
            _execute_instrument_research(db, run)
        except Exception:
            fail_instrument_research_run(db, run_id)


def recover_stale_research_run(run: InstrumentResearchRun, db: Session) -> None:
    if run.status != "running":
        return
    now = datetime.now(timezone.utc)
    started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
    if now - started <= timedelta(minutes=15):
        return
    run.status = "failed"
    run.finished_at = now
    run.failures = [*(run.failures or []), {
        "source": "orchestrator", "stage": "recovery", "url": "",
        "reason": "The run exceeded the recovery window and can be retried.",
    }]
    run.progress = [*(run.progress or []), {
        "component": "recovery", "state": "failed",
        "detail": "A previous worker stopped before completion.", "updated_at": now.isoformat(),
    }]
    db.commit()


def run_instrument_research(
    db: Session, user_id: UUID, instrument: Instrument
) -> tuple[InstrumentResearchRun, InstrumentResearchBrief]:
    """Synchronous compatibility helper for internal callers and focused tests."""
    run, created = start_instrument_research(db, user_id, instrument)
    if not created:
        existing = db.scalar(select(InstrumentResearchBrief).where(InstrumentResearchBrief.run_id == run.id))
        if existing is None:
            raise RuntimeError("research_run_already_active")
        return run, existing
    run, brief = _execute_instrument_research(db, run)
    if brief is None:
        raise RuntimeError("research_run_cancelled_or_interrupted")
    return run, brief


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
