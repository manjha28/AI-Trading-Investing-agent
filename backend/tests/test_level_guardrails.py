"""B04 Part A: deterministic level guardrails, reproduced from point-in-time data.

The five fixtures in tests/fixtures/level_guardrails/ are TEST DATA: snapshots of real stored Dhan daily candles whose last completed candle is 2026-11-20 (the chart
audit of the 22 Nov 2026 list). They are read only by these tests. Small hand-built frames below are likewise test data for isolated rule checks.
Each audited defect has a test showing it exists with the flag OFF (documenting the old behaviour) and is handled with the flag ON.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.services import level_guardrails as lg
from app.services import technical_engine as te

FIX = Path(__file__).parent / "fixtures" / "level_guardrails"
AS_OF = pd.Timestamp("2026-11-22")


def _frame(symbol: str) -> pd.DataFrame:
    raw = json.loads((FIX / f"{symbol}.json").read_text())
    return te.frame_from_bars([(date.fromisoformat(b[0]), *b[1:]) for b in raw["bars"]])


def _run(symbol: str, on: bool) -> dict:
    return te.analyze(_frame(symbol), None, cfg=te.EngineConfig(guardrails=on), as_of=AS_OF)["horizons"]


def _prices(card: dict) -> list[float]:
    return [float(t["price"]) for t in card["targets"]]


def _codes(card: dict) -> set[str]:
    return {f["code"] for f in (card.get("guardrails") or {}).get("flags", [])}


# ───────────── default and isolation ─────────────
def test_flag_defaults_off_and_off_output_has_no_guardrail_section():
    assert te.EngineConfig().guardrails is False
    off = _run("GROWW", False)
    assert all("guardrails" not in card for card in off.values())


# ───────────── 1. targets above price (GROWW, IPCALAB) ─────────────
@pytest.mark.parametrize("symbol,horizon", [("GROWW", "positional"), ("IPCALAB", "positional")])
def test_targets_must_be_above_last_close(symbol, horizon):
    close = float(_frame(symbol)["close"].iloc[-1])
    assert min(_prices(_run(symbol, False)[horizon])) < close                 # the audited defect
    card = _run(symbol, True)[horizon]
    assert "TARGET_BELOW_PRICE" in _codes(card)
    assert all(p > close for p in _prices(card))
    assert _prices(card) == sorted(set(_prices(card)))
    assert all(p > float(card["entry"]["high"]) for p in _prices(card))


# ───────────── 2. stale historical high (ADANIENSOL) ─────────────
def test_stale_2022_high_is_not_a_target():
    close = float(_frame("ADANIENSOL")["close"].iloc[-1])
    off = _run("ADANIENSOL", False)
    assert max(_prices(off["positional"])) > 2.5 * close                        # the 2022 high used as a target
    on = _run("ADANIENSOL", True)
    for horizon in ("positional", "long_term"):
        assert max(_prices(on[horizon])) < 2.2 * close, horizon
    assert any(d["code"] == "STALE_LEVEL" for d in on["long_term"]["guardrails"]["dropped_levels"])


def test_52_week_high_comes_from_252_daily_bars_not_the_primary_frame():
    daily = _frame("ADANIENSOL")
    on = te.analyze(daily, None, cfg=te.EngineConfig(guardrails=True), as_of=AS_OF)["horizons"]["positional"]
    labels = [lv for lv in on["resistance_levels"] if "52-week high" in lv["labels"]]
    assert not labels or float(labels[0]["high"]) <= float(daily["high"].iloc[-252:].max()) + 0.05


def test_stale_rule_unit(  # hand-built test data: a high of 100, then a crash to 40, price now 55
):
    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    close = [100.0] * 20 + [40.0 + (i % 5) for i in range(260)] + [55.0] * 20
    df = pd.DataFrame({"open": close, "high": [c + 1 for c in close], "low": [c - 1 for c in close], "close": close, "volume": 1.0}, index=idx)
    assert lg.is_stale_high(df, 101.0, lg.GuardConfig())
    assert not lg.is_stale_high(df, 56.0, lg.GuardConfig())


# ───────────── 3. stop too wide (JAMNAAUTO) ─────────────
def test_wide_stop_is_flagged_and_enter_now_downgraded():
    off, on = _run("JAMNAAUTO", False), _run("JAMNAAUTO", True)
    assert off["positional"]["stop_loss"]["percent"] > 20                      # audited: about 24% away
    assert "STOP_TOO_WIDE" in _codes(on["positional"])
    assert on["positional"]["stop_loss"]["price"] == off["positional"]["stop_loss"]["price"]      # the stop itself is not moved
    assert off["long_term"]["entry_status"] == "ENTER_NOW" and on["long_term"]["entry_status"] == "WAIT_FOR_ENTRY"


# ───────────── 4. overhead resistance -> WAIT (BHARTIARTL) ─────────────
def test_entry_directly_under_52_week_high_is_not_buyable_now():
    off, on = _run("BHARTIARTL", False), _run("BHARTIARTL", True)
    assert off["positional"]["entry_status"] == "ENTER_NOW"
    assert on["positional"]["entry_status"] == "WAIT_FOR_ENTRY"
    assert "OVERHEAD_RESISTANCE" in _codes(on["positional"])
    assert any("52-week high" in f["detail"] for f in on["positional"]["guardrails"]["flags"])


# ───────────── 5. repeated rejection (JAMNAAUTO) ─────────────
def test_repeated_rejection_zone_is_resistance_not_breakout():
    on = _run("JAMNAAUTO", True)
    assert "REPEATED_REJECTION" in _codes(on["positional"]) or "REPEATED_REJECTION" in _codes(on["long_term"])
    zones = on["long_term"]["guardrails"]["rejection_zones"]
    assert zones and max(z["rejections"] for z in zones) >= 3


def test_rejection_count_unit():
    idx = pd.date_range("2025-01-01", periods=120, freq="B")
    close = [90.0] * 120
    high = [91.0] * 120
    for i in (20, 40, 60, 80):                                                 # four separate rejections of 100-102, none closes above
        high[i], close[i] = 101.0, 95.0
    df = pd.DataFrame({"open": close, "high": high, "low": [89.0] * 120, "close": close, "volume": 1.0}, index=idx)
    assert lg.rejection_count(df, 100.0, 102.0, 2.0, lg.GuardConfig()) == (4, False)
    df.loc[idx[100], "close"] = 103.0
    assert lg.rejection_count(df, 100.0, 102.0, 2.0, lg.GuardConfig())[1] is True


# ───────────── 6. card consistency ─────────────
def _card(**over):
    card = {"entry_status": "WAIT_FOR_ENTRY", "current_price": "100.00", "entry": {"low": "98.00", "high": "101.00", "planned_price": "99.00"}, "stop_loss": {"price": "95.00"},
            "targets": [{"label": "T1", "price": "107.00", "risk_reward": 2.0}, {"label": "T2", "price": "111.00", "risk_reward": 3.0}]}
    card.update(over)
    return card


def test_consistent_card_passes():
    assert lg.validate_card(_card(), 100.0) == []


@pytest.mark.parametrize("over,needle", [
    ({"stop_loss": {"price": "99.50"}}, "stop is not below"),
    ({"targets": [{"label": "T1", "price": "111.00", "risk_reward": 3.0}, {"label": "T2", "price": "107.00", "risk_reward": 2.0}]}, "not strictly ascending"),
    ({"targets": [{"label": "T1", "price": "100.50", "risk_reward": 0.4}]}, "not above the entry band"),
    ({"targets": [{"label": "T1", "price": "107.00", "risk_reward": 5.0}]}, "does not match"),
    ({"current_price": "104.00"}, "last completed daily close"),
])
def test_inconsistent_cards_are_caught(over, needle):
    assert any(needle in p for p in lg.validate_card(_card(**over), 100.0))


@pytest.mark.parametrize("symbol", ["GROWW", "IPCALAB", "ADANIENSOL", "JAMNAAUTO", "BHARTIARTL"])
def test_every_guarded_card_is_internally_consistent(symbol):
    for horizon, card in _run(symbol, True).items():
        if card.get("status") == "available" and card.get("entry"):
            assert "CARD_INCONSISTENT" not in _codes(card), (symbol, horizon, card["guardrails"]["flags"])
