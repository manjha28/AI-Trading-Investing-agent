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
    """Classify a quote using retrieval time, synthetic status, and currency only."""
    retrieved_at = _utc(quote.retrieved_at) if quote is not None else None
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
    maximum_age = settings.price_usable_max_age_seconds
    evidence = {
        "price_freshness_basis": "retrieved_at",
        "price_usable_max_age_seconds": maximum_age,
        "quote_retrieved_at": retrieved_at.isoformat() if retrieved_at else None,
        "quote_retrieved_age_seconds": retrieved_age,
        "quote_timestamp": quote_timestamp.isoformat() if quote_timestamp else None,
        "quote_timestamp_age_seconds": timestamp_age,
        "quote_is_synthetic": quote.is_synthetic if quote is not None else None,
        "instrument_currency": instrument.currency if instrument is not None else None,
        "portfolio_currency": portfolio_currency,
        "quote_age_within_limit": (
            retrieved_age is not None and 0 <= retrieved_age <= maximum_age
        ),
    }
    if (
        portfolio_currency is not None
        and instrument is not None
        and instrument.currency != portfolio_currency
    ):
        return False, "foreign_currency_no_fx", evidence
    if quote is None:
        return False, "quote_unavailable", evidence
    if quote.is_synthetic:
        return False, "synthetic_price", evidence
    if retrieved_age is None or not 0 <= retrieved_age <= maximum_age:
        return False, "stale_price", evidence
    return True, None, evidence
