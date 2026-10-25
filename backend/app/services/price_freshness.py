"""Shared usability classification for stored and API quote objects."""

from datetime import datetime, timezone
from typing import Any

from app.config import settings


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def classify_quote(
    quote: Any,
    instrument: Any,
    portfolio_currency: str | None,
    now: datetime,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Classify whether a quote is safe to use as a current market observation."""
    retrieved_at_value = getattr(quote, "retrieved_at", None) if quote is not None else None
    retrieved_at = _utc(retrieved_at_value) if retrieved_at_value is not None else None
    quote_timestamp = (
        getattr(quote, "timestamp", None)
        or getattr(getattr(quote, "bar", None), "timestamp", None)
    )
    quote_timestamp = _utc(quote_timestamp) if quote_timestamp is not None else None
    retrieved_age = (
        (_utc(now) - retrieved_at).total_seconds() if retrieved_at is not None else None
    )
    timestamp_age = (
        (_utc(now) - quote_timestamp).total_seconds()
        if quote_timestamp is not None
        else None
    )
    maximum_age = min(
        settings.price_usable_max_age_seconds,
        settings.market_data_quote_freshness_seconds,
    )
    historical_maximum_age = settings.daily_close_price_max_age_seconds
    clock_skew_tolerance = settings.market_data_clock_skew_tolerance_seconds
    source = getattr(quote, "source", None) if quote is not None else None
    data_type = (
        "daily_close"
        if source == "yfinance"
        else "live_tick"
        if source in {"dhan_ws", "angel_one_ws"}
        else "provider_snapshot"
        if source == "dhan"
        else "provider_quote"
    )
    provider_mappings = getattr(instrument, "provider_identifiers", ())
    mapping_verified = (
        any(
            mapping.provider == (
                "dhan"
                if source == "dhan_ws"
                else "angel_one"
                if source == "angel_one_ws"
                else source
            ) and mapping.is_verified
            for mapping in provider_mappings
        )
        if source is not None
        else False
    )
    timestamp_is_future = (
        timestamp_age is not None and timestamp_age < -clock_skew_tolerance
    )
    timestamp_is_fresh = (
        timestamp_age is not None
        and -clock_skew_tolerance <= timestamp_age <= maximum_age
    )
    retrieved_at_is_fresh = (
        retrieved_age is not None and 0 <= retrieved_age <= maximum_age
    )
    status = "fresh"
    if quote is None or quote_timestamp is None:
        status = "unavailable"
    elif getattr(quote, "is_synthetic", False):
        status = "synthetic"
    elif timestamp_is_future:
        status = "future_dated"
    elif data_type == "daily_close":
        status = (
            "historical"
            if timestamp_age is not None
            and 0 <= timestamp_age <= historical_maximum_age
            and retrieved_age is not None
            and 0 <= retrieved_age <= historical_maximum_age
            else "stale"
        )
    elif not timestamp_is_fresh or not retrieved_at_is_fresh:
        status = "stale"
    elif not mapping_verified:
        status = "unverified"
    evidence = {
        "price_freshness_basis": "market_timestamp_and_retrieved_at",
        "price_usable_max_age_seconds": maximum_age,
        "historical_close_max_age_seconds": historical_maximum_age,
        "market_data_clock_skew_tolerance_seconds": clock_skew_tolerance,
        "quote_retrieved_at": retrieved_at.isoformat() if retrieved_at else None,
        "quote_retrieved_age_seconds": retrieved_age,
        "quote_timestamp": quote_timestamp.isoformat() if quote_timestamp else None,
        "quote_timestamp_age_seconds": timestamp_age,
        "quote_data_type": data_type if quote is not None else None,
        "quote_mapping_verified": mapping_verified if quote is not None else None,
        "quote_freshness_status": status,
        "quote_is_synthetic": (
            getattr(quote, "is_synthetic", None) if quote is not None else None
        ),
        "instrument_currency": instrument.currency if instrument is not None else None,
        "portfolio_currency": portfolio_currency,
        "quote_age_within_limit": timestamp_is_fresh and retrieved_at_is_fresh,
    }
    if (
        portfolio_currency is not None
        and instrument is not None
        and instrument.currency != portfolio_currency
    ):
        return False, "foreign_currency_no_fx", evidence
    if quote is None:
        return False, "quote_unavailable", evidence
    if getattr(quote, "is_synthetic", False):
        return False, "synthetic_price", evidence
    if quote_timestamp is None:
        return False, "market_timestamp_unavailable", evidence
    if timestamp_is_future:
        return False, "future_dated_price", evidence
    if data_type == "daily_close":
        return False, "historical_close_not_current", evidence
    if not timestamp_is_fresh or not retrieved_at_is_fresh:
        return False, "stale_price", evidence
    if not mapping_verified:
        return False, "unverified_provider_mapping", evidence
    return True, None, evidence
