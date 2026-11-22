"""Nexus v2 Phase 1 (flag levels_v2_enabled): regression tests, one or more per rule.

TEST DATA: tests/fixtures/level_guardrails/*.json are snapshots of real stored Dhan daily candles (last completed candle 2026-11-20). tests/fixtures/levels_v2/golden_v1.json is the
engine's v1 output on five of them, frozen BEFORE Phase 1 (proves flags off = unchanged). Small hand-built dicts/frames below are test data for isolated rule checks.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import levels_v2 as L
from app.services import research_report
from app.services import technical_engine as te
from app.services.research_levels import build_levels

FIX = Path(__file__).parent / "fixtures" / "level_guardrails"
GOLD = Path(__file__).parent / "fixtures" / "levels_v2" / "golden_v1.json"
AS_OF = pd.Timestamp("2026-11-22")
SYMS = ("GROWW", "IPCALAB", "ADANIENSOL", "JAMNAAUTO", "BHARTIARTL")


def _frame(symbol: str) -> pd.DataFrame:
    raw = json.loads((FIX / f"{symbol}.json").read_text())
    return te.frame_from_bars([(date.fromisoformat(b[0]), *b[1:]) for b in raw["bars"]])


def _v2(symbol: str) -> dict:
    return te.analyze(_frame(symbol), None, cfg=te.EngineConfig(levels_v2=True, guardrails=False), as_of=AS_OF)["horizons"]


# ───────────── flags off = v1 byte-identical ─────────────
def test_all_flags_default_off():
    from app.config import settings
    assert (settings.levels_v2_enabled, settings.confidence_v2_enabled, settings.pipeline_v2_enabled, settings.ml_overlay_enabled) == (False, False, False, False)
    assert te.EngineConfig().levels_v2 is False


@pytest.mark.parametrize("symbol", SYMS)
def test_flags_off_output_is_identical_to_frozen_v1(symbol):
    gold = json.loads(GOLD.read_text())[symbol]
    eng = te.analyze(_frame(symbol), None, as_of=AS_OF)                        # default config: every flag off
    levels = build_levels({"status": "available"}, {"horizons": {h: {"recommendation": "BUY"} for h in te.HORIZONS}}, eng)
    now = json.dumps(json.loads(json.dumps({"engine": eng, "levels": levels}, default=str)), sort_keys=True)
    assert now == json.dumps(gold, sort_keys=True)


# ───────────── 1. bearish veto ─────────────
def test_bearish_downtrend_horizon_has_no_ladder():
    gold_swing = json.loads(GOLD.read_text())["IPCALAB"]["engine"]["horizons"]["swing"]
    assert gold_swing["direction"] == "BEARISH" and gold_swing["watch_only"] is True and gold_swing["targets"]      # v1 showed a buy ladder
    card = _v2("IPCALAB")["swing"]
    assert card["selected_setup"] == "NO_VALID_SETUP" and card["entry"] is None and card["targets"] == [] and card["stop_loss"] is None
    assert "bearish_veto" in card["v2"]["rules"] and "downtrend" in card["v2"]["blocked_reason"]
    lv = build_levels({"status": "available"}, {"horizons": {"swing": {"recommendation": "BUY"}}}, {"horizons": {"swing": card}})["horizons"]["swing"]
    assert lv["status"] == "no_valid_setup" and "ladder" in lv["reason"]
    assert "watch_band" in lv                                                   # Radar-only band, never a ladder


# ───────────── 2. targets above the close ─────────────
def _unit_inputs(**over):
    ind = {"atr14": 2.0, "close": 100.0}
    scen = {"zone": [94.0, 97.0], "support_zone": {"core": 95.0, "mid": 95.0, "low": 94.0}, "reference": 95.0}
    base = dict(horizon="swing", setup="PULLBACK_ENTRY", status="WAIT_FOR_ENTRY", scen=scen, ind=ind, atr_d=2.0, zones=[], swing_lows=[94.0], buffer_atr=1.0, cfg=L.LevelsV2Config(), weekly_atr=None)
    base.update(over)
    return base


def test_blue_sky_t1_is_the_larger_of_2r_and_close_plus_2_weekly_atr():
    plan, why = L.plan(**_unit_inputs())                                       # no barrier above the close: blue sky
    assert why is None
    t1 = plan["targets"][0]
    assert t1["price"] == "101.00" and t1["risk_reward"] == 2.0 and "Blue sky, no resistance" in t1["basis"]       # 2R = 95 + 2 x 3; close 100 + margin is lower
    plan, _ = L.plan(**_unit_inputs(weekly_atr=3.0))                           # close + 2 weekly ATR = 106 beats 2R
    assert plan["targets"][0]["price"] == "106.00"
    assert all(float(t["price"]) >= 100.5 for t in plan["targets"])            # every target stays above close + 0.25 daily ATR
    assert [t["label"] for t in plan["targets"]] == sorted(t["label"] for t in plan["targets"])


def test_stale_targets_never_exceed_five_r_without_a_zone():
    plan, _ = L.plan(**_unit_inputs())
    assert max(t["risk_reward"] for t in plan["targets"]) <= 5.0


@pytest.mark.parametrize("symbol", SYMS + ("APARINDS",))
def test_every_v2_target_is_above_close_plus_margin(symbol):
    f = _frame(symbol)
    atr_d = te.atr(f, 14).iloc[-1]
    for h, c in _v2(symbol).items():
        for t in c["targets"]:
            assert float(t["price"]) >= float(f["close"].iloc[-1]) + 0.25 * atr_d - 0.05, (symbol, h, t)
            assert float(t["price"]) > float(c["entry"]["high"])


# ───────────── 3. one R definition ─────────────
def test_r_definition_and_rr_from_close_on_a_ready_card():
    card = _v2("APARINDS")["long_term"]
    assert card["v2"]["ready_to_buy"] and card["v2"]["r_definition"].startswith("R = planned entry minus stop")
    entry, stop = float(card["entry"]["planned_price"]), float(card["stop_loss"]["price"])
    for t in card["targets"]:
        assert t["risk_reward"] == pytest.approx((float(t["price"]) - entry) / (entry - stop), abs=0.03 + 0.2 / (entry - stop))
        assert "risk_reward_from_close" in t
    lv = build_levels({"status": "available"}, {"horizons": {"long_term": {"recommendation": "BUY"}}}, {"horizons": {"long_term": card}})["horizons"]["long_term"]
    assert lv["r_definition"] == card["v2"]["r_definition"] and lv["ready_to_buy"] is True


# ───────────── 4. nearest resistance not skipped; T1 >= 1.5R ─────────────
def test_t1_is_the_nearest_barrier_and_must_be_at_least_1_5r():
    zone = {"kind": "resistance", "low": 99.8, "high": 101.0, "families": ["swing", "ema"], "strength": 2.5, "labels": ["swing high", "EMA 50"], "rejections": 0}
    plan, why = L.plan(**_unit_inputs(zones=[zone]))                         # a barrier straddling the last close leaves no room for a target above it
    assert plan is None and "leaves no room above the last close" in why
    far = {**zone, "low": 112.0, "high": 113.0}
    plan, why = L.plan(**_unit_inputs(zones=[far]))
    assert why is None and plan["targets"][0]["price"] == "111.80"             # zone low minus 0.1 ATR (0.2) = 111.80
    assert plan["targets"][0]["risk_reward"] >= 1.5
    enter_now = dict(status="ENTER_NOW", scen={"zone": [98.0, 101.0], "support_zone": {"core": 99.5, "mid": 99.5, "low": 96.0}, "reference": 99.5}, swing_lows=[96.0])
    short = {**zone, "low": 106.0, "high": 107.0}
    plan, why = L.plan(**_unit_inputs(zones=[short], **enter_now))             # T1 105.80 clears the close but is only 0.97R from a planned entry of 100
    assert plan is None and "only 0.97R" in why and "(minimum 1.5R)" in why


def test_a_lone_minor_swing_high_is_not_a_barrier_but_the_52_week_high_is():
    minor = {"kind": "resistance", "low": 102.0, "high": 102.0, "families": ["swing"], "strength": 1.5, "labels": ["swing high"], "rejections": 0}
    plan, why = L.plan(**_unit_inputs(zones=[minor]))
    assert why is None and "Blue sky" in plan["targets"][0]["basis"]            # the lone minor swing high did not become T1
    plan, why = L.plan(**_unit_inputs(zones=[], extra=[(102.0, "52-week high")]))
    assert why is None and plan["targets"][0]["price"] == "101.80" and "52-week high" in plan["targets"][0]["basis"]


# ───────────── 5. headroom gate ─────────────
def test_headroom_blocks_enter_now():
    plan, _ = L.plan(**_unit_inputs(status="ENTER_NOW", scen={"zone": [98.0, 101.0], "support_zone": {"core": 99.5, "mid": 99.5, "low": 96.0}, "reference": 99.5}))
    assert plan is not None
    near = {"kind": "resistance", "low": 100.8, "high": 101.5, "families": ["swing", "ema"], "strength": 2.5, "labels": ["x"], "rejections": 0, "closed_above": False}
    g = L.gates("swing", "PULLBACK_ENTRY", "ENTER_NOW", plan, {"zone": [98.0, 101.0]}, {"close": 100.0}, [near], [], (98.0, 101.0), L.LevelsV2Config())
    assert g["status"] == "WAIT_FOR_ENTRY" and "headroom" in g["rules"] and g["ready_to_buy"] is False


# ───────────── 6. repeated rejections ─────────────
def test_repeated_rejection_zone_makes_the_stock_range_bound_and_blocks_momentum():
    z = {"kind": "resistance", "low": 148.0, "high": 156.0, "families": ["swing", "ema"], "strength": 2.5, "labels": ["x"], "rejections": 4, "closed_above": False}
    assert L.range_bound_zone([z], 137.55) is z
    assert L.range_bound_zone([{**z, "rejections": 2}], 137.55) is None
    assert L.range_bound_zone([{**z, "closed_above": True}], 137.55) is None
    scen = _v2("JAMNAAUTO")["long_term"]["scenarios"]["MOMENTUM_ENTRY"]
    assert scen["status"] == "INVALID"


def test_jamnaauto_momentum_entry_under_its_fourth_rejection_is_invalid_in_v2():
    cards = _v2("JAMNAAUTO")
    assert any("repeated_rejection" in c["v2"]["rules"] for c in cards.values())
    assert all(c["entry"] is None for c in cards.values())


# ───────────── 7. stop bounds ─────────────
def test_stop_bounds_reject_instead_of_moving_the_stop():
    wide = _unit_inputs(scen={"zone": [94.0, 97.0], "support_zone": {"core": 95.0, "mid": 95.0, "low": 80.0}, "reference": 95.0}, swing_lows=[80.0])
    plan, why = L.plan(**wide)
    assert plan is None and "wider than the 8%" in why
    tight = _unit_inputs(buffer_atr=0.1)                                       # stop = 93.8, risk 1.2 < 1 daily ATR (2.0)
    plan, why = L.plan(**tight)
    assert plan is None and "inside one day's range" in why


def test_real_wide_stops_have_no_setup():
    cards = _v2("JAMNAAUTO")
    assert cards["positional"]["entry"] is None and "wider than the 12%" in cards["positional"]["v2"]["blocked_reason"]
    assert cards["long_term"]["entry"] is None and "wider than the 20%" in cards["long_term"]["v2"]["blocked_reason"]
    assert "inside one day's range" in cards["swing"]["v2"]["blocked_reason"]


def test_every_v2_stop_is_within_bounds():
    caps = {"swing": 8.0, "positional": 12.0, "long_term": 20.0}
    for sym in SYMS + ("APARINDS",):
        for h, c in _v2(sym).items():
            if c["stop_loss"]:
                assert c["stop_loss"]["percent"] <= caps[h] and c["stop_loss"]["method"] == "STRUCTURAL"


# ───────────── 8. recency and the true 52-week high ─────────────
def test_prior_high_reference_is_the_true_52_week_high_from_daily_bars():
    f = _frame("ADANIENSOL")
    card = _v2("ADANIENSOL")["long_term"]
    assert card["v2"]["prior_high_reference"] == te._s(float(f["high"].iloc[-252:].max()))
    assert float(card["v2"]["prior_high_reference"]) < 0.6 * float(f["high"].max())      # not the 2022 high of ~4236
    assert max(float(r["high"]) for r in card["resistance_levels"]) < 0.6 * float(f["high"].max())


def test_window_edge_is_explicit():                                               # item 12: position -252 is IN, -253 is OUT
    idx = pd.date_range("2025-01-01", periods=400, freq="B")
    high = np.full(400, 100.0)
    high[-252] = 150.0
    df = pd.DataFrame({"open": high, "high": high, "low": high - 1, "close": high - 0.5, "volume": 1.0}, index=idx)
    assert L.high52(df) == 150.0
    high2 = np.full(400, 100.0)
    high2[-253] = 150.0
    df2 = df.assign(high=high2)
    assert L.high52(df2) == 100.0


# ───────────── 9. one status rule ─────────────
def _gate(status, close, band, setup="PULLBACK_ENTRY", recent_lows=(), scen=None):
    plan = {"risk": 5.0}
    return L.gates("swing", setup, status, plan, scen or {"zone": list(band)}, {"close": close}, [], list(recent_lows), band, L.LevelsV2Config())


def test_close_inside_the_band_is_in_band_even_when_momentum_is_unconfirmed():       # HONASA: close 525.40, band 519.5-531.85
    g = _gate("WAIT_FOR_ENTRY", 525.40, (519.5, 531.85))
    assert g["band_position"] == "inside" and g["ready_to_buy"] is True and "In the band" in g["entry_label"]


def test_band_above_the_market_is_a_breakout_trigger_not_a_wait_band():           # LALPATHLAB: wait band 2010-2055 above a 2006 close
    g = _gate("WAIT_FOR_ENTRY", 2006.0, (2010.3, 2054.6), scen={"zone": [2010.3, 2054.6], "trigger": 2054.6})
    assert g["entry_kind"] == "breakout_trigger" and "band_above_market" in g["rules"] and g["ready_to_buy"] is False and "confirming close" in g["entry_label"]


def test_pullback_band_under_a_broken_support_is_invalid():                       # HINDZINC: band 475-487 under a 490 support price closed below
    g = _gate("WAIT_FOR_ENTRY", 482.0, (475.0, 487.0), recent_lows=[490.0])
    assert g["status"] == "NO_VALID_SETUP" and "broken support" in g["blocked_reason"]
    assert _gate("WAIT_FOR_ENTRY", 495.0, (475.0, 487.0), recent_lows=[490.0])["status"] != "NO_VALID_SETUP"     # price back above it: not broken


def test_wait_for_pullback_label_when_price_is_above_the_band():
    g = _gate("WAIT_FOR_ENTRY", 560.0, (519.5, 531.85))
    assert g["band_position"] == "above" and g["ready_to_buy"] is False and "pullback" in g["entry_label"]


# ───────────── 10. split / bonus basis ─────────────
def _bonus_frame(raw: bool) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=60, freq="B")
    px = np.full(60, 200.0)
    if raw:
        px[30:] = 100.0                                                         # a 1:1 bonus (share count x2) halves the RAW price at the ex-date
    return pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 1000.0}, index=idx)


def test_raw_bars_are_adjusted_at_a_bonus_and_adjusted_bars_are_left_alone():
    ex = pd.date_range("2026-01-01", periods=60, freq="B")[30].date()
    fixed, audit = L.adjust_raw_bars(_bonus_frame(raw=True), [(ex, "bonus", 1.0)])
    assert audit[0]["basis"] == "raw_adjusted" and fixed["close"].iloc[0] == pytest.approx(100.0) and fixed["close"].iloc[-1] == 100.0
    assert fixed["volume"].iloc[0] == pytest.approx(2000.0)
    unchanged, audit2 = L.adjust_raw_bars(_bonus_frame(raw=False), [(ex, "bonus", 1.0)])
    assert audit2[0]["basis"] == "already_adjusted" and unchanged["close"].iloc[0] == 200.0


# ───────────── 11. every liquid stock gets a horizon read or a stated reason ─────────────
@pytest.mark.parametrize("symbol", SYMS + ("APARINDS",))
def test_every_stock_with_three_years_of_bars_has_a_read_or_a_reason(symbol):
    assert len(_frame(symbol)) >= 3 * 252 or symbol == "GROWW"
    for h, c in _v2(symbol).items():
        assert c["status"] == "available" or c.get("reason"), (symbol, h)
        if c["status"] == "available" and not c["entry"]:
            assert c["v2"]["blocked_reason"], (symbol, h)


# ───────────── 14. Ready to buy only holds in-band / enter-now cards ─────────────
def test_ready_to_buy_bucket_splits_waiting_cards():
    recs = {"swing": "BUY", "positional": "HOLD", "long_term": None}
    assert research_report.v2_bucket("buy", recs, {"swing": {"ready_to_buy": True}}) == "buy"
    assert research_report.v2_bucket("buy", recs, {"swing": {"ready_to_buy": False}}) == "setup_waiting"
    assert research_report.v2_bucket("radar", recs, {}) == "radar"
    assert "setup_waiting" in research_report.BUCKETS_V2 and "setup_waiting" not in research_report.BUCKETS


# ───────────── precise definition of a "major swing high" barrier ─────────────
def _bars(close, high=None, low=None):
    idx = pd.date_range("2025-01-01", periods=len(close), freq="B")
    c = np.array(close, float)
    return pd.DataFrame({"open": c, "high": np.array(high, float) if high is not None else c + 1, "low": np.array(low, float) if low is not None else c - 1, "close": c, "volume": 1.0}, index=idx)


def test_major_swing_high_needs_a_fall_or_two_rejections_and_must_be_unbroken():
    cfg = L.LevelsV2Config()
    atr = 2.0
    # pivot at index 20 (high 120), then a 20-point fall (>= 2 ATR = 4 and >= 8%): a barrier
    close = [100.0] * 20 + [119.0] + [100.0] * 40
    high = [101.0] * 20 + [120.0] + [101.0] * 40
    df = _bars(close, high)
    assert L.qualify_swing_highs([(20, 120.0)], df, atr, cfg) == [(120.0, "major swing high")]
    # a pivot followed by a fall of only 1.5 points (< 2 ATR, < 8%) and no rejections: not a barrier
    low = [99.0] * 20 + [118.5] + [118.5] * 40
    flat = _bars([119.0] * 61, [119.0] * 20 + [120.0] + [119.0] * 40, low)
    assert L.qualify_swing_highs([(20, 120.0)], flat, atr, cfg) == []
    # same small fall but price was rejected at 120 twice (reached within 0.25 ATR, closed below, 5+ bars apart): a barrier
    high2 = list(flat["high"]); close2 = list(flat["close"])
    for j in (30, 40):
        high2[j], close2[j] = 119.8, 119.0
    twice = _bars(close2, high2, low)
    assert L.qualify_swing_highs([(20, 120.0)], twice, atr, cfg) == [(120.0, "major swing high")]
    # broken: a close 0.5 ATR (1.0) or more above the pivot ends it, even though it fell 20 afterwards
    close3 = list(close); close3[30] = 121.0
    assert L.qualify_swing_highs([(20, 120.0)], _bars(close3, high), atr, cfg) == []
    close4 = list(close); close4[30] = 120.9                                   # only 0.9 above (< 1.0): still a wall
    assert L.qualify_swing_highs([(20, 120.0)], _bars(close4, high), atr, cfg) == [(120.0, "major swing high")]


def test_pivot_widths_are_10_daily_5_weekly_3_monthly_and_a_pivot_with_no_later_bars_is_ignored():
    assert L.LevelsV2Config().major_swing_k == {"D": 10, "W": 5, "M": 3}
    df = _bars([100.0] * 30)
    assert L.qualify_swing_highs([(29, 101.0)], df, 2.0, L.LevelsV2Config()) == []
