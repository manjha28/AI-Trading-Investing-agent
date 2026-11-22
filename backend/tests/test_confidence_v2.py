"""Nexus v2 Phase 2 (flag confidence_v2_enabled): factor rank and setup grade.

TEST DATA: the small price/volume panels below are hand-built to isolate one property each; they are not market data and never reach runtime paths.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services import factor_rank as fr
from app.services import research_confidence as rc
from app.services import setup_grade as sg


# ───────────── factor construction ─────────────
def _panel(n_days: int = 320) -> tuple[pd.DataFrame, pd.DataFrame]:
    idx = pd.date_range("2025-01-01", periods=n_days, freq="B")
    rng = np.random.default_rng(3)
    cols = {}
    vols = {}
    for k in range(12):
        drift = (k - 6) * 0.0004
        px = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n_days)))
        cols[f"S{k}"], vols[f"S{k}"] = px, rng.integers(900, 1100, n_days).astype(float)
    return pd.DataFrame(cols, index=idx), pd.DataFrame(vols, index=idx)


def _bench(close: pd.DataFrame) -> pd.Series:
    return close.mean(axis=1)


def test_volume_is_signed_by_price_direction():
    close, vol = _panel()
    close["SELL"] = close["S0"]
    vol["SELL"] = vol["S0"]
    close["BUY"] = close["S0"]
    vol["BUY"] = vol["S0"]
    last = close.index[-20:]
    d = close["SELL"].diff()
    vol.loc[last, "SELL"] = np.where(d.loc[last] < 0, 5000.0, 100.0)              # heavy volume on DOWN days: this is selling
    vol.loc[last, "BUY"] = np.where(d.loc[last] > 0, 5000.0, 100.0)               # heavy volume on UP days: this is buying
    raw = fr.raw_factors(close, vol, _bench(close))
    assert raw.loc["SELL", "vol_flow"] < 0 < raw.loc["BUY", "vol_flow"]


def test_reversal_only_applies_while_the_200_day_average_is_rising():
    close, vol = _panel()
    close["DOWN"] = np.linspace(200, 100, len(close))                              # a falling 200-day average: a drop is not "oversold"
    close["UP"] = np.linspace(100, 200, len(close))
    close.loc[close.index[-5:], "UP"] *= 0.95                                      # a short dip inside an uptrend
    vol["DOWN"], vol["UP"] = 1000.0, 1000.0
    tab = fr.rank_table(close, vol, _bench(close))
    assert pd.isna(tab["swing"].loc["DOWN", "reversal"]) and not pd.isna(tab["swing"].loc["UP", "reversal"])
    assert "reversal" not in tab["positional"].columns and "reversal" not in tab["long_term"].columns


def test_factor_sets_differ_by_horizon_and_momentum_is_not_in_swing():
    close, vol = _panel()
    tab = fr.rank_table(close, vol, _bench(close))
    raw = tab["raw"]
    p = {k: raw[k].rank(pct=True) * 100 for k in ("lowvol", "near_52w_high", "mom_12_1")}
    swing_expect = pd.concat([p["lowvol"], p["near_52w_high"]], axis=1).mean(axis=1)
    pos_expect = pd.concat([p["lowvol"], p["near_52w_high"], p["mom_12_1"]], axis=1).mean(axis=1)
    pd.testing.assert_series_equal(tab["swing"]["trend_quality"], swing_expect, check_names=False)
    pd.testing.assert_series_equal(tab["positional"]["trend_quality"], pos_expect, check_names=False)
    assert fr.GROUPS["swing"] == ("trend_quality", "relative_strength", "reversal", "volume_flow")


def test_rank_is_nth_of_m_and_composite_is_the_published_weighted_mean_of_groups():
    close, vol = _panel()
    tab = fr.rank_table(close, vol, _bench(close))
    t = tab["positional"]
    assert int(t["of"].iloc[0]) == 12 and t["rank"].min() == 1 and t["rank"].max() == 12
    w = fr.GROUP_WEIGHTS["positional"]
    expected = (t["trend_quality"] * w["trend_quality"] + t["relative_strength"] * w["relative_strength"]) / (w["trend_quality"] + w["relative_strength"])
    pd.testing.assert_series_equal(t["composite"], expected, check_names=False)      # transparent: the composite is the weighted mean of the displayed groups, not a percentile of percentiles
    assert w["volume_flow"] == 0.0 and "volume_flow" in t.columns                    # shown for context, weight 0 because its rank IC was negative
    best = t["composite"].idxmax()
    assert t.loc[best, "rank"] == 1 and t.loc[best, "percentile"] == 100.0


def test_swing_composite_renormalises_when_the_reversal_group_is_not_applicable():
    close, vol = _panel()
    close["DOWN"] = np.linspace(200, 100, len(close))
    vol["DOWN"] = 1000.0
    t = fr.rank_table(close, vol, _bench(close))["swing"]
    w = fr.GROUP_WEIGHTS["swing"]
    assert pd.isna(t.loc["DOWN", "reversal"]) and not pd.isna(t.loc["DOWN", "composite"])
    expected = (t.loc["DOWN", "trend_quality"] * w["trend_quality"] + t.loc["DOWN", "relative_strength"] * w["relative_strength"]) / (w["trend_quality"] + w["relative_strength"])
    assert t.loc["DOWN", "composite"] == pytest.approx(expected)


def test_relative_strength_needs_the_benchmark_and_missing_data_is_not_neutral():
    close, vol = _panel()
    tab = fr.rank_table(close, vol, None)
    assert tab["positional"]["composite"].isna().all()                               # no NIFTY history: no rank, never a middle value


# ───────────── setup grade ─────────────
def _card(**over):
    base = {"status": "available", "direction": "BULLISH", "reference_close": "100.00", "planned_entry": "99.00", "stop": "92.00", "stop_percent": 7.07, "stop_method": "STRUCTURAL", "risk_atr": 1.6,
            "entry_zone": {"low": "98.00", "high": "101.00"}, "entry_status": "ENTER_NOW", "targets": [{"label": "T1", "price": "112.00", "risk_reward": 1.86}],
            "resistance_levels": [], "v2": None}
    base.update(over)
    return base


FACTOR_OK = {"inputs": {"rs_20": 0.02, "rs_60": 0.05}}


def test_grade_a_when_every_check_passes():
    g = sg.grade("positional", _card(), FACTOR_OK)
    assert g["grade"] == "A" and g["failed_vetoes"] == [] and g["failed_soft"] == []
    assert any(c["key"] == "liquidity_events" and c["result"] == "not_evaluated" for c in g["checks"])    # Phase 3: reported, not silently passed or failed


@pytest.mark.parametrize("over,veto", [
    ({"direction": "NEUTRAL"}, "direction"), ({"direction": "BEARISH"}, "direction"),
    ({"targets": [{"label": "T1", "price": "110.00", "risk_reward": 1.2}]}, "t1"),
    ({"targets": [{"label": "T1", "price": "99.00", "risk_reward": 1.9}]}, "t1"),
    ({"stop_percent": 14.0}, "stop"), ({"stop_method": "ATR"}, "stop"),
    ({"resistance_levels": [{"low": "105.00", "high": "106.00", "strength": 2.5, "labels": ["x"]}]}, "headroom"),
])
def test_each_veto_makes_the_horizon_not_ready(over, veto):
    g = sg.grade("positional", _card(**over), FACTOR_OK)
    assert g["grade"] is None and g["label"] == "Not ready" and veto in g["failed_vetoes"]


def test_swing_stop_must_be_at_least_one_atr_and_long_term_cap_is_20_percent():
    assert sg.grade("swing", _card(risk_atr=0.8), FACTOR_OK)["failed_vetoes"] == ["stop"]
    assert sg.grade("long_term", _card(stop_percent=18.0), FACTOR_OK)["grade"] == "A"
    assert sg.grade("positional", _card(stop_percent=18.0), FACTOR_OK)["grade"] is None


def test_soft_checks_set_grades_b_and_c_and_unknown_counts_as_failed():
    weak_rs = {"inputs": {"rs_20": -0.03, "rs_60": 0.02}}
    assert sg.grade("positional", _card(), weak_rs)["grade"] == "B"
    assert sg.grade("positional", _card(), {"inputs": {}})["grade"] == "B"                          # unknown relative strength is not a pass
    wait_inside = _card(entry_status="WAIT_FOR_ENTRY")                                                # v1: 'wait' with the close inside the band (HONASA)
    g = sg.grade("positional", wait_inside, weak_rs)
    assert g["grade"] == "C" and set(g["failed_soft"]) == {"rs", "status"}


def test_a_wait_band_above_the_market_is_a_status_mismatch_on_v1_cards():                         # LALPATHLAB
    card = _card(entry_status="WAIT_FOR_ENTRY", entry_zone={"low": "104.00", "high": "108.00"}, planned_entry="106.00", stop="99.00", stop_percent=6.6, targets=[{"label": "T1", "price": "120.00", "risk_reward": 2.0}])
    assert "status" in sg.grade("positional", card, FACTOR_OK)["failed_soft"]


def test_high_only_for_grade_a_and_rank_percentile_at_least_70():
    a = sg.grade("positional", _card(), FACTOR_OK)
    b = sg.grade("positional", _card(), {"inputs": {}})
    assert sg.combined_label(a, 70.0) == "High" and sg.combined_label(a, 69.9) == "A"
    assert sg.combined_label(b, 99.0) == "B"                                                           # a top rank never lifts a lower grade
    assert sg.combined_label(sg.grade("positional", _card(direction="BEARISH"), FACTOR_OK), 99.0) == "Not ready"


# ───────────── confidence builder ─────────────
FACTOR_SECTION = {"status": "available", "as_of": "2026-11-20", "universe_size": 249, "inputs": {"rs_20": 0.02, "rs_60": 0.05},
                  "horizons": {"positional": {"status": "available", "rank": 12, "of": 249, "composite": 81.2, "percentile": 95.2,
                                              "groups": [{"key": "trend_quality", "label": "t", "percentile": 80.0, "weight": 0.75}, {"key": "volume_flow", "label": "v", "percentile": 70.0, "weight": 0.0}, {"key": "relative_strength", "label": "r", "percentile": 90.0, "weight": 0.25}]}}}
HORIZON = {"recommendation": "BUY", "score": 80.0, "confidence": 70.0}


def test_flag_off_keeps_the_legacy_badge(monkeypatch):
    monkeypatch.setattr(settings, "confidence_v2_enabled", False)
    c = rc.buy_confidence(HORIZON, _card(), 1, 65.0, 4.5, None, horizon_key="positional", factor=FACTOR_SECTION)
    assert c["basis"] == "legacy_formula" and c["band"] in ("High", "Medium", "Low")


def test_flag_on_replaces_the_badge_with_rank_and_grade(monkeypatch):
    monkeypatch.setattr(settings, "confidence_v2_enabled", True)
    c = rc.buy_confidence(HORIZON, _card(), 1, 65.0, 4.5, None, horizon_key="positional", factor=FACTOR_SECTION)
    assert c["kind"] == "factor_v2" and c["band"] == "High" and c["factor_rank"]["text"] == "Factor rank 12th of 249 (factor screen)"
    assert "confidence" not in c["note"].lower().replace("not a confidence", "")
    low = rc.buy_confidence(HORIZON, _card(direction="BEARISH"), 1, 65.0, 4.5, None, horizon_key="positional", factor=FACTOR_SECTION)
    assert low["band"] == "Not ready" and low["grade"] is None and low["score"] < c["score"]
    unranked = rc.buy_confidence(HORIZON, _card(), 1, 65.0, 4.5, None, horizon_key="positional", factor={"status": "unavailable"})
    assert unranked["factor_rank"] is None and unranked["band"] in ("B", "C")                          # no rank and unknown RS: never "High"


def test_sort_score_orders_by_grade_first_then_factor_rank(monkeypatch):
    monkeypatch.setattr(settings, "confidence_v2_enabled", True)
    def conf(pct, card):
        f = {**FACTOR_SECTION, "horizons": {"positional": {**FACTOR_SECTION["horizons"]["positional"], "percentile": pct}}}
        return rc.buy_confidence(HORIZON, card, 1, 65.0, 4.5, None, horizon_key="positional", factor=f)["score"]
    assert conf(40.0, _card()) > conf(99.0, _card(entry_status="WAIT_FOR_ENTRY"))                      # grade A at rank 40 beats grade B at rank 99
    assert conf(90.0, _card()) > conf(60.0, _card())


def test_ordinals():
    assert [rc._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 112)] == ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "112th"]


# ───────────── the measured line (positional and long-term, grade A, >= 100 past trades per horizon) ─────────────
TABLE = {"min_trades": 100, "horizons": {"positional": {"A": {"n": 119, "t1_before_stop": 0.34, "mean_net_r": 0.41}}, "long_term": {"A": {"n": 99, "t1_before_stop": 0.30, "mean_net_r": 0.20}},
                                          "swing": {"A": {"n": 500, "t1_before_stop": 0.5, "mean_net_r": 0.5}}}}


def test_measured_line_only_for_grade_a_on_positional_and_long_term_with_enough_trades():
    assert sg.measured_line("positional", "A", TABLE) == "T1 before stop in 34% of 119 past setups, average +0.41R after costs"
    assert sg.measured_line("long_term", "A", TABLE) is None                    # 99 trades: one short of the bar for that horizon
    assert sg.measured_line("swing", "A", TABLE) is None                        # never on swing cards, whatever the count
    assert sg.measured_line("positional", "B", TABLE) is None and sg.measured_line("positional", None, TABLE) is None
    assert sg.measured_line("positional", "A", {"min_trades": 100, "horizons": {}}) is None
    assert sg.measured_line("positional", "A", {}) is None                      # missing calibration: grade only


def test_the_shipped_calibration_has_no_horizon_with_100_grade_a_trades_yet_so_no_line_is_shown():
    cal = sg.load_calibration()
    assert cal["min_trades"] == 100 and cal["horizons"]["positional"]["A"]["n"] < 100 and cal["horizons"]["long_term"]["A"]["n"] < 100
    assert sg.measured_line("positional", "A") is None and sg.measured_line("long_term", "A") is None


def test_the_line_reaches_the_card_only_through_the_builder(monkeypatch):
    monkeypatch.setattr(settings, "confidence_v2_enabled", True)
    monkeypatch.setattr(sg, "load_calibration", lambda path=None: TABLE)
    c = rc.buy_confidence(HORIZON, _card(), 1, 65.0, 4.5, None, horizon_key="positional", factor=FACTOR_SECTION)
    assert c["grade"] == "A" and c["measured_line"] == "T1 before stop in 34% of 119 past setups, average +0.41R after costs"
    b = rc.buy_confidence(HORIZON, _card(entry_status="WAIT_FOR_ENTRY"), 1, 65.0, 4.5, None, horizon_key="positional", factor=FACTOR_SECTION)
    assert b["grade"] == "B" and b["measured_line"] is None                     # only grade A carries a line
