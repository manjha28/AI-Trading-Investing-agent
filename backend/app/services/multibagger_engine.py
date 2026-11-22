"""Nexus v2 Phase 6: the multibagger engine. A checklist with vetoes, not an averaged score. Pure functions: no database access here (``multibagger_loader`` builds the inputs).

Output per stock: one class (multibagger_candidate, quality_compounder, watch, avoid, not_assessable), sub-scores Q G V T X, a checklist count and a timing tag. Fundamental classes and vetoes apply to
LONG-TERM only; swing/positional only get an optional Leader tag. Every check returns pass | fail | unknown with a reason. Missing factors are DROPPED from the weight, never scored zero, and a score is
computed only when at least 70% of its weight is backed by data. Where the stored data is too shallow (Yahoo gives 3-4 annual and about 6 quarterly periods) the 5-year and 8-quarter checks are
UNKNOWN, never failed: the owner's decision. Hypothetical screen, decision support, not investment advice.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

import pandas as pd

from app.config import EmergingConfig, MultibaggerConfig, NewListingConfig
from app.services.research_scoring import _lerp, is_financial_sector
from app.services.valuation_scoring import sector_family

VERSION = "multibagger-0.1.0-provisional"
CLASSES = ("multibagger_candidate", "quality_compounder", "emerging", "new_listing", "watch", "avoid", "not_assessable")
CYCLICAL = ("metal", "steel", "cement", "oil", "gas", "sugar", "shipping", "mining", "chemical", "commodity")
UTILITY = ("utilit", "power", "electric")
ASSET_LIGHT = ("technology", "software", "consumer", "healthcare", "pharma", "it ")
REAL_ESTATE_EPC = ("real estate", "construction", "engineering", "infrastructure")
HOLDING = ("holding", "investment compan")
INSURANCE_AMC = ("insurance", "asset management")


def chk(cid: str, stage: str, result: str, reason: str, value: Any = None) -> dict[str, Any]:
    return {"id": cid, "stage": stage, "result": result, "reason": reason, "value": value}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def kind_of(sector: str | None, industry: str | None) -> str:
    text = f"{sector or ''} {industry or ''}".casefold()
    if any(w in text for w in HOLDING):
        return "holding"
    if is_financial_sector(sector, industry):
        return "insurance_amc" if any(w in text for w in INSURANCE_AMC) else "financial"
    if any(w in text for w in UTILITY):
        return "utility"
    if any(w in text for w in CYCLICAL):
        return "cyclical"
    if any(w in text for w in REAL_ESTATE_EPC):
        return "real_estate_epc"
    if any(w in text for w in ASSET_LIGHT) or sector_family(sector, industry) == "growth_asset_light":
        return "asset_light"
    return "default"


# ───────────────────────── helpers over the stored annual / quarterly rows ─────────────────────────
def cagr(first: float | None, last: float | None, years: float) -> float | None:
    """Both ends must be positive (owner rule)."""
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def _series(rows: list[dict[str, Any]], key: str) -> list[float | None]:
    return [_f(r.get(key)) for r in rows]


def _window(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    return rows[-n:] if len(rows) >= n else []


def adjusted_shares(rows: list[dict[str, Any]], splits: list[tuple[date, float]]) -> list[float | None]:
    """Share count per annual row, restated to today's share basis: a split/bonus after the period end multiplies the older count."""
    out: list[float | None] = []
    for r in rows:
        sh = _f(r.get("shares"))
        if sh is None:
            out.append(None)
            continue
        for ex, ratio in splits:
            if ex > r["period_end"] and ratio and ratio > 0:
                sh *= ratio
        out.append(sh)
    return out


def ebit_of(r: dict[str, Any]) -> tuple[float | None, str]:
    """EBIT = PBT + interest; fallback operating margin x revenue (labelled approximate); None if neither."""
    pbt, intr = _f(r.get("pbt")), _f(r.get("interest"))
    if pbt is not None and intr is not None:
        return pbt + intr, "PBT + interest"
    ebit = _f(r.get("ebit"))
    if ebit is not None:
        return ebit, "reported EBIT"
    om, rev = _f(r.get("op_margin")), _f(r.get("revenue"))
    if om is not None and rev is not None:
        return om / 100 * rev, "approx: operating margin x revenue"
    return None, "no EBIT inputs"


def roce_series(rows: list[dict[str, Any]]) -> list[tuple[float | None, str]]:
    out = []
    for r in rows:
        ebit, basis = ebit_of(r)
        eq, debt, cash = _f(r.get("equity")), _f(r.get("debt")), _f(r.get("cash")) or 0.0
        if ebit is None or eq is None or debt is None:
            out.append((None, basis))
            continue
        cap = eq + debt - cash
        out.append((None if cap <= 0 else ebit / cap * 100, basis))
    return out


def weighted(parts: list[tuple[str, float, float | None]], min_share: float) -> dict[str, Any]:
    """parts = (name, weight, score or None). Missing factors are dropped from the weight; the score exists only if >= min_share of the weight is backed."""
    total = sum(w for _, w, _ in parts)
    have = [(n, w, s) for n, w, s in parts if s is not None]
    share = sum(w for _, w, _ in have) / total if total else 0.0
    score = sum(w * s for _, w, s in have) / sum(w for _, w, _ in have) if have and share >= min_share else None
    return {"score": None if score is None else round(max(0.0, min(100.0, score)), 1), "data_share": round(share, 3), "factors": {n: (None if s is None else round(s, 1)) for n, _, s in parts}}


# ───────────────────────── Stage 1: gates and long-term vetoes ─────────────────────────
def stage1(x: dict[str, Any], cfg: MultibaggerConfig, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ann, qtr, today = x.get("annual") or [], x.get("quarterly") or [], x["as_of"]
    fin = kind in ("financial", "insurance_amc")
    liq = _f(x.get("median_traded_value_cr"))
    out.append(chk("liquidity", "gate", "unknown" if liq is None else "pass" if liq >= cfg.liquidity_floor_cr else "fail",
                   "20-session median traded value unknown" if liq is None else f"Rs {liq:.1f} crore vs the long-term floor of Rs {cfg.liquidity_floor_cr:g} crore", liq))
    px = _f(x.get("price"))
    out.append(chk("price", "gate", "unknown" if px is None else "pass" if px >= cfg.min_price else "fail", "price unknown" if px is None else f"price {px:.2f} vs minimum {cfg.min_price:g}", px))
    out.append(chk("surveillance", "gate", "unknown", "ASM/GSM/T2T list not stored (the NSE adapter is disabled); flag only", None))
    # data current
    if not ann:
        out.append(chk("data_current", "veto", "unknown", "no annual statements stored", None))
    else:
        age_a = (today - ann[-1]["period_end"]).days / 30.4
        age_q = None if not qtr else (today - qtr[-1]["period_end"]).days / 30.4
        ok = age_a <= cfg.data_current_annual_months and (age_q is None or age_q <= cfg.data_current_quarter_months)
        out.append(chk("data_current", "veto", "pass" if ok else "fail", f"latest annual {age_a:.0f} months old (limit {cfg.data_current_annual_months})" + ("" if age_q is None else f", latest quarter {age_q:.0f} months old (limit {cfg.data_current_quarter_months})"), round(age_a, 1)))
    # listing age
    first_bar = x.get("first_bar_date")
    if first_bar is None:
        out.append(chk("listing_age", "cap", "unknown", "first stored candle unknown", None))
    else:
        yrs = (today - first_bar).days / 365.25
        out.append(chk("listing_age", "cap", "pass" if yrs >= cfg.min_listing_fiscal_years else "fail", f"first stored candle {first_bar} ({yrs:.1f} years of history; need {cfg.min_listing_fiscal_years})" + ("" if yrs >= cfg.min_listing_fiscal_years else ": too new"), round(yrs, 1)))
    # pledge
    pl, pl_hist = _f(x.get("pledged_pct")), x.get("pledge_history") or []
    if pl is None:
        out.append(chk("pledge", "veto", "unknown", "pledge of promoter holding not stored (Dhan gives none; the NSE adapter is disabled): verify pledge", None))
    else:
        rising = len(pl_hist) >= 4 and pl > pl_hist[-4]
        out.append(chk("pledge", "veto", "pass" if pl <= cfg.pledge_max_pct and not rising else "fail", f"pledge {pl:.1f}% of promoter holding" + (", rising over 4 quarters" if rising else ""), pl))
    # share count CAGR
    sh = adjusted_shares(ann, x.get("splits") or [])
    if len(ann) >= 4 and sh[-1] is not None and sh[-4] is not None and sh[-4] > 0:
        g = (sh[-1] / sh[-4]) ** (1 / 3) - 1
        out.append(chk("share_count_cagr", "veto", "pass" if g <= cfg.share_cagr_3y_max else "fail", f"split/bonus-adjusted share count CAGR 3y {g:.1%} (max {cfg.share_cagr_3y_max:.0%})", round(g, 4)))
    else:
        out.append(chk("share_count_cagr", "veto", "unknown", "needs 4 annual share counts", None))
    w3 = _window(ann, 3)
    if fin:
        out.append(chk("cash_conversion_veto", "veto", "pass", "not applied to financials", None))
        out.append(chk("leverage_veto", "veto", "pass", "D/E and interest coverage are not applied to financials", None))
    else:
        cfo, pat = _series(w3, "cfo"), _series(w3, "pat")
        if len(w3) == 3 and None not in cfo and None not in pat:
            s_cfo, s_pat = sum(cfo), sum(pat)                                                                                  # type: ignore[arg-type]
            ratio = None if s_pat <= 0 else s_cfo / s_pat
            ok = s_cfo > 0 and (ratio is not None and ratio >= cfg.cfo_pat_min)
            out.append(chk("cash_conversion_veto", "veto", "pass" if ok else "fail", f"3y CFO {s_cfo:,.0f}, 3y PAT {s_pat:,.0f}" + ("" if ratio is None else f", CFO/PAT {ratio:.2f} (min {cfg.cfo_pat_min})"), None if ratio is None else round(ratio, 2)))
        else:
            out.append(chk("cash_conversion_veto", "veto", "unknown", "needs 3 annual CFO and PAT values", None))
        last = ann[-1] if ann else None
        de = None if last is None or not _f(last.get("equity")) or _f(last.get("debt")) is None else _f(last["debt"]) / _f(last["equity"])        # type: ignore[operator]
        ebit = None if last is None else ebit_of(last)[0]
        intr = None if last is None else _f(last.get("interest"))
        if de is None:
            out.append(chk("leverage_veto", "veto", "unknown", "debt or equity missing", None))
        else:
            cov_ok = intr is None or intr <= 0 or (ebit is not None and ebit / intr >= cfg.coverage_min)
            cov_known = intr is not None and ebit is not None or (intr is not None and intr <= 0)
            res = "fail" if de > cfg.de_max or (cov_known and not cov_ok) else ("pass" if cov_known else "unknown")
            out.append(chk("leverage_veto", "veto", res, f"D/E {de:.2f} (max {cfg.de_max:g})" + ("; interest coverage unknown (interest expense not stored)" if not cov_known else (f", coverage {ebit / intr:.1f}x" if intr and intr > 0 and ebit is not None else ", no interest cost")), round(de, 2)))
    eq = _f(ann[-1].get("equity")) if ann else None
    out.append(chk("equity_positive", "veto", "unknown" if eq is None else "pass" if eq > 0 else "fail", "equity unknown" if eq is None else f"equity {eq:,.0f}", eq))
    gov = x.get("governance_events_24m")
    out.append(chk("auditor_or_regulatory", "flag", "unknown" if gov is None else "pass" if not gov else "fail", "no governance feed stored (the NSE adapter is disabled); flag only" if gov is None else f"{len(gov)} event(s) in 24 months", None if gov is None else len(gov)))
    pats = _series(_window(ann, 3), "pat")
    if len(pats) == 3 and None not in pats:
        losses = sum(1 for p in pats if p < 0)                                                                           # type: ignore[operator]
        out.append(chk("net_losses", "cap", "pass" if losses <= cfg.max_losses_in_3y else "fail", f"{losses} loss year(s) in the last 3", losses))
    else:
        out.append(chk("net_losses", "cap", "unknown", "needs 3 annual net income values", None))
    return out


# ───────────────────────── Stage 2: Quality Q ─────────────────────────
def stage_q(x: dict[str, Any], cfg: MultibaggerConfig, kind: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ann, qtr = x.get("annual") or [], x.get("quarterly") or []
    checks: list[dict[str, Any]] = []
    shift = {"utility": -5.0, "asset_light": 5.0}.get(kind, 0.0)
    anchors = tuple((a + shift, b) for a, b in cfg.roce_anchors)
    parts: list[tuple[str, float, float | None]] = []
    if kind in ("financial", "insurance_amc"):
        roas = [(_f(r.get("pat")) / _f(r.get("assets")) * 100) for r in ann[-3:] if _f(r.get("pat")) is not None and _f(r.get("assets"))]
        roes = [(_f(r.get("pat")) / _f(r.get("equity")) * 100) for r in ann[-3:] if _f(r.get("pat")) is not None and _f(r.get("equity"))]
        if kind == "financial":
            parts.append(("roa", 0.35, _lerp(sorted(roas)[len(roas) // 2], list(cfg.roa_anchors)) if roas else None))
        parts.append(("roe", 0.35 if kind == "financial" else 0.6, _lerp(sorted(roes)[len(roes) // 2], list(cfg.roe_anchors)) if roes else None))
        prom = _f(x.get("promoter_pct"))
        parts.append(("promoter_holding", 0.05, None if prom is None else _lerp(prom, list(cfg.promoter_anchors))))
        q = weighted(parts, cfg.min_data_share)
        checks.append(chk("q_financial", "Q", "pass" if (q["score"] or 0) >= cfg.qc_q else "unknown" if q["score"] is None else "fail", f"financials: ROA/ROE based Q = {q['score']}", q["score"]))
        return q, checks
    window = ann[-5:] if (kind in ("cyclical",) and len(ann) >= 4) else ann[-3:]
    ro = [v for v, _ in roce_series(window) if v is not None]
    basis = next((b for v, b in roce_series(window) if v is not None), "no EBIT inputs")
    dhan_roce = _f(x.get("dhan_roce"))
    if len(ro) >= 2:
        med = sorted(ro)[len(ro) // 2] if len(ro) % 2 else sum(sorted(ro)[len(ro) // 2 - 1:len(ro) // 2 + 1]) / 2
        parts.append(("roce_level", 0.35, _lerp(med, list(anchors))))
        mn = min(ro)
        stab = _lerp(mn, list(cfg.roce_min_anchors)) - (15.0 if len(ro) >= 3 and ro[0] - ro[-1] > 5 else 0.0)
        parts.append(("roce_stability", 0.15, max(0.0, stab)))
        checks.append(chk("roce", "Q", "pass" if med >= 15 else "fail", f"median ROCE {med:.1f}% over {len(ro)} years ({basis})", round(med, 1)))
    elif dhan_roce is not None:
        parts.append(("roce_level", 0.35, _lerp(dhan_roce, list(anchors))))
        parts.append(("roce_stability", 0.15, None))
        checks.append(chk("roce", "Q", "pass" if dhan_roce >= 15 else "fail", f"current ROCE {dhan_roce:.1f}% (Dhan, current only; history needs EBIT inputs)", dhan_roce))
    else:
        parts += [("roce_level", 0.35, None), ("roce_stability", 0.15, None)]
        checks.append(chk("roce", "Q", "unknown", "no EBIT or capital inputs stored", None))
    w3 = _window(ann, 3)
    cfo, pat = _series(w3, "cfo"), _series(w3, "pat")
    cc = None
    if len(w3) == 3 and None not in cfo and None not in pat and sum(pat) > 0:                                       # type: ignore[arg-type]
        cc = sum(cfo) / sum(pat)                                                                                    # type: ignore[arg-type]
    parts.append(("cash_conversion", 0.20, None if cc is None else _lerp(cc, list(cfg.cash_conv_anchors))))
    checks.append(chk("cash_conversion", "Q", "unknown" if cc is None else "pass" if cc >= 0.8 else "fail", "needs 3 years of CFO and positive PAT" if cc is None else f"3y CFO/PAT {cc:.2f}", None if cc is None else round(cc, 2)))
    last = ann[-1] if ann else {}
    de = None if not _f(last.get("equity")) or _f(last.get("debt")) is None else _f(last["debt"]) / _f(last["equity"])        # type: ignore[operator]
    if de is None and _f(x.get("dhan_de")) is not None:
        de = _f(x.get("dhan_de"))
    parts.append(("debt_to_equity", 0.15, None if de is None else _lerp(de, list(cfg.de_anchors))))
    ebit, intr = (ebit_of(last)[0] if last else None), _f(last.get("interest")) if last else None
    if intr is not None and intr <= 0:
        cov_score = 100.0
    elif intr is not None and ebit is not None:
        cov_score = _lerp(ebit / intr, list(cfg.coverage_anchors))
    else:
        cov_score = None
    parts.append(("interest_coverage", 0.10, cov_score))
    prom, prom_hist = _f(x.get("promoter_pct")), x.get("promoter_history") or []
    ps = None if prom is None else _lerp(prom, list(cfg.promoter_anchors))
    if ps is not None and len(prom_hist) >= 4:
        delta = prom - prom_hist[-4]
        ps = min(100.0, ps + 10.0) if delta >= 1 else max(0.0, ps - 20.0) if delta <= -3 else ps
    parts.append(("promoter_holding", 0.05, ps))
    return weighted(parts, cfg.min_data_share), checks


# ───────────────────────── Stage 3: Growth G ─────────────────────────
def stage_g(x: dict[str, Any], cfg: MultibaggerConfig, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ann = x.get("annual") or []
    rev_key = "income" if kind in ("financial", "insurance_amc") else "revenue"
    parts: list[tuple[str, float, float | None]] = []
    facts: dict[str, Any] = {}
    revs, pats = _series(ann, rev_key if ann and ann[-1].get(rev_key) is not None else "revenue"), _series(ann, "pat")
    span = 3 if len(ann) >= 4 else 2 if len(ann) == 3 else 0                       # 3 annual periods (a new listing's restated pre-IPO years) give a 2-year CAGR, labelled
    rc = cagr(revs[-span - 1], revs[-1], span) if span else None
    pc = cagr(pats[-span - 1], pats[-1], span) if span else None
    facts.update({"revenue_cagr_3y": rc, "pat_cagr_3y": pc, "cagr_years": span or None, "eps_cagr_3y": cagr(_series(ann, "eps")[-span - 1], _series(ann, "eps")[-1], span) if span else None})
    parts.append(("revenue_cagr", 0.25, None if rc is None else _lerp(rc * 100, list(cfg.rev_cagr_anchors))))
    parts.append(("pat_cagr", 0.25, None if pc is None else _lerp(min(pc, 0.60) * 100, list(cfg.pat_cagr_anchors))))
    ok_years, n_years = 0, 0
    for i in range(1, len(ann)):
        r0, r1, p0, p1 = revs[i - 1], revs[i], pats[i - 1], pats[i]
        if None in (r0, r1, p0, p1) or r0 <= 0:
            continue
        n_years += 1
        ok_years += int(r1 / r0 - 1 > 0.10 and p1 > p0)                                                                # type: ignore[operator]
    facts["consistency"] = None if n_years < 2 else ok_years / n_years
    parts.append(("consistency", 0.20, None if n_years < 2 else _lerp(ok_years / n_years, list(cfg.consistency_anchors))))
    om = _series(ann, "op_margin")
    parts.append(("margin_change", 0.10, _lerp(om[-1] - om[-4], list(cfg.margin_change_anchors)) if len(om) >= 4 and om[-1] is not None and om[-4] is not None else None))        # type: ignore[operator]
    mcap = _f(x.get("market_cap_cr"))
    runway = None if mcap is None else 0.0 if kind == "utility" else _lerp(mcap, list(cfg.runway_anchors_cr))
    facts["market_cap_cr"] = mcap
    parts.append(("market_cap_runway", 0.10, runway))
    tw = _f(x.get("industry_revenue_cagr_pct"))
    parts.append(("sector_tailwind", 0.10, None if tw is None or (x.get("industry_peers") or 0) < 5 else _lerp(tw, list(cfg.tailwind_anchors))))
    return weighted(parts, cfg.min_data_share), facts


# ───────────────────────── Stage 4: Valuation V ─────────────────────────
def stage_v(x: dict[str, Any], cfg: MultibaggerConfig, kind: str, facts: dict[str, Any]) -> dict[str, Any]:
    pe = _f(x.get("pe"))
    parts: list[tuple[str, float, float | None]] = []
    eps_cagr = facts.get("eps_cagr_3y") if facts.get("eps_cagr_3y") is not None else facts.get("pat_cagr_3y")
    if kind in ("financial", "insurance_amc"):
        pb, roe = _f(x.get("pb")), _f(x.get("roe_pct"))
        just = None if roe is None else roe / 13.0
        parts.append(("pb_vs_justified", 0.60, None if pb is None or not just else _lerp(pb / just, [(0.6, 100.0), (1.0, 60.0), (1.5, 20.0), (2.0, 0.0)])))
    else:
        peg = None
        if pe is not None and pe > 0 and eps_cagr is not None and eps_cagr > 0.05:
            peg = pe / (100 * min(eps_cagr, 0.30))
        om = [v for v in _series(x.get("annual") or [], "op_margin") if v is not None]
        peak_margin = kind == "cyclical" and pe is not None and pe < 8 and bool(om) and om[-1] >= max(om)
        parts.append(("peg", 0.40, 0.0 if peak_margin else None if peg is None else _lerp(peg, list(cfg.peg_anchors))))      # a cyclical on a low PE at a margin peak: the PEG scores 0
    med_pe = _f(x.get("pe_median_5y"))
    parts.append(("pe_vs_own_median", 0.25, None if pe is None or med_pe is None or med_pe <= 0 else _lerp(pe / med_pe, list(cfg.pe_rel_anchors))))
    peer = _f(x.get("peer_valuation_score"))
    pts = _f(x.get("pe_to_sector_pe"))
    parts.append(("peer_valuation", 0.20, peer if peer is not None else None if pts is None else _lerp(pts, list(cfg.pe_to_sector_anchors))))
    ey = None if pe is None or pe <= 0 or cfg.gsec_10y_pct is None else 100.0 / pe - cfg.gsec_10y_pct
    parts.append(("earnings_yield_spread", 0.15, None if ey is None else _lerp(ey, list(cfg.ey_spread_anchors))))
    v = weighted(parts, cfg.min_data_share)
    if v["score"] is not None and pe is not None and pe > cfg.pe_cap_at:
        v["score"] = min(v["score"], cfg.pe_cap_v)
        v["capped_by"] = f"PE {pe:.0f} above {cfg.pe_cap_at:g}"
    return v


# ───────────────────────── Stage 5: turning point T ─────────────────────────
def stage_t(x: dict[str, Any], cfg: MultibaggerConfig) -> dict[str, Any]:
    q, ann = x.get("quarterly") or [], x.get("annual") or []
    sig: dict[str, tuple[int, bool | None]] = {}
    revs = _series(q, "revenue")
    if len(q) >= 10 and None not in revs[-10:]:
        yoy = [revs[i] / revs[i - 4] - 1 for i in range(len(q) - 6, len(q)) if revs[i - 4]]                         # type: ignore[operator]
        sig["revenue_acceleration"] = (25, len(yoy) == 6 and (sum(yoy[-2:]) / 2 - sum(yoy[:4]) / 4) >= 0.05)
    else:
        sig["revenue_acceleration"] = (25, None)                                                                       # needs 10 quarters
    om = _series(q, "op_margin")
    sig["margin_expansion"] = (25, None if len(q) < 6 or None in (om[-1], om[-5], om[-2], om[-6]) else (om[-1] - om[-5] >= 1.5 and om[-2] - om[-6] >= 1.5))                 # type: ignore[operator]
    pat = _series(q, "pat")
    sig["pat_yoy"] = (15, None if len(q) < 6 or None in (pat[-1], pat[-5], pat[-2], pat[-6]) or pat[-5] <= 0 or pat[-6] <= 0 else (pat[-1] / pat[-5] - 1 > 0.25 and pat[-2] / pat[-6] - 1 > 0.25))   # type: ignore[operator]
    fa = _series(ann, "fixed_assets")
    rv = _series(ann, "revenue")
    if len(ann) >= 3 and fa[-1] is not None and fa[-3] is not None and fa[-3] > 0 and None not in rv[-3:] and rv[-2] and rv[-3]:
        sig["capex"] = (15, fa[-1] / fa[-3] - 1 >= 0.30 and (rv[-1] / rv[-2] - 1) > (rv[-2] / rv[-3] - 1))           # type: ignore[operator]
    else:
        sig["capex"] = (15, None)
    debt, cash = _series(ann, "debt"), _series(ann, "cash")
    if len(ann) >= 3 and debt[-1] is not None and debt[-3] is not None and debt[-3] > 0:
        sig["deleveraging"] = (10, debt[-1] / debt[-3] - 1 <= -0.30 or (cash[-1] is not None and cash[-1] >= debt[-1]))
    else:
        sig["deleveraging"] = (10, None)
    sig["institutions"] = (10, None)                                                                                   # needs 4 quarters of dated FII+DII history: stored from 2026-11-22 onward
    known = {k: v for k, v in sig.items() if v[1] is not None}
    n_known = len(known)
    if n_known < 3:
        return {"score": None, "signals": {k: v[1] for k, v in sig.items()}, "n_signals": n_known, "from_text": f"T from {n_known} of 6 signals",
                "reason": f"unknown: only {n_known} of 6 signals can be evaluated with {len(q)} stored quarters and {len(ann)} annual periods (needs 3; the YoY signals need 6 quarters, revenue acceleration 10)"}
    hits = [k for k, v in known.items() if v[1]]
    total_w = sum(sig[k][0] for k in known)                                           # weights renormalised over the signals that can be computed
    score = 0.0 if len(hits) < 2 else min(100.0, 100.0 * sum(sig[k][0] for k in hits) / total_w)
    return {"score": round(score, 1), "signals": {k: v[1] for k, v in sig.items()}, "n_signals": n_known, "from_text": f"T from {n_known} of 6 signals",
            "reason": f"T from {n_known} of 6 signals: {len(hits)} turning-point signal(s): {', '.join(hits) or 'none'}; not computable: {', '.join(k for k, v in sig.items() if v[1] is None) or 'none'}"}


# ───────────────────────── Stage 6: chart stage X ─────────────────────────
def stage_x(weekly: pd.DataFrame | None, bench_weekly: pd.Series | None, cfg: MultibaggerConfig) -> dict[str, Any]:
    """Weekly completed candles (columns open/high/low/close/volume, W-FRI index) and the NIFTY 50 weekly close. X never sets the class."""
    if weekly is None or len(weekly) < 40:
        return {"score": None, "rs": None, "reason": "fewer than 40 completed weekly candles stored"}
    c, v = weekly["close"].astype(float), weekly["volume"].astype(float)
    sma30 = c.rolling(30).mean()
    stage2 = bool(c.iloc[-1] > sma30.iloc[-1] and sma30.iloc[-1] > sma30.iloc[-11]) if len(c) >= 41 else False
    pts, notes = 0, []
    if stage2:
        pts += 30
        notes.append("stage 2 (above a rising 30-week average)")
    base_ok = False
    if len(c) >= 78 + 1:
        for k in range(1, 27):                                                                                           # breakout within the last 26 weeks
            end = len(c) - k
            base = weekly.iloc[max(0, end - 78):end]
            if len(base) < 78:
                break
            hi, lo = float(base["high"].max()), float(base["low"].min())
            if (hi - lo) / hi <= 0.50 and c.iloc[end] > hi and v.iloc[end] >= 1.5 * v.iloc[end - 20:end].mean():
                base_ok = True
                break
        if base_ok:
            pts += 30
            notes.append("multi-year base breakout on volume")
    rs_now = None
    if bench_weekly is not None and len(bench_weekly):
        b = bench_weekly.reindex(c.index, method="ffill")
        ratio = (c / b).dropna()
        if len(ratio) >= 66:
            mans = ratio / ratio.rolling(52, min_periods=40).mean() - 1
            rs_now = float(mans.iloc[-1]) if not math.isnan(mans.iloc[-1]) else None
            if rs_now is not None and rs_now > 0 and len(mans) > 13 and mans.iloc[-1] > mans.iloc[-14]:
                pts += 25
                notes.append("relative strength positive and rising")
    if sma30.iloc[-1] and c.iloc[-1] <= 1.25 * sma30.iloc[-1]:
        pts += 15
        notes.append("not extended (within 25% of the 30-week average)")
    falling_below = bool(c.iloc[-1] < sma30.iloc[-1] and sma30.iloc[-1] < sma30.iloc[-11]) if len(c) >= 41 else False
    score = min(pts, 20) if falling_below else pts
    return {"score": float(score), "rs": rs_now, "reason": "; ".join(notes) or "no chart-stage signal" + ("; below a falling 30-week average (capped at 20)" if falling_below else "")}


# ───────────────────────── Stage 7: class ─────────────────────────
def assess(x: dict[str, Any], cfg: MultibaggerConfig | None = None, weekly: pd.DataFrame | None = None, bench_weekly: pd.Series | None = None, *, daily: pd.DataFrame | None = None,
           bench_daily: pd.Series | None = None, ecfg: EmergingConfig | None = None, nlcfg: NewListingConfig | None = None) -> dict[str, Any]:
    cfg, ecfg, nlcfg = cfg or MultibaggerConfig(), ecfg or EmergingConfig(), nlcfg or NewListingConfig()
    kind = kind_of(x.get("sector"), x.get("industry"))
    checks = stage1(x, cfg, kind)
    if kind == "holding":
        return _out(x, kind, "not_assessable", ["holding company: not assessable"], checks, {}, None)
    q, qchecks = stage_q(x, cfg, kind)
    g, gfacts = stage_g(x, cfg, kind)
    v = stage_v(x, cfg, kind, gfacts)
    t = stage_t(x, cfg)
    xs = stage_x(weekly, bench_weekly, cfg)
    checks += qchecks
    checks.append(chk("growth_data", "G", "unknown" if g["score"] is None else "pass" if (g["score"] or 0) >= cfg.qc_g else "fail", f"G = {g['score']} (data share {g['data_share']:.0%})", g["score"]))
    checks.append(chk("valuation", "V", "unknown" if v["score"] is None else "pass" if v["score"] >= cfg.qc_v else "fail", f"V = {v['score']} (data share {v['data_share']:.0%})" + (f"; {v['capped_by']}" if v.get("capped_by") else ""), v["score"]))
    checks.append(chk("turning_point", "T", "unknown" if t["score"] is None else "pass" if t["score"] >= cfg.mb_t else "fail", t["reason"], t["score"]))
    checks.append(chk("chart_stage", "X", "unknown" if xs["score"] is None else "pass" if xs["score"] >= cfg.buy_zone_x else "fail", xs["reason"], xs["score"]))
    scores = {"Q": q["score"], "G": g["score"], "V": v["score"], "T": t["score"], "X": xs["score"]}
    hard = [c for c in checks if c["stage"] in ("veto",) and c["result"] == "fail"] + [c for c in checks if c["id"] in ("liquidity", "price") and c["result"] == "fail"]
    capped = [c for c in checks if (c["stage"] == "cap" and c["result"] == "fail") or (c["stage"] == "veto" and c["result"] == "unknown" and c["id"] != "data_current")]
    unknown_vetoes = [c for c in checks if c["stage"] == "veto" and c["result"] == "unknown"]
    dc = next(c for c in checks if c["id"] == "data_current")
    reasons: list[str] = []

    def classify(ignore_unknown_caps: bool) -> tuple[str, list[str]]:
        cap_list = [c for c in capped if not (ignore_unknown_caps and c["result"] == "unknown")]
        if dc["result"] == "unknown" or q["data_share"] < cfg.min_data_share or g["data_share"] < cfg.min_data_share:
            return "not_assessable", ["data current unknown" if dc["result"] == "unknown" else "less than 70% of the Q or G weight is backed by data"]
        if hard or (q["score"] is not None and q["score"] < cfg.q_min_avoid):
            return "avoid", [f"failed veto: {c['id']} ({c['reason']})" for c in hard] or [f"Q {q['score']} is below {cfg.q_min_avoid:g}: does not meet the quality bar"]
        s = lambda k: scores[k]                                                                                         # noqa: E731
        mcap = gfacts.get("market_cap_cr")
        if (not cap_list and q["score"] is not None and q["score"] >= cfg.mb_q and s("G") is not None and s("G") >= cfg.mb_g and s("V") is not None and s("V") >= cfg.mb_v
                and s("T") is not None and s("T") >= cfg.mb_t and mcap is not None and cfg.mb_cap_cr[0] <= mcap <= cfg.mb_cap_cr[1] and kind not in ("financial", "insurance_amc")):
            return "multibagger_candidate", ["passes Q, G, V and T with market cap in range"]
        if not cap_list and q["score"] is not None and q["score"] >= cfg.qc_q and s("G") is not None and s("G") >= cfg.qc_g and s("V") is not None and s("V") >= cfg.qc_v:
            return "quality_compounder", ["quality and growth with a reasonable valuation"]
        if q["score"] is not None and q["score"] >= cfg.watch_q and ((s("G") is not None and s("G") >= cfg.watch_g) or (s("T") is not None and s("T") >= cfg.watch_t)):
            return "watch", ["quality is adequate with growth or a turning point"] + [f"capped: {c['id']} ({c['reason']})" for c in cap_list]
        if cap_list and q["score"] is not None and q["score"] >= cfg.qc_q and s("G") is not None and s("G") >= cfg.qc_g and s("V") is not None and s("V") >= cfg.qc_v:
            return "watch", [f"capped: {c['id']} ({c['reason']})" for c in cap_list]
        return "avoid", ["does not meet the quality bar"]

    cls, reasons = classify(False)
    ceiling, ceiling_reasons = classify(True)
    if kind in ("financial", "insurance_amc") and cls in ("multibagger_candidate",):
        cls = "quality_compounder"
    if kind in ("financial", "insurance_amc") and ceiling == "multibagger_candidate":
        ceiling = "quality_compounder"
    if cls in ("multibagger_candidate", "quality_compounder") and capped:
        cls, reasons = "watch", [f"capped: {c['id']} ({c['reason']})" for c in capped]
    extra: dict[str, Any] = {}
    status = listing_status(x, nlcfg)
    hard_ids = {c["id"] for c in hard}
    # Stage 7c: a new listing (checked first, never a multibagger or compounder). The liquidity/price floors and negative equity are never relaxed.
    if status["is_new"] and (hard_ids & {"liquidity", "price", "equity_positive"}):
        cls, reasons = "avoid", [f"failed veto: {c['id']} ({c['reason']})" for c in hard if c["id"] in ("liquidity", "price", "equity_positive")]
    elif status["is_new"]:
        cls, reasons = "new_listing", [f"new listing: {status['reason']}", "never a multibagger or compounder until it graduates (4 quarterly results)"]
        extra["new_listing"] = new_listing_assess(x, nlcfg, q, g, daily, bench_daily, status)
    elif cls not in ("multibagger_candidate", "quality_compounder") and kind not in ("financial", "insurance_amc"):
        # Stage 7b: the emerging track runs for stocks that did not reach a main class and fail on history-based rules only
        dil = None
        shs = adjusted_shares(x.get("annual") or [], x.get("splits") or [])
        if len(shs) >= 2 and shs[-1] is not None and shs[-2] not in (None, 0):
            dil = shs[-1] / shs[-2] - 1
        dilution = chk("dilution_10pct", "veto", "unknown" if dil is None else "pass" if dil <= ecfg.dilution_max_per_year else "fail", "share count change unknown" if dil is None else f"share count {dil:+.1%} in the latest year (max {ecfg.dilution_max_per_year:.0%})", dil)
        never = [c for c in checks if c["id"] in ("pledge", "equity_positive", "auditor_or_regulatory", "liquidity", "price")] + [dilution]
        em = emerging_assess(x, cfg, ecfg, scores, xs, never, gfacts)
        extra["emerging"] = em
        if em["passed"]:
            cls, reasons = "emerging", [em["label"], "passes the emerging-track checks; the chart confirms"]
    out = _out(x, kind, cls, reasons, checks, scores, xs)
    out.update(extra)
    out["class_if_unknown_checks_cleared"] = {"class": ceiling, "note": "informational ceiling: the class this stock could reach if the unknown vetoes (for example pledge, which no stored source provides) were verified; it is NOT the assigned class" if ceiling != cls else "same as the assigned class"}
    out["unknown_vetoes"] = [c["id"] for c in unknown_vetoes]
    return out


# ───────────────────────── Stage 7b: Emerging track ─────────────────────────
def _qs(x: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(x.get("quarterly") or [], key=lambda r: r["period_end"])


def consecutive(qs: list[dict[str, Any]]) -> bool:
    return all(60 <= (b["period_end"] - a["period_end"]).days <= 125 for a, b in zip(qs, qs[1:]))


def ttm(qs: list[dict[str, Any]], key: str, back: int = 0) -> float | None:
    """Sum of 4 consecutive quarters ending ``back`` quarters before the latest; None if any is missing or the quarters are not consecutive."""
    end = len(qs) - back
    win = qs[end - 4:end] if end >= 4 else []
    if len(win) < 4 or not consecutive(win):
        return None
    vals = [_f(r.get(key)) for r in win]
    return None if None in vals else float(sum(vals))                                       # type: ignore[arg-type]


def yoy_q(qs: list[dict[str, Any]], key: str, back: int = 0) -> float | None:
    i = len(qs) - 1 - back
    if i < 4:
        return None
    a, b = _f(qs[i - 4].get(key)), _f(qs[i].get(key))
    if a is None or b is None or a <= 0 or not 330 <= (qs[i]["period_end"] - qs[i - 4]["period_end"]).days <= 400:
        return None
    return b / a - 1


def _capital(r: dict[str, Any] | None) -> float | None:
    if not r:
        return None
    eq, debt, cash = _f(r.get("equity")), _f(r.get("debt")), _f(r.get("cash")) or 0.0
    return None if eq is None or debt is None or eq + debt - cash <= 0 else eq + debt - cash


def emerging_assess(x: dict[str, Any], cfg: MultibaggerConfig, ecfg: EmergingConfig, scores: dict[str, Any], xs: dict[str, Any] | None, never: list[dict[str, Any]], gfacts: dict[str, Any]) -> dict[str, Any]:
    """All Stage 7b checks. ``never`` = the never-relaxed rules (pledge, dilution, equity, governance, liquidity, price) as already evaluated by Stage 1. Every check is pass | fail | unknown."""
    qs, ann = _qs(x), x.get("annual") or []
    ck: list[dict[str, Any]] = []
    n = len(qs)
    ck.append(chk("e_quarters", "E", "pass" if n >= ecfg.min_quarters else "fail", f"{n} quarterly results stored (need {ecfg.min_quarters}, {ecfg.preferred_quarters} preferred)", n))
    t_rev, p_rev = ttm(qs, "revenue"), ttm(qs, "revenue", 4)
    g = None if t_rev is None or not p_rev or p_rev <= 0 else t_rev / p_rev - 1
    y1, y2 = yoy_q(qs, "revenue", 0), yoy_q(qs, "revenue", 1)
    if g is None:
        ck.append(chk("e_revenue", "E", "unknown", "TTM revenue growth needs 8 consecutive quarters", None))
    else:
        ok = g >= ecfg.ttm_revenue_growth and y1 is not None and y2 is not None and y1 >= ecfg.quarter_revenue_yoy and y2 >= ecfg.quarter_revenue_yoy
        unk = y1 is None or y2 is None
        ck.append(chk("e_revenue", "E", "unknown" if unk and g >= ecfg.ttm_revenue_growth else "pass" if ok else "fail", f"TTM revenue {g:+.0%} vs previous TTM (need {ecfg.ttm_revenue_growth:+.0%}); last two quarters YoY {None if y1 is None else f'{y1:+.0%}'}, {None if y2 is None else f'{y2:+.0%}'} (need {ecfg.quarter_revenue_yoy:+.0%})", round(g, 3)))
    t_pat, p_pat = ttm(qs, "pat"), ttm(qs, "pat", 4)
    last2 = [_f(r.get("pat")) for r in qs[-2:]]
    if t_pat is None or p_pat is None or len(last2) < 2 or None in last2:
        ck.append(chk("e_pat", "E", "unknown", "TTM profit growth needs 8 consecutive quarters of net income", None))
        pat_g = None
    else:
        turnaround = p_pat <= 0 < t_pat
        pat_g = None if p_pat <= 0 else t_pat / p_pat - 1
        ok = (turnaround or (pat_g is not None and pat_g >= ecfg.ttm_pat_growth)) and all(v > 0 for v in last2)                       # type: ignore[operator]
        ck.append(chk("e_pat", "E", "pass" if ok else "fail", ("loss-to-profit turnaround" if turnaround else f"TTM PAT {0 if pat_g is None else pat_g:+.0%} vs previous TTM (need {ecfg.ttm_pat_growth:+.0%})") + f"; last two quarters {'positive' if all(v > 0 for v in last2) else 'not both positive'}", None if pat_g is None else round(pat_g, 3)))     # type: ignore[operator]
    t_op, p_op = ttm(qs, "op_profit"), ttm(qs, "op_profit", 4)
    if None in (t_op, p_op, t_rev, p_rev) or not t_rev or not p_rev:
        ck.append(chk("e_margin", "E", "unknown", "TTM operating margin needs 8 consecutive quarters of operating profit", None))
    else:
        d = t_op / t_rev * 100 - p_op / p_rev * 100                                                       # type: ignore[operator]
        ck.append(chk("e_margin", "E", "pass" if d >= ecfg.ttm_margin_gain_pts else "fail", f"TTM operating margin {d:+.1f} pts vs previous TTM (need +{ecfg.ttm_margin_gain_pts:g})", round(d, 2)))
    cap_now, cap_prev = _capital(ann[-1] if ann else None), _capital(ann[-2] if len(ann) >= 2 else None)
    e_now, e_prev = (None if ttm(qs, "ebit") is None else ttm(qs, "ebit")), ttm(qs, "ebit", 4)
    if None in (cap_now, cap_prev, e_now, e_prev):
        ck.append(chk("e_roce", "E", "unknown", "TTM ROCE needs 8 quarters of EBIT and two annual balance sheets (approximate: quarterly balance sheets are not stored)", None))
    else:
        r_now, r_prev = e_now / cap_now * 100, e_prev / cap_prev * 100                                      # type: ignore[operator]
        ck.append(chk("e_roce", "E", "pass" if r_now >= ecfg.ttm_roce_min and r_now - r_prev >= ecfg.ttm_roce_gain_pts else "fail", f"TTM ROCE {r_now:.1f}% vs {r_prev:.1f}% a year earlier (approx; need {ecfg.ttm_roce_min:g}% and +{ecfg.ttm_roce_gain_pts:g} pts)", round(r_now, 1)))
    t_score = scores.get("T")
    ck.append(chk("e_turning_point", "E", "unknown" if t_score is None else "pass" if t_score >= ecfg.t_min else "fail", f"T = {t_score}", t_score))
    cfo_now, cfo_prev, pat_now = (_f(ann[-1].get("cfo")) if ann else None), (_f(ann[-2].get("cfo")) if len(ann) >= 2 else None), (_f(ann[-1].get("pat")) if ann else None)
    if cfo_now is None:
        ck.append(chk("e_cash", "E", "unknown", "operating cash flow not stored (latest annual used as the TTM proxy)", None))
    else:
        ratio = None if not pat_now or pat_now <= 0 else cfo_now / pat_now
        ok = cfo_now > 0 or (ratio is not None and ratio >= ecfg.cfo_pat_min and cfo_prev is not None and cfo_now > cfo_prev)
        ck.append(chk("e_cash", "E", "pass" if ok else "fail", f"latest annual CFO {cfo_now:,.0f}" + ("" if ratio is None else f", CFO/PAT {ratio:.2f}"), None if ratio is None else round(ratio, 2)))
    de_now = None if not ann or not _f(ann[-1].get("equity")) or _f(ann[-1].get("debt")) is None else _f(ann[-1]["debt"]) / _f(ann[-1]["equity"])        # type: ignore[operator]
    de_prev = None if len(ann) < 2 or not _f(ann[-2].get("equity")) or _f(ann[-2].get("debt")) is None else _f(ann[-2]["debt"]) / _f(ann[-2]["equity"])    # type: ignore[operator]
    if de_now is None:
        ck.append(chk("e_leverage", "E", "unknown", "debt or equity missing", None))
    else:
        ck.append(chk("e_leverage", "E", "pass" if de_now <= ecfg.de_max and (de_prev is None or de_now <= de_prev + 1e-9) else "fail", f"D/E {de_now:.2f} (max {ecfg.de_max:g})" + ("" if de_prev is None else f", previous year {de_prev:.2f}"), round(de_now, 2)))
    mc = gfacts.get("market_cap_cr")
    ck.append(chk("e_market_cap", "E", "unknown" if mc is None else "pass" if ecfg.mcap_cr[0] <= mc <= ecfg.mcap_cr[1] else "fail", f"market cap Rs {mc} crore (range {ecfg.mcap_cr[0]:g}-{ecfg.mcap_cr[1]:g})" if mc is not None else "market cap unknown", mc))
    pe = _f(x.get("pe"))
    peg = None if pe is None or pe <= 0 or pat_g is None or pat_g <= 0 else pe / (pat_g * 100)
    if pe is None:
        ck.append(chk("e_valuation", "E", "unknown", "PE unknown", None))
    else:
        ok = pe <= ecfg.pe_max and (peg is None or peg <= ecfg.peg_max)
        ck.append(chk("e_valuation", "E", "pass" if ok and peg is not None else "unknown" if ok else "fail", f"TTM PE {pe:.0f} (max {ecfg.pe_max:g})" + (", PEG on TTM growth unknown" if peg is None else f", PEG {peg:.2f} (max {ecfg.peg_max:g})"), round(pe, 1)))
    xsc = None if not xs else xs.get("score")
    ck.append(chk("e_chart", "E", "unknown" if xsc is None else "pass" if xsc >= ecfg.x_min and (xs.get("rs") or 0) > 0 else "fail", f"X = {xsc}, relative strength {None if not xs else xs.get('rs')} (the chart must confirm when the record is short)", xsc))
    blockers = [c for c in never if c["result"] == "fail"]
    passed = not blockers and all(c["result"] == "pass" for c in ck)
    prove = [f"{c['id']}: {c['reason']}" for c in ck if c["result"] != "pass"]
    trans = emerging_transition(qs, ecfg)
    return {"passed": passed, "checks": ck, "never_relaxed_failed": [c["id"] for c in blockers], "label": "unproven: about 1 year of evidence", "suggested_size_fraction": ecfg.size_fraction,
            "prove_it": prove if prove else ["keep growing: 2 more quarters with revenue growth above 20% and a steady margin upgrade it to a multibagger candidate; revenue growth below 15% in a quarter or a 2-point margin fall downgrades it to watch"],
            "transition": trans}


def emerging_transition(qs: list[dict[str, Any]], ecfg: EmergingConfig) -> dict[str, Any]:
    """Upgrade to multibagger_candidate after 2 more quarters with revenue growth > 20% and a steady margin; downgrade to watch if revenue growth < 15% in a quarter or the margin falls 2 pts (YoY)."""
    y = [yoy_q(qs, "revenue", b) for b in range(ecfg.upgrade_quarters)]
    om = [(_f(qs[-1 - b].get("op_margin")), _f(qs[-5 - b].get("op_margin")) if len(qs) >= 5 + b else None) for b in range(ecfg.upgrade_quarters)] if len(qs) >= 5 else []
    drops = [None if a is None or p is None else a - p for a, p in om]
    if y and y[0] is not None and y[0] < ecfg.downgrade_revenue_yoy:
        return {"action": "downgrade", "reason": f"latest quarter revenue growth {y[0]:+.0%} is below {ecfg.downgrade_revenue_yoy:.0%}"}
    if drops and drops[0] is not None and drops[0] <= -ecfg.downgrade_margin_drop_pts:
        return {"action": "downgrade", "reason": f"operating margin fell {abs(drops[0]):.1f} pts year on year"}
    if len(y) == ecfg.upgrade_quarters and None not in y and all(v > ecfg.upgrade_revenue_yoy for v in y) and drops and None not in drops and all(abs(d) <= ecfg.margin_steady_pts or d > 0 for d in drops):
        return {"action": "upgrade_ready", "reason": f"{ecfg.upgrade_quarters} quarters of revenue growth above {ecfg.upgrade_revenue_yoy:.0%} with a steady margin"}
    return {"action": "none", "reason": "no transition"}


# ───────────────────────── Stage 7c: New listings ─────────────────────────
def listing_status(x: dict[str, Any], nl: NewListingConfig) -> dict[str, Any]:
    """new_listing = a recent listing (under a year) with fewer than 4 quarterly results. Graduation (4 results -> eligible for emerging; 3 years of results -> main rules) is the same test re-run nightly."""
    today = x["as_of"]
    lst = x.get("listing") or {}
    ldate = lst.get("listing_date") or x.get("first_bar_date")
    quarters = len(_qs(x))
    if ldate is None:
        return {"is_new": False, "reason": "listing date unknown", "quarters": quarters}
    age = (today - ldate).days
    recent = age < nl.recent_days
    return {"is_new": recent and quarters < nl.min_quarters_to_graduate, "recent": recent, "age_days": age, "quarters": quarters, "listing_date": ldate,
            "reason": f"listed {age} days ago with {quarters} quarterly result(s)" + ("" if recent and quarters < nl.min_quarters_to_graduate else (" (graduated: eligible for emerging)" if recent else ""))}


def ipo_base(daily: pd.DataFrame | None, bench_daily: pd.Series | None, listing_date: date | None, issue_price: float | None, nl: NewListingConfig) -> dict[str, Any]:
    """IPO-base chart mode from 15 sessions after listing (the normal engine needs 120 daily / 40 weekly candles). daily: columns high/low/close/volume, completed sessions."""
    if daily is None or listing_date is None or len(daily) == 0:
        return {"state": "unknown", "reason": "no candles or listing date"}
    d = daily[daily.index >= pd.Timestamp(listing_date)]
    n = len(d)
    out: dict[str, Any] = {"sessions_since_listing": n}
    if n < nl.min_ipo_base_sessions:
        return {**out, "state": "too_early", "reason": f"only {n} sessions since listing; the IPO base needs {nl.min_ipo_base_sessions}"}
    close, high, low, vol = d["close"].astype(float), d["high"].astype(float), d["low"].astype(float), d["volume"].astype(float)
    base = d.iloc[:-1]
    hi = float(base["high"].max())
    depth = (hi - float(base["low"].min())) / hi if hi > 0 else None
    last, avg_vol = float(close.iloc[-1]), float(base["volume"].mean())
    breakout = last > hi and avg_vol > 0 and float(vol.iloc[-1]) >= nl.breakout_volume_mult * avg_vol
    typical = (high + low + close) / 3
    vwap = float((typical * vol).sum() / vol.sum()) if float(vol.sum()) > 0 else None
    demand = issue_price is not None and vwap is not None and last > issue_price and last > vwap
    rs = None
    if bench_daily is not None and len(bench_daily):
        b = bench_daily.reindex(d.index, method="ffill").dropna()
        if len(b) >= 2 and float(close.iloc[0]) > 0:
            rs = float(close.iloc[-1] / close.iloc[0] - 1) - float(b.iloc[-1] / b.iloc[0] - 1)
    state = "breakout" if breakout and depth is not None and depth <= nl.max_base_depth else "base_forming" if depth is not None and depth <= nl.max_base_depth else "no_base"
    below = 0
    if issue_price is not None:
        for c in reversed(close.tolist()):
            if c < issue_price:
                below += 1
            else:
                break
    return {**out, "state": state, "depth": None if depth is None else round(depth, 3), "base_high": hi, "breakout": breakout, "demand": demand if issue_price is not None else None, "vwap_from_listing": vwap, "rs_vs_nifty": rs,
            "sessions_below_issue": below if issue_price is not None else None, "weak_demand": issue_price is not None and below >= nl.weak_demand_sessions,
            "reason": f"IPO base {state.replace('_', ' ')}: depth {depth:.0%}" + ("" if issue_price is None else f", {below} consecutive sessions below the issue price") if depth is not None else "IPO base undefined"}


def lockin_gate(x: dict[str, Any], nl: NewListingConfig) -> dict[str, Any]:
    """No buy within 5 sessions before an anchor lock-in expiry (about 30 and 90 days after allotment) or the main pre-IPO lock-in expiry. Estimated dates are labelled approximate."""
    from datetime import timedelta
    lst = x.get("listing") or {}
    allot, today = lst.get("allotment_date") or lst.get("listing_date") or x.get("first_bar_date"), x["as_of"]
    if allot is None:
        return {"blocked": None, "reason": "allotment date unknown: lock-in dates cannot be checked", "dates": []}
    dates = [(allot + timedelta(days=d), "anchor lock-in (approximate)") for d in nl.anchor_lockin_days]
    main = lst.get("pre_ipo_lockin_end")
    dates.append((main, "pre-IPO lock-in") if main else (allot + timedelta(days=nl.pre_ipo_lockin_days), "main pre-IPO lock-in (approximate)"))
    buf = timedelta(days=int(nl.lockin_buffer_sessions * 7 / 5) + 1)                                         # 5 sessions in calendar days
    near = [(d, lab) for d, lab in dates if d - buf <= today <= d]
    return {"blocked": bool(near), "reason": ("; ".join(f"{lab} on {d}" for d, lab in near) + ": no buy within 5 sessions before it") if near else "no lock-in expiry within 5 sessions", "dates": [(str(d), lab) for d, lab in dates]}


def new_listing_assess(x: dict[str, Any], nl: NewListingConfig, q: dict[str, Any], g: dict[str, Any], daily: pd.DataFrame | None, bench_daily: pd.Series | None, status: dict[str, Any]) -> dict[str, Any]:
    lst = x.get("listing") or {}
    issue = _f(lst.get("issue_price"))
    base = ipo_base(daily, bench_daily, status.get("listing_date"), issue, nl)
    lock = lockin_gate(x, nl)
    n = base.get("sessions_since_listing") or 0
    weak = bool(base.get("weak_demand"))
    allowed_entry = base.get("state") in ("breakout",) or bool(base.get("breakout"))
    def hz(min_sessions: int) -> dict[str, Any]:
        if weak:
            return {"allowed": False, "reason": "weak demand: below the issue price for 20 or more sessions"}
        if lock.get("blocked"):
            return {"allowed": False, "reason": lock["reason"]}
        if n < min_sessions:
            return {"allowed": False, "reason": f"{n} sessions since listing; needs {min_sessions}"}
        if not allowed_entry:
            return {"allowed": False, "reason": "only an IPO-base breakout or retest is allowed for a new listing"}
        return {"allowed": True, "reason": "IPO-base breakout; normal level rules still apply"}
    pending = []
    if q["score"] is None or g["score"] is None:
        pending.append("pre-IPO statements (3 restated years) not stored: Q and G are unknown, not zero")
    pending.append(f"{status['quarters']} of {nl.min_quarters_to_graduate} quarterly results needed to graduate to the emerging track")
    if issue is None:
        pending.append("issue price not stored")
    return {"base": base, "lockin": lock, "weak_demand": weak, "horizons": {"swing": hz(nl.swing_from_sessions), "positional": hz(nl.positional_from_sessions), "long_term": {"allowed": False, "reason": "a new listing is never a multibagger or compounder; shown with pre-IPO Q and G and pending checks"}},
            "suggested_size_fraction": nl.size_fraction, "pending_checks": pending, "pre_ipo_q": q["score"], "pre_ipo_g": g["score"]}


# ───────────────────────── Stage 8: thesis-break alerts (pure) ─────────────────────────
def thesis_alerts(x: dict[str, Any], result: dict[str, Any], previous_class: str | None = None, pledge_before: float | None = None) -> list[dict[str, str]]:
    """Alerts for a long-term HOLDING. Price falls alone never alert. Wiring to research_alerts / the Dhan holdings is not done yet."""
    out: list[dict[str, str]] = []
    qs = _qs(x)
    y = [yoy_q(qs, "revenue", b) for b in range(2)]
    if None not in y and all(v < 0.05 for v in y):
        out.append({"kind": "revenue_slowdown", "text": "Revenue growth below 5% year on year for 2 quarters in a row."})
    ann = x.get("annual") or []
    ro = [v for v, _ in roce_series(ann[-3:]) if v is not None]
    if len(ro) >= 3:
        med = sorted(ro)[len(ro) // 2]
        if ro[-1] <= med - 5:
            out.append({"kind": "roce_down", "text": f"ROCE {ro[-1]:.1f}% is 5 or more points below its 3-year median {med:.1f}%."})
    pl = _f(x.get("pledged_pct"))
    if pl is not None and pl > 0 and (pledge_before is None or pl > pledge_before):
        out.append({"kind": "pledge", "text": f"Promoter pledge {pl:.1f}% appeared or rose."})
    if x.get("governance_events_24m"):
        out.append({"kind": "governance", "text": "An auditor resignation or regulatory order was recorded."})
    pe, med_pe = _f(x.get("pe")), _f(x.get("pe_median_5y"))
    eg = (result.get("scores") or {}).get("G")
    cg = None
    if len(ann) >= 4:
        cg = cagr(_f(ann[-4].get("eps")), _f(ann[-1].get("eps")), 3)
    if pe and cg and cg > 0 and pe > 2.5 * cg * 100 and med_pe and pe > 1.5 * med_pe:
        out.append({"kind": "valuation_trim", "text": f"PE {pe:.0f} is above 2.5x EPS growth and 1.5x its own median: consider trimming (not selling)."})
    if result.get("class") == "avoid" and previous_class not in (None, "avoid"):
        out.append({"kind": "class_avoid", "text": f"Class dropped from {previous_class} to avoid."})
    del eg
    return out


def _out(x: dict[str, Any], kind: str, cls: str, reasons: list[str], checks: list[dict[str, Any]], scores: dict[str, Any], xs: dict[str, Any] | None) -> dict[str, Any]:
    passed = sum(1 for c in checks if c["result"] == "pass")
    applicable = [c for c in checks if c["result"] in ("pass", "fail", "unknown")]
    rank = None
    q, g, v, t = (scores.get(k) for k in ("Q", "G", "V", "T"))
    if cls == "multibagger_candidate" and None not in (q, g, v, t):
        rank = round((max(q, 1) * max(g, 1) * max(v, 1) * max(t, 1)) ** 0.25, 1)                                       # type: ignore[type-var]
    elif cls == "quality_compounder" and None not in (q, g, v):
        rank = round((max(q, 1) * max(g, 1) * max(v, 1)) ** (1 / 3), 1)                                                # type: ignore[type-var]
    xsc = None if not xs else xs.get("score")
    return {"version": VERSION, "symbol": x.get("symbol"), "as_of": str(x.get("as_of")), "kind": kind, "class": cls, "reasons": reasons[:6], "scores": scores, "rank": rank,
            "checklist": f"{passed} of {len(applicable)} checks passed", "checks_passed": passed, "checks_total": len(applicable), "checks": checks,
            "timing": None if xsc is None else ("buy_zone" if xsc >= 70 else "wait_for_chart"), "leader": bool(xs and xsc is not None and xsc >= 70 and (xs.get("rs") or 0) > 0),
            "label": "Decision support, not investment advice. Hypothetical screen; never an order."}
