"""Template-rendered, provenance-preserving instrument research briefs."""

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
    MAX_BARS,
    MarketDataProviderChain,
    create_market_data_provider_chain,
    sync_history,
)
from app.broker_adapters import BrokerAdapterError, DhanNoData
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
    User,
)
from app.research_service import is_stale as is_source_stale
from app.services import fundamentals as fundamentals_service
from app.services.indicators import compute_indicators
from app.services import news_highlights
from app.services.news_sentiment import CLASSIFIER_VERSION, aggregate_instrument_sentiment
from app.services.price_freshness import is_continuous_market, is_official_session_close
from app import yahoo_guard
from app.services import dhan_company_info, valuation_inputs
from app.services import valuation as valuation_service
from app.services.research_scoring import build_assessment, daily_bar_staleness, expected_last_completed_session
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
    """Verified Dhan fields only; see services/dhan_company_info.py for what is (and is deliberately not) read."""
    return dhan_company_info.normalize(data, security_id)


NO_COMPANY_DATA_MESSAGE = "Dhan has no company data for this security (common for ETFs, funds and recent listings); fundamentals stay unavailable and are not retried for a week."


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
    if dhan_company_info.has_no_data(security_id):
        return None, NO_COMPANY_DATA_MESSAGE
    try:
        _, provider, _ = _market_data_credentials(db, user_id, "dhan")
        if provider is None:
            return None, "No authenticated Dhan connection is available."
        def request() -> dict[str, Any] | None:
            data = provider.adapter.get_company_info(
                provider.tokens["access_token"], provider.tokens, security_id, segment, metrics=("CO", "RATIOS", "SHP")
            )
            return _company_info_snapshot(data, security_id)

        snapshot = dhan_company_info.fetch_once(security_id, request)  # concurrent runs share one request
        if snapshot is None or (snapshot["status"] == "unavailable" and snapshot["shareholding"]["status"] == "unavailable"):
            return None, "Dhan Company Info returned no usable company-overview or ratio fields."
        snapshot = dict(snapshot)
        # Every usable Dhan field goes to the valuation input store (first source in the Dhan -> Yahoo -> Screener chain); the brief keeps JSON only.
        inputs = snapshot.pop("valuation_inputs", None)
        if inputs:
            valuation_inputs.store(db, instrument.id, "dhan", inputs, datetime.fromisoformat(snapshot["fetched_at"]))
        return snapshot, None
    except HTTPException:
        return None, "Dhan Company Info is unavailable; verify the Dhan connection and its data access."
    except DhanNoData:
        dhan_company_info.mark_no_data(security_id)
        return None, NO_COMPANY_DATA_MESSAGE
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
        snapshot_section["values"] = {name: dict(item) for name, item in external_snapshot.get("values", {}).items()}
        alternate_fields: dict[str, dict[str, str]] = {}
        conflicts: list[dict[str, str]] = []
        # Relative disagreement tolerated before two providers' numbers are called conflicting (providers price at different times).
        tolerance = {"market_cap": Decimal("0.10"), "pb": Decimal("0.10"), "pe": Decimal("0.25")}
        if snapshot is not None and snapshot.source != external_snapshot.get("source"):
            values = snapshot_section["values"]
            for field in ("market_cap", "pe", "pb", "dividend_yield", "sector", "industry"):
                stored_value = getattr(snapshot, field)
                entry = values.get(field) if isinstance(values.get(field), dict) else None
                current_value = entry.get("value") if entry else None
                if stored_value is None:
                    continue
                if current_value is None and field != "dividend_yield":
                    # Dhan returned null / missing / "" for this field: keep the existing stored value, with ITS source and time.
                    # (dividend_yield is excluded: providers use different units, so it is never moved between sources.)
                    values[field] = {"value": str(stored_value) if isinstance(stored_value, Decimal) else stored_value,
                                     "field_path": f"instrument_snapshots.{field}", "source": snapshot.source,
                                     "fetched_at": snapshot.fetched_at.isoformat(), "filled_from_existing": True}
                    continue
                alternate_fields[field] = {
                    "value": str(stored_value), "source": snapshot.source,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                }
                if current_value is not None and field != "dividend_yield":
                    try:
                        a_val, b_val = Decimal(str(current_value)), Decimal(str(stored_value))
                        differs = (abs(a_val - b_val) / abs(b_val) > tolerance.get(field, Decimal(0))) if b_val else a_val != b_val
                    except (InvalidOperation, ValueError):
                        differs = str(current_value).casefold() != str(stored_value).casefold()
                    if differs:
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
            provenance = (row.field_sources or {}).get(field)
            if provenance and value is not None:
                # A field filled by another provider (e.g. Screener) keeps its own source/time/basis.
                values[field].update(source=provenance.get("source"), fetched_at=provenance.get("fetched_at"),
                                     basis=provenance.get("basis"), derived=bool(provenance.get("derived")))
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
        extras = dict(row.extra_metrics or {})
        conflicts = extras.pop("screener_conflicts", None) or {}
        for conflict_field, detail in conflicts.items():
            quality.append({
                "kind": "conflicting",
                "field_path": f"{row_path}.{conflict_field}",
                "reason": (
                    f"{str(detail.get('source', 'screener')).capitalize()} reports {detail['screener']} but {detail['existing_source']} reports {detail['existing']} "
                    f"({Decimal(detail['relative_diff']) * 100:.1f}% apart); the existing value was kept."
                ),
            })
        period_values.append({
            "period_type": row.period_type,
            "period_end": row.period_end.isoformat(),
            "currency": row.currency,
            "values": values,
            "source": row.source,
            "fetched_at": row.fetched_at.isoformat(),
            "stale": stale,
            "statement_basis": row.statement_basis,
            "extra_metrics": extras,
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


def classify_change(
    current: Decimal | None, prior: Decimal | None
) -> tuple[str, Decimal | None, Decimal | None]:
    """(kind, percentage growth, absolute change) for a period-over-period comparison.

    A percentage is only reported when both values are positive (or the current value is zero),
    where it is unambiguous. Across a sign change, or from a non-positive base, a percentage
    misleads (e.g. 37.12 -> -116.30 is "-413%"), so the kind and the absolute change are reported instead.
    Raw values are never altered.
    """
    if current is None or prior is None:
        return "unavailable", None, None
    absolute = current - prior
    if prior == 0:
        return ("unchanged" if current == 0 else "from_zero"), None, absolute
    if prior > 0:
        if current >= 0:
            return "growth", absolute / prior * Decimal("100"), absolute
        return "turned_negative", None, absolute
    if current > 0:
        return "turned_positive", None, absolute
    if current == 0:
        return "turned_zero", None, absolute  # a loss/outflow that reached break-even
    return ("both_negative_narrowed" if current > prior else "both_negative_widened" if current < prior
            else "both_negative_unchanged"), None, absolute


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
            change_kind, growth, absolute_change = classify_change(current_value, prior_value)
            field_path = f"{current_path}.{metric}"
            values[metric] = {
                "value": _number(current_value),
                "year_on_year_growth_pct": _number(growth),
                "change_kind": change_kind,
                "absolute_change": _number(absolute_change),
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


def _reconcile_dhan_snapshot(
    fundamentals: dict[str, Any], technicals: dict[str, Any], periods: list[InstrumentFundamental], quality: list[dict[str, str]]
) -> None:
    """Flag (never overwrite) Dhan P/B and market cap that disagree with the current price. Needs a CURRENT verified close."""
    snapshot = fundamentals.get("snapshot") or {}
    if snapshot.get("source") != "dhan" or not isinstance(snapshot.get("values"), dict):
        return
    price = None
    problem = (technicals.get("staleness") or {}).get("reason") or technicals.get("message") or f"technical data is {technicals.get('status')}"
    if technicals.get("status") == "available":
        try:
            price = Decimal(str(technicals.get("latest_close")))
            price = price if price > 0 else None
        except (InvalidOperation, TypeError, ValueError):
            price = None
    shares_row = next((r for r in sorted(periods, key=lambda r: r.period_end, reverse=True) if r.shares_outstanding and r.shares_outstanding > 0), None)
    result = dhan_company_info.reconcile(
        snapshot["values"], price=price, price_problem=problem, shares=shares_row.shares_outstanding if shares_row else None,
        shares_problem=None if shares_row else "no shares-outstanding figure is stored",
    )
    snapshot["reconciliation"] = result
    for name, check in result.items():
        if name in snapshot["values"]:
            snapshot["values"][name]["reconciliation"] = check["status"]
        if check["status"] == "discrepancy":
            quality.append({"kind": "discrepancy", "field_path": f"dhan.company_info.{name}", "reason": check["reason"]})


IST = ZoneInfo("Asia/Kolkata")
BENCHMARK_SYMBOLS = ("NIFTY 50", "NIFTY50", "NIFTY", "NIFTYBEES")


def _daily_frame(db: Session, instrument_id: UUID, source: str | None, *, limit: int = 1800):
    """Completed real daily OHLCV (oldest first) as a DataFrame indexed by session date. Floats feed indicator maths only."""
    import pandas as pd
    query = select(MarketBar).where(MarketBar.instrument_id == instrument_id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False))
    if source:
        query = query.where(MarketBar.source == source)
    rows = db.scalars(query.order_by(MarketBar.timestamp.desc()).limit(limit)).all()
    if not rows:
        return None
    return pd.DataFrame([(pd.Timestamp(r.timestamp.date()), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume or 0)) for r in reversed(rows)],
                        columns=["date", "open", "high", "low", "close", "volume"]).drop_duplicates("date").set_index("date")


def _benchmark_series(db: Session):
    """NIFTY 50 (or a tracking ETF) daily closes if real bars are stored; None otherwise, so relative strength is reported as not scored."""
    for symbol in BENCHMARK_SYMBOLS:
        inst = db.scalar(select(Instrument).where(func.upper(Instrument.symbol) == symbol, Instrument.is_synthetic.is_(False)).limit(1))
        if inst is not None:
            frame = _daily_frame(db, inst.id, None, limit=1800)
            if frame is not None and len(frame) >= 150:
                return frame["close"]
    return None


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
    _reconcile_dhan_snapshot(fundamentals, technicals, periods, quality)
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
        "shareholding": (fundamentals.get("snapshot") or {}).get("shareholding") or {
            "status": "unavailable", "source": None, "retrieved_at": None, "reporting_period": None,
            "reporting_period_note": "Not provided by source", "holdings": [],
            "warnings": ["No shareholding data was retrieved (Dhan Company Info unavailable or not mapped)."]},
        "data_quality": {
            "status": "issues" if quality else "clear",
            "items": quality,
        },
        "disclaimer": DISCLAIMER,
    }
    # One valuation evidence layer: computed first, then read by BOTH the production scoring engine and the AI Analyst.
    try:
        sections["valuation"] = valuation_service.build_valuation(db, instrument, sections)
    except Exception as exc:  # noqa: BLE001
        logger.exception("research_valuation_failed error_class=%s", type(exc).__name__)
        sections["valuation"] = {"error": f"Valuation metrics failed ({type(exc).__name__}); other sections are unaffected."}
    # Scoring failure must never discard the evidence pack; it is reported, not swallowed.
    try:
        sections["assessment"] = build_assessment(sections, instrument_id=str(instrument.id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("research_assessment_failed error_class=%s", type(exc).__name__)
        sections["assessment"] = {"error": f"Scoring failed ({type(exc).__name__}); evidence is unaffected."}
    try:
        from app.services import research_alerts, research_levels, technical_engine
        if technicals.get("status") == "available":
            import pandas as pd
            frame = _daily_frame(db, instrument.id, technicals.get("source"))
            sections["technical_engine"] = technical_engine.analyze(frame, _benchmark_series(db), as_of=pd.Timestamp(datetime.now(IST).date())) if frame is not None else {
                "status": "unavailable", "reason": "No daily candles are stored.", "horizons": {}}
        else:
            sections["technical_engine"] = {"status": "suppressed", "reason": technicals.get("message") or "Daily price data is not current, so technical levels are suppressed.", "horizons": {}}
        sections["levels"] = research_levels.build_levels(technicals, sections.get("assessment"), sections["technical_engine"])
        sections["buy_confidence"] = research_alerts.confidence_by_horizon(sections)
    except Exception as exc:  # noqa: BLE001
        logger.exception("research_levels_failed error_class=%s", type(exc).__name__)
        sections["levels"] = {"error": f"Levels failed ({type(exc).__name__}); other sections are unaffected."}
    # Each added section fails alone: a gap in one never blanks the report.
    try:
        sections["shareholding_history"] = _shareholding_history(periods)
    except Exception as exc:  # noqa: BLE001
        logger.exception("research_shareholding_history_failed error_class=%s", type(exc).__name__)
        sections["shareholding_history"] = {"status": "unavailable", "error": f"Shareholding history failed ({type(exc).__name__}); other sections are unaffected."}
    try:
        sections["news_highlights"] = news_highlights.build(sentiment, news["items"], instrument.id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("research_news_highlights_failed error_class=%s", type(exc).__name__)
        sections["news_highlights"] = {"status": "unavailable", "error": f"News highlights failed ({type(exc).__name__}); other sections are unaffected."}
    return sections, quality

_HOLDING_KEYS = (("promoter_holding_pct", "Promoters"), ("fii_holding_pct", "FIIs"), ("dii_holding_pct", "DIIs"),
                 ("government_holding_pct", "Government"), ("public_holding_pct", "Public"))
SHAREHOLDING_HISTORY_QUARTERS = 12


def _holding_value(row: InstrumentFundamental, key: str) -> Decimal | None:
    raw = (row.extra_metrics or {}).get(key)
    if isinstance(raw, dict):
        raw = raw.get("value")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))        # via string: never Decimal(float)
    except InvalidOperation:
        return None


def _shareholding_history(periods: list[InstrumentFundamental]) -> dict[str, Any]:
    """Quarterly holding % per category with change vs the previous stored quarter and vs the same quarter a year earlier.

    Values come only from stored quarterly rows (Screener page imports). A quarter, category or comparison that is not stored is None, never 0.
    A change is computed only between ADJACENT quarters (about 3 months apart) or a true year-ago quarter (about 12 months apart).
    """
    quarters = sorted((p for p in periods if p.period_type == "quarterly"), key=lambda p: p.period_end, reverse=True)
    quarters = [q for q in quarters if any(_holding_value(q, k) is not None for k, _ in _HOLDING_KEYS)]
    if not quarters:
        return {"status": "unavailable", "source": None, "quarters": [], "categories": [],
                "message": "No quarterly shareholding history is stored for this company. Import a saved Screener page to add it; Dhan gives an undated snapshot only."}
    by_end = {q.period_end: q for q in quarters}

    def near(end: date, days: int) -> InstrumentFundamental | None:
        target = end - timedelta(days=days)
        hits = [q for e, q in by_end.items() if abs((e - target).days) <= 20]
        return hits[0] if hits else None

    shown = quarters[:SHAREHOLDING_HISTORY_QUARTERS]
    categories = []
    for key, label in _HOLDING_KEYS:
        points = []
        for q in shown:
            now_v, prev, year = _holding_value(q, key), near(q.period_end, 91), near(q.period_end, 365)
            prev_v, year_v = (_holding_value(prev, key) if prev else None), (_holding_value(year, key) if year else None)
            points.append({"period_end": q.period_end.isoformat(), "percent": None if now_v is None else str(now_v),
                           "change_qoq": None if now_v is None or prev_v is None else str(now_v - prev_v),
                           "change_yoy": None if now_v is None or year_v is None else str(now_v - year_v)})
        if any(p["percent"] is not None for p in points):
            categories.append({"key": key, "label": label, "points": points})
    return {"status": "available", "source": "screener_import", "quarters": [q.period_end.isoformat() for q in shown], "categories": categories,
            "message": None, "note": "Change is in percentage points. \u2014 means that quarter is not stored; it is never filled or assumed zero."}


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
        _refresh_yahoo_valuation_inputs(db, instrument, now)
        return "cached", None, snapshot.fetched_at.isoformat()
    if not yahoo_guard.allowed():
        return "unavailable", f"Yahoo Finance is blocking or rate-limiting this server; its calls are paused for about {yahoo_guard.seconds_left() // 3600 + 1} h.", None
    try:
        providers = _providers(create_market_data_provider_chain(("yfinance",)))
        provider = next((item for item in providers if item.name == "yfinance"), None)
        if provider is None:
            return "unavailable", "Yahoo Finance collector is not configured.", None
        candidate, _mapping = _resolve_provider_candidate(db, instrument, provider)
        _rows, snapshot = fundamentals_service.sync_instrument_fundamentals(
            db, instrument, candidate.provider_symbol
        )
        yahoo_guard.record(True)
        return "refreshed", None, snapshot.fetched_at.isoformat()
    except (BrokerAdapterError, HTTPException, MarketDataProviderError, TimeoutError, ValueError) as exc:
        yahoo_guard.record(False)
        db.rollback()
        category = getattr(exc, "code", type(exc).__name__)
        return "failed", str(category)[:120], None


def _refresh_yahoo_valuation_inputs(db: Session, instrument: Instrument, now: datetime) -> None:
    """Best effort: Yahoo's price-linked valuation inputs are usable for 3 days, so refresh them even when the statements are cached."""
    stored = (valuation_inputs.load(db, instrument.id).get("yfinance") or {})
    newest = max((datetime.fromisoformat(r["retrieved_at"]) for r in stored.values()), default=None)
    if newest is not None and now - newest.astimezone(timezone.utc) < timedelta(hours=12):
        return
    if not yahoo_guard.allowed():
        return
    try:
        providers = _providers(create_market_data_provider_chain(("yfinance",)))
        provider = next((item for item in providers if item.name == "yfinance"), None)
        if provider is None:
            return
        candidate, _mapping = _resolve_provider_candidate(db, instrument, provider)
        fundamentals_service.refresh_valuation_inputs(db, instrument, candidate.provider_symbol)
        yahoo_guard.record(True)
    except Exception:  # noqa: BLE001 - Yahoo is optional; any failure counts toward the shared breaker
        yahoo_guard.record(False)
        db.rollback()
        logger.warning("yahoo_valuation_inputs_refresh_failed instrument_id=%s", instrument.id)


def _cached_dhan_snapshot(
    db: Session, user_id: UUID, instrument_id: UUID
) -> dict[str, Any] | None:
    previous = latest_brief(db, user_id, instrument_id)
    if previous is None:
        return None
    section = (previous[1].sections or {}).get("fundamentals", {})
    snapshot = section.get("snapshot") if isinstance(section, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("source") != "dhan" or snapshot.get("schema_version") != dhan_company_info.SCHEMA_VERSION:
        return None  # older cached snapshots lack shareholding and contain fields that are no longer integrated
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


# ───────────── Automatic recovery of the latest completed daily candle ─────────────
_SESSION_FETCH_LOCKS: dict[tuple[UUID, date], threading.Lock] = {}
_SESSION_FETCH_GUARD = threading.Lock()
_SESSION_FETCH_FAILURES: dict[tuple[UUID, date], tuple[float, dict[str, Any]]] = {}
SESSION_FETCH_RETRY_SECONDS = 60  # do not hammer providers when a candle is not published yet


def latest_session_candle_state(
    db: Session, instrument: Instrument, session_date: date, source: str | None = None
) -> str:
    """present | incomplete | missing for the daily candle of ``session_date``.

    ``present`` needs a real (non-synthetic), internally consistent OHLC row that was retrieved after that session's
    official close. A row stored earlier is a partial candle and is NOT treated as completed.
    """
    start = datetime(session_date.year, session_date.month, session_date.day, tzinfo=timezone.utc)
    query = select(MarketBar).where(
        MarketBar.instrument_id == instrument.id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False),
        MarketBar.timestamp >= start, MarketBar.timestamp < start + timedelta(days=1),
    )
    if source is not None:
        query = query.where(MarketBar.source == source)
    bars = list(db.scalars(query))
    if not bars:
        return "missing"
    def consistent(bar: MarketBar) -> bool:
        return (
            min(bar.open, bar.high, bar.low, bar.close) > 0 and bar.high >= max(bar.open, bar.close, bar.low)
            and bar.low <= min(bar.open, bar.close) and (bar.volume is None or bar.volume >= 0)
        )
    return "present" if any(consistent(b) and is_official_session_close(b, instrument, session_date) for b in bars) else "incomplete"


def _attempt_reasons(attempts: list[Any]) -> list[dict[str, str]]:
    return [
        {"provider": str(getattr(a, "provider", "")), "outcome": str(getattr(a, "outcome", "")),
         "reason": str(getattr(a, "reason", None) or getattr(a, "reason_category", None) or "")}
        for a in attempts
    ]


def ensure_latest_daily_candle(
    db: Session, instrument: Instrument, user_id: UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    """Recover the latest completed NSE session's daily candle through the existing history-sync path.

    Reuses ``sync_history`` (configured providers in their existing priority, validation, persistence, sync
    records). Never fabricates a candle. Never raises: the caller scores with whatever is stored afterwards, so a
    failure only leaves Technical Intelligence gated by its normal staleness rule, with the reason recorded.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"action": "none", "session": None, "state_before": None, "persisted": None,
                              "provider": None, "attempts": [], "reason": None}
    if instrument.is_synthetic or (instrument.country or "").upper() != "IN" or is_continuous_market(instrument):
        result["reason"] = "not_applicable"
        return result
    session_date, basis = expected_last_completed_session(now)
    if session_date is None:
        result["reason"] = f"nse_calendar_unavailable:{basis}"
        return result
    result["session"] = session_date.isoformat()
    source = _exclusive_selected_source(db, user_id)
    state = latest_session_candle_state(db, instrument, session_date, source)
    result["state_before"] = state
    if state == "present":
        return result  # already stored and valid: no provider request
    key = (instrument.id, session_date)
    with _SESSION_FETCH_GUARD:
        lock = _SESSION_FETCH_LOCKS.setdefault(key, threading.Lock())
    with lock:  # one fetch per instrument and session; concurrent runs wait, then re-check the store
        if latest_session_candle_state(db, instrument, session_date, source) == "present":
            result.update(action="none", persisted=True, reason="stored_by_concurrent_request")
            return result
        failed = _SESSION_FETCH_FAILURES.get(key)
        if failed is not None and time.monotonic() - failed[0] < SESSION_FETCH_RETRY_SECONDS:
            return {**failed[1], "reason": f"{failed[1]['reason']} (retry suppressed for {SESSION_FETCH_RETRY_SECONDS}s)"}
        result["action"] = "fetch"
        attempts: list[dict[str, str]] = []
        try:
            user = db.get(User, user_id)
            provider = dhan_only_provider(db, user_id)
            start = datetime.combine(session_date - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)
            response = sync_history(
                instrument_id=instrument.id, start=start, end=now, background_tasks=None, interval="1d",
                limit=MAX_BARS, provider=provider, _user=user, db=db,
            )
            attempts = _attempt_reasons(response.provider_attempts)
            result["provider"] = next((a["provider"] for a in attempts if a["outcome"] == "used"), None)
        except HTTPException as exc:
            db.rollback()
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            attempts = [{"provider": str(p.get("provider")), "outcome": "unavailable", "reason": str(p.get("reason"))}
                        for p in detail.get("providers", []) if isinstance(p, dict)]
            result["reason"] = str(detail.get("message") or detail.get("stage") or f"HTTP {exc.status_code}")
        except (MarketDataProviderError, BrokerAdapterError, TimeoutError, ValueError) as exc:
            db.rollback()
            result["reason"] = f"{type(exc).__name__}: {getattr(exc, 'code', None) or str(exc)}"[:200]
        result["attempts"] = attempts
        db.expire_all()  # re-read what the sync persisted
        after = latest_session_candle_state(db, instrument, session_date, source)
        result["persisted"] = after == "present"
        if after == "present":
            result["reason"] = None
            _SESSION_FETCH_FAILURES.pop(key, None)
            return result
        problems = "; ".join(f"{a['provider']}: {a['reason'] or a['outcome']}" for a in attempts if a["outcome"] != "used") or "no provider returned the candle"
        result["action"] = "failed"
        result["reason"] = (
            f"Latest completed NSE session {session_date.isoformat()} candle could not be stored "
            f"({'only a partial candle exists' if after == 'incomplete' else 'no candle returned'}): "
            f"{result['reason'] + ' - ' if result['reason'] else ''}{problems}"
        )[:600]
        _SESSION_FETCH_FAILURES[key] = (time.monotonic(), dict(result))
        return result


def dhan_only_provider(db: Session, user_id: UUID) -> MarketDataProviderChain:
    """Price-history provider restricted to Dhan, as the owner requires: no fallback to Angel One, Yahoo or any other source.

    Raises HTTPException (caught by the callers, which record the reason) when Dhan is not connected, instead of silently using another provider."""
    _angel, dhan_provider, _upstox = _market_data_credentials(db, user_id, "dhan")
    if dhan_provider is None:
        raise HTTPException(status_code=409, detail={"message": "Dhan is not connected or its session has expired; reconnect Dhan to fetch price history (no other data source is used).", "providers": [{"provider": "dhan", "reason": "authentication_required"}]})
    chain = MarketDataProviderChain(create_market_data_provider_chain(("dhan",), dhan_provider=dhan_provider).providers)
    chain.unavailable_providers = []
    return chain


MIN_DAILY_HISTORY_BARS = 900          # about 3.5 years: enough for weekly and monthly candles
HISTORY_WINDOW_DAYS = 700             # one provider request covers at most MAX_BARS (500) sessions
HISTORY_WINDOWS = 3                   # ~5.7 years in total
_HISTORY_FAILURES: dict[UUID, float] = {}
HISTORY_RETRY_SECONDS = 6 * 3600


def ensure_daily_history(db: Session, instrument: Instrument, user_id: UUID) -> dict[str, Any]:
    """Fetch about 3 years of real daily bars through the existing history-sync path when fewer than MIN_DAILY_HISTORY_BARS are stored.

    Technical scoring and levels need history (ATR, 200-day context, swing lows). Never fabricates data and never raises; a failure is reported
    and retried only after HISTORY_RETRY_SECONDS so a bad symbol is not hammered.
    """
    out: dict[str, Any] = {"action": "none", "bars_before": None, "reason": None}
    if instrument.is_synthetic or (instrument.country or "").upper() != "IN" or is_continuous_market(instrument):
        return out
    have = int(db.scalar(select(func.count(MarketBar.id)).where(MarketBar.instrument_id == instrument.id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False))) or 0)
    out["bars_before"] = have
    if have >= MIN_DAILY_HISTORY_BARS:
        return out
    last_fail = _HISTORY_FAILURES.get(instrument.id)
    if last_fail is not None and time.monotonic() - last_fail < HISTORY_RETRY_SECONDS:
        return {**out, "action": "skipped", "reason": "A recent history fetch for this stock failed; it will be retried later."}
    out["action"] = "fetch"
    try:
        user = db.get(User, user_id)
        provider = dhan_only_provider(db, user_id)
        now = datetime.now(timezone.utc)
        for i in range(HISTORY_WINDOWS):   # sync_history only requests ranges not already stored, so re-runs are cheap
            end = now - timedelta(days=i * HISTORY_WINDOW_DAYS)
            sync_history(instrument_id=instrument.id, start=end - timedelta(days=HISTORY_WINDOW_DAYS), end=end, background_tasks=None, interval="1d",
                         limit=MAX_BARS, provider=provider, _user=user, db=db)
    except HTTPException as exc:
        db.rollback()
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        out["reason"] = str(detail.get("message") or f"HTTP {exc.status_code}")[:300]
    except (MarketDataProviderError, BrokerAdapterError, TimeoutError, ValueError) as exc:
        db.rollback()
        out["reason"] = f"{type(exc).__name__}: {getattr(exc, 'code', None) or str(exc)}"[:300]
    db.expire_all()
    out["bars_after"] = int(db.scalar(select(func.count(MarketBar.id)).where(MarketBar.instrument_id == instrument.id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False))) or 0)
    if out["bars_after"] <= have:
        _HISTORY_FAILURES[instrument.id] = time.monotonic()
        out["reason"] = out["reason"] or "The data provider returned no additional daily bars."
    return out


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
    # Recover a missing latest completed session candle BEFORE scoring; a failure never blocks the run.
    history = ensure_daily_history(db, instrument, run.user_id)   # enough history for ATR / swing lows / pivots
    recovery = ensure_latest_daily_candle(db, instrument, run.user_id)
    if recovery["action"] != "none" and not _progress_event(
        db, run, "market_data_recovery", "completed" if recovery["persisted"] else "failed",
        (f"Latest completed session {recovery['session']} candle {'fetched and stored' if recovery['persisted'] else 'not recovered'}"
         + (f" via {recovery['provider']}." if recovery["provider"] else ".")) if recovery["persisted"] else (recovery["reason"] or "Candle not recovered."),
        source=recovery["provider"], validation_status="persisted" if recovery["persisted"] else "failed",
    ):
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
        "Using stored daily bars (the latest completed session is recovered through the existing history sync first).",
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
    sections["technicals"]["latest_session_recovery"] = recovery
    if recovery["action"] == "failed":
        quality.append({
            "kind": "failed_fetch", "field_path": "market_bars[interval=1d].latest_session",
            "reason": recovery["reason"], "stage": "latest_candle_recovery",
        })
        sections["data_quality"]["status"] = "issues"
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
    sections["market_data_recovery"] = {
        "history": {k: history.get(k) for k in ("action", "bars_before", "bars_after", "reason")},
        "latest_candle": {"session": recovery.get("session"), "state_before": recovery.get("state_before"), "action": recovery.get("action"),
                          "persisted": recovery.get("persisted"), "provider": recovery.get("provider"), "reason": recovery.get("reason"), "attempts": recovery.get("attempts")},
    }
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
    from app.services import research_alerts
    research_alerts.notify_if_new_buy(db, run, brief)
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
