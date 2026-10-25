from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.market_data import (
    MARKET_DATA_FALLBACK_PRIORITY,
    _safe_provider_error_code,
    market_data_provider_for_user,
)
from app.db import engine
from app.market_data import HistoricalBar, InstrumentCandidate, MarketDataProviderError
from app.models import Instrument, User


def _exchange_alias(value: str) -> str:
    return {
        "XNSE": "NSE",
        "NSI": "NSE",
        "NSE_EQ": "NSE",
        "XBOM": "BSE",
        "BOM": "BSE",
        "BSE_EQ": "BSE",
    }.get(value.upper(), value.upper())


def _candidate_for(
    provider,
    instrument: Instrument | None,
    symbol: str,
    exchange: str,
) -> InstrumentCandidate:
    if instrument is not None:
        mapping = next(
            (
                item for item in instrument.provider_identifiers
                if item.provider == provider.name and item.is_verified
            ),
            None,
        )
        if mapping is not None:
            candidate = provider.get_instrument(mapping.provider_instrument_id)
            if (
                candidate.symbol.casefold() == symbol.casefold()
                and _exchange_alias(candidate.venue) == _exchange_alias(exchange)
            ):
                return candidate
    matches = provider.search_instruments(symbol, 50)
    candidate = next(
        (
            item for item in matches
            if item.symbol.casefold() == symbol.casefold()
            and _exchange_alias(item.venue) == _exchange_alias(exchange)
        ),
        None,
    )
    if candidate is None:
        raise MarketDataProviderError("instrument_not_found")
    return candidate


def _verify_provider(
    provider,
    instrument: Instrument | None,
    symbol: str,
    exchange: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    result: dict[str, object] = {
        "provider": provider.name,
        "auth": "configured",
        "mapping": "unresolved",
        "ltp": "unavailable",
        "ltp_timestamp": "unavailable",
        "bars": 0,
        "first_bar": "unavailable",
        "last_bar": "unavailable",
        "latency_ms": 0.0,
        "http_status": None,
        "error": None,
    }
    started = time.monotonic()
    try:
        candidate = _candidate_for(provider, instrument, symbol, exchange)
        result["mapping"] = candidate.provider_instrument_id
        quote = HistoricalBar.model_validate(provider.get_latest_quote(candidate))
        if not start <= quote.timestamp < end:
            # The quote endpoint may report the latest session outside a narrow
            # historical window; it remains a valid quote, not a candle.
            result["ltp"] = str(quote.close)
        else:
            result["ltp"] = str(quote.close)
        result["ltp_timestamp"] = quote.timestamp.isoformat()
        bars = [
            HistoricalBar.model_validate(bar)
            for bar in provider.get_historical_bars(
                candidate, interval, start, end, 5000
            )
        ]
        timestamps = [bar.timestamp for bar in bars]
        if timestamps != sorted(set(timestamps)):
            raise MarketDataProviderError(
                "provider_returned_duplicate_or_unsorted_bars"
            )
        if any(not start <= bar.timestamp < end for bar in bars):
            raise MarketDataProviderError("provider_returned_out_of_range_bar")
        result["bars"] = len(bars)
        if bars:
            result["first_bar"] = bars[0].timestamp.isoformat()
            result["last_bar"] = bars[-1].timestamp.isoformat()
        if not bars:
            result["error"] = "provider_no_history_for_range"
    except MarketDataProviderError as error:
        result["error"] = _safe_provider_error_code(error.code)
        result["http_status"] = error.status_code
    except (TimeoutError, ValueError):
        result["error"] = "provider_request_failed"
    finally:
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["http_status"] = result["http_status"] or getattr(
            provider, "last_response_status", None
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostics for market-data provider access."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--interval", choices=("1d", "1h", "15m", "5m", "1m"), default="1d")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--user-id", type=UUID)
    args = parser.parse_args(argv)
    if args.days < 1 or args.days > 3650:
        parser.error("--days must be between 1 and 3650")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    with Session(engine) as db:
        users = db.scalars(
            select(User).where(User.id == args.user_id)
            if args.user_id is not None
            else select(User).order_by(User.created_at)
        ).all()
        if not users:
            print("No application user was found; market credentials are unavailable.", file=sys.stderr)
            return 2
        if args.user_id is None and len(users) != 1:
            print("Use --user-id when the database has multiple users.", file=sys.stderr)
            return 2
        user = users[0]
        instrument = db.scalar(
            select(Instrument).where(
                Instrument.symbol.ilike(args.symbol),
                Instrument.venue.ilike(args.exchange),
            )
        )
        chain = market_data_provider_for_user(db, user.id)
        configured = {item.name: item for item in chain.providers}
        unavailable = {
            item["provider"]: item["reason"]
            for item in chain.unavailable_providers
        }
        results: dict[str, dict[str, object]] = {}
        for name in MARKET_DATA_FALLBACK_PRIORITY:
            provider = configured.get(name)
            if provider is None:
                results[name] = {
                    "provider": name,
                    "auth": unavailable.get(name, "not_configured"),
                    "mapping": "unresolved",
                    "ltp": "unavailable",
                    "ltp_timestamp": "unavailable",
                    "bars": 0,
                    "first_bar": "unavailable",
                    "last_bar": "unavailable",
                    "latency_ms": 0.0,
                    "http_status": None,
                    "error": _safe_provider_error_code(
                        str(unavailable.get(name, "not_configured"))
                    ),
                }
                continue
            results[name] = _verify_provider(
                provider,
                instrument,
                args.symbol,
                args.exchange,
                args.interval,
                start,
                end,
            )

    headers = (
        "Provider", "Auth", "Token/securityId", "LTP @ timestamp", "Bars",
        "First bar", "Last bar", "Latency ms", "HTTP", "Error",
    )
    print(" | ".join(headers))
    for name in MARKET_DATA_FALLBACK_PRIORITY:
        row = results[name]
        quote = f"{row['ltp']} @ {row['ltp_timestamp']}"
        print(" | ".join(str(value) for value in (
            name,
            row["auth"],
            row["mapping"],
            quote,
            row["bars"],
            row["first_bar"],
            row["last_bar"],
            row["latency_ms"],
            row["http_status"] or "—",
            row["error"] or "—",
        )))

    serving_provider = next(
        (
            name for name in MARKET_DATA_FALLBACK_PRIORITY
            if results[name]["error"] is None and int(results[name]["bars"]) > 0
        ),
        None,
    )
    print(
        "Router decision: "
        + (
            f"{serving_provider} (first provider in fixed priority with valid quote and candles)"
            if serving_provider
            else "unavailable (no provider returned valid candles)"
        )
    )
    if results["angel_one"]["error"] is not None:
        print("Primary provider Angel One could not serve the requested data.", file=sys.stderr)
        return 1
    return 0 if serving_provider else 1


if __name__ == "__main__":
    raise SystemExit(main())
