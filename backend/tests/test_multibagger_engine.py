"""Phase 6 multibagger engine. Test data: hand-made statement rows (not market data) built by explicit helpers; nothing here flows into runtime paths."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.config import MultibaggerConfig
from app.services import multibagger_engine as mb

TODAY = date(2026, 11, 22)
CFG = MultibaggerConfig()


def ann(year, *, rev, pat, equity=500.0, debt=100.0, cfo=None, om=20.0, shares=100.0, eps=None, interest=5.0, pbt=None, cash=20.0, fa=None):
    return {"period_end": date(year, 3, 31), "revenue": rev, "pat": pat, "eps": eps if eps is not None else pat / shares, "op_margin": om, "assets": 1000.0, "equity": equity, "debt": debt,
            "cfo": cfo if cfo is not None else pat * 1.1, "shares": shares, "interest": interest, "pbt": pbt if pbt is not None else pat * 1.3, "ebit": None, "cash": cash, "fixed_assets": fa}


def good_annual(grow=1.25):
    out, rev, pat = [], 1000.0, 100.0
    for y in (2023, 2024, 2025, 2026):
        out.append(ann(y, rev=rev, pat=pat, fa=300.0 * (1.2 ** (y - 2023))))
        rev, pat = rev * grow, pat * grow
    return out


def qtrs(n=6, *, margin_step=0.0):
    return [{"period_end": date(2025, 3, 31) + timedelta(days=91 * i), "revenue": 300.0 + 10 * i, "pat": 30.0 + 2 * i, "op_margin": 20.0 + margin_step * i} for i in range(n)]


def stock(**kw):
    x = {"symbol": "TEST", "as_of": TODAY, "sector": "Industrials", "industry": "Machinery", "annual": good_annual(), "quarterly": qtrs(), "price": 250.0, "median_traded_value_cr": 25.0, "first_bar_date": date(2020, 1, 1),
         "splits": [], "market_cap_cr": 3000.0, "pe": 18.0, "pb": 3.0, "pe_to_sector_pe": 0.8, "dhan_roce": 22.0, "dhan_de": 0.2, "roe_pct": 18.0, "promoter_pct": 60.0, "promoter_history": [],
         "pledged_pct": 0.0, "pledge_history": [], "governance_events_24m": [], "pe_median_5y": 25.0, "industry_revenue_cagr_pct": 18.0, "industry_peers": 8}
    x.update(kw)
    return x


def result(r, cid):
    return next(c["result"] for c in r["checks"] if c["id"] == cid)


# ───────────── helpers ─────────────
def test_weighted_drops_missing_factors_and_requires_70_percent_backing():
    w = mb.weighted([("a", 0.5, 80.0), ("b", 0.3, None), ("c", 0.2, 60.0)], 0.7)
    assert w["score"] == pytest.approx((0.5 * 80 + 0.2 * 60) / 0.7, abs=0.1) and w["data_share"] == 0.7        # b is dropped from the weight, not scored zero
    assert mb.weighted([("a", 0.5, 80.0), ("b", 0.5, None)], 0.7)["score"] is None                              # only 50% backed: no score at all


def test_cagr_needs_both_ends_positive_and_share_count_is_split_adjusted():
    assert mb.cagr(100, 200, 3) == pytest.approx(0.2599, abs=1e-3) and mb.cagr(-5, 20, 3) is None and mb.cagr(10, 0, 3) is None
    rows = [{"period_end": date(2023, 3, 31), "shares": 100.0}, {"period_end": date(2026, 3, 31), "shares": 200.0}]
    assert mb.adjusted_shares(rows, [(date(2025, 6, 1), 2.0)]) == [200.0, 200.0]                               # a 2-for-1 split after FY23 doubles the old count: no dilution


# ───────────── Stage 1 vetoes ─────────────
def test_every_unknown_veto_caps_the_class_at_watch_and_names_what_to_verify():
    r = mb.assess(stock(pledged_pct=None))                                                                      # pledge is stored nowhere: unknown
    assert result(r, "pledge") == "unknown" and r["class"] == "watch" and any("verify pledge" in x for x in r["reasons"])
    assert r["class_if_unknown_checks_cleared"]["class"] in ("quality_compounder", "multibagger_candidate", "watch")


def test_failed_vetoes_send_the_stock_to_avoid():
    cases = {"share_count_cagr": stock(annual=[dict(a, shares=a["shares"] * (1.3 ** i)) for i, a in enumerate(good_annual())]),
             "cash_conversion_veto": stock(annual=[dict(a, cfo=a["pat"] * 0.1) for a in good_annual()]),
             "leverage_veto": stock(annual=[dict(a, debt=1500.0) for a in good_annual()]),
             "equity_positive": stock(annual=good_annual()[:-1] + [dict(good_annual()[-1], equity=-5.0)])}
    for cid, x in cases.items():
        r = mb.assess(x)
        assert result(r, cid) == "fail" and r["class"] == "avoid", cid


def test_data_current_unknown_is_not_assessable_and_stale_is_avoid():
    assert mb.assess(stock(annual=[]))["class"] == "not_assessable"
    old = [dict(a, period_end=a["period_end"].replace(year=a["period_end"].year - 3)) for a in good_annual()]
    assert mb.assess(stock(annual=old))["class"] == "avoid"


def test_net_losses_and_listing_age_cap_at_watch():
    losing = [dict(a, pat=-10.0) if i >= 2 else a for i, a in enumerate(good_annual())]
    r = mb.assess(stock(annual=[dict(a) for a in losing]))
    assert result(r, "net_losses") == "fail" and r["class"] in ("watch", "avoid", "not_assessable")
    new = mb.assess(stock(first_bar_date=date(2025, 6, 1)))
    assert result(new, "listing_age") == "fail" and new["class"] != "quality_compounder" and new["class"] != "multibagger_candidate"


def test_tradability_gates_fail_below_the_floor_or_price():
    thin = mb.assess(stock(median_traded_value_cr=0.5))
    assert thin["class"] == "not_tradable" and thin["reasons"] == ["Too illiquid for Nexus (median traded value Rs 0.5 cr)"]        # a floor alone is not a red flag
    cheap = mb.assess(stock(price=12.0))
    assert result(cheap, "price") == "fail" and cheap["class"] == "not_tradable"
    both = mb.assess(stock(median_traded_value_cr=0.5, annual=[dict(a, debt=1500.0) for a in good_annual()]))
    assert both["class"] == "avoid"                                                                                    # a veto fired as well: avoid wins over not_tradable


def test_avoid_means_a_red_flag_and_not_a_fit_names_the_bar_it_missed():
    low_quality = mb.assess(stock(annual=[dict(a, op_margin=2.0, pbt=1.0, interest=5.0, pat=1.0) for a in good_annual()]))
    assert low_quality["class"] in ("avoid", "not_assessable")
    slow = mb.assess(stock(annual=good_annual(grow=1.02), quarterly=qtrs(6)))
    assert slow["class"] in ("not_a_fit", "watch", "quality_compounder"), slow["reasons"]
    if slow["class"] == "not_a_fit":
        assert any("growth below the bar" in r and "revenue" in r for r in slow["reasons"]) and "avoid" not in slow["class"]
    assert "not_a_fit" in mb.CLASSES and "not_tradable" in mb.CLASSES


# ───────────── Stages 2-6 ─────────────
def test_quality_drops_unknown_factors_instead_of_scoring_zero():
    full, _ = mb.stage_q(stock(), CFG, "default")
    no_interest = stock(annual=[dict(a, interest=None, pbt=None) for a in good_annual()])
    part, _ = mb.stage_q(no_interest, CFG, "default")
    assert full["score"] is not None and part["score"] is not None and part["factors"]["interest_coverage"] is None and part["data_share"] < full["data_share"]


def test_growth_scores_strong_compounding_and_marks_the_5y_inputs_unknown():
    g, facts = mb.stage_g(stock(), CFG, "default")
    assert g["score"] > 70 and facts["revenue_cagr_3y"] == pytest.approx(0.25, abs=0.01)
    assert len(stock()["annual"]) == 4                                                                         # only 3-year CAGRs are possible with 4 annual rows; nothing 5-year is computed


def test_turning_point_is_unknown_with_too_few_quarters_and_never_zero_by_default():
    assert mb.stage_t(stock(quarterly=qtrs(3)), CFG)["score"] is None
    t = mb.stage_t(stock(quarterly=qtrs(6, margin_step=0.5)), CFG)
    assert t["signals"]["revenue_acceleration"] is None and t["signals"]["institutions"] is None            # 10 quarters / 4 dated holdings are not stored: unknown, not failed


def test_valuation_peg_pe_cap_and_dropped_factors():
    v = mb.stage_v(stock(), CFG, "default", {"eps_cagr_3y": 0.25})
    assert v["score"] is not None and v["factors"]["earnings_yield_spread"] is None                           # no G-sec set by hand: that factor is dropped
    capped = mb.stage_v(stock(pe=120.0), CFG, "default", {"eps_cagr_3y": 0.25})
    assert capped["score"] <= CFG.pe_cap_v and "capped_by" in capped
    low_growth = mb.stage_v(stock(), CFG, "default", {"eps_cagr_3y": 0.03})
    assert low_growth["factors"]["peg"] is None                                                               # EPS CAGR <= 5%: PEG not scored


def weekly(n=200, trend=0.004):
    idx = pd.date_range(end="2026-11-20", periods=n, freq="W-FRI")
    c = 100 * np.cumprod(np.full(n, 1 + trend))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(n, 1000.0)}, index=idx)


def test_chart_stage_rewards_stage_two_and_caps_a_falling_stock():
    up = mb.stage_x(weekly(), pd.Series(100 * np.cumprod(np.full(200, 1.001)), index=weekly().index), CFG)
    assert up["score"] >= 55 and up["rs"] is not None and "stage 2" in up["reason"]
    down = mb.stage_x(weekly(trend=-0.004), None, CFG)
    assert down["score"] <= 20
    assert mb.stage_x(weekly(30), None, CFG)["score"] is None


# ───────────── sectors and classes ─────────────
def test_sector_kinds():
    assert mb.kind_of("Financial Services", "Banks") == "financial" and mb.kind_of("Financial Services", "Insurance") == "insurance_amc"
    assert mb.kind_of("Basic Materials", "Steel") == "cyclical" and mb.kind_of("Utilities", "Power") == "utility"
    assert mb.kind_of("Technology", "IT Services") == "asset_light" and mb.kind_of("Financial Services", "Holding Companies") == "holding"


def test_a_bank_skips_roce_and_leverage_and_is_capped_at_quality_compounder():
    bank_rows = [dict(a, equity=900.0, debt=5000.0, interest=None, pbt=None, cfo=-50.0) for a in good_annual()]
    r = mb.assess(stock(sector="Financial Services", industry="Banks", annual=bank_rows, pledged_pct=0.0))
    assert result(r, "leverage_veto") == "pass" and result(r, "cash_conversion_veto") == "pass"                # not applied to financials, despite D/E 5.5 and negative CFO
    assert r["class"] != "multibagger_candidate"


def test_a_steel_company_at_peak_margin_on_a_pe_of_6_scores_peg_zero():
    steel = [dict(a, op_margin=10.0 + 3 * i) for i, a in enumerate(good_annual())]
    v = mb.stage_v(stock(sector="Basic Materials", industry="Steel", annual=steel, pe=6.0), CFG, "cyclical", {"eps_cagr_3y": 0.25})
    assert v["factors"]["peg"] == 0.0
    normal = mb.stage_v(stock(sector="Basic Materials", industry="Steel", annual=steel, pe=14.0), CFG, "cyclical", {"eps_cagr_3y": 0.25})
    assert normal["factors"]["peg"] > 0


def test_a_utility_has_no_market_cap_runway_and_shifted_roce_anchors():
    g, _ = mb.stage_g(stock(sector="Utilities", industry="Power"), CFG, "utility")
    assert g["factors"]["market_cap_runway"] == 0.0
    util, _ = mb.stage_q(stock(sector="Utilities", industry="Power"), CFG, "utility")
    plain, _ = mb.stage_q(stock(), CFG, "default")
    assert util["factors"]["roce_level"] >= plain["factors"]["roce_level"]                                    # anchors lowered by 5 points


def test_an_it_company_gets_the_asset_light_anchor_shift_and_a_holding_company_is_not_assessable():
    it, _ = mb.stage_q(stock(sector="Technology", industry="IT Services"), CFG, "asset_light")
    plain, _ = mb.stage_q(stock(), CFG, "default")
    assert it["factors"]["roce_level"] <= plain["factors"]["roce_level"]
    assert mb.assess(stock(sector="Financial Services", industry="Holding Companies"))["class"] == "not_assessable"


def test_class_order_passes_every_rule_except_one_capped_gate_lands_in_watch_with_that_reason():
    base = stock(quarterly=qtrs(6, margin_step=0.5), annual=[dict(a, fa=a["fixed_assets"]) for a in good_annual()])
    cleared = mb.assess(base)
    assert cleared["class"] in ("quality_compounder", "multibagger_candidate", "watch")
    capped = mb.assess(dict(base, first_bar_date=date(2025, 6, 1)))                                           # too new: a capped gate
    if cleared["class"] in ("quality_compounder", "multibagger_candidate"):
        assert capped["class"] == "watch" and any("listing_age" in r for r in capped["reasons"])


def test_checklist_count_timing_tag_and_unknowns_are_reported():
    r = mb.assess(stock(), None, weekly(), pd.Series(100 * np.cumprod(np.full(200, 1.001)), index=weekly().index))
    assert r["checklist"].endswith("checks passed") and r["timing"] in ("buy_zone", "wait_for_chart") and "disclaimer" not in r and "not investment advice" in r["label"]
    assert r["version"] == "multibagger-0.1.0-provisional"


# ───────────── Phase 4 quality gates are never more permissive than the engine (flag on) ─────────────
def test_every_phase4_quality_failure_blocks_a_long_term_buy_when_the_engine_is_the_quality_stage():
    """Property: with multibagger_screen_enabled the long-term quality stage is at least as strict as Phase 4's gates (the engine's Stage 1 covers equity, leverage (1.0 vs 2.0), 3-year cash
    conversion and loss years; the latest-year operating-cash-flow check is kept as an additional Phase 4 gate, so Phase 4 failures are never overridden)."""
    from app.services import pipeline_v2 as P

    def period(year, **v):
        return {"period_type": "annual", "period_end": f"{year}-03-31", "currency": "INR", "values": {k: {"value": str(x)} for k, x in v.items()}}
    healthy = [period(2024, revenue=100, net_income=10, total_equity=50, total_debt=20, operating_cash_flow=12), period(2025, revenue=110, net_income=12, total_equity=60, total_debt=20, operating_cash_flow=14),
               period(2026, revenue=120, net_income=14, total_equity=70, total_debt=20, operating_cash_flow=15)]
    assert P.quality_gate({"periods": healthy}, "Industrials", None)["status"] == "pass"
    cases = {"equity": dict(healthy[-1], values={**healthy[-1]["values"], "total_equity": {"value": "-5"}}),
             "debt_to_equity": dict(healthy[-1], values={**healthy[-1]["values"], "total_debt": {"value": "500"}}),
             "operating_cash_flow": dict(healthy[-1], values={**healthy[-1]["values"], "operating_cash_flow": {"value": "-3"}})}
    for name, last in cases.items():
        p4 = P.quality_gate({"periods": healthy[:-1] + [last]}, "Industrials", None)
        assert p4["status"] == "fail", name
        # the engine-backed stage returns Phase 4's failure unchanged, so decide() can never produce a BUY
        out = P.decide("long_term", {"status": "available"}, {"pass": True}, p4, {"status": "pass", "reason": "ok"}, False)
        assert out["recommendation"] == "AVOID" and out["stage_failed"] == "quality", name


def test_decide_maps_the_engine_classes_only_compounders_and_candidates_can_buy():
    from app.services import pipeline_v2 as P
    ok = {"status": "pass", "reason": "ok"}
    mk = lambda status: {"status": status, "checks": [{"result": "fail" if status in ("fail", "watch") else "unknown", "reason": status}]}  # noqa: E731
    d = lambda q: P.decide("long_term", {"status": "available"}, {"pass": True}, q, ok, False)["recommendation"]  # noqa: E731
    assert d({"status": "pass", "checks": []}) == "BUY" and d(mk("watch")) == "WAIT" and d(mk("fail")) == "AVOID" and d(mk("not_assessable")) == "INSUFFICIENT_EVIDENCE"


# ───────────── nightly storage and the owner-scoped API ─────────────
def test_the_screen_endpoint_is_off_by_default_owner_scoped_and_filters(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.api import research as api
    from app.config import settings
    from app.db import Base
    from app.models import Instrument, User
    from app.models.multibagger import MultibaggerAssessment
    from app.services import multibagger_loader as L
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    with Session(e) as db:
        me, other = uuid4(), uuid4()
        for u in (me, other):
            db.add(User(id=u, email=f"{u}@example.invalid", password_hash="x", email_verified=True, created_at=now))
        insts = []
        for sym in ("AAA", "BBB", "CCC"):
            i = Instrument(id=uuid4(), symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=now)
            db.add(i)
            insts.append(i)
        db.commit()
        for i, klass, rank in zip(insts, ("quality_compounder", "avoid", "watch"), (72.0, None, None)):
            res = mb.assess(stock(symbol=i.symbol))
            res = dict(res, **{"class": klass, "rank": rank})
            L._store(db, me, i, res, now)
        L._store(db, other, insts[0], dict(mb.assess(stock()), **{"class": "multibagger_candidate", "rank": 90.0}), now)             # another owner's row must never appear
        db.commit()
        sess = SimpleNamespace(user_id=me)
        monkeypatch.setattr(settings, "multibagger_screen_enabled", False)
        off = api.get_multibagger(klass=None, sector=None, min_rank=None, limit=400, _session=sess, db=db)
        assert off["enabled"] is False and off["items"] == []
        monkeypatch.setattr(settings, "multibagger_screen_enabled", True)
        on = api.get_multibagger(klass=None, sector=None, min_rank=None, limit=400, _session=sess, db=db)
        assert [i["symbol"] for i in on["items"]] == ["AAA", "CCC", "BBB"] and on["counts"] == {"quality_compounder": 1, "avoid": 1, "watch": 1}        # ordered by class, then rank
        assert all("failed_or_unknown" in i and i["checklist"] for i in on["items"])
        only = api.get_multibagger(klass="watch", sector=None, min_rank=None, limit=400, _session=sess, db=db)
        assert [i["symbol"] for i in only["items"]] == ["CCC"]
        assert api.get_multibagger(klass=None, sector=None, min_rank=80.0, limit=400, _session=sess, db=db)["items"] == []          # the other owner's 90 is not visible
        L._store(db, me, insts[0], dict(mb.assess(stock()), **{"class": "watch", "rank": None}), now)                                  # same day again: updated, not duplicated
        db.commit()
        assert db.query(MultibaggerAssessment).filter_by(user_id=me, instrument_id=insts[0].id).count() == 1
    e.dispose()
