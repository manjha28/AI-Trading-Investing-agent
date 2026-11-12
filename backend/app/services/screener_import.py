"""Optional Screener.in gap-filler over the existing canonical ``instrument_fundamentals`` model.

ACCESS POLICY (see report): Screener's Terms license the site for "personal, non-commercial transitory
viewing only" and forbid copying/mirroring its materials; they do not address automated access. This module
therefore NEVER fetches from screener.in. It parses a company page the owner saved from their own browser
(or pasted) and imports only the gaps. An automated fetcher must not be added without Screener's written
permission; it could reuse ``parse_screener_html`` unchanged.

Rules: existing non-null values are never overwritten (a material disagreement is recorded as a conflict
instead); missing values stay NULL (never zero); every imported field carries its own source, retrieval
time, statement basis and whether it was derived; units are converted from Rs. Crores to INR.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument, InstrumentFundamental
from app.services import valuation_inputs

SOURCE = "screener"
CRORE = Decimal("10000000")
MAX_PERIODS_PER_TYPE = 12
CONFLICT_TOLERANCE = Decimal("0.02")  # >2% relative disagreement with an existing value is a conflict
PERIOD_MATCH_DAYS = 5  # a Screener period within this many days of a stored one is the same period
MAX_HTML_BYTES = 3_000_000
CANONICAL_FIELDS = (
    "revenue", "net_income", "eps", "operating_margin", "total_assets", "total_liabilities", "total_equity",
    "operating_cash_flow", "free_cash_flow", "total_debt", "shares_outstanding",
)
# Screener's free company page has no capex or share-count rows, so these can never be filled from it.
UNFILLABLE_FIELDS = ("free_cash_flow", "shares_outstanding")
EXPECTED_HISTORY = {"annual": 10, "quarterly": 12}


class ScreenerParseError(ValueError):
    pass


# ───────────────────────── HTML table extraction (stdlib only) ─────────────────────────
class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section: str | None = None
        self.container: str | None = None
        self.sub_text: dict[str, str] = {}
        self.tables: list[dict[str, Any]] = []
        self._in_sub = False
        self._table: dict[str, Any] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._sub_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "section" and a.get("id"):
            self.section, self.container = a["id"], None
        elif tag == "div" and a.get("id") and self.section:
            self.container = a["id"]
        elif tag == "p" and "sub" in (a.get("class") or "").split() and self.section:
            self._in_sub, self._sub_buf = True, []
        elif tag == "table" and self.section:
            self._table = {"section": self.section, "container": self.container, "rows": []}
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._in_sub:
            self._in_sub = False
            if self.section:
                self.sub_text.setdefault(self.section, " ".join("".join(self._sub_buf).split()))
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).replace("\xa0", " ").split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._in_sub:
            self._sub_buf.append(data)


def _number(text: str) -> Decimal | None:
    """'1,23,456' -> 123456; '12%' -> 12; blanks and dashes are missing (None), never zero."""
    cleaned = text.replace(",", "").replace("%", "").replace("₹", "").strip()
    if cleaned in {"", "-", "—", "NA", "N/A"}:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _period_end(label: str) -> date | None:
    match = re.fullmatch(r"([A-Za-z]{3})\s+(\d{4})", label.strip())
    if not match:
        return None  # e.g. "TTM"
    month = list(calendar.month_abbr).index(match.group(1).title()) if match.group(1).title() in calendar.month_abbr else 0
    if month == 0:
        return None
    year = int(match.group(2))
    return date(year, month, calendar.monthrange(year, month)[1])


def _label(raw: str) -> str:
    return re.sub(r"\s*\+\s*$", "", raw).strip()


@dataclass
class ParsedPeriod:
    fields: dict[str, Decimal] = field(default_factory=dict)
    derived: set[str] = field(default_factory=set)
    extras: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class ParsedScreener:
    basis: str
    periods: dict[tuple[str, date], ParsedPeriod]
    top_ratios: dict[str, dict[str, Any]] = field(default_factory=dict)  # canonical valuation inputs from the page's top-ratios block


# label -> (canonical field, scale)  /  label -> (extra name, scale)
_PL = {"Sales": ("revenue", CRORE), "Revenue": ("revenue", CRORE), "Net Profit": ("net_income", CRORE),
       "EPS in Rs": ("eps", Decimal(1)), "OPM %": ("operating_margin", Decimal(1))}
_PL_EXTRA = {"Interest": ("interest_expense", CRORE), "Operating Profit": ("operating_profit", CRORE),
             "Depreciation": ("depreciation", CRORE), "Other Income": ("other_income", CRORE),
             "Profit before tax": ("profit_before_tax", CRORE), "Tax %": ("tax_rate_pct", Decimal(1))}
_BS_EXTRA = {"Other Liabilities": ("other_liabilities", CRORE), "Fixed Assets": ("fixed_assets", CRORE),
             "Investments": ("investments", CRORE), "Other Assets": ("other_assets", CRORE)}
_CF = {"Cash from Operating Activity": ("operating_cash_flow", CRORE)}
_CF_EXTRA = {"Cash from Investing Activity": ("cash_from_investing", CRORE),
             "Cash from Financing Activity": ("cash_from_financing", CRORE), "Net Cash Flow": ("net_cash_flow", CRORE)}
_RATIOS_EXTRA = {"Debtor Days": ("debtor_days", Decimal(1)), "Inventory Days": ("inventory_days", Decimal(1)),
                 "Days Payable": ("days_payable", Decimal(1)), "Cash Conversion Cycle": ("cash_conversion_cycle_days", Decimal(1)),
                 "Working Capital Days": ("working_capital_days", Decimal(1)), "ROCE %": ("roce_pct", Decimal(1))}
_SHAREHOLDING = {"Promoters": "promoter_holding_pct", "FIIs": "fii_holding_pct", "DIIs": "dii_holding_pct",
                 "Government": "government_holding_pct", "Public": "public_holding_pct"}
_PLAUSIBLE = {"operating_margin": (Decimal(-500), Decimal(100)), "eps": (Decimal(-1_000_000), Decimal(1_000_000))}


_TOP_RATIOS = {"Market Cap": ("market_cap", "INR"), "Stock P/E": ("pe", "x"), "Book Value": ("book_value", "INR/share"),
               "Dividend Yield": ("dividend_yield", "percent"), "ROCE": ("roce", "percent"), "ROE": ("roe", "percent")}


def _top_ratios(html: str) -> dict[str, dict[str, Any]]:
    """The page's #top-ratios block: Market Cap (Rs. Cr -> INR), Stock P/E, Book Value, Dividend Yield %, ROCE %, ROE %. Unknown names are ignored."""
    block = re.search(r'<div id="top-ratios">(.*?)</ul>', html, re.S)
    if not block:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name_html, value_html in re.findall(r'<li[^>]*>\s*<span class="name">(.*?)</span>\s*<span class="value">(.*?)</li>', block.group(1), re.S):
        name = " ".join(re.sub(r"<[^>]+>", " ", name_html).split())
        if name not in _TOP_RATIOS:
            continue
        number = re.search(r'<span class="number">([^<]*)</span>', value_html)
        value = _number(number.group(1)) if number else None
        if value is None:
            continue
        key, unit = _TOP_RATIOS[name]
        if key == "market_cap":
            if "cr" not in re.sub(r"<[^>]+>", " ", value_html).lower():
                continue  # never guess the scale
            value = value * CRORE
        out[key] = {"value": value, "unit": unit, "unit_assumed": False}
    return out


def parse_screener_html(html: str) -> ParsedScreener:
    if len(html.encode("utf-8", "ignore")) > MAX_HTML_BYTES:
        raise ScreenerParseError("page_too_large")
    parser = _PageParser()
    parser.feed(html)
    subs = parser.sub_text
    statement_sections = [s for s in ("quarters", "profit-loss", "balance-sheet", "cash-flow") if s in subs]
    if not statement_sections:
        raise ScreenerParseError("no_screener_financial_sections_found")
    bases = {("consolidated" if text.lower().startswith("consolidated") else "standalone" if text.lower().startswith("standalone") else "")
             for text in (subs[s] for s in statement_sections)}
    if len(bases) != 1 or "" in bases:
        raise ScreenerParseError("statement_basis_not_identified")
    if not all("rs. crore" in subs[s].lower() for s in statement_sections):
        raise ScreenerParseError("unit_not_rs_crores")  # never guess a scale or currency
    basis = bases.pop()
    periods: dict[tuple[str, date], ParsedPeriod] = {}

    def put(kind: str, end: date, name: str, value: Decimal | None, scale: Decimal, *, canonical: bool, derived: bool = False) -> None:
        if value is None:
            return
        entry = periods.setdefault((kind, end), ParsedPeriod())
        scaled = value * scale
        if canonical:
            low_high = _PLAUSIBLE.get(name)
            if low_high and not low_high[0] <= scaled <= low_high[1]:
                return  # implausible (probable scrape/unit error): excluded, never repaired
            entry.fields[name] = scaled
            if derived:
                entry.derived.add(name)
        else:
            entry.extras[name] = scaled

    def rows_of(section: str, container: str | None = None) -> tuple[list[date | None], dict[str, list[Decimal | None]]]:
        candidates = [t for t in parser.tables if t["section"] == section and (container is None or t["container"] == container)]
        if not candidates:
            return [], {}
        rows = candidates[0]["rows"]
        header = rows[0]
        ends = [_period_end(cell) for cell in header[1:]]
        body = {_label(row[0]): [_number(cell) for cell in row[1:]] for row in rows[1:] if row and row[0]}
        return ends, body

    def ingest(section: str, kind: str, canon: dict, extras: dict, container: str | None = None) -> dict[str, list[Decimal | None]]:
        ends, body = rows_of(section, container)
        for label, values in body.items():
            for mapping, canonical in ((canon, True), (extras, False)):
                if label in mapping:
                    name, scale = mapping[label]
                    for end, value in zip(ends, values):
                        if end is not None:
                            put(kind, end, name, value, scale, canonical=canonical)
        return body

    ingest("quarters", "quarterly", _PL, _PL_EXTRA)
    ingest("profit-loss", "annual", _PL, _PL_EXTRA)
    ingest("cash-flow", "annual", _CF, _CF_EXTRA)
    ingest("ratios", "annual", {}, _RATIOS_EXTRA)
    # Balance sheet: equity = capital + reserves, debt = borrowings; total liabilities (ex-equity) is derived.
    ends, body = rows_of("balance-sheet")
    for index, end in enumerate(ends):
        if end is None:
            continue

        def cell(name: str) -> Decimal | None:
            values = body.get(name)
            return values[index] if values and index < len(values) else None

        capital, reserves, borrowings, other, assets = (cell(n) for n in ("Equity Capital", "Reserves", "Borrowings", "Other Liabilities", "Total Assets"))
        if capital is not None and reserves is not None:
            put("annual", end, "total_equity", capital + reserves, CRORE, canonical=True, derived=True)
        put("annual", end, "total_debt", borrowings, CRORE, canonical=True)
        put("annual", end, "total_assets", assets, CRORE, canonical=True)
        if borrowings is not None and other is not None:
            put("annual", end, "total_liabilities", borrowings + other, CRORE, canonical=True, derived=True)
        for label, (name, scale) in _BS_EXTRA.items():
            put("annual", end, name, cell(label), scale, canonical=False)
    # Shareholding (quarterly pattern).
    ends, body = rows_of("shareholding", "quarterly-shp") if any(t["container"] == "quarterly-shp" for t in parser.tables) else rows_of("shareholding")
    for label, name in _SHAREHOLDING.items():
        for end, value in zip(ends, body.get(label, [])):
            if end is not None:
                put("quarterly", end, name, value, Decimal(1), canonical=False)
    periods = {key: value for key, value in periods.items() if value.fields or value.extras}
    if not periods:
        raise ScreenerParseError("no_periods_parsed")
    return ParsedScreener(basis=basis, periods=periods, top_ratios=_top_ratios(html))


# ───────────────────────── Gap audit ─────────────────────────
def fundamentals_gaps(db: Session, instrument_id: Any) -> dict[str, Any]:
    """Which canonical fields are NULL per stored period, and how deep the stored history is."""
    rows = db.scalars(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == instrument_id)
                      .order_by(InstrumentFundamental.period_type, InstrumentFundamental.period_end)).all()
    report: dict[str, Any] = {}
    for kind in ("annual", "quarterly"):
        subset = [r for r in rows if r.period_type == kind]
        missing = {}
        for row in subset:
            absent = [f for f in CANONICAL_FIELDS if _is_missing(row, f)]
            if absent:
                missing[row.period_end.isoformat()] = absent
        fillable = {end: [f for f in fields if f not in UNFILLABLE_FIELDS] for end, fields in missing.items()}
        report[kind] = {
            "stored_periods": len(subset), "expected_history": EXPECTED_HISTORY[kind],
            "missing_history_periods": max(0, EXPECTED_HISTORY[kind] - len(subset)),
            "missing_fields_by_period": missing,
            "screener_fillable": {end: fields for end, fields in fillable.items() if fields},
        }
    report["unfillable_from_screener"] = list(UNFILLABLE_FIELDS)
    return report


def _is_missing(row: InstrumentFundamental, name: str) -> bool:
    value = getattr(row, name)
    return value is None or (name == "shares_outstanding" and value == 0)  # 0 shares is a provider placeholder


# ───────────────────────── Gap fill ─────────────────────────
@dataclass
class GapFillResult:
    created_periods: list[str] = field(default_factory=list)
    filled: dict[str, list[str]] = field(default_factory=dict)
    extras_written: int = 0
    conflicts: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    unchanged_periods: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"created_periods": self.created_periods, "filled_fields": self.filled, "extras_written": self.extras_written,
                "conflicts": self.conflicts, "skipped": self.skipped, "unchanged_periods": self.unchanged_periods}


def _same_period(db: Session, instrument_id: Any, kind: str, end: date) -> InstrumentFundamental | None:
    rows = db.scalars(select(InstrumentFundamental).where(
        InstrumentFundamental.instrument_id == instrument_id, InstrumentFundamental.period_type == kind,
        InstrumentFundamental.period_end.between(end - timedelta(days=PERIOD_MATCH_DAYS), end + timedelta(days=PERIOD_MATCH_DAYS)),
    )).all()
    return min(rows, key=lambda r: abs((r.period_end - end).days), default=None)


def apply_gap_fill(
    db: Session, instrument: Instrument, parsed: ParsedScreener, now: datetime | None = None, *, source: str = SOURCE
) -> GapFillResult:
    """``source`` labels provenance; it is ``screener`` for the owner-saved page and the licensed feed's own name otherwise."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    result = GapFillResult()
    if (instrument.country or "").upper() != "IN" or (instrument.currency or "").upper() != "INR":
        raise ScreenerParseError("instrument_is_not_an_inr_indian_listing")
    newest = {kind: sorted((e for k, e in parsed.periods if k == kind), reverse=True)[:MAX_PERIODS_PER_TYPE] for kind in ("annual", "quarterly")}

    for (kind, end), period in sorted(parsed.periods.items(), key=lambda item: item[0][1], reverse=True):
        if end not in newest[kind]:
            continue
        label = f"{kind} {end.isoformat()}"
        row = _same_period(db, instrument.id, kind, end)
        provenance = lambda name, derived=False: {"source": source, "fetched_at": stamp, "basis": parsed.basis, "unit": "INR", "derived": derived}  # noqa: E731
        if row is None:
            if not period.fields and not period.extras:
                continue
            row = InstrumentFundamental(
                id=uuid4(), instrument_id=instrument.id, period_type=kind, period_end=end, currency="INR", source=source,
                fetched_at=now, statement_basis=parsed.basis, field_sources={}, extra_metrics={},
            )
            db.add(row)
            result.created_periods.append(label)
        else:
            if (row.currency or "").upper() != "INR":
                result.skipped.append({"period": label, "reason": "currency_mismatch_no_conversion"})
                continue
            # Existing provider statements are consolidated unless recorded otherwise; never mix bases in one row.
            if (row.statement_basis or "consolidated") != parsed.basis:
                result.skipped.append({"period": label, "reason": f"statement_basis_mismatch_{row.statement_basis or 'consolidated'}_vs_{parsed.basis}"})
                continue
            if row.statement_basis is None and parsed.basis == "consolidated":
                pass  # basis stays unrecorded for the existing provider's row; field provenance records Screener's basis
        field_sources = dict(row.field_sources or {})
        extras = dict(row.extra_metrics or {})
        filled_here: list[str] = []
        for name, value in period.fields.items():
            current = getattr(row, name)
            if _is_missing(row, name):
                setattr(row, name, value)
                field_sources[name] = provenance(name, name in period.derived)
                filled_here.append(name)
            elif current != 0 and abs(value - current) / abs(current) > CONFLICT_TOLERANCE:
                conflicts = dict(extras.get("screener_conflicts") or {})
                previous = conflicts.get(name) or {}
                if not (previous.get("screener") == str(value) and previous.get("existing") == str(current)):
                    # An unchanged, still-unresolved conflict keeps its original record (re-import stays byte-identical).
                    conflicts[name] = {"screener": str(value), "existing": str(current),
                                       "existing_source": (field_sources.get(name) or {}).get("source", row.source),
                                       "relative_diff": str((abs(value - current) / abs(current)).quantize(Decimal("0.0001"))),
                                       "retrieved_at": stamp, "basis": parsed.basis, "source": source}
                    extras["screener_conflicts"] = conflicts
                result.conflicts.append({"period": label, "field": name, "screener": str(value), "existing": str(current)})
        for name, value in period.extras.items():
            previous = extras.get(name)
            if previous is None or (isinstance(previous, dict) and previous.get("source") == source):
                if previous and previous.get("value") == str(value) and previous.get("basis") == parsed.basis:
                    continue  # unchanged: keep the original retrieval record so a re-import is byte-identical
                result.extras_written += 1
                extras[name] = {"value": str(value), "source": source, "fetched_at": stamp, "basis": parsed.basis}
        if filled_here:
            result.filled[label] = filled_here
        elif label not in result.created_periods and not any(c["period"] == label for c in result.conflicts):
            result.unchanged_periods += 1
        row.field_sources = field_sources or None
        row.extra_metrics = extras or None
    if source == SOURCE and parsed.top_ratios:
        valuation_inputs.store(db, instrument.id, SOURCE, parsed.top_ratios, now)  # the chain's third provider; a licensed feed is not "screener"
    db.commit()
    return result
