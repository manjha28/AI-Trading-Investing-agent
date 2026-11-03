"""Shared usability classification for stored and API quote objects."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import (
    NSE_CALENDAR_YEARS,
    NSE_EQUITY_HOLIDAYS,
    NSE_FRESHNESS_SESSION_END,
    NSE_FRESHNESS_SESSION_START,
    NSE_NON_TRADING_DATES,
    NSE_SPECIAL_SESSION_DATES,
    NSE_UNVERIFIED_HOLIDAYS,
    settings,
)
from app.models import Instrument, MarketBar
from app.services.price_states import (
    BASIS_PRIOR_CLOSE,
    BASIS_SESSION_CLOSE,
    FRESHNESS_DELAYED,
    FRESHNESS_FUTURE_DATED,
    FRESHNESS_HISTORICAL,
    FRESHNESS_LIVE,
    FRESHNESS_PRIOR_CLOSE,
    FRESHNESS_SESSION_CLOSE,
    FRESHNESS_STALE,
    FRESHNESS_SYNTHETIC,
    FRESHNESS_UNAVAILABLE,
    FRESHNESS_UNKNOWN,
    FRESHNESS_UNVERIFIED,
    basis_for_freshness,
)

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
USABLE_FRESHNESS_STATUSES = frozenset({FRESHNESS_LIVE, FRESHNESS_SESSION_CLOSE})
# Quote sources whose stored daily bars live under a different bar source.
QUOTE_SOURCE_BAR_SOURCE = {"dhan_ws": "dhan", "angel_one_ws": "angel_one"}


@dataclass(frozen=True)
class VenueSession:
    state: str  # "open" | "closed" | "unknown"
    reason: str


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_continuous_market(instrument: Any) -> bool:
    """True for 24/7 markets (crypto, e.g. Delta IN), which have no session boundary."""
    return (getattr(instrument, "asset_class", None) or "").lower() == "crypto"


def venue_session(instrument: Any, now: datetime) -> VenueSession | None:
    """Return the IN venue session state at ``now``, or None when no session model applies.

    None covers non-IN venues (legacy age test), which are CA, CH, DE, FR, GB, JP,
    NL, US and ZZ, and 24/7 markets, which never close.
    """
    country = (getattr(instrument, "country", None) or "").upper()
    if country != "IN" or is_continuous_market(instrument):
        return None
    local = _utc(now).astimezone(IST)
    day = local.date()
    if day.year not in NSE_CALENDAR_YEARS:
        return VenueSession("unknown", "calendar_not_covered")
    if day in NSE_SPECIAL_SESSION_DATES:
        return VenueSession("unknown", "special_session_hours_unconfigured")
    if day in NSE_UNVERIFIED_HOLIDAYS:
        return VenueSession("unknown", "holiday_unverified")
    if day.weekday() >= 5 or day in NSE_EQUITY_HOLIDAYS:
        return VenueSession("closed", "weekend_or_holiday")
    if NSE_FRESHNESS_SESSION_START <= local.time() < NSE_FRESHNESS_SESSION_END:
        return VenueSession("open", "regular_session")
    return VenueSession("closed", "outside_session_window")


def venue_last_completed_session(
    db: Session, instrument: Any, quote_source: str | None
) -> date | None:
    """Latest stored 1d session date on the instrument's venue for one quote source.

    Returns None for non-IN venues (which use the legacy age test) and when no
    stored daily bars exist for the source.
    """
    if (
        quote_source is None
        or (getattr(instrument, "country", None) or "").upper() != "IN"
        or is_continuous_market(instrument)
    ):
        return None
    bar_source = QUOTE_SOURCE_BAR_SOURCE.get(quote_source, quote_source)
    latest_timestamp = db.scalar(
        select(func.max(MarketBar.timestamp))
        .join(Instrument, Instrument.id == MarketBar.instrument_id)
        .where(
            Instrument.country == instrument.country,
            MarketBar.interval == "1d",
            MarketBar.source == bar_source,
        )
    )
    if latest_timestamp is None:
        return None
    # Stored daily keys are UTC midnight of the exchange-local session date.
    return _utc(latest_timestamp).date()


def select_usable_quote(
    db: Session,
    instrument: Any,
    quotes: list[Any],
    now: datetime,
    *,
    allow_delayed: bool = False,
) -> tuple[Any | None, dict[str, Any], list[str]]:
    """Single quote-selection path shared by the watchlist and portfolio valuation.

    Candidates are tried freshest-first by provider timestamp. The first one that
    classifies as usable and non-synthetic wins. Returns ``(quote, evidence,
    rejections)``; ``quote`` is None when no candidate is usable, and ``rejections``
    explains each candidate that was skipped.

    ``allow_delayed`` is for factual valuation and display only. A strictly usable
    candidate still wins; failing that, the freshest candidate accepted by the delayed-
    or prior-close valuation check is returned, and its evidence retains its precise
    freshness state. The default stays strict, so every action-implying caller is unchanged.
    """
    rejections: list[str] = []
    delayed_pick: tuple[Any, dict[str, Any]] | None = None
    ordered = sorted(
        quotes,
        key=lambda quote: (_utc(quote.timestamp), _utc(quote.retrieved_at)),
        reverse=True,
    )
    for quote in ordered:
        usable, reason, evidence = classify_quote(
            quote,
            instrument,
            "INR",
            now,
            last_completed_session=venue_last_completed_session(
                db, instrument, quote.source
            ),
        )
        status = str(evidence.get("quote_freshness_status", ""))
        if usable and not quote.is_synthetic and status in USABLE_FRESHNESS_STATUSES:
            return quote, evidence, rejections
        if (
            allow_delayed
            and delayed_pick is None
            and not quote.is_synthetic
            and (
                delayed_price_acceptable_for_valuation(usable, reason, evidence, now)
                or prior_close_acceptable_for_valuation(
                    usable, reason, evidence, now
                )
            )
        ):
            delayed_pick = (quote, evidence)
            continue
        rejections.append(
            f"cached {quote.source} quote is {status or 'unknown'} "
            f"({reason or 'freshness check failed'})"
        )
    if delayed_pick is not None:
        return delayed_pick[0], delayed_pick[1], rejections
    return None, {}, rejections


def prior_close_applies(price_timestamp: datetime, instrument: Any, now: datetime) -> bool:
    """True when the price belongs to an earlier session and today's session has traded.

    Uses the venue calendar: today must be a trading day whose session is open or has
    reached its freshness end. Any unknown calendar state returns False.
    """
    session = venue_session(instrument, now)
    if session is None or session.state == "unknown":
        return False
    if session.reason == "weekend_or_holiday":
        return False
    local_now = _utc(now).astimezone(IST)
    price_date = _utc(price_timestamp).astimezone(IST).date()
    if price_date >= local_now.date():
        return False
    return session.state == "open" or local_now.time() >= NSE_FRESHNESS_SESSION_END


def valuation_basis_for_quote(
    freshness_status: str, price_timestamp: datetime, instrument: Any, now: datetime
) -> str | None:
    """Valuation basis for a quote's freshness state: ``prior_close`` once today has traded.

    A prior-session quote during an open session is ``prior_close``; ``delayed`` is reserved
    for same-session staleness. Returns None for any state without a basis.
    """
    basis = basis_for_freshness(freshness_status)
    if basis == BASIS_SESSION_CLOSE and prior_close_applies(price_timestamp, instrument, now):
        return BASIS_PRIOR_CLOSE
    return basis


def nse_trading_sessions_elapsed(price_session_date: date, now: datetime) -> int | None:
    """Count NSE trading sessions after ``price_session_date`` up to and including today (IST).

    Returns None when either date falls outside the configured calendar, because
    the count would be a guess.
    """
    today = _utc(now).astimezone(IST).date()
    if today.year not in NSE_CALENDAR_YEARS or price_session_date.year not in NSE_CALENDAR_YEARS:
        return None
    elapsed = 0
    day = price_session_date + timedelta(days=1)
    while day <= today:
        if day in NSE_SPECIAL_SESSION_DATES or (
            day.weekday() < 5 and day not in NSE_NON_TRADING_DATES
        ):
            elapsed += 1
        day += timedelta(days=1)
    return elapsed


def delayed_price_acceptable_for_valuation(
    usable: bool, reason: str | None, evidence: dict[str, Any], now: datetime
) -> bool:
    """True when a ``delayed`` NSE quote may be valued, always labelled ``delayed``.

    Takes the output of ``classify_quote``. This is for a factual valuation at a stated
    time, never for an action-implying output: alerts, risk metrics, signals and thesis
    monitoring keep using ``usable`` from ``classify_quote`` and so still suppress it.

    Accepted only for an open NSE session, a verified provider mapping, and a price no more
    than ``nse_session_close_max_trading_sessions`` trading sessions old (the same cap the
    closed-session rule applies). Synthetic, foreign-currency (no FX), future-dated,
    unverified, unknown-calendar and 24/7-market quotes are never accepted.
    """
    if usable or reason != "delayed_price":
        return False
    if evidence.get("quote_freshness_status") != FRESHNESS_DELAYED:
        return False
    if evidence.get("quote_session_model") != "nse_ist_calendar":
        return False
    if evidence.get("venue_session_state") != "open":
        return False
    if evidence.get("quote_mapping_verified") is not True:
        return False
    session_date = evidence.get("quote_session_date")
    if not session_date:
        return False
    elapsed = nse_trading_sessions_elapsed(date.fromisoformat(session_date), now)
    return elapsed is not None and elapsed <= settings.nse_session_close_max_trading_sessions


def prior_close_acceptable_for_valuation(
    usable: bool, reason: str | None, evidence: dict[str, Any], now: datetime
) -> bool:
    """Allow a verified prior-session NSE close for factual valuation only."""
    if usable or reason != "prior_close_price":
        return False
    if evidence.get("quote_freshness_status") != FRESHNESS_PRIOR_CLOSE:
        return False
    if evidence.get("quote_session_model") != "nse_ist_calendar":
        return False
    if evidence.get("venue_session_state") != "open":
        return False
    if evidence.get("quote_mapping_verified") is not True:
        return False
    if evidence.get("instrument_currency") != evidence.get("portfolio_currency"):
        return False
    session_date = evidence.get("quote_session_date")
    last_completed = evidence.get("last_completed_session")
    if not session_date or not last_completed:
        return False
    if date.fromisoformat(session_date) < date.fromisoformat(last_completed):
        return False
    elapsed = nse_trading_sessions_elapsed(date.fromisoformat(session_date), now)
    return elapsed is not None and elapsed <= settings.nse_session_close_max_trading_sessions


def classify_closed_session_price(
    *,
    now: datetime,
    price_session_date: date | None,
    last_completed_session: date | None,
) -> tuple[str, str | None]:
    """Shared closed-session rule for quotes and stored daily bars.

    Returns ``(status, reason)``. ``reason`` is set only for ``unknown``.
    The trading-session cap is checked first, so a price more than
    ``nse_session_close_max_trading_sessions`` completed sessions old is ``stale``
    even when its session date matches the last completed session.
    """
    if price_session_date is not None:
        elapsed = nse_trading_sessions_elapsed(price_session_date, now)
        if elapsed is None:
            logger.warning(
                "session_state_unknown reason=calendar_not_covered price_session_date=%s",
                price_session_date.isoformat(),
            )
            return FRESHNESS_UNKNOWN, "calendar_not_covered"
        if elapsed > settings.nse_session_close_max_trading_sessions:
            return FRESHNESS_STALE, None
    if last_completed_session is None:
        logger.warning(
            "session_state_unknown reason=last_completed_session_unavailable"
            " price_session_date=%s",
            price_session_date.isoformat() if price_session_date else None,
        )
        return FRESHNESS_UNKNOWN, "last_completed_session_unavailable"
    if price_session_date is not None and price_session_date >= last_completed_session:
        return FRESHNESS_SESSION_CLOSE, None
    return FRESHNESS_STALE, None


def classify_quote(
    quote: Any,
    instrument: Any,
    portfolio_currency: str | None,
    now: datetime,
    *,
    last_completed_session: date | None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Classify whether a quote is safe to use as a current market observation.

    ``last_completed_session`` is required so no caller can omit it silently. Pass
    ``venue_last_completed_session(...)``; it is None only for non-IN venues or
    when no stored daily bars exist. A closed-market IN quote with None is
    ``unknown`` with reason ``last_completed_session_unavailable``.
    """
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
    session = venue_session(instrument, now)
    quote_session_date = (
        quote_timestamp.astimezone(IST).date() if quote_timestamp is not None else None
    )
    fresh_observation = timestamp_is_fresh and retrieved_at_is_fresh
    status = FRESHNESS_LIVE
    unknown_reason = "session_state_unknown"
    if quote is None or quote_timestamp is None:
        status = FRESHNESS_UNAVAILABLE
    elif getattr(quote, "is_synthetic", False):
        status = FRESHNESS_SYNTHETIC
    elif timestamp_is_future:
        status = FRESHNESS_FUTURE_DATED
    elif data_type == "daily_close":
        status = (
            FRESHNESS_HISTORICAL
            if timestamp_age is not None
            and 0 <= timestamp_age <= historical_maximum_age
            and retrieved_age is not None
            and 0 <= retrieved_age <= historical_maximum_age
            else FRESHNESS_STALE
        )
    elif is_continuous_market(instrument):
        # 24/7 market: age alone decides, and no session boundary is ever consulted.
        status = FRESHNESS_LIVE if fresh_observation else FRESHNESS_DELAYED
    elif session is None:
        # Legacy age test for non-IN venues (CA, CH, DE, FR, GB, JP, NL, US, ZZ).
        status = FRESHNESS_LIVE if fresh_observation else FRESHNESS_STALE
    elif session.state == "open":
        if (
            quote_session_date is not None
            and quote_session_date < _utc(now).astimezone(IST).date()
            and last_completed_session is not None
        ):
            status, closed_reason = classify_closed_session_price(
                now=now,
                price_session_date=quote_session_date,
                last_completed_session=last_completed_session,
            )
            if status == FRESHNESS_SESSION_CLOSE:
                status = FRESHNESS_PRIOR_CLOSE
            if closed_reason is not None:
                unknown_reason = closed_reason
        else:
            status = FRESHNESS_LIVE if fresh_observation else FRESHNESS_DELAYED
    elif session.state == "closed":
        status, closed_reason = classify_closed_session_price(
            now=now,
            price_session_date=quote_session_date,
            last_completed_session=last_completed_session,
        )
        if closed_reason is not None:
            unknown_reason = closed_reason
    else:
        status = FRESHNESS_UNKNOWN
    if status in {
        FRESHNESS_LIVE,
        FRESHNESS_SESSION_CLOSE,
        FRESHNESS_PRIOR_CLOSE,
    } and not mapping_verified:
        status = FRESHNESS_UNVERIFIED
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
        "quote_session_model": (
            (
                "continuous_age"
                if is_continuous_market(instrument)
                else "nse_ist_calendar"
                if session is not None
                else "legacy_age"
            )
            if quote is not None
            else None
        ),
        "venue_session_state": session.state if session is not None else None,
        "venue_session_reason": session.reason if session is not None else None,
        "last_completed_session": (
            last_completed_session.isoformat() if last_completed_session else None
        ),
        "quote_session_date": (
            quote_session_date.isoformat() if quote_session_date else None
        ),
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
    if status == "delayed":
        return False, "delayed_price", evidence
    if status == FRESHNESS_PRIOR_CLOSE:
        return False, "prior_close_price", evidence
    if status == "stale":
        return False, "stale_price", evidence
    if status == "unknown":
        return False, unknown_reason, evidence
    if status == "unverified":
        return False, "unverified_provider_mapping", evidence
    return True, None, evidence
