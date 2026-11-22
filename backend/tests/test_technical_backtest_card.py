"""Full-card replay (mode="card"): BUY + levels exactly as the card shows them. Test data: a stubbed engine card and hand-built bars, so each fill/exit case is exact; plus a run on a real candle fixture."""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.services import technical_backtest as tb
from app.services import technical_engine as te


def _df(rows):
    idx = pd.bdate_range("2026-01-05", periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1000.0)


def _card(status, planned=100.0, stop=95.0, t1=110.0):
    return {"status": "available", "direction": "BULLISH", "entry_status": status, "market_regime": "UPTREND", "selected_setup": "PULLBACK_ENTRY", "entry": {"low": str(stop + 1), "high": str(planned + 1), "planned_price": str(planned)},
            "stop_loss": {"price": str(stop)}, "targets": [{"price": str(t1)}], "atr": {"atr14": 2.0}}


def _run(monkeypatch, rows, card, horizon="swing"):
    monkeypatch.setattr(te, "analyze_horizon", lambda *a, **k: card)
    return tb.backtest(_df(rows), horizon, mode="card", step=500, warmup=0, cost_pct=0.0)


def test_simulate_card_rules():
    sim = tb.simulate_card
    # ENTER_NOW: next open, T1 on the next bar
    r = sim("ENTER_NOW", 100, 95, 110, [101, 103], [104, 111], [100, 102], [103, 110], 15)
    assert r["outcome"] == "t1" and r["entry"] == 101 and r["k_out"] == 1 and r["exit"] == 110
    # stop before T1 when one bar reaches both; a gap through the stop fills at the open
    assert sim("ENTER_NOW", 100, 95, 110, [101], [112], [94], [100], 15)["exit"] == 95
    assert sim("ENTER_NOW", 100, 95, 110, [101, 92], [102, 93], [100, 90], [101, 91], 15)["exit"] == 92
    # waiting order: gap below the limit fills at the open; fill-bar T1 is not credited
    assert sim("WAIT_FOR_ENTRY", 99, 90, 110, [101, 98], [102, 99], [100, 97], [101, 98], 15)["entry"] == 98
    assert sim("WAIT_FOR_ENTRY", 100, 95, 110, [101], [112], [99.5], [111], 15)["outcome"] == "pending"
    # never fills inside 5 sessions: unfilled, not a trade; order cancelled if it fills at or under the stop
    assert sim("WAIT_FOR_ENTRY", 90, 85, 110, [100] * 6, [101] * 6, [99] * 6, [100] * 6, 15) == {"outcome": "unfilled"}
    assert sim("WAIT_FOR_ENTRY", 100, 95, 110, [94], [95], [93], [94], 15)["outcome"] == "invalid"
    # time exit counts the window from the FILL, at that bar's close
    t = sim("WAIT_FOR_ENTRY", 100, 95, 110, [101, 101, 101, 101, 101, 101, 101, 101], [103] * 8, [99.5] + [99] * 7, [102] * 8, 3)
    assert t["outcome"] == "time" and t["k_in"] == 0 and t["k_out"] == 3


def test_enter_now_card_fills_at_the_next_open_and_exits_at_t1(monkeypatch):
    out = _run(monkeypatch, [(100, 100, 100, 100), (100, 104, 99, 103), (103, 111, 102, 110)] + [(110, 111, 109, 110)] * 5, _card("ENTER_NOW"))
    t = out["all"]
    assert out["mode"] == "card" and t["trades"] == 1 and out["trades"][0]["exit"] == "target" and out["trades"][0]["r"] == pytest.approx(2.0)      # (110-100)/(100-95)
    assert out["unfilled_orders"] == 0


def test_the_stop_is_checked_before_t1_on_the_same_bar(monkeypatch):
    out = _run(monkeypatch, [(100, 100, 100, 100), (100, 112, 94, 100)] + [(100, 101, 99, 100)] * 5, _card("ENTER_NOW"))
    assert out["trades"][0]["exit"] == "stop" and out["trades"][0]["r"] == pytest.approx(-1.0)


def test_a_waiting_card_fills_at_its_limit_and_t1_on_the_fill_bar_is_not_credited(monkeypatch):
    rows = [(100, 100, 100, 100), (101, 112, 99.5, 111), (102, 103, 94, 95)] + [(95, 96, 94, 95)] * 5      # bar 1 trades down to 100 AND reaches T1: not credited; bar 2 hits the stop
    out = _run(monkeypatch, rows, _card("WAIT_FOR_ENTRY"))
    assert out["trades"][0]["exit"] == "stop" and out["trades"][0]["r"] == pytest.approx(-1.0)


def test_an_order_that_never_fills_is_counted_not_traded(monkeypatch):
    out = _run(monkeypatch, [(100, 100, 100, 100)] + [(105, 106, 103, 105)] * 8, _card("WAIT_FOR_ENTRY"))
    assert out["all"] == {"trades": 0} and out["unfilled_orders"] == 1


def test_bearish_or_planless_cards_are_not_traded(monkeypatch):
    bearish = dict(_card("ENTER_NOW"), direction="BEARISH")
    assert _run(monkeypatch, [(100, 101, 99, 100)] * 9, bearish)["all"] == {"trades": 0}
    no_plan = dict(_card("ENTER_NOW"), entry=None)
    assert _run(monkeypatch, [(100, 101, 99, 100)] * 9, no_plan)["all"] == {"trades": 0}


def test_runs_on_real_candles_with_the_v2_rules_and_never_looks_ahead():
    raw = json.loads((Path(__file__).parent / "fixtures" / "level_guardrails" / "APARINDS.json").read_text())
    df = te.frame_from_bars([(date.fromisoformat(b[0]), *b[1:]) for b in raw["bars"]])
    out = tb.backtest(df, "positional", te.EngineConfig(levels_v2=True, guardrails=False), mode="card", step=40, warmup=700, cost_pct=0.325)
    assert out["mode"] == "card" and "unfilled_orders" in out and out["label"].startswith("Hypothetical")
    for t in out["trades"]:
        assert pd.Timestamp(t["date"]) <= df.index[-3]            # a decision needs later candles to be replayed on
