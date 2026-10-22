"""Ledger-derived portfolio risk metrics; price aggregates never silently convert FX."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting import PortfolioLedger
from app.models import Instrument, MarketQuote, Portfolio, PortfolioTransaction
from app.services.price_freshness import classify_quote

ZERO = Decimal("0")
HUNDRED = Decimal("100")
LTCG_BOUNDARY_DAYS = 365
LTCG_WINDOW_DAYS = 30


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def position_currency_exposure(
    positions: dict[str, Any],
    instrument_by_symbol: dict[str, Instrument],
    quote_by_instrument: dict[UUID, MarketQuote | None],
    now: datetime,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for symbol, position in sorted(positions.items()):
        if position.quantity <= ZERO:
            continue
        instrument = instrument_by_symbol.get(symbol)
        currency = instrument.currency if instrument else "UNKNOWN"
        bucket = grouped.setdefault(
            currency,
            {"currency": currency, "position_count": 0, "market_value": ZERO,
             "valued_positions": 0, "synthetic_positions": 0, "stale_positions": 0},
        )
        bucket["position_count"] += 1
        if instrument is None:
            bucket["stale_positions"] += 1
            continue
        quote = quote_by_instrument.get(instrument.id)
        usable, reason, _evidence = classify_quote(
            quote, instrument, currency, now
        )
        if reason == "synthetic_price":
            bucket["synthetic_positions"] += 1
        elif not usable:
            bucket["stale_positions"] += 1
        else:
            assert quote is not None
            bucket["market_value"] += quote.close * position.quantity
            bucket["valued_positions"] += 1
    return [
        {
            **bucket,
            "complete": bucket["valued_positions"] == bucket["position_count"],
            "market_value": str(bucket["market_value"])
            if bucket["valued_positions"] == bucket["position_count"]
            else None,
        }
        for bucket in grouped.values()
    ]


def build_risk_metrics(
    db: Session, user_id: UUID, portfolio: Portfolio, *, as_of: datetime | None = None
) -> dict[str, Any]:
    now = _utc(as_of or datetime.now(timezone.utc))
    from app.api.portfolio_transactions import calculate_portfolio_ledger

    ledger: PortfolioLedger = calculate_portfolio_ledger(db, user_id, portfolio.id)
    linked_rows = db.execute(
        select(PortfolioTransaction.symbol, PortfolioTransaction.instrument_id)
        .where(
            PortfolioTransaction.portfolio_id == portfolio.id,
            PortfolioTransaction.instrument_id.is_not(None),
            PortfolioTransaction.transaction_type.in_(("buy", "sell")),
        )
        .order_by(PortfolioTransaction.occurred_at.desc(), PortfolioTransaction.created_at.desc())
    ).all()
    instrument_by_symbol: dict[str, Instrument] = {}
    for symbol, instrument_id in linked_rows:
        if symbol and symbol not in instrument_by_symbol:
            instrument = db.get(Instrument, instrument_id)
            if instrument is not None:
                instrument_by_symbol[symbol] = instrument
    instrument_ids = {item.id for item in instrument_by_symbol.values()}
    quotes: dict[UUID, MarketQuote | None] = {}
    if instrument_ids:
        rows = db.scalars(
            select(MarketQuote)
            .where(MarketQuote.instrument_id.in_(instrument_ids))
            .order_by(MarketQuote.retrieved_at.desc())
        )
        for quote in rows:
            quotes.setdefault(quote.instrument_id, quote)

    exposure = position_currency_exposure(
        ledger.positions, instrument_by_symbol, quotes, now
    )
    metrics_by_symbol: dict[str, dict[str, Any]] = {}
    base_values: dict[str, Decimal] = {}
    full_base_valuation = True
    open_positions = {
        symbol: position for symbol, position in ledger.positions.items()
        if position.quantity > ZERO
    }
    for symbol, position in sorted(open_positions.items()):
        instrument = instrument_by_symbol.get(symbol)
        quote = quotes.get(instrument.id) if instrument else None
        reason = None
        value = None
        if instrument is None:
            reason = "instrument_not_linked"
        else:
            usable, reason, _evidence = classify_quote(
                quote, instrument, portfolio.base_currency, now
            )
            if usable and quote is not None:
                value = quote.close * position.quantity
                base_values[symbol] = value
        if reason is not None:
            full_base_valuation = False

        lot_details = []
        boundary_lots = []
        for lot in position.acquisition_lots:
            acquired_at = _utc(lot.acquired_at)
            days_held = (now.date() - acquired_at.date()).days
            days_to_boundary = LTCG_BOUNDARY_DAYS - days_held
            lot_details.append({
                "acquired_at": acquired_at.isoformat(),
                "quantity": str(lot.quantity),
                "days_held": days_held,
                "days_to_365_day_boundary": days_to_boundary,
            })
            if -LTCG_WINDOW_DAYS <= days_to_boundary <= LTCG_WINDOW_DAYS:
                boundary_lots.append(lot_details[-1])
        held_days = [lot["days_held"] for lot in lot_details]
        metrics_by_symbol[symbol] = {
            "symbol": symbol,
            "instrument_id": str(instrument.id) if instrument else None,
            "currency": instrument.currency if instrument else None,
            "quantity": str(position.quantity),
            "market_value": str(value) if value is not None else None,
            "weight_pct": None,
            "valuation_unavailable_reason": reason,
            "days_held_min": min(held_days, default=None),
            "days_held_max": max(held_days, default=None),
            "lots": lot_details,
            "ltcg_boundary_flag": bool(boundary_lots),
            "ltcg_boundary_lots": boundary_lots,
            "tax_label": "Informational only, not tax advice",
        }

    cash = ledger.cash_balance
    denominator = cash + sum(base_values.values(), ZERO)
    if denominator > ZERO and full_base_valuation:
        for symbol, value in base_values.items():
            metrics_by_symbol[symbol]["weight_pct"] = str(value * HUNDRED / denominator)
        cash_pct = cash * HUNDRED / denominator
        weights = sorted(
            (Decimal(item["weight_pct"]) for item in metrics_by_symbol.values()
             if item["weight_pct"] is not None),
            reverse=True,
        )
        top_three = sum(weights[:3], ZERO)
        hhi = sum((weight / HUNDRED) ** 2 for weight in weights)
        concentration_reason = None
    else:
        cash_pct = None
        top_three = None
        hhi = None
        concentration_reason = (
            "Complete fresh, non-synthetic base-currency quotes are required; no FX conversion is applied."
        )

    return {
        "portfolio_id": str(portfolio.id),
        "as_of": now.isoformat(),
        "currency": portfolio.base_currency,
        "position_weights": list(metrics_by_symbol.values()),
        "top_three_concentration_pct": str(top_three) if top_three is not None else None,
        "hhi": str(hhi) if hhi is not None else None,
        "cash_balance": str(cash),
        "cash_pct": str(cash_pct) if cash_pct is not None else None,
        "currency_exposure": exposure,
        "ltcg_boundary_positions": [
            item for item in metrics_by_symbol.values() if item["ltcg_boundary_flag"]
        ],
        "tax_disclaimer": "Informational only, not tax advice.",
        "concentration_unavailable_reason": concentration_reason,
        "disclaimer": "Decision support, not investment advice.",
    }
