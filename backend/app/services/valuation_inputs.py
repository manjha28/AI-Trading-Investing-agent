"""Provider-reported valuation inputs: extraction to canonical units, storage and loading.

Canonical units: amounts in INR (not crore), ratios as ``x``, percentages as percent numbers (15 means 15%), per-share
figures in INR/share. Nothing here validates or selects a value; the Dhan -> Yahoo Finance -> Screener chain, freshness
and consistency checks live in ``valuation.py``. Null / empty / non-finite values are "not reported", never zero.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ValuationProviderInput

CRORE = Decimal("10000000")
PROVIDERS = ("dhan", "yfinance", "screener")
TEXT_METRICS = ("sector", "industry")


def _num(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Any) -> str | None:
    return value.strip()[:200] or None if isinstance(value, str) else None


def _record(value: Decimal | None, unit: str, *, assumed: bool = False, period: str | None = None) -> dict[str, Any] | None:
    return None if value is None else {"value": value, "unit": unit, "unit_assumed": assumed, "reporting_period": period}


# ───────────────────────── extraction ─────────────────────────
def dhan_inputs(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every usable Dhan Company Info field (CO + RATIOS), converted to canonical units. No field is excluded up front."""
    co = entry.get("CO") if isinstance(entry.get("CO"), dict) else {}
    ratios = entry.get("RATIOS") if isinstance(entry.get("RATIOS"), dict) else {}

    def amount(name: str) -> Decimal | None:  # Dhan amounts are INR crore (audit: market cap verified; EBITDA unit assumed, flagged below)
        value = _num(co.get(name))
        return value * CRORE if value is not None else None

    out = {
        "market_cap": _record(amount("MARKET_CAP"), "INR"),
        "ebitda": _record(amount("EBITDA"), "INR", assumed=True),  # unit "not stated"; validated against other sources in valuation.py
        "eps": _record(_num(co.get("EPS")), "INR/share"),
        "book_value": _record(_num(co.get("BOOK_VALUE")), "INR/share"),
        "dividend_yield": _record(_num(co.get("DIVIDEND_YIELD")), "percent"),
        "pe": _record(_num(ratios.get("PE_RATIO")), "x"),
        "pb": _record(_num(ratios.get("PB_RATIO")), "x"),
        "roe": _record(_num(ratios.get("ROE")), "percent"),
        "roce": _record(_num(ratios.get("ROCE")), "percent"),
        "debt_to_equity": _record(_num(ratios.get("DEBT_TO_EQUITY")), "x"),
        "pe_to_sector_pe": _record(_num(ratios.get("PE_TO_SECTOR_PE")), "ratio"),
        "industry_eps": _record(_num(ratios.get("INDUSTRY_EPS")), "INR/share"),
        "industry_pb": _record(_num(ratios.get("INDUSTRY_PB_RATIO")), "x"),
        "industry_debt_to_equity": _record(_num(ratios.get("INDUSTRY_DEBT_TO_EQUITY")), "x"),
        "industry_current_ratio": _record(_num(ratios.get("INDUSTRY_CURRENT_RATIO")), "x"),
        "industry_dividend_yield": _record(_num(ratios.get("INDUSTRY_DIVIDEND_YIELD")), "percent"),
        "industry_operating_margin": _record(_num(ratios.get("INDUSTRY_OPERATING_MARGIN")), "percent"),
    }
    for name, key in (("sector", "SECTOR"), ("industry", "INDUSTRY")):
        text = _text(co.get(key))
        if text:
            out[name] = {"text": text, "unit": "text"}
    return {k: v for k, v in out.items() if v is not None}


def yahoo_inputs(info: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Yahoo Finance ``info`` fields. returnOnEquity is a fraction (0.2 = 20%); dividendYield is stored as received (percent)."""
    info = info or {}
    roe = _num(info.get("returnOnEquity"))
    peg = _num(info.get("trailingPegRatio"))
    if peg is None:
        peg = _num(info.get("pegRatio"))
    out = {
        "market_cap": _record(_num(info.get("marketCap")), "INR"),
        "pe": _record(_num(info.get("trailingPE")), "x"),
        "pb": _record(_num(info.get("priceToBook")), "x"),
        "price_to_sales": _record(_num(info.get("priceToSalesTrailing12Months")), "x"),
        "peg": _record(peg, "x"),
        "dividend_yield": _record(_num(info.get("dividendYield")), "percent"),
        "enterprise_value": _record(_num(info.get("enterpriseValue")), "INR"),
        "ev_ebitda": _record(_num(info.get("enterpriseToEbitda")), "x"),
        "ebitda": _record(_num(info.get("ebitda")), "INR"),
        "eps": _record(_num(info.get("trailingEps")), "INR/share"),
        "book_value": _record(_num(info.get("bookValue")), "INR/share"),
        "roe": _record(roe * 100 if roe is not None else None, "percent"),
        "total_cash": _record(_num(info.get("totalCash")), "INR"),
        "total_debt": _record(_num(info.get("totalDebt")), "INR"),
    }
    for name, key in (("sector", "sector"), ("industry", "industry")):
        text = _text(info.get(key))
        if text:
            out[name] = {"text": text, "unit": "text"}
    return {k: v for k, v in out.items() if v is not None}


def screener_inputs(top_ratios: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Screener top ratios from an owner-saved page (already parsed to canonical units by the importer)."""
    return {k: v for k, v in (top_ratios or {}).items() if v is not None}


# ───────────────────────── storage ─────────────────────────
def store(db: Session, instrument_id: UUID, provider: str, inputs: dict[str, dict[str, Any]], fetched_at: datetime | None = None) -> int:
    """Upsert one row per metric. A metric the provider no longer reports is removed (so a stale value can never outlive the provider)."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown valuation provider {provider!r}")
    stamp = fetched_at or datetime.now(timezone.utc)
    existing = {row.metric: row for row in db.scalars(select(ValuationProviderInput).where(
        ValuationProviderInput.instrument_id == instrument_id, ValuationProviderInput.provider == provider))}
    for metric, rec in inputs.items():
        row = existing.pop(metric, None)
        if row is None:
            row = ValuationProviderInput(id=uuid4(), instrument_id=instrument_id, provider=provider, metric=metric, fetched_at=stamp)
            db.add(row)
        row.value, row.text_value = rec.get("value"), rec.get("text")
        row.unit, row.unit_assumed = rec.get("unit"), bool(rec.get("unit_assumed"))
        row.reporting_period, row.fetched_at = rec.get("reporting_period"), stamp
    for stale in existing.values():
        db.delete(stale)
    db.flush()
    return len(inputs)


def load(db: Session, instrument_id: UUID) -> dict[str, dict[str, dict[str, Any]]]:
    """{provider: {metric: {value, text, unit, unit_assumed, reporting_period, retrieved_at}}} for one instrument."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in db.scalars(select(ValuationProviderInput).where(ValuationProviderInput.instrument_id == instrument_id)):
        out.setdefault(row.provider, {})[row.metric] = {
            "value": row.value, "text": row.text_value, "unit": row.unit, "unit_assumed": row.unit_assumed,
            "reporting_period": row.reporting_period, "retrieved_at": row.fetched_at.isoformat()}
    return out
