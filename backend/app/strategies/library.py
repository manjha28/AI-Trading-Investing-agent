"""Shipped registry strategies.

Selected from a research study of 28 candidate rule sets (trend, momentum, mean-reversion, breakout, volatility, volume) at 5/30/60-minute
decision timeframes, plus the 5-EMA alert-candle setup, run on stored real 1-minute Dhan bars with Dhan NSE intraday charges and slippage.
The study's headline: NO configuration cleared costs. These are the best of the rest (and the 5-EMA, which was requested), shipped with their
honest recorded results (see ``evidence.py``) as starting points to edit, not as recommendations. Everything else was discarded.
"""
from __future__ import annotations

from typing import Any

from app.services.rule_dsl import A, AND, C, I, P, cmp, ind
from app.strategies.base import ParamSpec as PS, Rules, StrategySpec
from app.strategies.evidence import EVIDENCE
from app.strategies.registry import register


def _exits(p: dict[str, Any]) -> dict[str, Any]:
    """Stop at N x ATR; either a fixed R-multiple target or a trailing stop (rr = 0 means trail instead of a target)."""
    stop = {"mode": "atr", "value": float(p["stop_atr"]), "atr_length": 14}
    if float(p["rr"]) > 0:
        return {"stop": stop, "target": {"mode": "rr", "value": float(p["rr"]), "atr_length": 14}, "trail": {"mode": "none", "value": 0, "atr_length": 14}}
    return {"stop": stop, "target": {"mode": "none", "value": 0, "atr_length": 14}, "trail": {"mode": "atr", "value": float(p["trail_atr"]), "atr_length": 14}}


STOP = PS("stop_atr", 1.5, 0.5, 4.0, "stop distance in ATR(14)")
RR = PS("rr", 3.0, 0.0, 6.0, "target as a multiple of the stop; 0 = no target, use the trailing stop instead")
TRAIL = PS("trail_atr", 3.0, 1.0, 6.0, "trailing-stop distance in ATR(14) when rr = 0")
EXIT_PARAMS = (STOP, RR, TRAIL)


def _mfi_dip(tf: str):
    def build(p: dict[str, Any]) -> Rules:
        ind_ = [ind("mfi", "mfi", {"length": int(p["mfi_length"])}, tf), ind("ema", "ema", {"length": int(p["trend_ema"])}, tf)]
        return Rules(ind_, AND(cmp(I("mfi"), "crosses_above", C(p["level"])), cmp(P("close", tf), ">", I("ema"))),
                     AND(cmp(I("mfi"), "crosses_below", C(100 - p["level"])), cmp(P("close", tf), "<", I("ema"))), max_trades_per_day=2, timeframe=tf, **_exits(p))
    return build


def _prev_day(tf: str):
    def build(p: dict[str, Any]) -> Rules:
        ind_ = [ind("pd", "prev_day", {}, tf), ind("rvol", "rvol", {"length": 20}, tf), ind("adx", "adx", {"length": 14}, tf)]
        return Rules(ind_, AND(cmp(P("close", tf), "crosses_above", I("pd", "high")), cmp(I("rvol"), ">=", C(p["rvol"])), cmp(I("adx", "adx"), ">", C(p["adx_min"]))),
                     AND(cmp(P("close", tf), "crosses_below", I("pd", "low")), cmp(I("rvol"), ">=", C(p["rvol"])), cmp(I("adx", "adx"), ">", C(p["adx_min"]))),
                     max_trades_per_day=1, timeframe=tf, **_exits(p))
    return build


def _inside_bar(tf: str):
    def build(p: dict[str, Any]) -> Rules:
        ind_ = [ind("ema", "ema", {"length": int(p["trend_ema"])}, tf), ind("rvol", "rvol", {"length": 20}, tf)]

        def cond(up: bool) -> dict[str, Any]:
            return AND(cmp(P("high", tf, 1), "<", P("high", tf, 2)), cmp(P("low", tf, 1), ">", P("low", tf, 2)),
                       cmp(P("close", tf), "crosses_above", P("high", tf, 1)) if up else cmp(P("close", tf), "crosses_below", P("low", tf, 1)),
                       cmp(I("rvol"), ">=", C(p["rvol"])), cmp(P("close", tf), ">" if up else "<", I("ema")))
        return Rules(ind_, cond(True), cond(False), max_trades_per_day=2, timeframe=tf, **_exits(p))
    return build


def _psar(tf: str):
    def build(p: dict[str, Any]) -> Rules:
        ind_ = [ind("sar", "psar", {"step": 0.02, "max_step": 0.2}, tf), ind("ema", "ema", {"length": int(p["trend_ema"])}, tf), ind("rvol", "rvol", {"length": 20}, tf)]
        return Rules(ind_, AND(cmp(I("sar", "direction"), "crosses_above", C(0)), cmp(P("close", tf), ">", I("ema")), cmp(I("rvol"), ">=", C(1.0))),
                     AND(cmp(I("sar", "direction"), "crosses_below", C(0)), cmp(P("close", tf), "<", I("ema")), cmp(I("rvol"), ">=", C(1.0))),
                     max_trades_per_day=2, timeframe=tf, **_exits(p))
    return build


def _keltner(tf: str):
    def build(p: dict[str, Any]) -> Rules:
        ind_ = [ind("kc", "keltner", {"length": 20, "multiplier": p["mult"], "atr_length": 10}, tf), ind("chop", "choppiness", {"length": 14}, tf), ind("rvol", "rvol", {"length": 20}, tf)]
        return Rules(ind_, AND(cmp(I("chop"), "<", C(p["max_chop"])), cmp(P("close", tf), "crosses_above", I("kc", "upper")), cmp(I("rvol"), ">=", C(1.1))),
                     AND(cmp(I("chop"), "<", C(p["max_chop"])), cmp(P("close", tf), "crosses_below", I("kc", "lower")), cmp(I("rvol"), ">=", C(1.1))),
                     max_trades_per_day=2, timeframe=tf, **_exits(p))
    return build


def _five_ema():
    def build(p: dict[str, Any]) -> Rules:
        minutes = int(p["timeframe_min"])
        if minutes not in (5, 15):
            raise ValueError("five_ema_alert.timeframe_min must be 5 or 15")
        tf = f"{minutes}m"
        direction = int(p["direction"])
        inds = [ind("al", "ema_alert_candle", {"length": int(p["ema"])}, tf)]
        short = cmp(I("al", "short_trigger"), "==", C(1))
        long_ = cmp(I("al", "long_trigger"), "==", C(1))
        if int(p["vwap_filter"]):
            # trade only with the day's volume-weighted bias: sells while price is below session VWAP, buys while above
            inds.append(ind("vw", "vwap", {}, tf))
            short = AND(short, cmp(P("close", tf), "<", I("vw")))
            long_ = AND(long_, cmp(P("close", tf), ">", I("vw")))
        return Rules(
            inds, long_ if direction >= 0 else None, short if direction <= 0 else None,
            # the stop is the alert candle's own high (sell) / low (buy), not an ATR; the target is a multiple of that distance
            stop={"mode": "level", "value": float(p["stop_buffer_pct"]), "atr_length": 14, "short_level": I("al", "short_stop"), "long_level": I("al", "long_stop")},
            target={"mode": "rr", "value": float(p["rr"]), "atr_length": 14}, timeframe=tf)
    return build


def _add(key, name, family, summary, rationale, params, builder):
    register(StrategySpec(key, name, family, summary, rationale, tuple(params), builder, evidence=EVIDENCE.get(key)))


_add("five_ema_alert", "5 EMA Alert Candle (Subasish Pani)", "mean_reversion",
     "Sell setup: a candle whose range stays above the 5 EMA is the alert candle; enter when a later candle breaks its low, stop at the alert candle's high, target 1:N. "
     "Buy side mirrors it below the EMA. A later alert replaces an untriggered one.",
     "Price stretched away from a fast average tends to snap back. It fails when a trend keeps price away from the EMA (the stop is hit) and when costs exceed the small edge: "
     "it trades often, so charges dominate on equity intraday.",
     (PS("ema", 5, 2, 50, "EMA length"), PS("rr", 3.0, 1.0, 6.0, "target as a multiple of the alert-candle stop distance"),
      PS("direction", -1, -1, 1, "-1 sell only (the usual setup), 0 both directions, 1 buy only"), PS("stop_buffer_pct", 0.0, 0.0, 2.0, "extra % beyond the alert candle's extreme"),
      PS("timeframe_min", 15, 5, 15, "decision candle in minutes: 5 or 15"),
      PS("vwap_filter", 0, 0, 1, "1 = only sell below / buy above session VWAP (the best variant in the study: out-of-sample win rate 45.5%, profit factor 0.98 at 1:2, still not net-positive)")),
     _five_ema())
_add("mfi_dip_30m", "MFI Dip in Trend (30m)", "volume", "Money Flow Index recovers through the oversold level inside an EMA uptrend (mirror for shorts), 30-minute candles.",
     "Volume-weighted RSI recovering from oversold in a trend suggests capital returning. Few signals, so costs bite less; still did not clear them.",
     (PS("mfi_length", 14, 5, 50, "MFI window"), PS("level", 25, 10, 40, "oversold level"), PS("trend_ema", 50, 20, 200, "trend EMA"), *EXIT_PARAMS), _mfi_dip("30m"))
_add("prev_day_break_30m", "Previous-Day Level Break (30m)", "breakout", "First strong break of the prior session's high/low on 30-minute candles with volume and ADX confirmation; one trade a day; trailing stop.",
     "Prior-day extremes are widely watched acceptance levels. Fails on false breaks and in low-volatility sessions.",
     (PS("rvol", 1.5, 1.0, 3.0, "relative volume"), PS("adx_min", 15, 5, 40, "minimum ADX"), PS("stop_atr", 2.0, 0.5, 4.0, "stop distance in ATR(14)"), PS("rr", 0.0, 0.0, 6.0, "0 = trailing stop instead of a target"),
      TRAIL), _prev_day("30m"))
_add("inside_bar_trend_30m", "Inside-Bar Breakout with Trend (30m)", "breakout", "Close breaks the high/low of an inside bar in the EMA direction on rising volume, 30-minute candles.",
     "Contraction before expansion, with the trend as the tie-breaker. Fails when the break is a fakeout.",
     (PS("trend_ema", 50, 20, 200, "trend EMA"), PS("rvol", 1.2, 1.0, 3.0, "relative volume"), *EXIT_PARAMS), _inside_bar("30m"))
_add("psar_flip_trend_30m", "Parabolic SAR Flip with Trend (30m)", "trend", "SAR direction flips with the EMA trend and average volume, 30-minute candles; trailing stop.",
     "SAR flips against the larger trend are filtered out, leaving trend continuations. Fails in chop where SAR whipsaws.",
     (PS("trend_ema", 50, 20, 200, "trend EMA"), PS("stop_atr", 2.0, 0.5, 4.0, "stop distance in ATR(14)"), PS("rr", 0.0, 0.0, 6.0, "0 = trailing stop instead of a target"), TRAIL), _psar("30m"))
_add("keltner_chop_breakout_60m", "Keltner Breakout, Trending Regime (60m)", "breakout", "Close crosses the Keltner band only while Choppiness says the tape is trending; 60-minute candles, trailing stop.",
     "Range expansion that is allowed only in trending regimes. Fails when the regime filter lags and the breakout reverses.",
     (PS("mult", 2.0, 1.0, 3.5, "band multiple"), PS("max_chop", 45, 30, 60, "maximum choppiness"), PS("stop_atr", 2.0, 0.5, 4.0, "stop distance in ATR(14)"), PS("rr", 0.0, 0.0, 6.0, "0 = trailing stop instead of a target"), TRAIL),
     _keltner("60m"))
