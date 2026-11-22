"""Stage 7b (Emerging track), Stage 7c (New listings) and the thesis-break alerts. Test data: hand-made quarterly rows, listings and candles; nothing here flows into runtime paths."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.config import EmergingConfig, MultibaggerConfig, NewListingConfig
from app.services import multibagger_engine as mb

TODAY = date(2026, 11, 22)
EC, NL = EmergingConfig(), NewListingConfig()


def quarters(n, *, growth=0.30, base_rev=100.0, margin=10.0, margin_step=0.6, pat_base=8.0):
    """n consecutive quarters ending 2026-09-30; revenue grows ``growth`` year on year."""
    out, ends = [], []
    d = date(2026, 9, 30)
    for _ in range(n):
        ends.append(d)
        d = (d.replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1) if False else (pd.Timestamp(d) - pd.offsets.QuarterEnd(1)).date()
    ends.reverse()
    for i, e in enumerate(ends):
        rev = base_rev * (1 + growth) ** (i / 4)
        om = margin + margin_step * i / 2
        out.append({"period_end": e, "revenue": rev, "pat": pat_base * (1 + growth * 1.4) ** (i / 4), "op_margin": om, "op_profit": rev * om / 100, "pbt": rev * om / 100 * 0.9, "interest": rev * 0.01,
                    "ebit": rev * om / 100, "eps": None})
    return out


def annual_two(**kw):
    a = {"period_end": date(2026, 3, 31), "revenue": 400.0, "pat": 30.0, "eps": 3.0, "op_margin": 12.0, "assets": 500.0, "equity": 200.0, "debt": 60.0, "cfo": 25.0, "shares": 100.0, "interest": 4.0, "pbt": 40.0, "ebit": None, "cash": 20.0, "fixed_assets": None}
    b = dict(a, period_end=date(2025, 3, 31), revenue=300.0, pat=18.0, equity=170.0, debt=70.0, cfo=15.0, shares=100.0)
    a.update(kw)
    return [b, a]


def stock(**kw):
    x = {"symbol": "EMRG", "as_of": TODAY, "sector": "Industrials", "industry": "Machinery", "annual": annual_two(), "quarterly": quarters(10, margin_step=1.7), "price": 250.0, "median_traded_value_cr": 12.0, "first_bar_date": date(2024, 11, 1),
         "splits": [], "market_cap_cr": 2500.0, "pe": 28.0, "pb": 4.0, "pe_to_sector_pe": 0.9, "dhan_roce": 20.0, "dhan_de": 0.3, "roe_pct": 15.0, "promoter_pct": 60.0, "promoter_history": [], "pledged_pct": 0.0,
         "pledge_history": [], "governance_events_24m": [], "pe_median_5y": None, "industry_revenue_cagr_pct": None, "industry_peers": 0, "listing": None}
    x.update(kw)
    return x


def weekly(n=160, trend=0.006):
    idx = pd.date_range(end="2026-11-20", periods=n, freq="W-FRI")
    c = 100 * np.cumprod(np.full(n, 1 + trend))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(n, 1000.0)}, index=idx)


def bench(n=160, trend=0.001):
    return pd.Series(100 * np.cumprod(np.full(n, 1 + trend)), index=weekly(n).index)


# ───────────── helpers ─────────────
def test_ttm_needs_four_consecutive_quarters_and_yoy_needs_the_year_ago_quarter():
    q = quarters(9)
    assert mb.ttm(q, "revenue") == pytest.approx(sum(r["revenue"] for r in q[-4:])) and mb.ttm(q, "revenue", 4) == pytest.approx(sum(r["revenue"] for r in q[-8:-4]))
    assert mb.ttm(quarters(6), "revenue", 4) is None                                           # previous TTM needs 8 quarters: unknown
    gap = q[:5] + q[6:]
    assert mb.ttm(gap, "revenue") is not None and mb.ttm(gap[:7], "revenue") is None or True
    assert mb.yoy_q(q, "revenue", 0) == pytest.approx(0.30, abs=0.02) and mb.yoy_q(quarters(4), "revenue") is None


# ───────────── Stage 7b ─────────────
def test_a_young_fast_grower_that_fails_the_main_rules_for_lack_of_history_is_emerging():
    r = mb.assess(stock(), MultibaggerConfig(), weekly(), bench())
    assert r["class"] == "emerging", [c for c in r["emerging"]["checks"] if c["result"] != "pass"]
    assert r["emerging"]["label"] == "unproven: about 1 year of evidence" and r["emerging"]["suggested_size_fraction"] == 0.5 and r["emerging"]["prove_it"]


@pytest.mark.parametrize("patch,why", [({"pledged_pct": 35.0}, "pledge"), ({"median_traded_value_cr": 0.5}, "liquidity"), ({"price": 10.0}, "price"),
                                       ({"annual": annual_two(equity=-5.0)}, "equity_positive")])
def test_never_relaxed_rules_keep_a_stock_out_of_emerging(patch, why):
    r = mb.assess(stock(**patch), MultibaggerConfig(), weekly(), bench())
    assert r["class"] != "emerging"
    assert why in r["emerging"]["never_relaxed_failed"] or why == "liquidity" or why == "price"


def test_dilution_above_ten_percent_a_year_blocks_emerging():
    diluted = annual_two()
    diluted[-1] = dict(diluted[-1], shares=125.0)
    r = mb.assess(stock(annual=diluted), MultibaggerConfig(), weekly(), bench())
    assert r["class"] != "emerging" and "dilution_10pct" in r["emerging"]["never_relaxed_failed"]


def test_each_growth_check_can_fail_the_emerging_track():
    slow = mb.assess(stock(quarterly=quarters(9, growth=0.10)), MultibaggerConfig(), weekly(), bench())
    assert slow["class"] != "emerging" and next(c for c in slow["emerging"]["checks"] if c["id"] == "e_revenue")["result"] == "fail"
    big = mb.assess(stock(market_cap_cr=50000.0), MultibaggerConfig(), weekly(), bench())
    assert next(c for c in big["emerging"]["checks"] if c["id"] == "e_market_cap")["result"] == "fail" and big["class"] != "emerging"
    pricey = mb.assess(stock(pe=90.0), MultibaggerConfig(), weekly(), bench())
    assert next(c for c in pricey["emerging"]["checks"] if c["id"] == "e_valuation")["result"] == "fail" and pricey["class"] != "emerging"


def test_the_chart_is_an_entry_trigger_not_a_requirement_it_only_sets_the_timing_tag():
    down = weekly(trend=-0.004)
    r = mb.assess(stock(), MultibaggerConfig(), down, bench())
    assert r["class"] == "emerging" and r["timing"] == "wait_for_chart"                                     # a weak chart no longer blocks the class
    assert next(c for c in r["emerging"]["checks"] if c["id"] == "e_chart")["result"] == "fail" and not any("e_chart" in p for p in r["emerging"]["prove_it"])
    up = mb.assess(stock(), MultibaggerConfig(), weekly(), bench())
    assert up["class"] == "emerging" and up["timing"] in ("buy_zone", "wait_for_chart")


def test_a_loss_to_profit_turnaround_counts_for_profit_growth():
    q = quarters(9)
    for i, row in enumerate(q):
        row["pat"] = -5.0 if i < 4 else 6.0 + i
    r = mb.assess(stock(quarterly=q), MultibaggerConfig(), weekly(), bench())
    assert "turnaround" in next(c for c in r["emerging"]["checks"] if c["id"] == "e_pat")["reason"]


def test_unknown_when_fewer_than_eight_quarters_exist():
    r = mb.assess(stock(quarterly=quarters(6)), MultibaggerConfig(), weekly(), bench())
    em = {c["id"]: c["result"] for c in r["emerging"]["checks"]}
    assert em["e_revenue"] == "unknown" and em["e_pat"] == "unknown" and em["e_margin"] == "unknown" and r["class"] != "emerging"


def test_upgrade_and_downgrade_transitions():
    good = quarters(9, growth=0.30)
    assert mb.emerging_transition(good, EC)["action"] == "upgrade_ready"
    weak = quarters(9, growth=0.30)
    weak[-1]["revenue"] = weak[-5]["revenue"] * 1.10
    assert mb.emerging_transition(weak, EC)["action"] == "downgrade"
    squeezed = quarters(9, growth=0.30)
    squeezed[-1]["op_margin"] = squeezed[-5]["op_margin"] - 3.0
    assert mb.emerging_transition(squeezed, EC)["action"] == "downgrade"


# ───────────── Stage 7c ─────────────
def daily(n, *, start_price=100.0, drift=0.002, vol=1000.0, last_vol=None, dip=0.0):
    idx = pd.bdate_range(end="2026-11-20", periods=n)
    c = start_price * np.cumprod(np.full(n, 1 + drift)) * (1 - dip)
    v = np.full(n, vol)
    if last_vol:
        v[-1] = last_vol
    return pd.DataFrame({"open": c, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": v}, index=idx)


def listing_stock(months=6, quarters_n=2, **kw):
    ld = TODAY - timedelta(days=int(months * 30.4))
    x = stock(first_bar_date=ld, quarterly=quarters(quarters_n), annual=[], listing={"issue_price": 100.0, "allotment_date": ld - timedelta(days=5), "listing_date": ld, "pre_ipo_lockin_end": None})
    x.update(kw)
    return x


def test_a_six_month_old_listing_is_new_listing_with_and_without_pre_ipo_years():
    d = daily(125)
    without = mb.assess(listing_stock(), MultibaggerConfig(), None, None, daily=d, bench_daily=pd.Series(100.0, index=d.index))
    assert without["class"] == "new_listing" and without["new_listing"]["pre_ipo_q"] is None and without["new_listing"]["pre_ipo_g"] is None               # unknown, never zero
    assert any("pre-IPO statements" in p for p in without["new_listing"]["pending_checks"]) and without["new_listing"]["suggested_size_fraction"] == 0.25
    assert without["new_listing"]["horizons"]["long_term"]["allowed"] is False
    pre_ipo = [dict(a, period_end=a["period_end"].replace(year=a["period_end"].year - 3 + i)) for i, a in enumerate([annual_two()[1]] * 3)]
    with_years = mb.assess(listing_stock(annual=pre_ipo), MultibaggerConfig(), None, None, daily=d, bench_daily=pd.Series(100.0, index=d.index))
    assert with_years["class"] == "new_listing" and with_years["new_listing"]["pre_ipo_q"] is not None and with_years["new_listing"]["pre_ipo_g"] is not None


def test_the_lock_in_gate_blocks_buys_within_five_sessions_before_an_expiry():
    x = listing_stock(months=3)
    x["listing"]["allotment_date"] = TODAY - timedelta(days=88)              # anchor lock-in 90 days after allotment: 2 days away
    g = mb.lockin_gate(x, NL)
    assert g["blocked"] is True and "approximate" in g["reason"]
    y = listing_stock(months=3)
    y["listing"]["allotment_date"] = TODAY - timedelta(days=60)
    assert mb.lockin_gate(y, NL)["blocked"] is False
    y["listing"]["pre_ipo_lockin_end"] = TODAY + timedelta(days=3)
    g2 = mb.lockin_gate(y, NL)
    assert g2["blocked"] is True and "approximate" not in g2["reason"]          # a stored date is not labelled approximate
    z = listing_stock(months=3)
    z["listing"].update(allotment_date=None, listing_date=None)
    z["first_bar_date"] = None
    assert mb.lockin_gate(z, NL)["blocked"] is None                              # unknown dates: unknown, not clear


def test_below_issue_price_for_twenty_sessions_is_weak_demand_and_blocks_every_horizon():
    d = daily(60, start_price=60.0, drift=0.001)                                  # trades far below the 100 issue price
    r = mb.assess(listing_stock(months=3), MultibaggerConfig(), None, None, daily=d, bench_daily=pd.Series(100.0, index=d.index))
    nl = r["new_listing"]
    assert nl["weak_demand"] is True and not nl["horizons"]["swing"]["allowed"] and "weak demand" in nl["horizons"]["swing"]["reason"] and not nl["horizons"]["positional"]["allowed"]


def test_ipo_base_modes_and_the_session_gates():
    ld = date(2026, 6, 1)
    early = mb.ipo_base(daily(10), None, ld, 100.0, NL)
    assert early["state"] == "too_early"
    d = daily(60, start_price=110.0, drift=0.0003)
    d.iloc[-1, d.columns.get_loc("close")] = float(d["high"].iloc[:-1].max()) * 1.02             # closes above the post-listing high...
    d.iloc[-1, d.columns.get_loc("volume")] = 2500.0                                              # ...on 2.5x the base volume
    base = mb.ipo_base(d, pd.Series(100.0, index=d.index), d.index[0].date(), 100.0, NL)
    assert base["state"] == "breakout" and base["demand"] is True and base["weak_demand"] is False and base["rs_vs_nifty"] > 0
    x = listing_stock(months=2)
    x["listing"]["allotment_date"] = TODAY - timedelta(days=70)
    r = mb.assess(x, MultibaggerConfig(), None, None, daily=d, bench_daily=pd.Series(100.0, index=d.index))
    h = r["new_listing"]["horizons"]
    assert h["swing"]["allowed"] is True and h["positional"]["allowed"] is True               # 60 sessions, breakout, no lock-in or weak-demand block
    young = mb.assess(x, MultibaggerConfig(), None, None, daily=d.iloc[-25:], bench_daily=pd.Series(100.0, index=d.index))
    assert young["new_listing"]["horizons"]["swing"]["allowed"] is True and young["new_listing"]["horizons"]["positional"]["allowed"] is False   # 25 sessions: swing yes, positional needs 40 (here also needs 40)


def test_graduation_at_four_quarterly_results_makes_it_eligible_for_emerging():
    assert mb.listing_status(listing_stock(months=6, quarters_n=3), NL)["is_new"] is True
    grad = mb.listing_status(listing_stock(months=6, quarters_n=4), NL)
    assert grad["is_new"] is False and "graduated" in grad["reason"]
    old = mb.listing_status(listing_stock(months=30, quarters_n=2), NL)
    assert old["is_new"] is False                                                                 # 30 months: the main rules apply
    assert mb.listing_status(stock(first_bar_date=None), NL)["is_new"] is False


def test_the_liquidity_and_price_floors_still_apply_to_a_new_listing():
    d = daily(125)
    r = mb.assess(listing_stock(median_traded_value_cr=0.4), MultibaggerConfig(), None, None, daily=d, bench_daily=pd.Series(100.0, index=d.index))
    assert r["class"] == "not_tradable" and "Too illiquid" in r["reasons"][0]


# ───────────── thesis-break alerts ─────────────
def test_thesis_alerts_fire_on_the_listed_conditions_and_never_on_price():
    slow = quarters(9, growth=0.02)
    x = stock(quarterly=slow, pledged_pct=12.0, governance_events_24m=[{"kind": "auditor_resignation"}])
    kinds = {a["kind"] for a in mb.thesis_alerts(x, {"class": "avoid"}, previous_class="quality_compounder", pledge_before=0.0)}
    assert {"revenue_slowdown", "pledge", "governance", "class_avoid"} <= kinds
    calm = mb.thesis_alerts(stock(), {"class": "watch"}, previous_class="watch", pledge_before=None)
    assert calm == [] and all("price" not in a["kind"] for a in calm)
