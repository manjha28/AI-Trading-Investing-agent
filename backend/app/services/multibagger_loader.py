"""Builds the multibagger engine's inputs from stored data and runs it for the owner's research universe (Phase 6). Data source rule: Dhan first (company info, prices, holdings), Yahoo only for the
statements Dhan does not provide; every value used is stored, nothing is fetched here. Funds are not stocks (instrument_kind) and are skipped. Results go to ``multibagger_assessments`` nightly in shadow
mode; ``multibagger_screen_enabled`` only decides whether they are shown or used."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Any

import pandas as pd
from sqlalchemy import select

from app.models import CorporateAction, Instrument, InstrumentFundamental, MarketBar, ValuationProviderInput
from app.models.listings import InstrumentListing
from app.models.multibagger import MultibaggerAssessment
from app.models.fundamental_snapshots import ShareholdingSnapshot
from app.services import gates_v2, multibagger_engine as mb

logger = logging.getLogger(__name__)
CRORE = 1e7


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError):
        return None


def _row(r: InstrumentFundamental) -> dict[str, Any]:
    row = _row_raw(r)
    if row["ebit"] is None and row["pbt"] is not None and row["interest"] is not None:
        row["ebit"] = row["pbt"] + row["interest"]                 # EBIT = PBT + interest (the same definition the engine uses for annual rows)
    return row


def _row_raw(r: InstrumentFundamental) -> dict[str, Any]:
    ex = r.extra_metrics or {}
    return {"period_end": r.period_end, "revenue": _num(r.revenue), "pat": _num(r.net_income), "eps": _num(r.eps), "op_margin": _num(r.operating_margin), "assets": _num(r.total_assets),
            "equity": _num(r.total_equity), "debt": _num(r.total_debt), "cfo": _num(r.operating_cash_flow), "shares": _num(r.shares_outstanding), "interest": _num(ex.get("interest_expense")),
            "pbt": _num(ex.get("profit_before_tax")), "ebit": _num(ex.get("ebit")), "cash": _num(ex.get("cash")), "fixed_assets": _num(ex.get("fixed_assets")),
            "op_profit": _num(ex.get("operating_profit"))}


def build_inputs(db: Any, user_id: Any, inst: Instrument, now: datetime, daily: pd.DataFrame | None) -> dict[str, Any]:
    rows = list(db.scalars(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == inst.id).order_by(InstrumentFundamental.period_end)))
    val: dict[str, Any] = {}
    for m in db.scalars(select(ValuationProviderInput).where(ValuationProviderInput.instrument_id == inst.id)):
        key = m.metric
        if m.provider == "dhan" or key not in val:                      # Dhan first; Yahoo fills only what Dhan lacks
            val[key] = m.text_value if m.metric in ("sector", "industry") else (float(m.value) if m.value is not None else None)
    snaps = list(db.scalars(select(ShareholdingSnapshot).where(ShareholdingSnapshot.instrument_id == inst.id).order_by(ShareholdingSnapshot.captured_on)))
    last = snaps[-1] if snaps else None
    splits = sorted((a.ex_date, float(a.ratio)) for a in db.scalars(select(CorporateAction).where(CorporateAction.user_id == user_id, CorporateAction.instrument_id == inst.id, CorporateAction.action_type == "split")) if a.ratio)
    first_bar = db.scalar(select(MarketBar.timestamp).where(MarketBar.instrument_id == inst.id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False)).order_by(MarketBar.timestamp).limit(1))
    lst = db.scalar(select(InstrumentListing).where(InstrumentListing.instrument_id == inst.id))
    med, _n = (None, 0) if daily is None or len(daily) == 0 else gates_v2.median_traded_value_crore(daily)
    price = None if daily is None or len(daily) == 0 else float(daily["close"].iloc[-1])
    mcap = val.get("market_cap")
    pe = val.get("pe")
    equity = next((r.total_equity for r in reversed(rows) if r.period_type == "annual" and r.total_equity), None)
    pat = next((r.net_income for r in reversed(rows) if r.period_type == "annual" and r.net_income is not None), None)
    return {"symbol": inst.symbol, "as_of": now.date(), "sector": val.get("sector"), "industry": val.get("industry"),
            "annual": [_row(r) for r in rows if r.period_type == "annual"], "quarterly": [_row(r) for r in rows if r.period_type == "quarterly"],
            "price": price, "median_traded_value_cr": med, "first_bar_date": None if first_bar is None else first_bar.date(), "splits": [(d, r) for d, r in splits],
            "market_cap_cr": None if mcap is None else mcap / CRORE, "pe": pe, "pb": val.get("pb"), "pe_to_sector_pe": val.get("pe_to_sector_pe"), "dhan_roce": val.get("roce"), "dhan_de": val.get("debt_to_equity"),
            "roe_pct": None if not equity or pat is None else float(pat) / float(equity) * 100,
            "promoter_pct": None if last is None or last.promoter_pct is None else float(last.promoter_pct),
            "promoter_history": [float(s.promoter_pct) for s in snaps if s.promoter_pct is not None],
            "pledged_pct": None if last is None or last.pledged_pct_of_promoter is None else float(last.pledged_pct_of_promoter),
            "pledge_history": [float(s.pledged_pct_of_promoter) for s in snaps if s.pledged_pct_of_promoter is not None],
            "listing": None if lst is None else {"issue_price": None if lst.issue_price is None else float(lst.issue_price), "allotment_date": lst.allotment_date, "listing_date": lst.listing_date, "pre_ipo_lockin_end": lst.pre_ipo_lockin_end},
            "governance_events_24m": None,                                  # NSE adapter disabled: unknown, not "none"
            "pe_median_5y": pe_median_3y(daily, [_row(r) for r in rows if r.period_type == "annual"], now.date()),   # the 3-year fallback (5 years of statements are not stored)
            "industry_revenue_cagr_pct": None, "industry_peers": 0}


def pe_median_3y(daily: pd.DataFrame | None, annual: list[dict[str, Any]], now: date) -> float | None:
    """Median month-end P/E over the last 36 months from stored closes and the annual EPS KNOWN at that month end (annual results usable 75 days after the fiscal year end, the Phase 6 lag rule). The owner's
    5-year version needs 5 years of statements, which are not stored, so this is the 3-year fallback; fewer than 24 usable months = unknown."""
    if daily is None or len(daily) == 0 or not annual:
        return None
    from datetime import timedelta
    month_ends = pd.date_range(end=pd.Timestamp(now), periods=36, freq="ME")
    pes = []
    for me in month_ends:
        px = daily["close"].loc[:me]
        if len(px) == 0 or (me - px.index[-1]).days > 7:
            continue
        known = [r for r in annual if r.get("eps") and r["period_end"] + timedelta(days=75) <= me.date()]
        if not known or known[-1]["eps"] <= 0:
            continue
        pes.append(float(px.iloc[-1]) / known[-1]["eps"])
    return median(pes) if len(pes) >= 24 else None


def weekly_frames(db: Any, daily: pd.DataFrame | None, bench: pd.Series | None) -> tuple[pd.DataFrame | None, pd.Series | None]:
    from app.services import technical_engine as te
    if daily is None or len(daily) < 250:
        return None, None
    w = te.resample(daily, "W", None)
    bw = None if bench is None or len(bench) == 0 else bench.resample("W-FRI").last().dropna()
    return w, bw


def run_universe(db: Any, user_id: Any, *, now: datetime | None = None, store: bool = True, limit: int | None = None) -> list[dict[str, Any]]:
    """Assess the owner's research universe (stocks only). Two passes: gather inputs, derive each industry's median revenue CAGR (needs >= 5 peers), then assess and store."""
    from app.research_daily import user_universe
    from app.services import research_pipeline as rp
    from app.config import MultibaggerConfig, settings
    cfg = MultibaggerConfig(gsec_10y_pct=settings.multibagger_gsec_10y_pct)
    now = now or datetime.now(timezone.utc)
    bench = rp._benchmark_series(db)
    inputs, frames = {}, {}
    for inst in user_universe(db, user_id)[:limit]:
        daily = rp._daily_frame(db, inst.id, None)
        inputs[inst.id] = (inst, build_inputs(db, user_id, inst, now, daily))
        frames[inst.id] = daily
    by_ind: dict[str, list[float]] = defaultdict(list)
    for _, x in inputs.values():
        ann = x["annual"]
        rev = [r["revenue"] for r in ann]
        c = mb.cagr(rev[-4], rev[-1], 3) if len(ann) >= 4 else None
        x["_own_rev_cagr"] = c
        if c is not None and x.get("industry"):
            by_ind[x["industry"]].append(c * 100)
    results = []
    for iid, (inst, x) in inputs.items():
        peers = by_ind.get(x.get("industry") or "", [])
        if len(peers) >= 5:
            x["industry_revenue_cagr_pct"], x["industry_peers"] = median(peers), len(peers)
        w, bw = weekly_frames(db, frames[iid], bench)
        res = mb.assess(x, cfg, w, bw, daily=frames[iid], bench_daily=bench)
        results.append(res)
        if store:
            _store(db, user_id, inst, res, now)
    if store:
        db.commit()
    return results


def _store(db: Any, user_id: Any, inst: Instrument, res: dict[str, Any], now: datetime) -> None:
    day = now.date()
    row = db.scalar(select(MultibaggerAssessment).where(MultibaggerAssessment.instrument_id == inst.id, MultibaggerAssessment.user_id == user_id, MultibaggerAssessment.as_of == day))
    fields = dict(computed_at=now, klass=res["class"], q=res["scores"].get("Q"), g=res["scores"].get("G"), v=res["scores"].get("V"), t=res["scores"].get("T"), x=res["scores"].get("X"), rank=res["rank"],
                  checks=_jsonable(res), model_version=res["version"])
    if row is None:
        db.add(MultibaggerAssessment(instrument_id=inst.id, user_id=user_id, as_of=day, **fields))
    else:
        for k, v in fields.items():
            setattr(row, k, v)


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return o


def log_nightly(db: Any, user_id: Any) -> None:
    """One log line per night; never raises. Runs in shadow mode whatever the flag says."""
    try:
        res = run_universe(db, user_id)
        counts: dict[str, int] = defaultdict(int)
        for r in res:
            counts[r["class"]] += 1
        logger.info("multibagger_nightly %s", dict(counts))
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("multibagger_nightly_failed error_class=%s", type(exc).__name__)
