"""Valuation evidence layer shared by Research scoring and the Gemini AI Analyst.

Source priority for EVERY metric, in this exact order (strict, one value per metric, never mixed or double counted):
  1. Dhan  ->  2. Yahoo Finance  ->  3. Screener  ->  4. calculation from stored statements  ->  5. unavailable (with the reason).
A provider value is used only if it is present, fresh and passes unit / sign / range / consistency validation. If it fails, the
discrepancy is recorded (``discrepancies``) and the next source is tried. Every metric carries its ``source_chain`` (what each
source returned) so the order of evaluation is auditable. Nothing is estimated, substituted or converted across currencies.

Price-linked provider values (market cap, P/E, P/B, P/S, PEG, EV) must be at most 3 days old; other provider values at most 95 days.
Per metric: status (reported | calculated | not_meaningful | unavailable), origin, provider, value, unit, reporting period, retrieval
time, freshness, statement basis, reason, warnings, inputs, optional cross_check, source_chain, discrepancies.
"""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Instrument, InstrumentFundamental, InstrumentSnapshot, MarketBar, ValuationProviderInput
from app.services import valuation_inputs
from app.services.research_scoring import daily_bar_staleness

D = Decimal
ZERO = D(0)
MODEL_VERSION = "valuation-evidence-0.3.0"
PRICE_LINKED_MAX_AGE_DAYS = 3      # provider market cap / P/E / P/B move with the price: older than this is not "current"
STATIC_MAX_AGE_DAYS = 95           # book value per share, dividend yield: stale after the existing 95-day rule
MCAP_MISMATCH = D("0.10")          # price x shares vs provider market cap beyond 10%: share count / price basis disagree
CROSS_CHECK_TOLERANCE = D("0.25")  # calculated vs provider-reported disagreement worth reporting
QUARTER_GAP_DAYS = (70, 110)       # consecutive quarter-ends
MAX_TTM_AGE_DAYS = 270
MAX_ANNUAL_FALLBACK_AGE_DAYS = 400
SHARES_MAX_AGE_DAYS = 400
MIN_PEERS = 5
LABELS = {
    "market_cap": "Market capitalisation", "enterprise_value": "Enterprise value", "pe_ttm": "P/E", "pb": "P/B",
    "ev_ebitda": "EV / EBITDA", "price_to_sales": "Price / Sales", "peg": "PEG", "dividend_yield": "Dividend yield",
    "book_value_per_share": "Book value per share", "net_debt_ebitda": "Net debt / EBITDA",
    "roe": "ROE", "roce": "ROCE", "interest_coverage": "Interest coverage", "eps": "EPS", "ebitda": "EBITDA",
}
SCORABLE = ("pe_ttm", "pb", "price_to_sales", "peg", "ev_ebitda")


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _q(value: Decimal, places: int = 2) -> str:
    return str(value.quantize(D(1).scaleb(-places)))


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _age_days(when: Any, now: datetime) -> int | None:
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return None
    if isinstance(when, datetime):
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        return (now - when).days
    if isinstance(when, date):
        return (now.date() - when).days
    return None


def _metric(key: str, status: str, *, value: Decimal | None = None, unit: str = "x", reason: str | None = None, basis: str = "",
            inputs: list[dict[str, Any]] | None = None, warnings: list[str] | None = None, freshness: str = "n/a", places: int = 2,
            statement_basis: str | None = None, origin: str | None = None, provider: str | None = None, reporting_period: str | None = None,
            retrieved_at: str | None = None, cross_check: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"key": key, "label": LABELS[key], "status": status, "value": _q(value, places) if value is not None else None, "unit": unit,
            "origin": origin, "provider": provider, "reporting_period": reporting_period, "retrieved_at": retrieved_at,
            "reason": reason, "basis": basis, "inputs": inputs or [], "warnings": warnings or [], "freshness": freshness,
            "statement_basis": statement_basis, "cross_check": cross_check, **(extra or {})}


def _unavailable(key: str, reason: str, basis: str = "") -> dict[str, Any]:
    return _metric(key, "unavailable", reason=reason, basis=basis)


def _inp(name: str, value: Any, unit: str, *, source: str | None = None, period_end: Any = None, fetched_at: Any = None, basis: str | None = None) -> dict[str, Any]:
    return {"name": name, "value": str(value) if value is not None else None, "unit": unit, "source": source,
            "period_end": str(period_end) if period_end else None, "fetched_at": str(fetched_at) if fetched_at else None, "statement_basis": basis}


# ───────────────────────── provider chain ─────────────────────────
PRIORITY = ("dhan", "yfinance", "screener")
PROVIDER_LABEL = {"dhan": "Dhan", "yfinance": "Yahoo Finance", "screener": "Screener"}
LEGACY_FIELDS = {"market_cap": "market_cap", "pe": "pe", "pb": "pb", "book_value": "book_value", "dividend_yield": "dividend_yield"}
PE_TOLERANCE = D("0.25")        # provider P/E vs the independent calculation (or price / EPS): beyond this the provider value is rejected
PB_TOLERANCE = D("0.05")        # provider P/B vs price / the same provider's book value per share
RATIO_TOLERANCE = D("0.40")     # P/S, book value per share, EBITDA: looser, statement bases differ more
ROE_ABSOLUTE_SLACK = D("5")     # ROE: percentage points tolerated on top of the relative tolerance (average vs year-end equity)
BOUNDS = {"pe": (0, 1000), "pb": (0, 200), "price_to_sales": (0, 500), "peg": (0, 20), "ev_ebitda": (0, 200), "dividend_yield": (0, 25),
          "roe": (-100, 200), "roce": (-100, 300)}


def _candidates(provider_metrics: dict[str, dict[str, dict[str, Any]]] | None, snapshot: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """{provider: {metric: record}}. ``provider_metrics`` is the stored per-provider table; the legacy snapshot shape (one source per
    field, used by older briefs and callers) is mapped onto the same structure so a single chain handles both."""
    out: dict[str, dict[str, dict[str, Any]]] = {p: {k: dict(v) for k, v in m.items()} for p, m in (provider_metrics or {}).items()}
    for name, rec in (snapshot.get("values") or {}).items():  # snapshot values fill only (provider, metric) pairs the stored table lacks
        if name not in LEGACY_FIELDS or not isinstance(rec, dict):
            continue
        value = _dec(rec.get("value"))
        provider = rec.get("source") or snapshot.get("source")
        if value is None or provider not in PRIORITY or LEGACY_FIELDS[name] in out.get(provider, {}):
            continue
        if name == "dividend_yield" and provider == "dhan":
            value = value * 100  # the snapshot Dhan mapping stores a fraction; canonical is percent
        out.setdefault(provider, {})[LEGACY_FIELDS[name]] = {
            "value": value, "retrieved_at": rec.get("fetched_at") or snapshot.get("fetched_at"), "reconciliation": rec.get("reconciliation"),
            "unit_assumed": False}
    for metrics in out.values():
        for rec in metrics.values():
            rec["value"] = _dec(rec.get("value"))
    return out


def _select(cands: dict[str, dict[str, dict[str, Any]]], name: str, now: datetime, max_age: int, validate=None):
    """Walk Dhan -> Yahoo -> Screener. Returns (chosen | None, trail, discrepancies). Evaluation stops at the first usable value."""
    trail: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    chosen = None
    for provider in PRIORITY:
        if chosen is not None:
            trail.append({"provider": provider, "outcome": "not_checked", "detail": "a higher-priority source was usable"})
            continue
        rec = (cands.get(provider) or {}).get(name)
        if rec is None or rec.get("value") is None:
            trail.append({"provider": provider, "outcome": "not_reported"})
            continue
        age = _age_days(rec.get("retrieved_at"), now)
        if age is None or age > max_age:
            trail.append({"provider": provider, "outcome": "stale", "detail": f"retrieved {str(rec.get('retrieved_at'))[:10]}; limit {max_age} days", "value": str(rec["value"])})
            continue
        ok, reason, warnings = (True, "", []) if validate is None else validate(provider, rec)
        if not ok:
            trail.append({"provider": provider, "outcome": "rejected", "detail": reason, "value": str(rec["value"])})
            discrepancies.append({"provider": provider, "value": str(rec["value"]), "reason": reason})
            continue
        chosen = {**rec, "provider": provider, "warnings": warnings}
        trail.append({"provider": provider, "outcome": "used", "value": str(rec["value"])})
    return chosen, trail, discrepancies


def _finish(metric: dict[str, Any], trail: list[dict[str, Any]] | None, discrepancies: list[dict[str, Any]] | None, *, calculated: bool | None = None) -> dict[str, Any]:
    """Attach the audit trail. The calculation step is recorded as used / unavailable / not_needed (a provider value was usable)."""
    chain = list(trail or [])
    if metric["origin"] == "provider_reported":
        chain.append({"provider": "calculated", "outcome": "not_needed", "detail": "a provider value was usable"})
    elif metric["status"] == "calculated":
        chain.append({"provider": "calculated", "outcome": "used"})
    elif calculated is not False:
        chain.append({"provider": "calculated", "outcome": "unavailable", "detail": metric.get("reason")})
    metric["source_chain"] = chain
    metric["discrepancies"] = discrepancies or []
    return metric


def _reported(key: str, chosen: dict[str, Any], *, unit: str, trail, discrepancies, basis: str | None = None, places: int = 2,
              cross_check: dict[str, Any] | None = None, value: Decimal | None = None, scale: Decimal = D(1)) -> dict[str, Any]:
    shown = value if value is not None else chosen["value"] * scale
    label = PROVIDER_LABEL[chosen["provider"]]
    warnings = list(chosen.get("warnings") or [])
    if chosen.get("unit_assumed"):
        warnings.append("the provider does not state this unit; INR crore is assumed and was checked against other sources where available")
    inputs = [_inp(f"{label} {LABELS.get(key, key)}", chosen["value"], chosen.get("unit") or unit, source=chosen["provider"], fetched_at=chosen["retrieved_at"], period_end=chosen.get("reporting_period"))]
    metric = _metric(key, "reported", value=shown, unit=unit, places=places, origin="provider_reported", provider=chosen["provider"],
                     retrieved_at=chosen["retrieved_at"], reporting_period=chosen.get("reporting_period"), freshness="current", statement_basis="not stated by provider",
                     basis=basis or f"as reported by {label}", warnings=warnings, inputs=inputs, cross_check=cross_check)
    return _finish(metric, trail, discrepancies)


def _rel(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / abs(b) if b else D(999)


def _in_bounds(name: str, value: Decimal) -> str | None:
    low, high = BOUNDS.get(name, (None, None))
    if low is not None and (value <= low if name in {"pe", "pb", "price_to_sales", "peg", "ev_ebitda"} else value < low):
        return f"not positive ({value})" if name in {"pe", "pb", "price_to_sales", "peg", "ev_ebitda"} else f"below the plausible range ({value})"
    if high is not None and value > high:
        return f"implausibly large ({value})"
    return None


# ───────────────────────── period helpers ─────────────────────────
def _value(period: dict[str, Any], field: str) -> Decimal | None:
    item = (period.get("values") or {}).get(field)
    value = _dec(item.get("value")) if isinstance(item, dict) else None
    return None if (field == "shares_outstanding" and value == ZERO) else value  # 0 shares is a provider placeholder


def _extra(period: dict[str, Any], name: str) -> Decimal | None:
    item = (period.get("extra_metrics") or {}).get(name)
    return _dec(item.get("value")) if isinstance(item, dict) else None


def _basis(periods: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    bases = {p.get("statement_basis") for p in periods}
    known = bases - {None}
    if len(known) > 1:
        return None, "the periods used mix consolidated and standalone statements"
    if known and None in bases:
        return None, "the periods used mix a stated statement basis with provider rows whose basis is not stated"
    return (next(iter(known)) if known else "unknown (provider default, not stated)"), None


def _currency_problem(periods: list[dict[str, Any]], currency: str) -> str | None:
    found = {str(p.get("currency", "")).upper() for p in periods}
    return None if found <= {currency.upper()} else f"statement currency {sorted(found)} differs from the share-price currency {currency}; no FX conversion is applied"


def _ttm(periods: list[dict[str, Any]], getter, now: datetime) -> tuple[Decimal | None, list[dict[str, Any]], str | None]:
    quarters = sorted((p for p in periods if p.get("period_type") == "quarterly" and getter(p) is not None), key=lambda p: p["period_end"], reverse=True)[:4]
    if len(quarters) < 4:
        return None, quarters, f"only {len(quarters)} quarter(s) with this figure are stored (4 needed for TTM)"
    ends = [_iso_date(p["period_end"]) for p in quarters]
    for newer, older in zip(ends, ends[1:]):
        if newer is None or older is None or not QUARTER_GAP_DAYS[0] <= (newer - older).days <= QUARTER_GAP_DAYS[1]:
            return None, quarters, "the latest four stored quarters are not consecutive"
    if (now.date() - ends[0]).days > MAX_TTM_AGE_DAYS:
        return None, quarters, f"the latest reported quarter ({ends[0]}) is more than {MAX_TTM_AGE_DAYS} days old"
    return sum((getter(p) for p in quarters), ZERO), quarters, None


def _earnings_basis(periods: list[dict[str, Any]], field: str, now: datetime) -> dict[str, Any]:
    """TTM if four consecutive quarters exist; otherwise the latest fiscal year, clearly labelled as NOT TTM."""
    total, quarters, problem = _ttm(periods, lambda p: _value(p, field), now)
    if total is not None:
        return {"value": total, "periods": quarters, "label": "TTM (sum of the latest 4 consecutive quarters)", "is_ttm": True}
    annual = sorted((p for p in periods if p.get("period_type") == "annual" and _value(p, field) is not None), key=lambda p: p["period_end"], reverse=True)
    if annual and (now.date() - _iso_date(annual[0]["period_end"])).days <= MAX_ANNUAL_FALLBACK_AGE_DAYS:
        return {"value": _value(annual[0], field), "periods": [annual[0]], "label": f"last fiscal year (NOT TTM: {problem})", "is_ttm": False}
    return {"value": None, "periods": [], "label": "", "is_ttm": False, "problem": problem or "no usable period"}


def _period_text(periods: list[dict[str, Any]]) -> str | None:
    return ", ".join(p["period_end"] for p in periods) if periods else None


def _cross(value: Decimal | None, other: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    """Disagreement record between the displayed value and the other-origin value (never scored)."""
    if value is None or other is None or other.get("value") is None:
        return None
    other_value = _dec(other["value"])
    if other_value is None or other_value == ZERO:
        return None
    diff = abs(value - other_value) / abs(other_value)
    return {"origin": other["origin"], "value": _q(other_value), "difference_pct": _q(diff * 100, 1), "disagrees": diff > CROSS_CHECK_TOLERANCE, "label": label}


# ───────────────────────── core computation ─────────────────────────
def compute_valuation(*, currency: str, technicals: dict[str, Any], fundamentals: dict[str, Any], now: datetime,
                      provider_metrics: dict[str, dict[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    periods: list[dict[str, Any]] = fundamentals.get("periods") or []
    snapshot = fundamentals.get("snapshot") or {}
    cands = _candidates(provider_metrics, snapshot)
    metrics: dict[str, dict[str, Any]] = {}
    supporting: dict[str, dict[str, Any]] = {}

    def cand_value(provider: str, name: str) -> Decimal | None:
        rec = (cands.get(provider) or {}).get(name)
        return rec["value"] if rec and rec.get("value") is not None else None

    # ----- price: only a CURRENT verified daily close
    price = _dec(technicals.get("latest_close")) if technicals.get("status") == "available" else None
    price_problem = None
    if price is None:
        price_problem = (technicals.get("staleness") or {}).get("reason") or technicals.get("message") or f"technical data status is {technicals.get('status')}; no current share price"
    elif price <= ZERO:
        price, price_problem = None, "the stored share price is not positive"
    price_input = _inp("Share price (latest completed daily close)", price, currency, source=technicals.get("source"), period_end=(technicals.get("latest_bar") or "")[:10])

    # ----- shares outstanding
    share_rows = sorted((p for p in periods if _value(p, "shares_outstanding") is not None), key=lambda p: p["period_end"], reverse=True)
    shares_period = share_rows[0] if share_rows else None
    shares = _value(shares_period, "shares_outstanding") if shares_period else None
    shares_problem = None
    if shares is None:
        shares_problem = "no shares-outstanding figure is stored"
    elif (now.date() - _iso_date(shares_period["period_end"])).days > SHARES_MAX_AGE_DAYS:
        shares, shares_problem = None, f"the latest stored share count ({shares_period['period_end']}) is more than {SHARES_MAX_AGE_DAYS} days old"
    shares_input = _inp("Shares outstanding", shares, "shares", source=shares_period.get("source") if shares_period else None,
                        period_end=shares_period["period_end"] if shares_period else None, fetched_at=shares_period.get("fetched_at") if shares_period else None)
    computed = price * shares if price is not None and shares is not None else None
    currency_problem = _currency_problem(periods, currency)

    # ----- market cap: Dhan -> Yahoo -> Screener, each reconciled against price x shares or corroborated by another provider
    def v_mcap(provider: str, rec: dict[str, Any]):
        value = rec["value"]
        if value <= ZERO:
            return False, "not positive", []
        if computed is None:
            return True, "", [f"not cross-checked: {price_problem or shares_problem}"]
        diff = _rel(computed, value)
        if diff <= MCAP_MISMATCH:
            return True, "", []
        for other in PRIORITY:
            other_value = cand_value(other, "market_cap")
            if other != provider and other_value and other_value > ZERO and _rel(other_value, value) <= MCAP_MISMATCH:
                return True, "", [f"differs from price x shares by {diff * 100:.1f}% but is corroborated by {PROVIDER_LABEL[other]}"]
        return False, f"differs from price x shares ({_q(computed, 0)}) by {diff * 100:.1f}% and no other provider corroborates it", []

    chosen, trail, disc = _select(cands, "market_cap", now, PRICE_LINKED_MAX_AGE_DAYS, v_mcap)
    market_cap: Decimal | None = None
    mcap_block: str | None = None
    if chosen is not None:
        market_cap = chosen["value"]
        metrics["market_cap"] = _reported("market_cap", chosen, unit=currency, places=0, trail=trail, discrepancies=disc,
                                          basis=f"market capitalisation as reported by {PROVIDER_LABEL[chosen['provider']]} (currency assumed to be the instrument currency)",
                                          cross_check=_cross(market_cap, {"value": str(computed), "origin": "calculated"} if computed is not None else None, "price x shares"))
    elif disc and computed is not None:
        mcap_block = ("share-count mismatch: " + "; ".join(f"{PROVIDER_LABEL[d['provider']]} market cap {d['reason']}" for d in disc) +
                      "; price, share count or units disagree, so no market-cap-based ratio is shown")
        metrics["market_cap"] = _finish(_metric("market_cap", "not_meaningful", unit=currency, reason=mcap_block, basis="provider-reported, reconciled against price x shares",
                                                inputs=[price_input, shares_input] + [_inp(f"{PROVIDER_LABEL[d['provider']]} market cap", d["value"], currency, source=d["provider"]) for d in disc]),
                                        trail, disc, calculated=False)
    elif computed is not None:
        market_cap = computed
        metrics["market_cap"] = _finish(_metric("market_cap", "calculated", value=market_cap, unit=currency, places=0, freshness="current", origin="calculated",
                                                retrieved_at=technicals.get("latest_bar"), basis="latest completed daily close x latest stored shares outstanding",
                                                warnings=["no usable provider market cap"], inputs=[price_input, shares_input]), trail, disc)
    else:
        why = [r for r in (price_problem and f"no current price ({price_problem})", shares_problem and f"no share count ({shares_problem})") if r]
        mcap_block = "no usable provider market cap; " + "; ".join(why)
        metrics["market_cap"] = _finish(_unavailable("market_cap", mcap_block, "provider-reported, or price x shares outstanding"), trail, disc)
    mcap_input = _inp("Market capitalisation", market_cap, currency, source=metrics["market_cap"].get("provider") or "calculated")

    def need_mcap(key: str, basis: str) -> dict[str, Any] | None:
        if market_cap is None:
            return _unavailable(key, mcap_block or "market capitalisation is unavailable", basis)
        if currency_problem:
            return _unavailable(key, currency_problem, basis)
        return None

    # ----- statement-based building blocks
    equity_periods = sorted((p for p in periods if _value(p, "total_equity") is not None), key=lambda p: p["period_end"], reverse=True)
    equity_p = equity_periods[0] if equity_periods else None
    equity = _value(equity_p, "total_equity") if equity_p else None
    basis_b, _mix = _basis([equity_p]) if equity_p else (None, None)
    equity_input = _inp("Shareholders' equity", equity, currency, source=equity_p.get("source") if equity_p else None, period_end=equity_p["period_end"] if equity_p else None,
                        fetched_at=equity_p.get("fetched_at") if equity_p else None, basis=basis_b)

    def calc_ratio(key: str, field: str, label: str, what: str) -> tuple[dict[str, Any], Decimal | None]:
        basis_text = f"market capitalisation / {what}"
        blocked = need_mcap(key, basis_text)
        if blocked:
            return blocked, None
        e = _earnings_basis(periods, field, now)
        if e["value"] is None:
            return _unavailable(key, f"{label} unavailable: {e['problem']}", basis_text), None
        used = e["periods"]
        basis_label, mix_problem = _basis(used)
        cur = _currency_problem(used, currency)
        inputs = [mcap_input, _inp(f"{label} ({e['label']})", e["value"], currency, source=used[0].get("source"), period_end=_period_text(used),
                                   fetched_at=max(str(p.get("fetched_at")) for p in used), basis=basis_label)]
        if mix_problem or cur:
            return _metric(key, "unavailable", reason=mix_problem or cur, basis=basis_text, inputs=inputs), None
        if e["value"] <= ZERO:
            return _metric(key, "not_meaningful", reason=f"{label} is not positive ({_q(e['value'], 0)}); the ratio is not meaningful", basis=f"{basis_text} - {e['label']}", inputs=inputs, statement_basis=basis_label), None
        warnings = []
        if not e["is_ttm"]:
            warnings.append("not TTM: " + e["label"])
        if basis_label and basis_label.startswith("unknown"):
            warnings.append("statement basis (consolidated/standalone) is not stated by the provider")
        elif basis_label == "standalone":
            warnings.append("standalone earnings compared with whole-company market cap")
        value = market_cap / e["value"]
        return _metric(key, "calculated", value=value, basis=f"{basis_text} - {e['label']}", inputs=inputs, warnings=warnings, freshness="current" if e["is_ttm"] else "stale",
                       statement_basis=basis_label, origin="calculated", reporting_period=_period_text(used), retrieved_at=inputs[1]["fetched_at"],
                       extra={"is_ttm": e["is_ttm"]}), value

    pe_calc_metric, pe_calc = calc_ratio("pe_ttm", "net_income", "Net income", "net income (TTM)")
    ps_calc_metric, ps_calc = calc_ratio("price_to_sales", "revenue", "Revenue", "revenue (TTM)")
    ni_ttm = _earnings_basis(periods, "net_income", now)
    calc_roe = None
    if ni_ttm["value"] is not None and equity is not None and equity > ZERO and not currency_problem:
        calc_roe = ni_ttm["value"] / equity * 100
    calc_bv = equity / shares if equity is not None and shares is not None and equity > ZERO and not currency_problem else None

    # ----- P/E: Dhan -> Yahoo -> Screener -> market cap / TTM net income
    def v_pe(provider: str, rec: dict[str, Any]):
        value = rec["value"]
        problem = _in_bounds("pe", value)
        if problem:
            return False, problem + (" - P/E is not meaningful for loss-making companies" if value <= ZERO else ""), []
        eps = cand_value(provider, "eps")
        if pe_calc is not None:
            ref, ref_name = pe_calc, "market cap / TTM net income"
        elif price is not None and eps is not None and eps > ZERO:
            ref, ref_name = price / eps, f"price / {PROVIDER_LABEL[provider]} EPS"
        else:
            return True, "", ["not cross-checked: no TTM earnings or current price / EPS to verify against"]
        if _rel(value, ref) > PE_TOLERANCE:
            return False, f"differs from {ref_name} ({_q(ref)}) by {_rel(value, ref) * 100:.0f}% (tolerance {PE_TOLERANCE * 100:.0f}%)", []
        return True, "", []

    chosen, trail, disc = _select(cands, "pe", now, PRICE_LINKED_MAX_AGE_DAYS, v_pe)
    if chosen is not None:
        metrics["pe_ttm"] = _reported("pe_ttm", chosen, unit="x", trail=trail, discrepancies=disc, basis=f"P/E as reported by {PROVIDER_LABEL[chosen['provider']]} (trailing)",
                                      cross_check=_cross(chosen["value"], {"value": pe_calc_metric["value"], "origin": "calculated"} if pe_calc_metric["status"] == "calculated" else None, "market cap / TTM net income"))
    else:
        metrics["pe_ttm"] = _finish(pe_calc_metric, trail, disc)
    metrics["pe_ttm"].setdefault("label", LABELS["pe_ttm"])

    # ----- P/B: Dhan -> Yahoo -> Screener -> market cap / equity
    pb_basis = "market capitalisation / latest reported shareholders' equity"

    def calc_pb() -> dict[str, Any]:
        blocked = need_mcap("pb", pb_basis)
        if blocked:
            return blocked
        if equity is None:
            return _unavailable("pb", "no shareholders' equity is stored", pb_basis)
        if equity <= ZERO:
            return _metric("pb", "not_meaningful", reason=f"shareholders' equity is not positive ({_q(equity, 0)}); P/B is not meaningful", basis=pb_basis, inputs=[mcap_input, equity_input], statement_basis=basis_b)
        warnings = []
        if basis_b and basis_b.startswith("unknown"):
            warnings.append("statement basis (consolidated/standalone) is not stated by the provider")
        elif basis_b == "standalone":
            warnings.append("standalone equity compared with whole-company market cap")
        fresh = "current" if (now.date() - _iso_date(equity_p["period_end"])).days <= MAX_ANNUAL_FALLBACK_AGE_DAYS else "stale"
        return _metric("pb", "calculated", value=market_cap / equity, basis=pb_basis, inputs=[mcap_input, equity_input], warnings=warnings, freshness=fresh, statement_basis=basis_b,
                       origin="calculated", reporting_period=equity_p["period_end"], retrieved_at=equity_p.get("fetched_at"))

    calc_pb_metric = calc_pb()

    def v_pb(provider: str, rec: dict[str, Any]):
        value = rec["value"]
        problem = _in_bounds("pb", value)
        if problem:
            return False, problem, []
        if rec.get("reconciliation") == "discrepancy":
            return False, "disagrees with price / book value per share", []
        bvps = cand_value(provider, "book_value")
        if price is not None and bvps is not None and bvps > ZERO:
            implied = price / bvps
            if _rel(value, implied) > PB_TOLERANCE:
                return False, f"differs from price / book value ({_q(implied)}) by {_rel(value, implied) * 100:.1f}% (tolerance {PB_TOLERANCE * 100:.0f}%)", []
            return True, "", []
        return True, "", ["not cross-checked against price / book value (no current price or provider book value)"]

    chosen, trail, disc = _select(cands, "pb", now, PRICE_LINKED_MAX_AGE_DAYS, v_pb)
    if chosen is not None:
        metrics["pb"] = _reported("pb", chosen, unit="x", trail=trail, discrepancies=disc, basis=f"P/B as reported by {PROVIDER_LABEL[chosen['provider']]}",
                                  cross_check=_cross(chosen["value"], {"value": calc_pb_metric["value"], "origin": "calculated"} if calc_pb_metric["status"] == "calculated" else None, "market cap / equity"))
    else:
        metrics["pb"] = _finish(calc_pb_metric, trail, disc)

    # ----- book value per share: Dhan -> Yahoo -> Screener -> equity / shares
    bv_basis = "latest reported shareholders' equity / latest stored shares outstanding"

    def v_bv(provider: str, rec: dict[str, Any]):
        if rec["value"] <= ZERO:
            return False, "not positive", []
        if calc_bv is not None and _rel(rec["value"], calc_bv) > RATIO_TOLERANCE:
            return False, f"differs from equity / shares ({_q(calc_bv)}) by {_rel(rec['value'], calc_bv) * 100:.0f}%", []
        return True, "", [] if calc_bv is not None else ["not cross-checked: no stored equity and share count"]

    chosen, trail, disc = _select(cands, "book_value", now, STATIC_MAX_AGE_DAYS, v_bv)
    if chosen is not None:
        metrics["book_value_per_share"] = _reported("book_value_per_share", chosen, unit=f"{currency}/share", trail=trail, discrepancies=disc,
                                                    basis=f"book value per share as reported by {PROVIDER_LABEL[chosen['provider']]}",
                                                    cross_check=_cross(chosen["value"], {"value": str(calc_bv), "origin": "calculated"} if calc_bv is not None else None, bv_basis))
    elif equity is None or shares is None:
        metrics["book_value_per_share"] = _finish(_unavailable("book_value_per_share", "; ".join(r for r in (equity is None and "no shareholders' equity is stored", shares is None and shares_problem) if r), bv_basis), trail, disc)
    elif currency_problem:
        metrics["book_value_per_share"] = _finish(_unavailable("book_value_per_share", currency_problem, bv_basis), trail, disc)
    elif equity <= ZERO:
        metrics["book_value_per_share"] = _finish(_metric("book_value_per_share", "not_meaningful", reason="shareholders' equity is not positive", basis=bv_basis, inputs=[equity_input, shares_input]), trail, disc)
    else:
        warns = [f"equity is at {equity_p['period_end']} but the share count is at {shares_period['period_end']}"] if equity_p["period_end"] != shares_period["period_end"] else []
        metrics["book_value_per_share"] = _finish(_metric("book_value_per_share", "calculated", value=calc_bv, unit=f"{currency}/share", basis=bv_basis, inputs=[equity_input, shares_input], warnings=warns,
                                                          statement_basis=basis_b, origin="calculated", reporting_period=equity_p["period_end"], retrieved_at=equity_p.get("fetched_at"), freshness="current"), trail, disc)

    # ----- price / sales: Yahoo only reports it -> market cap / TTM revenue
    def v_ps(provider: str, rec: dict[str, Any]):
        problem = _in_bounds("price_to_sales", rec["value"])
        if problem:
            return False, problem, []
        if ps_calc is not None and _rel(rec["value"], ps_calc) > RATIO_TOLERANCE:
            return False, f"differs from market cap / TTM revenue ({_q(ps_calc)}) by {_rel(rec['value'], ps_calc) * 100:.0f}%", []
        return True, "", [] if ps_calc is not None else ["not cross-checked: no TTM revenue stored"]

    chosen, trail, disc = _select(cands, "price_to_sales", now, PRICE_LINKED_MAX_AGE_DAYS, v_ps)
    if chosen is not None:
        metrics["price_to_sales"] = _reported("price_to_sales", chosen, unit="x", trail=trail, discrepancies=disc, basis=f"P/S as reported by {PROVIDER_LABEL[chosen['provider']]}",
                                              cross_check=_cross(chosen["value"], {"value": ps_calc_metric["value"], "origin": "calculated"} if ps_calc_metric["status"] == "calculated" else None, "market cap / TTM revenue"))
    else:
        metrics["price_to_sales"] = _finish(ps_calc_metric, trail, disc)

    # ----- EPS and EBITDA (supporting inputs; shown with provenance, used by the checks and by EV/EBITDA)
    chosen, trail, disc = _select(cands, "eps", now, STATIC_MAX_AGE_DAYS)
    eps_calc = ni_ttm["value"] / shares if ni_ttm["value"] is not None and shares is not None and not currency_problem else None
    if chosen is not None:
        supporting["eps"] = _reported("eps", chosen, unit=f"{currency}/share", trail=trail, discrepancies=disc, basis=f"EPS as reported by {PROVIDER_LABEL[chosen['provider']]} (basis not stated)")
    elif eps_calc is not None:
        supporting["eps"] = _finish(_metric("eps", "calculated", value=eps_calc, unit=f"{currency}/share", basis=f"net income ({ni_ttm['label']}) / latest shares outstanding", origin="calculated",
                                            reporting_period=_period_text(ni_ttm["periods"]), freshness="current" if ni_ttm["is_ttm"] else "stale", warnings=[] if ni_ttm["is_ttm"] else ["not TTM: " + ni_ttm["label"]]), trail, disc)
    else:
        supporting["eps"] = _finish(_unavailable("eps", "no provider EPS and no TTM net income with a share count", "net income (TTM) / shares outstanding"), trail, disc)

    ttm_op, op_periods, _op_problem = _ttm(periods, lambda p: _extra(p, "operating_profit"), now)
    ebitda_calc = ttm_op if ttm_op is not None and not currency_problem else None

    def v_ebitda(provider: str, rec: dict[str, Any]):
        value = rec["value"]
        refs = [("TTM operating profit", ebitda_calc)] + [(f"{PROVIDER_LABEL[o]} EBITDA", cand_value(o, "ebitda")) for o in PRIORITY if o != provider and not ((cands.get(o) or {}).get("ebitda") or {}).get("unit_assumed")]
        for name, ref in refs:
            if ref is not None and ref > ZERO and value > ZERO and _rel(value, ref) > D("0.5"):
                return False, f"differs from {name} ({_q(ref, 0)}) by {_rel(value, ref) * 100:.0f}% (unit or period mismatch)", []
        return True, "", []

    chosen, trail, disc = _select(cands, "ebitda", now, STATIC_MAX_AGE_DAYS, v_ebitda)
    if chosen is not None:
        supporting["ebitda"] = _reported("ebitda", chosen, unit=currency, places=0, trail=trail, discrepancies=disc, basis=f"EBITDA as reported by {PROVIDER_LABEL[chosen['provider']]} (period not stated)")
    elif ebitda_calc is not None:
        supporting["ebitda"] = _finish(_metric("ebitda", "calculated", value=ebitda_calc, unit=currency, places=0, basis="TTM operating profit (revenue less operating expenses before depreciation)", origin="calculated",
                                               reporting_period=_period_text(op_periods), freshness="current", statement_basis=_basis(op_periods)[0]), trail, disc)
    else:
        supporting["ebitda"] = _finish(_unavailable("ebitda", "no provider EBITDA and no TTM operating profit is stored (it comes from a Screener import or licensed feed)", "TTM operating profit"), trail, disc)
    ebitda_value = _dec(supporting["ebitda"]["value"]) if supporting["ebitda"]["value"] is not None else None

    # ----- PEG (trailing): Yahoo -> P/E (TTM) / growth of TTM net income
    peg_basis = "P/E (TTM) / year-on-year growth (%) of TTM net income; trailing, not forward"
    if pe_calc is None:
        peg_calc = _unavailable("peg", f"P/E (TTM) cannot be calculated: {pe_calc_metric['reason'] or pe_calc_metric['status']}", peg_basis)
    else:
        quarters = sorted((p for p in periods if p.get("period_type") == "quarterly" and _value(p, "net_income") is not None), key=lambda p: p["period_end"], reverse=True)
        older = [p for p in quarters if (_iso_date(quarters[0]["period_end"]) - _iso_date(p["period_end"])).days >= 350][:4] if quarters else []
        prior_total = None
        if len(older) == 4:
            ends = [_iso_date(p["period_end"]) for p in older]
            if all(QUARTER_GAP_DAYS[0] <= (a - b).days <= QUARTER_GAP_DAYS[1] for a, b in zip(ends, ends[1:])):
                prior_total = sum((_value(p, "net_income") for p in older), ZERO)
        current_total = ni_ttm
        if prior_total is None or not current_total["is_ttm"]:
            peg_calc = _unavailable("peg", "eight consecutive quarters of net income are needed to measure TTM growth", peg_basis)
        elif prior_total <= ZERO or current_total["value"] <= ZERO:
            peg_calc = _metric("peg", "not_meaningful", reason="growth from or to non-positive earnings is not meaningful, so PEG is not shown", basis=peg_basis)
        else:
            growth = (current_total["value"] - prior_total) / prior_total * 100
            if growth <= ZERO:
                peg_calc = _metric("peg", "not_meaningful", reason=f"TTM net income growth is {_q(growth, 1)}%; PEG needs positive growth", basis=peg_basis,
                                   inputs=[_inp("P/E (TTM)", _q(pe_calc), "x"), _inp("TTM net income growth", _q(growth, 1), "%")])
            else:
                peg_calc = _metric("peg", "calculated", value=pe_calc / growth, basis=peg_basis, warnings=["trailing growth is not a forecast"], freshness="current", origin="calculated",
                                   reporting_period=_period_text(current_total["periods"]), retrieved_at=max(str(p.get("fetched_at")) for p in current_total["periods"]),
                                   statement_basis=_basis(current_total["periods"])[0],
                                   inputs=[_inp("P/E (TTM)", _q(pe_calc), "x"), _inp("TTM net income growth", _q(growth, 1), "%")])

    def v_peg(provider: str, rec: dict[str, Any]):
        problem = _in_bounds("peg", rec["value"])
        return (False, problem, []) if problem else (True, "", ["trailing, not a forecast"])

    chosen, trail, disc = _select(cands, "peg", now, PRICE_LINKED_MAX_AGE_DAYS, v_peg)
    metrics["peg"] = _reported("peg", chosen, unit="x", trail=trail, discrepancies=disc, basis=f"PEG as reported by {PROVIDER_LABEL[chosen['provider']]}") if chosen is not None else _finish(peg_calc, trail, disc)

    # ----- dividend yield: Dhan -> Yahoo -> Screener (providers only; stored in canonical percent, a reported 0 may mean "none")
    def v_dy(provider: str, rec: dict[str, Any]):
        value = rec["value"]
        if value < ZERO or value > BOUNDS["dividend_yield"][1]:
            return False, f"outside 0-{BOUNDS['dividend_yield'][1]}% ({value})", []
        return True, "", ["a reported 0 may mean no dividend or no data"] if value == ZERO else []

    chosen, trail, disc = _select(cands, "dividend_yield", now, STATIC_MAX_AGE_DAYS, v_dy)
    metrics["dividend_yield"] = (_reported("dividend_yield", chosen, unit="%", trail=trail, discrepancies=disc, basis=f"dividend yield as reported by {PROVIDER_LABEL[chosen['provider']]} (percent)")
                                 if chosen is not None else _finish(_unavailable("dividend_yield", "no provider reports a usable dividend yield", "provider-reported"), trail, disc, calculated=False))

    # ----- enterprise value and EV/EBITDA, net debt / EBITDA
    yf_debt, yf_cash = cand_value("yfinance", "total_debt"), cand_value("yfinance", "total_cash")
    yf_debt_age = _age_days(((cands.get("yfinance") or {}).get("total_debt") or {}).get("retrieved_at"), now)
    yf_cash_age = _age_days(((cands.get("yfinance") or {}).get("total_cash") or {}).get("retrieved_at"), now)
    cash_fresh = yf_cash is not None and yf_cash_age is not None and yf_cash_age <= STATIC_MAX_AGE_DAYS
    debt_fresh = yf_debt is not None and (yf_debt_age if yf_debt_age is not None else 999) <= STATIC_MAX_AGE_DAYS
    debt_rows = sorted((p for p in periods if _value(p, "total_debt") is not None), key=lambda p: p["period_end"], reverse=True)
    total_debt, debt_src = None, None
    if debt_fresh:
        total_debt, debt_src = yf_debt, "Yahoo Finance balance sheet (same source as cash)"
    elif debt_rows and (now.date() - _iso_date(debt_rows[0]["period_end"])).days <= MAX_ANNUAL_FALLBACK_AGE_DAYS and not currency_problem:
        total_debt, debt_src = _value(debt_rows[0], "total_debt"), f"stored statements ({debt_rows[0]['period_end']})"
    total_cash = yf_cash if cash_fresh else None
    ev_basis = "market capitalisation + total debt - cash and equivalents"
    ev_missing = [r for r in (market_cap is None and "market capitalisation", total_debt is None and "total debt", total_cash is None and "cash and equivalents (stored only from Yahoo Finance)") if r]

    def v_ev(provider: str, rec: dict[str, Any]):
        return (False, "not positive", []) if rec["value"] <= ZERO else (True, "", [])

    chosen, trail, disc = _select(cands, "enterprise_value", now, PRICE_LINKED_MAX_AGE_DAYS, v_ev)
    if chosen is not None:
        metrics["enterprise_value"] = _reported("enterprise_value", chosen, unit=currency, places=0, trail=trail, discrepancies=disc, basis=f"enterprise value as reported by {PROVIDER_LABEL[chosen['provider']]}")
    elif ev_missing:
        metrics["enterprise_value"] = _finish(_unavailable("enterprise_value", "cannot be calculated without: " + "; ".join(ev_missing), ev_basis), trail, disc)
    else:
        ev_warn = [] if debt_src and debt_src.startswith("Yahoo") else [f"debt is from {debt_src} while cash is from Yahoo Finance (different dates)"]
        metrics["enterprise_value"] = _finish(_metric("enterprise_value", "calculated", value=market_cap + total_debt - total_cash, unit=currency, places=0, basis=ev_basis, origin="calculated", freshness="current", warnings=ev_warn,
                                                      inputs=[mcap_input, _inp("Total debt", total_debt, currency, source=debt_src), _inp("Cash and equivalents", total_cash, currency, source="yfinance")]), trail, disc)
    ev_value = _dec(metrics["enterprise_value"]["value"]) if metrics["enterprise_value"]["value"] is not None else None

    ev_ebitda_calc_basis = "enterprise value / EBITDA"
    if ev_value is None or ebitda_value is None:
        ev_calc = _unavailable("ev_ebitda", "cannot be calculated without: " + "; ".join(r for r in (ev_value is None and f"enterprise value ({metrics['enterprise_value']['reason'] or metrics['enterprise_value']['status']})", ebitda_value is None and f"EBITDA ({supporting['ebitda']['reason'] or supporting['ebitda']['status']})") if r), ev_ebitda_calc_basis)
    elif ebitda_value <= ZERO:
        ev_calc = _metric("ev_ebitda", "not_meaningful", reason="EBITDA is not positive; EV/EBITDA is not meaningful", basis=ev_ebitda_calc_basis)
    else:
        ev_calc = _metric("ev_ebitda", "calculated", value=ev_value / ebitda_value, basis=ev_ebitda_calc_basis, origin="calculated", freshness="current",
                          inputs=[_inp("Enterprise value", ev_value, currency, source=metrics["enterprise_value"]["provider"] or "calculated"), _inp("EBITDA", ebitda_value, currency, source=supporting["ebitda"]["provider"] or "calculated")])

    def v_ev_ebitda(provider: str, rec: dict[str, Any]):
        problem = _in_bounds("ev_ebitda", rec["value"])
        if problem:
            return False, problem, []
        own_ev, own_ebitda = cand_value(provider, "enterprise_value"), cand_value(provider, "ebitda")
        if own_ev and own_ebitda and own_ebitda > ZERO and _rel(rec["value"], own_ev / own_ebitda) > PE_TOLERANCE:
            return False, f"differs from the same provider's EV / EBITDA ({_q(own_ev / own_ebitda)})", []
        return True, "", []

    chosen, trail, disc = _select(cands, "ev_ebitda", now, PRICE_LINKED_MAX_AGE_DAYS, v_ev_ebitda)
    metrics["ev_ebitda"] = (_reported("ev_ebitda", chosen, unit="x", trail=trail, discrepancies=disc, basis=f"EV / EBITDA as reported by {PROVIDER_LABEL[chosen['provider']]}",
                                      cross_check=_cross(chosen["value"], {"value": ev_calc["value"], "origin": "calculated"} if ev_calc["status"] == "calculated" else None, "enterprise value / EBITDA"))
                            if chosen is not None else _finish(ev_calc, trail, disc))
    nd_basis = "(total debt - cash and equivalents) / EBITDA"
    if total_debt is None or total_cash is None or ebitda_value is None:
        metrics["net_debt_ebitda"] = _finish(_unavailable("net_debt_ebitda", "cannot be calculated without: " + "; ".join(r for r in (total_debt is None and "total debt", total_cash is None and "cash and equivalents", ebitda_value is None and "EBITDA") if r), nd_basis), [], [])
    elif ebitda_value <= ZERO:
        metrics["net_debt_ebitda"] = _finish(_metric("net_debt_ebitda", "not_meaningful", reason="EBITDA is not positive", basis=nd_basis), [], [])
    else:
        metrics["net_debt_ebitda"] = _finish(_metric("net_debt_ebitda", "calculated", value=(total_debt - total_cash) / ebitda_value, basis=nd_basis, origin="calculated", freshness="current",
                                                     inputs=[_inp("Total debt", total_debt, currency, source=debt_src), _inp("Cash and equivalents", total_cash, currency, source="yfinance"), _inp("EBITDA", ebitda_value, currency)]), [], [])

    # ----- ROE: Dhan -> Yahoo -> Screener -> TTM net income / year-end equity
    roe_basis = "TTM net income / latest reported shareholders' equity (year-end equity, not an average)"
    if ni_ttm["value"] is None or equity is None:
        roe_calc = _unavailable("roe", f"net income unavailable: {ni_ttm.get('problem')}" if ni_ttm["value"] is None else "no shareholders' equity is stored", roe_basis)
    else:
        used = ni_ttm["periods"] + [equity_p]
        label, mix_problem = _basis(used)
        cur = _currency_problem(used, currency)
        if mix_problem or cur:
            roe_calc = _unavailable("roe", mix_problem or cur, roe_basis)
        elif equity <= ZERO:
            roe_calc = _metric("roe", "not_meaningful", reason="shareholders' equity is not positive; ROE is not meaningful", basis=roe_basis, statement_basis=label)
        else:
            roe_calc = _metric("roe", "calculated", value=ni_ttm["value"] / equity * 100, unit="%", basis=roe_basis, origin="calculated", statement_basis=label, warnings=[] if ni_ttm["is_ttm"] else ["not TTM: " + ni_ttm["label"]],
                               reporting_period=_period_text(ni_ttm["periods"]), retrieved_at=max(str(p.get("fetched_at")) for p in used), freshness="current" if ni_ttm["is_ttm"] else "stale",
                               inputs=[_inp("Net income", ni_ttm["value"], currency, period_end=_period_text(ni_ttm["periods"])), equity_input])

    def v_roe(provider: str, rec: dict[str, Any]):
        value = rec["value"]
        problem = _in_bounds("roe", value)
        if problem:
            return False, problem, []
        if calc_roe is not None and abs(value - calc_roe) > max(ROE_ABSOLUTE_SLACK, abs(calc_roe) * RATIO_TOLERANCE):
            return False, f"differs from TTM net income / equity ({_q(calc_roe)}%) by {_q(abs(value - calc_roe))} points", []
        return True, "", [] if calc_roe is not None else ["not cross-checked: no TTM net income and equity stored"]

    chosen, trail, disc = _select(cands, "roe", now, STATIC_MAX_AGE_DAYS, v_roe)
    metrics["roe"] = (_reported("roe", chosen, unit="%", trail=trail, discrepancies=disc, basis=f"ROE as reported by {PROVIDER_LABEL[chosen['provider']]} (period not stated)",
                                cross_check=_cross(chosen["value"], {"value": roe_calc["value"], "origin": "calculated"} if roe_calc["status"] == "calculated" else None, roe_basis))
                      if chosen is not None else _finish(roe_calc, trail, disc))

    # ----- interest coverage (no provider reports it; Screener-imported extras only) and ROCE (Dhan -> Screener)
    ttm_dep, _p2, _d = _ttm(periods, lambda p: _extra(p, "depreciation"), now)
    ttm_int, int_periods, _i = _ttm(periods, lambda p: _extra(p, "interest_expense"), now)
    ic_basis = "EBIT (operating profit - depreciation) / interest expense (TTM)"
    ic_periods = int_periods
    if ttm_op is None or ttm_dep is None or ttm_int is None:
        annual = sorted((p for p in periods if p.get("period_type") == "annual" and None not in (_extra(p, "operating_profit"), _extra(p, "depreciation"), _extra(p, "interest_expense"))),
                        key=lambda p: p["period_end"], reverse=True)
        if annual and (now.date() - _iso_date(annual[0]["period_end"])).days <= MAX_ANNUAL_FALLBACK_AGE_DAYS:
            a = annual[0]
            ttm_op, ttm_dep, ttm_int, ic_basis, ic_periods = _extra(a, "operating_profit"), _extra(a, "depreciation"), _extra(a, "interest_expense"), "EBIT (operating profit - depreciation) / interest expense (last fiscal year, NOT TTM)", [a]
        else:
            ic_periods = []
    if ttm_op is None or ttm_dep is None or ttm_int is None:
        ic = _unavailable("interest_coverage", "operating profit, depreciation and interest expense are not all stored (no provider reports interest coverage; they come from the Screener importer or a licensed feed)", ic_basis)
    elif ttm_int <= ZERO:
        ic = _metric("interest_coverage", "not_meaningful", reason="interest expense is zero or negative, so a coverage ratio is not defined", basis=ic_basis, statement_basis=_basis(ic_periods)[0])
    else:
        ebit = ttm_op - ttm_dep
        label, mix_problem = _basis(ic_periods)
        ic = _metric("interest_coverage", "not_meaningful" if mix_problem else "calculated", value=None if mix_problem else ebit / ttm_int, reason=mix_problem, basis=ic_basis,
                     statement_basis=label, origin=None if mix_problem else "calculated", reporting_period=_period_text(ic_periods),
                     inputs=[_inp("EBIT", ebit, currency), _inp("Interest expense", ttm_int, currency, period_end=_period_text(ic_periods))],
                     warnings=(["EBIT is negative: interest is not covered by operations"] if ebit < ZERO else []) + ([] if "NOT TTM" not in ic_basis else ["not TTM"]))
    metrics["interest_coverage"] = _finish(ic, [], [])

    # Screener's annual ROCE (a statement ratio imported with the page) joins the chain as the Screener candidate when no top-ratio ROCE is stored.
    reported_roce = sorted((p for p in periods if p.get("period_type") == "annual" and _extra(p, "roce_pct") is not None), key=lambda p: p["period_end"], reverse=True)
    if reported_roce and "roce" not in (cands.get("screener") or {}):
        p = reported_roce[0]
        info = (p.get("extra_metrics") or {}).get("roce_pct") or {}
        cands.setdefault("screener", {})["roce"] = {"value": _extra(p, "roce_pct"), "retrieved_at": info.get("fetched_at"), "reporting_period": p["period_end"], "unit_assumed": False}

    def v_roce(provider: str, rec: dict[str, Any]):
        problem = _in_bounds("roce", rec["value"])
        return (False, problem, []) if problem else (True, "", ["capital-employed definition is provider-defined"])

    chosen, trail, disc = _select(cands, "roce", now, MAX_ANNUAL_FALLBACK_AGE_DAYS, v_roce)
    metrics["roce"] = (_reported("roce", chosen, unit="%", trail=trail, discrepancies=disc, basis=f"ROCE as reported by {PROVIDER_LABEL[chosen['provider']]} (provider-defined)")
                       if chosen is not None else _finish(_unavailable("roce", "no provider reports ROCE, and capital employed cannot be computed because current liabilities are not stored", "EBIT / capital employed"), trail, disc))

    # ----- Dhan industry benchmarks (context only: never scored, never an input to a calculation)
    benchmarks: dict[str, Any] = {}
    dhan = cands.get("dhan") or {}
    for name, label, unit in (("industry_pb", "Industry P/B", "x"), ("industry_eps", "Industry EPS", "INR/share"), ("industry_dividend_yield", "Industry dividend yield", "%"),
                              ("industry_operating_margin", "Industry operating margin", "%"), ("industry_debt_to_equity", "Industry debt / equity", "x"), ("industry_current_ratio", "Industry current ratio", "x")):
        rec = dhan.get(name)
        age = _age_days(rec.get("retrieved_at"), now) if rec else None
        if rec and rec.get("value") is not None and age is not None and age <= STATIC_MAX_AGE_DAYS and (name != "industry_pb" or rec["value"] > ZERO):
            benchmarks[name] = {"label": label, "value": _q(rec["value"]), "unit": unit, "provider": "dhan", "retrieved_at": rec["retrieved_at"]}
    pe_rec, ratio_rec = dhan.get("pe"), dhan.get("pe_to_sector_pe")
    if pe_rec and ratio_rec and pe_rec.get("value") and ratio_rec.get("value") and pe_rec["value"] > ZERO and ratio_rec["value"] > ZERO:
        benchmarks["industry_pe_implied"] = {"label": "Sector P/E (implied by Dhan P/E and P/E-to-sector)", "value": _q(pe_rec["value"] / ratio_rec["value"]), "unit": "x", "provider": "dhan",
                                             "retrieved_at": pe_rec["retrieved_at"]}
    elif ratio_rec and ratio_rec.get("value") is not None and ratio_rec["value"] <= ZERO:
        benchmarks["industry_pe_implied_rejected"] = {"label": "Sector P/E", "reason": f"Dhan PE_TO_SECTOR_PE is not positive ({ratio_rec['value']}); no implied sector P/E", "provider": "dhan"}

    return {
        "model_version": MODEL_VERSION, "currency": currency, "as_of": now.isoformat(),
        "price": {"value": str(price) if price is not None else None, "as_of": (technicals.get("latest_bar") or "")[:10] or None, "current": price is not None, "problem": price_problem},
        "metrics": metrics, "supporting_metrics": supporting, "provider_benchmarks": benchmarks, "peer_comparison": None, "scoring": None,
        "data_gaps": [f"{m['label']}: {m['reason']}" for m in metrics.values() if m["status"] == "unavailable" and m["reason"]][:12],
    }


# ───────────────────────── Peer comparison (only when enough comparable peers exist) ─────────────────────────
PEER_METRICS = ("pe_ttm", "pb", "price_to_sales")
MAX_PEERS = 80


def _periods_from_rows(rows: list[InstrumentFundamental]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        values = {f: {"value": str(getattr(row, f))} for f in ("revenue", "net_income", "total_equity", "shares_outstanding", "total_debt") if getattr(row, f) is not None}
        out.append({"period_type": row.period_type, "period_end": row.period_end.isoformat(), "currency": row.currency, "values": values,
                    "source": row.source, "fetched_at": row.fetched_at.isoformat(), "statement_basis": row.statement_basis, "extra_metrics": row.extra_metrics or {}})
    return out


def _peer_metrics(db: Session, peer: Instrument, now: datetime) -> dict[str, dict[str, Any]]:
    """A peer's multiples through the SAME Dhan -> Yahoo -> Screener -> calculation chain as the subject's."""
    bar = db.scalar(select(MarketBar).where(MarketBar.instrument_id == peer.id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False)).order_by(MarketBar.timestamp.desc()).limit(1))
    technicals: dict[str, Any] = {"status": "unavailable"}
    if bar is not None:
        stale = daily_bar_staleness(bar.timestamp, now, country=peer.country, continuous=False, age_limit_seconds=settings.daily_close_price_max_age_seconds)["stale"]
        technicals = {"status": "stale" if stale else "available", "latest_close": str(bar.close), "latest_bar": bar.timestamp.isoformat(), "source": bar.source}
    rows = list(db.scalars(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == peer.id)))
    snap = db.scalar(select(InstrumentSnapshot).where(InstrumentSnapshot.instrument_id == peer.id))
    stamp = snap.fetched_at.isoformat() if snap else None
    snapshot = {"source": snap.source if snap else None, "fetched_at": stamp, "values": {
        name: {"value": str(getattr(snap, name)) if snap and getattr(snap, name) is not None else None, "source": snap.source if snap else None, "fetched_at": stamp}
        for name in ("market_cap", "pe", "pb", "dividend_yield")}}
    result = compute_valuation(currency=peer.currency, technicals=technicals, fundamentals={"periods": _periods_from_rows(rows), "snapshot": snapshot}, now=now,
                               provider_metrics=valuation_inputs.load(db, peer.id))
    return {k: {"value": D(result["metrics"][k]["value"]), "origin": result["metrics"][k]["origin"], "provider": result["metrics"][k]["provider"]}
            for k in PEER_METRICS if result["metrics"][k]["status"] in {"calculated", "reported"}}


def classification(db: Session, instrument_id: Any) -> tuple[str | None, str | None, str | None]:
    """(sector, industry, provider) from the stored provider classification: Dhan first, then Yahoo Finance (a provider's labels are only
    compared with the same provider's labels, since the taxonomies differ)."""
    stored = valuation_inputs.load(db, instrument_id)
    for provider in ("dhan", "yfinance"):
        data = stored.get(provider) or {}
        sector, industry = (data.get("sector") or {}).get("text"), (data.get("industry") or {}).get("text")
        if sector or industry:
            return sector, industry, provider
    snap = db.scalar(select(InstrumentSnapshot).where(InstrumentSnapshot.instrument_id == instrument_id))
    return (snap.sector, snap.industry, "yfinance") if snap and (snap.sector or snap.industry) else (None, None, None)


def peer_instruments(db: Session, instrument: Instrument, level: str, label: str, provider: str, limit: int = MAX_PEERS) -> list[Instrument]:
    """Other listings classified into the same industry/sector by the same provider (same country and currency; one listing per symbol)."""
    rows = db.scalars(select(Instrument).join(ValuationProviderInput, ValuationProviderInput.instrument_id == Instrument.id).where(
        ValuationProviderInput.provider == provider, ValuationProviderInput.metric == level, ValuationProviderInput.text_value == label,
        Instrument.id != instrument.id, Instrument.symbol != instrument.symbol, Instrument.country == instrument.country, Instrument.currency == instrument.currency,
        Instrument.is_synthetic.is_(False)).order_by(Instrument.symbol)).all()
    if not rows and provider == "yfinance":  # legacy classification stored on the snapshot row
        column = InstrumentSnapshot.industry if level == "industry" else InstrumentSnapshot.sector
        rows = db.scalars(select(Instrument).join(InstrumentSnapshot, InstrumentSnapshot.instrument_id == Instrument.id).where(
            column == label, Instrument.id != instrument.id, Instrument.symbol != instrument.symbol, Instrument.country == instrument.country,
            Instrument.currency == instrument.currency, Instrument.is_synthetic.is_(False)).order_by(Instrument.symbol)).all()
    seen: set[str] = set()
    unique = [p for p in rows if not (p.symbol in seen or seen.add(p.symbol))]
    return unique[:limit]


def _collect(db: Session, peers: list[Instrument], now: datetime) -> dict[str, list[dict[str, Any]]]:
    collected: dict[str, list[dict[str, Any]]] = {k: [] for k in PEER_METRICS}
    for peer in peers:
        for key, record in _peer_metrics(db, peer, now).items():
            collected[key].append(record)
    return collected


def _comparable(collected: dict[str, list[dict[str, Any]]], valuation: dict[str, Any], key: str) -> list[Decimal]:
    subject = valuation["metrics"][key]
    # A peer is comparable only when its figure has the SAME origin and provider as the subject's: a provider-reported
    # P/B is never ranked against calculated P/Bs, nor one provider's figures against another's.
    return sorted(r["value"] for r in collected[key] if subject.get("origin") and r["origin"] == subject["origin"] and r["provider"] == subject.get("provider"))


def peer_comparison(db: Session, instrument: Instrument, valuation: dict[str, Any], now: datetime, *, max_peers: int = MAX_PEERS,
                    sector: str | None = None, industry: str | None = None) -> dict[str, Any]:
    stored_sector, stored_industry, provider = classification(db, instrument.id)
    sector, industry = stored_sector or sector, stored_industry or industry
    provider = provider or "yfinance"
    if not sector and not industry:
        return {"status": "unavailable", "reason": "no sector or industry is stored for this instrument", "metrics": {}}
    # Industry first; widen to the sector only when the industry cannot supply the minimum comparable peers (the level used is reported).
    levels = [(lv, lb) for lv, lb in (("industry", industry), ("sector", sector)) if lb]
    chosen_level, chosen_label, collected, peers_n, note = levels[0][0], levels[0][1], None, 0, None
    for index, (level, label) in enumerate(levels):
        peers = peer_instruments(db, instrument, level, label, provider, max_peers)
        trial = _collect(db, peers, now)
        chosen_level, chosen_label, collected, peers_n = level, label, trial, len(peers)
        if any(len(_comparable(trial, valuation, k)) >= MIN_PEERS for k in PEER_METRICS) or index == len(levels) - 1:
            if index > 0:
                note = f"the {levels[0][0]} '{levels[0][1]}' had too few comparable peers, so the broader {level} is used"
            break
    out: dict[str, Any] = {}
    for key in PEER_METRICS:
        subject = valuation["metrics"][key]
        same = _comparable(collected, valuation, key)
        entry: dict[str, Any] = {"peer_count": len(same), "peers_with_metric": len(collected[key]), "origin": subject.get("origin")}
        if len(same) < MIN_PEERS:
            extra = f" ({len(collected[key])} peer(s) have the metric on a different basis)" if len(collected[key]) > len(same) else ""
            entry.update(status="insufficient_peers", reason=f"only {len(same)} comparable peer(s) with this metric on the same basis (minimum {MIN_PEERS}){extra}")
        elif subject["status"] not in {"calculated", "reported"}:
            entry.update(status="unavailable", reason="the instrument's own metric is not available")
        else:
            mine = D(subject["value"])
            below = sum(1 for v in same if v < mine)
            entry.update(status="compared", peer_median=_q(median(same)), peer_p25=_q(same[len(same) // 4]), peer_p75=_q(same[(3 * len(same)) // 4]),
                         percentile_rank=_q(D(below) / D(len(same)) * 100, 0), subject=subject["value"])
        out[key] = entry
    return {"status": "compared" if any(e["status"] == "compared" for e in out.values()) else "insufficient_peers",
            "peer_group": f"{chosen_level}: {chosen_label} ({PROVIDER_LABEL.get(provider, provider)} classification)", "peer_group_level": chosen_level, "peers_considered": peers_n,
            "level_note": note,
            "note": "Peer multiples come from stored provider data for tracked instruments, through the same source chain as the instrument's own; they are not index-wide medians.",
            "metrics": out}


def build_valuation(db: Session, instrument: Instrument, sections: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    from app.services import valuation_scoring

    now = now or datetime.now(timezone.utc)
    stored = valuation_inputs.load(db, instrument.id)
    result = compute_valuation(currency=instrument.currency, technicals=sections["technicals"], fundamentals=sections["fundamentals"], now=now,
                               provider_metrics=stored)
    values = ((sections.get("fundamentals") or {}).get("snapshot") or {}).get("values") or {}
    pick = lambda name: ((values.get(name) or {}).get("value") if isinstance(values.get(name), dict) else None)  # noqa: E731
    sector, industry, _provider = classification(db, instrument.id)
    sector, industry = sector or pick("sector"), industry or pick("industry")
    try:
        result["peer_comparison"] = peer_comparison(db, instrument, result, now, sector=sector, industry=industry)
    except Exception as exc:  # noqa: BLE001 - a peer-lookup failure must not remove the instrument's own metrics
        result["peer_comparison"] = {"status": "unavailable", "reason": f"peer lookup failed ({type(exc).__name__})", "metrics": {}}
    result["sector"], result["industry"] = sector, industry
    # The scoring result lives with the evidence so Research scoring and the AI Analyst read exactly the same thing.
    result["scoring"] = valuation_scoring.score_valuation(result, sector=sector, industry=industry)
    return result


def normalized_evidence(valuation: dict[str, Any] | None) -> dict[str, Any] | None:
    """The normalized valuation evidence handed to the AI Analyst: the very metrics and scoring result production uses."""
    if not valuation or valuation.get("error"):
        return None
    metrics = {}
    for key, m in (valuation.get("metrics") or {}).items():
        metrics[key] = {k: m.get(k) for k in ("label", "status", "value", "unit", "origin", "provider", "reporting_period", "retrieved_at", "freshness", "statement_basis", "reason", "warnings")}
        if m.get("cross_check"):
            metrics[key]["cross_check"] = m["cross_check"]
    gaps = [f"{m['label']}: {m['reason']}" for m in (valuation.get("metrics") or {}).values() if m["status"] in {"unavailable", "not_meaningful"} and m.get("reason")]
    disagreements = [f"{m['label']}: displayed {m['value']} ({m['origin']}) vs {m['cross_check']['origin']} {m['cross_check']['value']} ({m['cross_check']['difference_pct']}% apart)"
                     for m in (valuation.get("metrics") or {}).values() if (m.get("cross_check") or {}).get("disagrees")]
    return {"evidence_version": valuation.get("model_version"), "currency": valuation.get("currency"), "as_of": valuation.get("as_of"), "price": valuation.get("price"),
            "sector": valuation.get("sector"), "industry": valuation.get("industry"), "metrics": metrics, "peer_comparison": valuation.get("peer_comparison"),
            "scoring": valuation.get("scoring"), "unavailable_or_unreliable": gaps, "provider_vs_calculated_disagreements": disagreements}
