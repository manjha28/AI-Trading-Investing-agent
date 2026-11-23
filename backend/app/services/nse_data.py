"""NSE shareholding patterns and filed financial results (Phase 6b; flags ``nse_shareholding_enabled`` and ``nse_results_enabled``, off in code, on in the owner's local .env).

Polite by construction: one request stream, at least ``nse_min_interval_seconds`` (2.2 s) between requests, exponential backoff on errors, every response cached on disk with its download date (the raw
file is kept), and after 5 consecutive failures the client STOPS and logs (``NseBlocked``) instead of hammering. No credentials, no tokens. Source order for statements: Dhan first (no statements endpoint
exists on the owner's plan, see the report), then NSE's filed results (official and dated), then Yahoo, which only fills what NSE lacks; where NSE and an existing Yahoo value disagree by more than 2%
NSE wins, Yahoo stays in provenance and the conflict is logged. Filing (broadcast) dates are stored as ``filed_on`` = the point-in-time "known from" date.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from lxml import etree

logger = logging.getLogger(__name__)
SOURCE = "nse_results"
SHP_SOURCE = "nse_shp"
CONFLICT_TOLERANCE = Decimal("0.02")


class NseDocumentUnavailable(RuntimeError):
    """NSE answered that one document does not exist (an old filing's file is gone): skip that filing; this is not a block and does not count toward stopping."""


class NseBlocked(RuntimeError):
    """NSE blocked us or failed repeatedly: stop and log, do not retry in a tight loop."""


# ───────────────────────── polite client ─────────────────────────
class PoliteNse:
    def __init__(self, raw_dir: str | Path, *, min_interval: float = 2.2, max_consecutive_errors: int = 5, backoff: tuple[float, ...] = (5.0, 15.0, 45.0), budget: int | None = None,
                 sleep: Any = time.sleep, clock: Any = time.monotonic, nse: Any = None) -> None:
        self.raw_dir, self.min_interval, self.max_errors, self.backoff, self.budget = Path(raw_dir), min_interval, max_consecutive_errors, backoff, budget
        self._sleep, self._clock, self._nse = sleep, clock, nse
        self._last: float | None = None
        self.consecutive_errors = 0
        self.requests = 0
        self.cache_hits = 0

    def _api(self) -> Any:
        if self._nse is None:
            import tempfile

            from nse import NSE, MemoryCookieStore
            self._nse = NSE(download_folder=tempfile.gettempdir() + "/trading-copilot-nse-data", cookie_store=MemoryCookieStore())
        return self._nse

    def close(self) -> None:
        if self._nse is not None and hasattr(self._nse, "exit"):
            self._nse.exit()

    def _pace(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _guarded(self, fn: Any) -> Any:
        if self.consecutive_errors >= self.max_errors:
            raise NseBlocked(f"{self.consecutive_errors} consecutive NSE errors: stopped")
        if self.budget is not None and self.requests >= self.budget:
            raise NseBlocked("the per-run NSE request budget is used up")
        last_exc: Exception | None = None
        for attempt in range(len(self.backoff) + 1):
            self._pace()
            self.requests += 1
            try:
                out = fn()
                self.consecutive_errors = 0
                return out
            except Exception as exc:  # noqa: BLE001 - classified below; credentials never appear in these errors
                if type(exc).__name__ in ("NSEFileUnavailableError", "FileNotFoundError") or "404" in str(exc)[:200]:
                    logger.info("nse_document_unavailable error_class=%s", type(exc).__name__)
                    raise NseDocumentUnavailable(str(exc)[:120]) from exc
                last_exc = exc
                self.consecutive_errors += 1
                logger.warning("nse_request_failed attempt=%d error_class=%s", attempt + 1, type(exc).__name__)
                if self.consecutive_errors >= self.max_errors:
                    logger.error("nse_blocked: %d consecutive errors, stopping NSE fetches", self.consecutive_errors)
                    raise NseBlocked(f"{self.consecutive_errors} consecutive NSE errors: stopped") from exc
                if attempt < len(self.backoff):
                    self._sleep(self.backoff[attempt])
        raise NseBlocked("NSE request failed after retries") from last_exc

    # cached JSON call ------------------------------------------------------------------------
    def json_call(self, key: str, fn: Any, *, ttl_hours: float = 20.0, now: datetime | None = None) -> Any:
        now = now or datetime.now(timezone.utc)
        path = self.raw_dir / "json" / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
        if path.exists():
            blob = json.loads(path.read_text())
            if now - datetime.fromisoformat(blob["downloaded_at"]) < timedelta(hours=ttl_hours):
                self.cache_hits += 1
                return blob["data"]
        data = self._guarded(fn)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"downloaded_at": now.isoformat(), "key": key, "data": data}, default=str))
        return data

    # cached document (XBRL etc.), immutable once filed -------------------------------------------
    def document(self, url: str, *, now: datetime | None = None) -> bytes:
        now = now or datetime.now(timezone.utc)
        name = Path(url.split("?")[0]).name or hashlib.sha1(url.encode()).hexdigest()
        path = self.raw_dir / "docs" / name
        if path.exists():
            self.cache_hits += 1
            return path.read_bytes()

        def get() -> bytes:
            p = self._api().download_document(url)
            return Path(p).read_bytes()
        content = self._guarded(get)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps({"url": url, "downloaded_at": now.isoformat(), "bytes": len(content)}))
        return content

    # API wrappers ---------------------------------------------------------------------------------
    def shareholding(self, symbol: str) -> list[dict[str, Any]]:
        return self.json_call(f"shp_{symbol}", lambda: self._api().shareholding(symbol))

    def results_old(self, symbol: str, period: str, from_date: date, to_date: date) -> list[dict[str, Any]]:
        return self.json_call(f"res_old_{symbol}_{period}_{from_date}_{to_date}", lambda: self._api().financial_results(segment="equities", period=period, symbol=symbol, from_date=from_date, to_date=to_date))

    def results_new(self, symbol: str, from_date: date, to_date: date) -> list[dict[str, Any]]:
        def get() -> list[dict[str, Any]]:
            return [dict(r) for r in self._api().iter_financial_results_with_xbrl(symbol=symbol, from_date=from_date, to_date=to_date)]
        return self.json_call(f"res_new_{symbol}_{from_date}_{to_date}", get)


# ───────────────────────── XBRL parsing (pure) ─────────────────────────
XB = "{http://www.xbrl.org/2003/instance}"
XBDI = "{http://xbrl.org/2006/xbrldi}"


def _dec(text: str | None) -> Decimal | None:
    try:
        return None if text is None or not text.strip() else Decimal(text.strip())
    except InvalidOperation:
        return None


def _contexts(root: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in root.iter(XB + "context"):
        st, en, ins = c.find(f".//{XB}startDate"), c.find(f".//{XB}endDate"), c.find(f".//{XB}instant")
        dim = c.find(f".//{XBDI}explicitMember") is not None or c.find(f".//{XBDI}typedMember") is not None
        out[c.get("id")] = {"start": None if st is None else date.fromisoformat(st.text.strip()), "end": date.fromisoformat((en if en is not None else ins).text.strip()) if (en is not None or ins is not None) else None, "dim": dim}
    return out


def parse_shareholding_xbrl(content: bytes) -> dict[str, Any]:
    """Promoter, FII, DII, public % (of total shares) and the pledged % of the promoter holding. A disclosed 'no promoter shares pledged' is a known 0; a missing disclosure is None (unknown)."""
    root = etree.fromstring(content)
    pct: dict[str, Decimal] = {}
    shares: dict[str, Decimal] = {}
    pledged_flag: str | None = None
    pledged_shares: Decimal | None = None
    for e in root.iter():
        ln = etree.QName(e).localname
        cref = e.get("contextRef")
        if ln == "ShareholdingAsAPercentageOfTotalNumberOfShares" and cref:
            v = _dec(e.text)
            if v is not None:
                pct[cref] = v * 100
        elif ln == "NumberOfFullyPaidUpEquityShares" and cref:
            v = _dec(e.text)
            if v is not None:
                shares[cref] = v
        elif ln == "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged" and pledged_flag is None and e.text:
            pledged_flag = e.text.strip().lower()
        elif ln == "NumberOfSharesEncumberedUnderPledged" and cref == "ShareholdingPattern_ContextI":
            pledged_shares = _dec(e.text)
    promoter_ctx = "ShareholdingOfPromoterAndPromoterGroup_ContextI"
    promoter_shares = shares.get(promoter_ctx)
    pledge: Decimal | None
    if pledged_flag == "false":
        pledge = Decimal("0")
    elif pledged_flag == "true" and pledged_shares is not None and promoter_shares:
        pledge = (pledged_shares / promoter_shares * 100).quantize(Decimal("0.0001"))
    else:
        pledge = None
    q = lambda d: None if d is None else d.quantize(Decimal("0.0001"))  # noqa: E731
    out = {"promoter_pct": q(pct.get(promoter_ctx)), "fii_pct": q(pct.get("InstitutionsForeign_ContextI")), "dii_pct": q(pct.get("InstitutionsDomestic_ContextI")),
           "public_pct": q(pct.get("PublicShareholding_ContextI")), "pledged_pct_of_promoter": pledge}
    # some filings state the percentage already as 0-100 instead of a fraction: the promoter + public total tells which (they add to 100)
    pr, pu = out["promoter_pct"], out["public_pct"]
    if pr is not None and pu is not None:
        total = pr + pu
        if Decimal(9500) <= total <= Decimal(10500):
            out.update({k: (None if out[k] is None else q(out[k] / 100)) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")})
        elif not (Decimal(95) <= total <= Decimal(105)):
            out.update({k: None for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")})          # does not add to 100: do not trust any of it
    return out


RESULT_TAGS = {"revenue": ("RevenueFromOperations",), "other_income": ("OtherIncome",), "total_income": ("Income",), "interest": ("FinanceCosts",), "pbt": ("ProfitBeforeTax",),
               "net_income": ("ProfitLossForPeriod",), "depreciation": ("DepreciationDepreciationAndAmortisationExpense", "DepreciationAndAmortisationExpense"),
               "eps": ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "BasicEarningsLossPerShareFromContinuingOperations")}


def parse_results_xbrl(content: bytes, period_end: date) -> dict[str, Any] | None:
    """Canonical P&L items for the period ending ``period_end`` (the quarter, or the year in a year-end filing). Returns {"quarterly": {...}, "annual": {...}} (each may be missing). INR absolute amounts."""
    root = etree.fromstring(content)
    ctx = _contexts(root)
    by_ctx: dict[str, dict[str, Decimal]] = {}
    tag_names = {t: k for k, ts in RESULT_TAGS.items() for t in ts}
    for e in root.iter():
        ln = etree.QName(e).localname
        key = tag_names.get(ln)
        cref = e.get("contextRef")
        if key is None or cref not in ctx or ctx[cref]["dim"] or ctx[cref]["end"] != period_end or ctx[cref]["start"] is None:
            continue
        v = _dec(e.text)
        if v is None:
            continue
        slot = by_ctx.setdefault(cref, {})
        if key not in slot or RESULT_TAGS[key].index(ln) == 0:
            slot[key] = v
    out: dict[str, dict[str, Any]] = {}
    for cref, vals in sorted(by_ctx.items(), key=lambda kv: (kv[0] != "OneD", kv[0])):               # the filing's own 'OneD' context wins when contexts repeat a period
        days = (ctx[cref]["end"] - ctx[cref]["start"]).days
        kind = "quarterly" if 80 <= days <= 100 else "annual" if 350 <= days <= 380 else None
        if kind is None or kind in out:
            continue
        v = dict(vals)
        if all(k in v for k in ("pbt", "interest")):
            v["operating_profit"] = v["pbt"] + v["interest"] - v.get("other_income", Decimal(0)) if "other_income" in v else None
            if v["operating_profit"] is None:
                del v["operating_profit"]
        if "operating_profit" in v and v.get("revenue"):
            v["operating_margin"] = (v["operating_profit"] / v["revenue"] * 100).quantize(Decimal("0.00000001"))
        out[kind] = v
    return out or None


BALANCE_TAGS = {"equity": ("Equity", "EquityAttributableToOwnersOfParent"), "assets": ("Assets",), "cash": ("CashAndCashEquivalents",), "fixed_assets": ("PropertyPlantAndEquipment",),
                "debt_noncurrent": ("BorrowingsNoncurrent",), "debt_current": ("BorrowingsCurrent",)}
CASH_FLOW_TAGS = ("CashFlowsFromUsedInOperatingActivities", "NetCashFlowsFromUsedInOperatingActivities")


def parse_balance_cash_xbrl(content: bytes, period_end: date, cf_days: tuple[int, int] = (350, 380)) -> dict[str, Decimal] | None:
    """Annual balance sheet (at the March year end) and operating cash flow for the year from a year-end filing: equity, total debt (current + non-current borrowings), cash, total assets, fixed assets, CFO.
    INR absolute. A figure the filing does not carry is simply absent (unknown), never zero. Debt is only reported when at least one borrowings line exists."""
    root = etree.fromstring(content)
    ctx = _contexts(root)
    bal: dict[str, Decimal] = {}
    cfo: Decimal | None = None
    names = {t: k for k, ts in BALANCE_TAGS.items() for t in ts}
    for e in root.iter():
        ln = etree.QName(e).localname
        cref = e.get("contextRef")
        if cref not in ctx or ctx[cref]["dim"]:
            continue
        c = ctx[cref]
        v = _dec(e.text)
        if v is None:
            continue
        key = names.get(ln)
        if key and c["end"] == period_end and c["start"] is None:                  # instant at the year end
            if key not in bal or BALANCE_TAGS[key].index(ln) == 0:
                bal[key] = v
        elif ln in CASH_FLOW_TAGS and c["end"] == period_end and c["start"] is not None and cf_days[0] <= (c["end"] - c["start"]).days <= cf_days[1] and cfo is None:
            cfo = v
    out: dict[str, Decimal] = {k: v for k, v in bal.items() if k in ("equity", "assets", "cash", "fixed_assets")}
    if "debt_noncurrent" in bal or "debt_current" in bal:
        out["debt"] = bal.get("debt_noncurrent", Decimal(0)) + bal.get("debt_current", Decimal(0))
    if cfo is not None:
        out["cfo"] = cfo
    return out or None


def store_balance_cash(db: Any, instrument_id: Any, period_end: date, vals: dict[str, Decimal], filed_on: date | None, basis: str, now: datetime, conflicts_log: list[dict[str, Any]] | None = None, period_type: str = "annual") -> str:
    """Gap-fill the ANNUAL row with the NSE-filed balance sheet and cash flow: a missing value is filled; a value within 2% is confirmed; a value more than 2% apart is replaced by NSE's (the earlier value and
    its source are kept in the conflict log), exactly as for the P&L. Source order: Dhan (no statements) -> NSE -> Yahoo."""
    from sqlalchemy import select

    from app.models import InstrumentFundamental
    row = db.scalar(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == instrument_id, InstrumentFundamental.period_type == period_type, InstrumentFundamental.period_end == period_end))
    iso = now.isoformat()
    if row is None:
        row = InstrumentFundamental(instrument_id=instrument_id, period_type=period_type, period_end=period_end, currency="INR", source=SOURCE, fetched_at=now, statement_basis=basis, filed_on=filed_on, field_sources={}, extra_metrics=None)
        db.add(row)
        created = True
    else:
        created = False
    fs, extras = dict(row.field_sources or {}), dict(row.extra_metrics or {})
    cols = {"equity": "total_equity", "assets": "total_assets", "debt": "total_debt", "cfo": "operating_cash_flow"}
    changed = created
    for key, col in cols.items():
        new = vals.get(key)
        if new is None:
            continue
        old = getattr(row, col)
        if old is None:
            setattr(row, col, new)
        elif old != 0 and abs(new - old) / abs(old) > CONFLICT_TOLERANCE:
            conflict = {"field": col, "period_end": str(period_end), "nse": str(new), "previous": str(old), "previous_source": (fs.get(col) or {}).get("source") or row.source, "logged_at": iso}
            extras.setdefault("conflicts", []).append(conflict)
            if conflicts_log is not None:
                conflicts_log.append(conflict)
            setattr(row, col, new)
        elif (fs.get(col) or {}).get("source") == SOURCE:
            continue
        fs[col] = prov(basis, iso)
        changed = True
    for key in ("cash", "fixed_assets", "cfo_6m"):
        new = vals.get(key)
        if new is None:
            continue
        cur = _num(extras.get(key))
        if cur is not None and cur != 0 and abs(new - cur) / abs(cur) > CONFLICT_TOLERANCE:
            extras.setdefault("conflicts", []).append({"field": key, "period_end": str(period_end), "nse": str(new), "previous": str(cur), "previous_source": (extras.get(key) or {}).get("source"), "logged_at": iso})
        if cur is None or (extras.get(key) or {}).get("source") != SOURCE:
            extras[key] = {"value": str(new), "source": SOURCE, "fetched_at": iso, "basis": basis}
            changed = True
    if filed_on is not None and row.filed_on is None:
        row.filed_on = filed_on
        changed = True
    if changed:
        row.field_sources, row.extra_metrics = fs, extras or None
        row.statement_basis = row.statement_basis or basis
    return "inserted" if created else "updated" if changed else "unchanged"


# ───────────────────────── storage ─────────────────────────
def _d(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _first_date(s: str | None) -> date | None:
    return _d(s.title() if s else s)


def store_shareholding(db: Any, instrument_id: Any, quarter_end: date, filed_on: date, parsed: dict[str, Any], now: datetime) -> bool:
    """Append one NSE shareholding row (captured_on = the filing date). Never updates: a pattern already stored for that quarter is left alone."""
    from sqlalchemy import select

    from app.models.fundamental_snapshots import ShareholdingSnapshot
    exists = db.scalar(select(ShareholdingSnapshot.id).where(ShareholdingSnapshot.instrument_id == instrument_id, ShareholdingSnapshot.source == SHP_SOURCE, ShareholdingSnapshot.quarter_end == quarter_end))
    if exists is not None:
        return False
    day_clash = db.scalar(select(ShareholdingSnapshot.id).where(ShareholdingSnapshot.instrument_id == instrument_id, ShareholdingSnapshot.source == SHP_SOURCE, ShareholdingSnapshot.captured_on == filed_on))
    if day_clash is not None:
        return False                                  # two quarters filed on one day (a late bulk filing): keep the first, the unique key is one row per day
    db.add(ShareholdingSnapshot(instrument_id=instrument_id, captured_on=filed_on, captured_at=now, source=SHP_SOURCE, quarter_end=quarter_end, filed_on=filed_on, **parsed))
    return True


def _num(v: Any) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v.get("value") if isinstance(v, dict) else v))
    except InvalidOperation:
        return None


def prov(basis: str, iso: str) -> dict[str, Any]:
    """Per-field provenance in the shape the statement readers expect: {source, fetched_at, basis, derived}."""
    return {"source": SOURCE, "fetched_at": iso, "basis": basis, "derived": False}


def store_results(db: Any, instrument_id: Any, period_type: str, period_end: date, vals: dict[str, Any], filed_on: date | None, basis: str, now: datetime, conflicts_log: list[dict[str, Any]] | None = None) -> str:
    """Upsert one NSE-filed period into instrument_fundamentals. Returns 'inserted' | 'updated' | 'unchanged'. NSE wins where it disagrees with an existing value by more than 2% (Yahoo's value
    stays in provenance and the conflict is logged); fields NSE does not have are never blanked."""
    from sqlalchemy import select

    from app.models import InstrumentFundamental
    row = db.scalar(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == instrument_id, InstrumentFundamental.period_type == period_type, InstrumentFundamental.period_end == period_end))
    iso = now.isoformat()
    extras_in = {"interest_expense": vals.get("interest"), "profit_before_tax": vals.get("pbt"), "depreciation": vals.get("depreciation"), "operating_profit": vals.get("operating_profit"),
                 "other_income": vals.get("other_income"), "total_income": vals.get("total_income")}
    if row is None:
        row = InstrumentFundamental(instrument_id=instrument_id, period_type=period_type, period_end=period_end, currency="INR", revenue=vals.get("revenue"), net_income=vals.get("net_income"), eps=vals.get("eps"),
                                    operating_margin=vals.get("operating_margin"), source=SOURCE, fetched_at=now, statement_basis=basis, filed_on=filed_on,
                                    field_sources={k: prov(basis, iso) for k in ("revenue", "net_income", "eps", "operating_margin") if vals.get(k) is not None},
                                    extra_metrics={k: {"value": str(v), "source": SOURCE, "fetched_at": iso, "basis": basis} for k, v in extras_in.items() if v is not None} or None)
        db.add(row)
        return "inserted"
    changed = False
    fs = dict(row.field_sources or {})
    extras = dict(row.extra_metrics or {})
    for field in ("revenue", "net_income", "eps"):
        new, old = vals.get(field), getattr(row, field)
        if new is None:
            continue
        if old is None:
            setattr(row, field, new)
            fs[field] = prov(basis, iso)
            changed = True
        elif old != 0 and abs(new - old) / abs(old) > CONFLICT_TOLERANCE:
            conflict = {"field": field, "period_end": str(period_end), "nse": str(new), "previous": str(old), "previous_source": (fs.get(field) or {}).get("source") or row.source, "logged_at": iso}
            extras.setdefault("conflicts", []).append(conflict)
            logger.warning("nse_yahoo_conflict field=%s period_end=%s nse=%s previous=%s", field, period_end, new, old)
            if conflicts_log is not None:
                conflicts_log.append(conflict)
            setattr(row, field, new)
            fs[field] = prov(basis, iso)
            changed = True
        elif (fs.get(field) or {}).get("source") != SOURCE:
            fs[field] = prov(basis, iso)                                        # NSE confirms the value within 2%: provenance records both
            changed = True
    for k, v in extras_in.items():
        if v is None:
            continue
        cur = _num(extras.get(k))
        if cur is None or (cur != 0 and abs(v - cur) / abs(cur) > CONFLICT_TOLERANCE) or (extras.get(k) or {}).get("source") != SOURCE:
            if cur is not None and cur != 0 and abs(v - cur) / abs(cur) > CONFLICT_TOLERANCE:
                extras.setdefault("conflicts", []).append({"field": k, "period_end": str(period_end), "nse": str(v), "previous": str(cur), "previous_source": (extras.get(k) or {}).get("source"), "logged_at": iso})
            extras[k] = {"value": str(v), "source": SOURCE, "fetched_at": iso, "basis": basis}
            changed = True
    if row.operating_margin is None and vals.get("operating_margin") is not None:
        row.operating_margin = vals["operating_margin"]
        fs["operating_margin"] = prov(basis, iso)
        changed = True
    if filed_on is not None and row.filed_on is None:
        row.filed_on = filed_on
        changed = True
    if changed:
        row.field_sources, row.extra_metrics = fs, extras or None
        row.statement_basis = row.statement_basis or basis
        return "updated"
    return "unchanged"


# ───────────────────────── jobs ─────────────────────────
def pick_filings(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One filing per (period end, period type): consolidated over standalone, then the latest broadcast (a revision replaces the original)."""
    items = []
    for r in old:
        pe = _first_date(r.get("toDate"))
        if pe and r.get("xbrl"):
            items.append({"period_end": pe, "kind": "annual" if str(r.get("period", "")).lower().startswith("annual") else "quarterly", "consolidated": str(r.get("consolidated", "")).lower().startswith("consol"),
                          "filed_on": _d(r.get("broadCastDate")) or _d(r.get("filingDate")), "xbrl": r["xbrl"], "stamp": r.get("broadCastDate") or ""})
    for r in new:
        pe = _first_date(r.get("qe_Date"))
        if pe and r.get("xbrl"):
            items.append({"period_end": pe, "kind": "quarterly", "consolidated": str(r.get("consolidated", "")).lower().startswith("consol"), "filed_on": _d(r.get("broadcast_Date")), "xbrl": r["xbrl"],
                          "stamp": r.get("broadcast_Date") or ""})
    best: dict[tuple[date, bool], dict[str, Any]] = {}
    for it in items:
        k = (it["period_end"], it["kind"] == "annual")
        cur = best.get(k)
        if cur is None or (it["consolidated"], it["filed_on"] or date.min) > (cur["consolidated"], cur["filed_on"] or date.min):
            best[k] = it
    return sorted(best.values(), key=lambda x: x["period_end"], reverse=True)


def sync_results(db: Any, client: PoliteNse, instrument: Any, *, since: date, now: datetime | None = None, max_filings: int | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    out: dict[str, int] = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    sym = instrument.symbol
    old = []
    for period in ("quarterly", "annual"):
        old += client.results_old(sym, period, since, date(2025, 6, 30)) if since <= date(2025, 6, 30) else []     # to mid-2025: the December 2024 quarter was filed in January 2025 in the old format; pick_filings drops duplicates
    new = client.results_new(sym, max(since, date(2025, 1, 1)), now.date())
    filings = pick_filings(old, new)
    from sqlalchemy import select

    from app.models import InstrumentFundamental
    have = {(r.period_type, r.period_end): r for r in db.scalars(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == instrument.id))}
    n = 0
    for f in filings:
        row = have.get(("annual" if f["kind"] == "annual" else "quarterly", f["period_end"]))
        year_end = f["period_end"].month == 3
        half_year = f["period_end"].month in (3, 9)                      # March and September filings carry the statement of assets and liabilities and the cash flow (6 / 12 months)
        arow = have.get(("annual", f["period_end"])) if year_end else have.get(("quarterly", f["period_end"]))
        need_balance = half_year and not ((arow is not None) and (arow.field_sources or {}).get("total_equity", {}).get("source") == SOURCE)
        if row is not None and row.filed_on is not None and (row.field_sources or {}).get("revenue", {}).get("source") == SOURCE and not need_balance:
            out["skipped"] += 1                                       # already stored from this filing
            continue
        if max_filings is not None and n >= max_filings:
            break
        n += 1
        try:
            parsed = parse_results_xbrl(client.document(f["xbrl"], now=now), f["period_end"])
        except NseBlocked:
            raise
        except NseDocumentUnavailable:
            out["failed"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one unparseable filing must not stop the stock
            logger.warning("nse_results_parse_failed symbol=%s period_end=%s error_class=%s", sym, f["period_end"], type(exc).__name__)
            out["failed"] += 1
            continue
        if not parsed:
            out["failed"] += 1
            continue
        basis = "consolidated" if f["consolidated"] else "standalone"
        for kind, vals in parsed.items():
            if kind != ("annual" if f["kind"] == "annual" else "quarterly") and not (f["kind"] == "quarterly" and kind == "annual" and f["period_end"].month == 3):
                continue
            res = store_results(db, instrument.id, kind, f["period_end"], vals, f["filed_on"], basis, now)
            out[res] += 1
        if half_year:                                                 # the March filing carries the balance sheet and the year's cash flow; the September filing the half-year balance sheet and 6-month cash flow
            try:
                bal = parse_balance_cash_xbrl(client.document(f["xbrl"], now=now), f["period_end"], (350, 380) if year_end else (170, 195))
            except Exception as exc:  # noqa: BLE001
                logger.warning("nse_balance_parse_failed symbol=%s error_class=%s", sym, type(exc).__name__)
                bal = None
            if bal and not year_end and "cfo" in bal:
                bal = {**bal, "cfo_6m": bal.pop("cfo")}                     # a 6-month figure never goes in the per-period cash-flow column
            if bal:
                key = "balance_" + store_balance_cash(db, instrument.id, f["period_end"], bal, f["filed_on"], basis, now, period_type="annual" if year_end else "quarterly")
                out[key] = out.get(key, 0) + 1
    db.commit()
    return out


def sync_shareholding(db: Any, client: PoliteNse, instrument: Any, *, max_quarters: int = 8, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    out = {"inserted": 0, "skipped": 0, "failed": 0}
    from sqlalchemy import select

    from app.models.fundamental_snapshots import ShareholdingSnapshot
    have = set(db.scalars(select(ShareholdingSnapshot.quarter_end).where(ShareholdingSnapshot.instrument_id == instrument.id, ShareholdingSnapshot.source == SHP_SOURCE)))
    for rec in (client.shareholding(instrument.symbol) or [])[:max_quarters]:
        qe, filed = _first_date(rec.get("date")), _d(rec.get("broadcastDate")) or _d(rec.get("submissionDate"))
        if qe is None or filed is None or not rec.get("xbrl"):
            out["failed"] += 1
            continue
        if qe in have:
            out["skipped"] += 1
            continue
        try:
            parsed = parse_shareholding_xbrl(client.document(rec["xbrl"], now=now))
        except NseBlocked:
            raise
        except NseDocumentUnavailable:
            out["failed"] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("nse_shp_parse_failed symbol=%s error_class=%s", instrument.symbol, type(exc).__name__)
            out["failed"] += 1
            continue
        if parsed["promoter_pct"] is None:                           # the summary row still gives promoter/public
            try:
                parsed["promoter_pct"], parsed["public_pct"] = Decimal(str(rec["pr_and_prgrp"])), Decimal(str(rec["public_val"]))
            except (KeyError, InvalidOperation):
                pass
        out["inserted" if store_shareholding(db, instrument.id, qe, filed, parsed, now) else "skipped"] += 1
    db.commit()
    return out


def nightly(db: Any, user_id: Any, *, budget: int | None = None) -> dict[str, Any]:
    """Incremental nightly run, bounded by a request budget; never raises. Stops quietly (and logs) if NSE blocks."""
    from app.config import settings
    from app.research_daily import user_universe
    out: dict[str, Any] = {"results": {}, "shareholding": {}, "stopped": None}
    if not (settings.nse_results_enabled or settings.nse_shareholding_enabled):
        return out
    client = PoliteNse(settings.nse_raw_dir, min_interval=settings.nse_min_interval_seconds, budget=budget or settings.nse_nightly_request_budget)
    try:
        universe = user_universe(db, user_id)
        start = (date.today().toordinal() * 61) % max(1, len(universe))             # rotate the starting stock each night so a bounded budget still reaches every stock within a few nights
        for inst in universe[start:] + universe[:start]:
            if settings.nse_results_enabled:
                out["results"][inst.symbol] = sync_results(db, client, inst, since=date.today() - timedelta(days=2 * 365 + 120), max_filings=4)
            if settings.nse_shareholding_enabled:
                out["shareholding"][inst.symbol] = sync_shareholding(db, client, inst, max_quarters=8)
    except NseBlocked as exc:
        db.rollback()
        out["stopped"] = str(exc)
        logger.error("nse_nightly_stopped %s", exc)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        out["stopped"] = type(exc).__name__
        logger.warning("nse_nightly_failed error_class=%s", type(exc).__name__)
    finally:
        client.close()
    return out
