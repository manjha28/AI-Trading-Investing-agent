"""Normalisation and reconciliation of the VERIFIED Dhan Company Info fields (see diagnostics/dhan_company_info/REPORT.md).

Integrated (approved) fields only:
  CO:     SECTOR, INDUSTRY, MARKET_CAP (INR crore), BOOK_VALUE (INR per share), DIVIDEND_YIELD (unit unverified)
  RATIOS: PB_RATIO
  SHP:    PROMOTER_HOLDING, FII_HOLDING, DII_HOLDING, PUBLIC_HOLDING (percent; an UNDATED snapshot)
The remaining fields (PE_RATIO, EPS, EBITDA, ROE, ROCE, DEBT_TO_EQUITY, INDUSTRY_*, PE_TO_SECTOR_PE) are returned under `valuation_inputs`
and are validated and selected by the valuation evidence layer (valuation.py); they are not added to the display `values`.

Rules: null / missing / empty-string values are "not provided" (never zero, never a replacement for an existing value);
Dhan values are flagged, never silently used to overwrite, when they disagree with price-based cross-checks; the
statement basis (consolidated/standalone) of Dhan figures is unknown and is never assumed.
"""

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.services import valuation_inputs

SCHEMA_VERSION = 3  # v3 adds `valuation_inputs` (every usable Dhan field in canonical units); older cached snapshots lack it
CRORE = Decimal("10000000")
# Tolerances, justified by the audit (8 and 7 checkable securities respectively):
#   P/B vs price / book value: audit range 0.998-1.008 (prices <= 1 trading day old)   -> 5% allows daily price drift.
#   Market cap vs price x shares: audit range 0.992-1.094 (the 1.094 case has a stale share count) -> 10%.
PB_TOLERANCE = Decimal("0.05")
MCAP_TOLERANCE = Decimal("0.10")
HOLDINGS = (("PROMOTER_HOLDING", "promoter", "Promoter"), ("FII_HOLDING", "fii", "Foreign institutional investors (FII)"),
            ("DII_HOLDING", "dii", "Domestic institutional investors (DII)"), ("PUBLIC_HOLDING", "public", "Public"))
SHAREHOLDING_WARNING = ("Dhan returns only these four categories and no reporting period. The categories are incomplete and do not "
                        "necessarily sum to 100%, and the figures must not be read as the latest quarter's pattern.")


def _num(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None  # "" is not a classification


def normalize(data: dict[str, Any], security_id: str, *, fetched_at: datetime | None = None) -> dict[str, Any] | None:
    entry = data.get(security_id)
    if isinstance(entry, list):
        entry = entry[0] if entry else None
    if not isinstance(entry, dict):
        # The adapter returns payload["data"] = {"CO": {...}, ...}; accept both that shape and a security-id keyed shape.
        entry = data if any(k in data for k in ("CO", "RATIOS", "SHP")) else None
    if not isinstance(entry, dict):
        return None
    co = entry.get("CO") if isinstance(entry.get("CO"), dict) else {}
    ratios = entry.get("RATIOS") if isinstance(entry.get("RATIOS"), dict) else {}
    shp = entry.get("SHP") if isinstance(entry.get("SHP"), dict) else {}
    stamp = (fetched_at or datetime.now(timezone.utc)).isoformat()

    market_cap = _num(co.get("MARKET_CAP"))
    book_value = _num(co.get("BOOK_VALUE"))
    pb = _num(ratios.get("PB_RATIO"))
    dy = _num(co.get("DIVIDEND_YIELD"))
    fields: dict[str, tuple[str | None, str]] = {
        "market_cap": (str(market_cap * CRORE) if market_cap is not None and market_cap > 0 else None, "INR"),  # source unit: INR crore
        "book_value": (str(book_value) if book_value is not None else None, "INR/share"),
        "pb": (str(pb) if pb is not None else None, "x"),
        # DIVIDEND_YIELD arrives as a percent number (audit: 1.16 = Yahoo's 1.16); stored as a fraction as before. Unit/basis UNVERIFIED.
        "dividend_yield": (str(dy / 100) if dy is not None and dy >= 0 else None, "fraction (source percent; unverified)"),
        "sector": (_text(co.get("SECTOR")), "text"),
        "industry": (_text(co.get("INDUSTRY")), "text"),
    }
    values = {name: {"value": value, "unit": unit, "field_path": f"dhan.company_info.{name}", "source": "dhan", "fetched_at": stamp}
              for name, (value, unit) in fields.items()}
    values["dividend_yield"]["verified"] = False

    holdings: list[dict[str, Any]] = []
    warnings = [SHAREHOLDING_WARNING]
    for key, slug, label in HOLDINGS:
        raw = shp.get(key)
        pct = _num(raw)
        if pct is not None and not Decimal(0) <= pct <= Decimal(100):
            warnings.append(f"{label}: value {raw} is outside 0-100% and was discarded.")
            pct = None
        holdings.append({"key": slug, "label": label, "percent": str(pct) if pct is not None else None, "unit": "percent"})
    present = [Decimal(h["percent"]) for h in holdings if h["percent"] is not None]
    total = sum(present, Decimal(0))
    if total > Decimal("100.5"):
        warnings.append(f"The four categories sum to {total}%, which exceeds 100%; treat them as unreliable.")
    shareholding = {
        "status": "available" if present else "unavailable", "source": "dhan", "retrieved_at": stamp,
        "reporting_period": None, "reporting_period_note": "Not provided by source",
        "holdings": holdings, "sum_percent": str(total) if present else None,
        "not_itemised_percent": str(max(Decimal(100) - total, Decimal(0))) if present else None, "warnings": warnings,
    }
    has_snapshot_values = any(v["value"] is not None for v in values.values())
    if not has_snapshot_values and not present:
        return None
    return {"status": "available" if has_snapshot_values else "unavailable", "values": values, "source": "dhan", "fetched_at": stamp,
            "stale": False, "schema_version": SCHEMA_VERSION, "shareholding": shareholding,
            "valuation_inputs": valuation_inputs.dhan_inputs(entry)}


# ───────────── reconciliation (flags only; never overwrites) ─────────────
def _check(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **extra}


def reconcile(values: dict[str, Any], *, price: Decimal | None, price_problem: str | None, shares: Decimal | None,
              shares_problem: str | None = None) -> dict[str, dict[str, Any]]:
    """Cross-check Dhan P/B and market cap against the current price. ``price`` must already be a CURRENT verified close."""
    def val(name: str) -> Decimal | None:
        item = values.get(name)
        return _num(item.get("value")) if isinstance(item, dict) else None

    out: dict[str, dict[str, Any]] = {}
    pb, bvps, mcap = val("pb"), val("book_value"), val("market_cap")
    if pb is None or bvps is None:
        out["pb"] = _check("not_verified", "Dhan did not return both PB_RATIO and BOOK_VALUE.")
    elif price is None:
        out["pb"] = _check("not_verified", f"No current price to verify against ({price_problem or 'unavailable'}).")
    elif bvps <= 0:
        out["pb"] = _check("not_verified", "Book value per share is not positive, so price / book value is not meaningful.")
    else:
        implied = price / bvps
        ratio = pb / implied if implied else None
        ok = ratio is not None and abs(ratio - 1) <= PB_TOLERANCE
        out["pb"] = _check("agrees" if ok else "discrepancy",
                           ("Dhan P/B agrees with price / book value per share" if ok else
                            f"Dhan P/B {pb} differs from price / book value ({implied:.2f}) by more than {PB_TOLERANCE:.0%}"),
                           dhan_value=str(pb), reference_value=str(round(implied, 4)), ratio=str(round(ratio, 4)) if ratio is not None else None,
                           tolerance=str(PB_TOLERANCE), reference_basis="latest completed daily close / Dhan BOOK_VALUE")
    if mcap is None:
        out["market_cap"] = _check("not_verified", "Dhan did not return MARKET_CAP.")
    elif price is None or shares is None:
        out["market_cap"] = _check("not_verified", f"Cannot verify without both a current price and a share count ({price_problem if price is None else shares_problem or 'no share count'}).")
    else:
        implied = price * shares
        ratio = mcap / implied if implied else None
        ok = ratio is not None and abs(ratio - 1) <= MCAP_TOLERANCE
        out["market_cap"] = _check("agrees" if ok else "discrepancy",
                                   ("Dhan market cap agrees with price x shares outstanding" if ok else
                                    f"Dhan market cap {mcap:.0f} differs from price x shares ({implied:.0f}) by more than {MCAP_TOLERANCE:.0%}; the stored share count or the price may be wrong"),
                                   dhan_value=str(mcap), reference_value=str(implied), ratio=str(round(ratio, 4)) if ratio is not None else None,
                                   tolerance=str(MCAP_TOLERANCE), reference_basis="latest completed daily close x latest stored shares outstanding")
    return out


# ───────────── single-flight + short-lived cache so concurrent research runs share one Dhan request ─────────────
_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_SECONDS = 600


NO_DATA_SECONDS = 7 * 24 * 3600
_NO_DATA: dict[str, float] = {}


def mark_no_data(security_id: str, *, now: Callable[[], float] = time.monotonic) -> None:
    """Dhan said DH-907 (no company data) for this security: do not ask again for a week (per process)."""
    with _LOCK_GUARD:
        _NO_DATA[security_id] = now()


def has_no_data(security_id: str, *, now: Callable[[], float] = time.monotonic) -> bool:
    with _LOCK_GUARD:
        marked = _NO_DATA.get(security_id)
        if marked is not None and now() - marked >= NO_DATA_SECONDS:
            del _NO_DATA[security_id]
            return False
        return marked is not None


def fetch_once(security_id: str, fetch: Callable[[], dict[str, Any] | None], *, now: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    with _LOCK_GUARD:
        lock = _LOCKS.setdefault(security_id, threading.Lock())
    with lock:
        cached = _CACHE.get(security_id)
        if cached is not None and now() - cached[0] < CACHE_SECONDS:
            return cached[1]
        result = fetch()
        if result is not None:  # failures are never cached: the next run may retry
            _CACHE[security_id] = (now(), result)
        return result


def clear_cache() -> None:
    with _LOCK_GUARD:
        _CACHE.clear()


# ───────────── display catalogue: EVERY field Dhan returns, with its audit status (display only, never a calculation input) ─────────────
USED, UNVALIDATED, UNRELIABLE = "verified", "unvalidated", "unreliable"
STATUS_TEXT = {USED: "Verified - used by Research", UNVALIDATED: "Shown only - needs validation, not used",
               UNRELIABLE: "Shown only - unreliable, not used"}
# (group, key, label, unit as evidenced by the audit, status, note)
CATALOGUE: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("CO", "SECTOR", "Sector", "text", USED, "Dhan's own taxonomy; an empty string means not provided."),
    ("CO", "INDUSTRY", "Industry", "text", USED, "Dhan's own taxonomy; an empty string means not provided."),
    ("CO", "MARKET_CAP", "Market capitalisation", "INR crore", USED, "Verified INR crore; moves with the live price. Cross-checked against price x shares."),
    ("CO", "BOOK_VALUE", "Book value per share", "INR per share", USED, "Per-share figure; basis (consolidated/standalone) and date not provided."),
    ("CO", "DIVIDEND_YIELD", "Dividend yield", "percent", UNVALIDATED, "Percent number (1.16 = 1.16%). Disagrees with Yahoo for some stocks; unit and basis not fully verified."),
    ("CO", "EPS", "Earnings per share", "INR per share", UNVALIDATED, "Period not stated; differed from stored trailing EPS by up to 40%."),
    ("CO", "EBITDA", "EBITDA", "unit not stated (magnitude suggests INR crore)", UNVALIDATED, "Unit and period unproven; not meaningful for banks."),
    ("CO", "FIFTY_TWO_WEEK_HIGH", "52-week high", "INR per share", UNVALIDATED, "Context only."),
    ("CO", "FIFTY_TWO_WEEK_LOW", "52-week low", "INR per share", UNVALIDATED, "Context only."),
    ("RATIOS", "PB_RATIO", "Price / book value", "x", USED, "Verified: equals live price / book value per share within 1%."),
    ("RATIOS", "PE_RATIO", "Price / earnings", "x", UNVALIDATED, "Basis not identified; does not reconcile with the returned EPS. Negative when earnings are negative."),
    ("RATIOS", "DEBT_TO_EQUITY", "Debt to equity", "x", UNVALIDATED, "0 for banks and insurers means not applicable, not a real zero."),
    ("RATIOS", "ROE", "Return on equity", "percent", UNRELIABLE, "Null for every security tested."),
    ("RATIOS", "ROCE", "Return on capital employed", "percent", UNVALIDATED, "Definition and period not stated."),
    ("RATIOS", "PE_TO_SECTOR_PE", "P/E relative to sector P/E", "ratio", UNRELIABLE, "Implied sector P/E is inconsistent and sometimes negative."),
    ("RATIOS", "INDUSTRY_EPS", "Industry EPS", "INR per share", UNRELIABLE, "Aggregate with unknown definition."),
    ("RATIOS", "INDUSTRY_DEBT_TO_EQUITY", "Industry debt to equity", "x", UNRELIABLE, "Zero or meaningless for financials."),
    ("RATIOS", "INDUSTRY_PB_RATIO", "Industry P/B", "x", UNRELIABLE, "Negative values seen."),
    ("RATIOS", "INDUSTRY_CURRENT_RATIO", "Industry current ratio", "x", UNRELIABLE, "Zero for banks, 159.51 for insurance."),
    ("RATIOS", "INDUSTRY_DIVIDEND_YIELD", "Industry dividend yield", "percent", UNRELIABLE, "Aggregate with unknown definition."),
    ("RATIOS", "INDUSTRY_OPERATING_MARGIN", "Industry operating margin", "percent", UNRELIABLE, "Zero for banks, over 90% for insurance and finance."),
    ("SHP", "PROMOTER_HOLDING", "Promoter holding", "percent", USED, "Undated snapshot. 0 can be a real zero (no promoter)."),
    ("SHP", "FII_HOLDING", "FII holding", "percent", USED, "Undated snapshot."),
    ("SHP", "DII_HOLDING", "DII holding", "percent", USED, "Undated snapshot."),
    ("SHP", "PUBLIC_HOLDING", "Public holding", "percent", USED, "Undated snapshot."),
    ("SHP", "CHANGE_IN_FII_HOLDING", "Change in FII holding", "percentage points (inferred)", UNVALIDATED, "Comparison period not stated."),
    ("SHP", "CHANGE_IN_DII_HOLDING", "Change in DII holding", "percentage points (inferred)", UNVALIDATED, "Comparison period not stated."),
    ("SHP", "REPORTING_PERIOD", "Shareholding reporting period", "date", UNRELIABLE, "Null for every security tested: Dhan gives no as-of date."),
)
GROUP_LABELS = {"CO": "Company overview", "RATIOS": "Ratios and industry benchmarks", "SHP": "Shareholding"}


def describe_all(data: dict[str, Any], *, fetched_at: datetime | None = None) -> dict[str, Any]:
    """Every field Dhan returned, exactly as received, each tagged with its audit status. Presentation only."""
    stamp = (fetched_at or datetime.now(timezone.utc)).isoformat()
    groups: list[dict[str, Any]] = []
    known = {(g, k) for g, k, *_ in CATALOGUE}
    for group, label in GROUP_LABELS.items():
        block = data.get(group)
        fields = []
        for g, key, name, unit, status, note in CATALOGUE:
            if g != group:
                continue
            if not isinstance(block, dict):
                presence, value = "group_not_returned", None
            elif key not in block:
                presence, value = "missing", None
            elif block[key] is None:
                presence, value = "null", None
            elif block[key] == "":
                presence, value = "empty_string", None
            else:
                presence, value = "populated", block[key]
            item = {"key": key, "label": name, "value": value, "presence": presence, "unit": unit, "audit_status": status,
                    "audit_text": STATUS_TEXT[status], "note": note}
            if key == "MARKET_CAP" and presence == "populated":
                parsed = _num(value)
                item["inr_value"] = str(parsed * CRORE) if parsed is not None and parsed > 0 else None
            fields.append(item)
        if isinstance(block, dict):  # a field Dhan adds in future is shown, never dropped
            for key, value in block.items():
                if (group, key) not in known:
                    fields.append({"key": key, "label": key, "value": value, "presence": "populated" if value not in (None, "") else "null",
                                   "unit": "not documented", "audit_status": UNVALIDATED, "audit_text": "New field - not audited", "note": "Not present in the audit."})
        groups.append({"key": group, "label": label, "returned": isinstance(block, dict), "fields": fields})
    return {
        "source": "Dhan Company Info", "retrieved_at": stamp, "reporting_period": None, "reporting_period_note": "Not provided by source",
        "statement_basis": "Not provided by source (consolidated vs standalone unknown)", "groups": groups,
        "warnings": [
            "Every value is an undated provider snapshot: no field carries a reporting period or timestamp.",
            "Only fields marked 'Verified' are used by Research. The rest are shown for transparency and are not used in any calculation.",
            SHAREHOLDING_WARNING,
        ],
    }
