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

from app.config import MultibaggerConfig
from app.services.research_scoring import _lerp, is_financial_sector
from app.services.valuation_scoring import sector_family

VERSION = "multibagger-0.1.0-provisional"
CLASSES = ("multibagger_candidate", "quality_compounder", "watch", "avoid", "not_assessable")
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
    rc = cagr(revs[-4], revs[-1], 3) if len(ann) >= 4 else None
    pc = cagr(pats[-4], pats[-1], 3) if len(ann) >= 4 else None
    facts.update({"revenue_cagr_3y": rc, "pat_cagr_3y": pc, "eps_cagr_3y": cagr(_series(ann, "eps")[-4], _series(ann, "eps")[-1], 3) if len(ann) >= 4 else None})
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
    if len(q) < 6 or len(known) < 3:
        return {"score": None, "signals": {k: v[1] for k, v in sig.items()}, "reason": f"unknown: {len(known)} of 6 signals can be evaluated with {len(q)} stored quarters (needs 6 quarters and 3 evaluable signals; YoY acceleration needs 10)"}
    hits = [k for k, v in known.items() if v[1]]
    score = 0 if len(hits) < 2 else min(100, sum(sig[k][0] for k in hits))
    return {"score": float(score), "signals": {k: v[1] for k, v in sig.items()}, "reason": f"{len(hits)} turning-point signal(s): {', '.join(hits) or 'none'}; unevaluable: {', '.join(k for k, v in sig.items() if v[1] is None) or 'none'}"}


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
def assess(x: dict[str, Any], cfg: MultibaggerConfig | None = None, weekly: pd.DataFrame | None = None, bench_weekly: pd.Series | None = None) -> dict[str, Any]:
    cfg = cfg or MultibaggerConfig()
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
    out = _out(x, kind, cls, reasons, checks, scores, xs)
    out["class_if_unknown_checks_cleared"] = {"class": ceiling, "note": "informational ceiling: the class this stock could reach if the unknown vetoes (for example pledge, which no stored source provides) were verified; it is NOT the assigned class" if ceiling != cls else "same as the assigned class"}
    out["unknown_vetoes"] = [c["id"] for c in unknown_vetoes]
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
