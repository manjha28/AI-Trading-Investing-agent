"""Provider-backed company fundamentals with explicit missing values."""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Callable
from uuid import uuid4

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.market_data import MarketDataProviderError
from app.models import Instrument, InstrumentFundamental, InstrumentSnapshot
from app.provider_adapters import YahooFinanceProvider

MAX_PERIODS_PER_TYPE = 12
logger = logging.getLogger(__name__)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _period_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    converter = getattr(value, "to_pydatetime", None)
    if converter is not None:
        converted = converter()
        return converted.date() if isinstance(converted, datetime) else None
    return None


def _statement_value(frame: Any, labels: tuple[str, ...], period: Any) -> Decimal | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    available = {str(label).casefold(): label for label in frame.index}
    for label in labels:
        actual = available.get(label.casefold())
        if actual is not None:
            try:
                return _decimal(frame.loc[actual, period])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _period_rows(
    income: Any,
    balance: Any,
    cashflow: Any,
    period_type: str,
    currency: str,
) -> list[dict[str, Any]]:
    if income is None or getattr(income, "empty", True):
        return []
    periods = sorted(
        (period for period in income.columns if _period_date(period) is not None),
        key=lambda period: _period_date(period) or date.min,
    )[-MAX_PERIODS_PER_TYPE:]
    result: list[dict[str, Any]] = []
    for period in periods:
        revenue = _statement_value(income, ("Total Revenue", "Operating Revenue"), period)
        net_income = _statement_value(income, ("Net Income", "Net Income Common Stockholders"), period)
        operating_income = _statement_value(income, ("Operating Income",), period)
        operating_margin = (
            operating_income / revenue * Decimal("100")
            if operating_income is not None and revenue not in (None, Decimal("0"))
            else None
        )
        result.append({
            "period_type": period_type,
            "period_end": _period_date(period),
            "currency": currency,
            "revenue": revenue,
            "net_income": net_income,
            "eps": _statement_value(income, ("Basic EPS", "Diluted EPS"), period),
            "operating_margin": operating_margin,
            "total_assets": _statement_value(balance, ("Total Assets",), period),
            "total_liabilities": _statement_value(
                balance,
                ("Total Liabilities Net Minority Interest", "Total Liabilities"),
                period,
            ),
            "total_equity": _statement_value(
                balance,
                ("Stockholders Equity", "Total Equity Gross Minority Interest"),
                period,
            ),
            "operating_cash_flow": _statement_value(
                cashflow, ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"), period
            ),
            "free_cash_flow": _statement_value(cashflow, ("Free Cash Flow",), period),
            "total_debt": _statement_value(balance, ("Total Debt",), period),
            "shares_outstanding": _statement_value(
                balance, ("Ordinary Shares Number", "Share Issued"), period
            ),
        })
    return result


def fetch_yfinance_fundamentals(provider_symbol: str) -> dict[str, Any]:
    """Fetch bounded annual and quarterly statement rows from Yahoo Finance."""
    def fetch() -> dict[str, Any]:
        ticker = yf.Ticker(provider_symbol)
        def field(name: str, read: Callable[[], Any]) -> Any:
            try:
                return YahooFinanceProvider._thread_call(read)
            except MarketDataProviderError as error:
                logger.warning(
                    "Fundamentals field unavailable provider=yfinance field=%s error_class=%s",
                    name,
                    type(error).__name__,
                )
                return None

        return {
            "info": field("snapshot", ticker.get_info) or {},
            "annual_income": field("annual_income", lambda: ticker.income_stmt),
            "quarterly_income": field(
                "quarterly_income", lambda: ticker.quarterly_income_stmt
            ),
            "annual_balance": field("annual_balance", lambda: ticker.balance_sheet),
            "quarterly_balance": field(
                "quarterly_balance", lambda: ticker.quarterly_balance_sheet
            ),
            "annual_cashflow": field("annual_cashflow", lambda: ticker.cashflow),
            "quarterly_cashflow": field(
                "quarterly_cashflow", lambda: ticker.quarterly_cashflow
            ),
        }

    result = fetch()
    info = result.get("info")
    annual = _period_rows(
        result.get("annual_income"),
        result.get("annual_balance"),
        result.get("annual_cashflow"),
        "annual",
        str((info or {}).get("currency") or ""),
    )
    quarterly = _period_rows(
        result.get("quarterly_income"),
        result.get("quarterly_balance"),
        result.get("quarterly_cashflow"),
        "quarterly",
        str((info or {}).get("currency") or ""),
    )
    snapshot = {
        "market_cap": _decimal((info or {}).get("marketCap")),
        "pe": _decimal((info or {}).get("trailingPE")),
        "pb": _decimal((info or {}).get("priceToBook")),
        "dividend_yield": _decimal((info or {}).get("dividendYield")),
        "sector": (info or {}).get("sector"),
        "industry": (info or {}).get("industry"),
    }
    if not annual and not quarterly and not info:
        raise MarketDataProviderError("yfinance_fundamentals_unavailable")
    return {"annual": annual, "quarterly": quarterly, "snapshot": snapshot}


def sync_instrument_fundamentals(
    db: Session,
    instrument: Instrument,
    provider_symbol: str,
    *,
    fetched_at: datetime | None = None,
) -> tuple[list[InstrumentFundamental], InstrumentSnapshot]:
    data = fetch_yfinance_fundamentals(provider_symbol)
    observed_at = fetched_at or datetime.now(timezone.utc)
    rows: list[InstrumentFundamental] = []
    for period_type in ("annual", "quarterly"):
        for values in data[period_type]:
            values["currency"] = values["currency"] or instrument.currency
            existing = db.scalar(
                select(InstrumentFundamental).where(
                    InstrumentFundamental.instrument_id == instrument.id,
                    InstrumentFundamental.period_type == period_type,
                    InstrumentFundamental.period_end == values["period_end"],
                )
            )
            if existing is None:
                existing = InstrumentFundamental(
                    id=uuid4(),
                    instrument_id=instrument.id,
                    source="yfinance",
                    fetched_at=observed_at,
                    **values,
                )
                db.add(existing)
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
                existing.source = "yfinance"
                existing.fetched_at = observed_at
            rows.append(existing)

    snapshot_values = data["snapshot"]
    snapshot = db.scalar(
        select(InstrumentSnapshot).where(InstrumentSnapshot.instrument_id == instrument.id)
    )
    if snapshot is None:
        snapshot = InstrumentSnapshot(
            id=uuid4(),
            instrument_id=instrument.id,
            source="yfinance",
            fetched_at=observed_at,
            **snapshot_values,
        )
        db.add(snapshot)
    else:
        for key, value in snapshot_values.items():
            setattr(snapshot, key, value)
        snapshot.source = "yfinance"
        snapshot.fetched_at = observed_at
    db.commit()
    for row in rows:
        db.refresh(row)
    db.refresh(snapshot)
    return rows, snapshot


def is_stale(fetched_at: datetime, now: datetime | None = None) -> bool:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    timestamp = fetched_at.replace(tzinfo=timezone.utc) if fetched_at.tzinfo is None else fetched_at
    return (reference - timestamp).total_seconds() > 95 * 24 * 60 * 60
