"""Built-in starter strategies: ordinary ``intraday-strategy/1`` definitions (data, not code) installed per user through the normal
strategy/version tables. Every period, threshold, multiplier and filter lives in the stored definition and is editable in the Builder.
No performance is claimed: until a real backtest exists the Library shows "not tested".
"""
from __future__ import annotations

from typing import Any

from app.services.rule_dsl import A, AND, C, I, LABEL, P, STARTER_VERSION, TF, cmp, ind  # noqa: F401  (re-exported)


RISK = {"stop": {"mode": "atr", "value": 1.5, "atr_length": 14}, "target": {"mode": "rr", "value": 2.0, "atr_length": 14},
        "trail": {"mode": "none", "value": 0, "atr_length": 14}, "sizing": {"method": "percent_equity", "value": 10}}


def _spec(key: str, name: str, description: str, indicators: list[dict[str, Any]], long: dict[str, Any], short: dict[str, Any], note: str = "") -> dict[str, Any]:
    return {
        "schema_version": "intraday-strategy/1", "name": f"Starter: {name}", "description": description[:500], "market": "equity_in", "universe": [],
        "base_timeframe": "1m", "decision_timeframe": TF, "indicators": indicators, "long_entry": long, "short_entry": short, "long_exit": None, "short_exit": None,
        **RISK, "session": {"no_entry_after": None, "square_off": None}, "constraints": {"allow_long": True, "allow_short": True, "max_trades_per_day": None},
        "python": None,
        "source": {"type": "starter", "artifact": None, "provenance": {"starter_key": key, "starter_version": STARTER_VERSION, "label": LABEL, "note": note,
                                                                        "performance": "not tested"}},
    }


def _catalog() -> list[dict[str, Any]]:
    out = []
    # 1 EMA + RSI + VWAP
    base = [ind("ema_fast", "ema", {"length": 9}), ind("ema_slow", "ema", {"length": 21}), ind("rsi", "rsi", {"length": 14}), ind("vwap", "vwap"), ind("vol_avg", "vol_sma", {"length": 20})]
    out.append(_spec("ema_rsi_vwap", "EMA + RSI + VWAP Confirmation", "EMA 9/21 crossover confirmed by RSI 14 side of 50, price versus session VWAP and volume above its 20-period average.", base,
                     AND(cmp(I("ema_fast"), "crosses_above", I("ema_slow")), cmp(I("rsi"), ">", C(50)), cmp(P("close"), ">", I("vwap")), cmp(P("volume"), ">", I("vol_avg"))),
                     AND(cmp(I("ema_fast"), "crosses_below", I("ema_slow")), cmp(I("rsi"), "<", C(50)), cmp(P("close"), "<", I("vwap")), cmp(P("volume"), ">", I("vol_avg")))))
    # 2 Supertrend + EMA trend
    st = [ind("st", "supertrend", {"length": 10, "multiplier": 3.0}), ind("ema_fast", "ema", {"length": 20}), ind("ema_slow", "ema", {"length": 50}), ind("rsi", "rsi", {"length": 14})]
    out.append(_spec("supertrend_ema", "Supertrend + EMA Trend", "Trades with the Supertrend direction while EMA 20 versus EMA 50 and RSI 14 versus 50 agree. State-based: it can signal on every candle while the regime holds; use exits or the single-position simulation to limit it.", st,
                     AND(cmp(I("st", "direction"), ">", C(0)), cmp(I("ema_fast"), ">", I("ema_slow")), cmp(I("rsi"), ">", C(50))),
                     AND(cmp(I("st", "direction"), "<", C(0)), cmp(I("ema_fast"), "<", I("ema_slow")), cmp(I("rsi"), "<", C(50)))))
    # 3 VWAP reversal
    vr = [ind("vwap", "vwap"), ind("rsi", "rsi", {"length": 14})]
    out.append(_spec("vwap_reversal", "VWAP Reversal", "Price crosses back through session VWAP with RSI 14 crossing 40 (long) or 60 (short) within the last 3 candles and a candle closing in the trade direction.", vr,
                     AND(cmp(P("close"), "crosses_above", I("vwap")), cmp(I("rsi"), "crosses_above", C(40), 3), cmp(P("close"), ">", P("open"))),
                     AND(cmp(P("close"), "crosses_below", I("vwap")), cmp(I("rsi"), "crosses_below", C(60), 3), cmp(P("close"), "<", P("open")))))
    # 4 Opening range breakout
    orb = [ind("orb", "opening_range", {"minutes": 15, "session_start": ""}, "1m"), ind("vwap", "vwap"), ind("vol_avg", "vol_sma", {"length": 20})]
    out.append(_spec("opening_range_breakout", "Opening Range Breakout", "Close breaks the completed first-15-minute range (NSE 09:15 IST open) with volume above its 20-period average and price on the correct side of VWAP. "
                     "For crypto set the opening_range session_start (HH:MM UTC) in the Builder; Indian hours are never assumed there.", orb,
                     AND(cmp(P("close"), "crosses_above", I("orb", "high")), cmp(P("volume"), ">", I("vol_avg")), cmp(P("close"), ">", I("vwap"))),
                     AND(cmp(P("close"), "crosses_below", I("orb", "low")), cmp(P("volume"), ">", I("vol_avg")), cmp(P("close"), "<", I("vwap"))),
                     note="The range is only available after its 15 minutes have completed."))
    # 5 EMA pullback
    pb = [ind("ema_fast", "ema", {"length": 20}), ind("ema_slow", "ema", {"length": 50}), ind("rsi", "rsi", {"length": 14}), ind("atr", "atr", {"length": 14})]
    tol = C(0.5)
    out.append(_spec("ema_pullback", "EMA Pullback Continuation", "In an EMA 20/50 trend, price pulls back to within 0.5 x ATR(14) of EMA 20 and a completed candle closes back in the trend direction, RSI 14 on the trend side of 50. "
                     "Pullback tolerance is the 0.5 constant in the condition: edit it, or swap in a percentage.", pb,
                     AND(cmp(I("ema_fast"), ">", I("ema_slow")), cmp(P("low"), "<=", A("+", I("ema_fast"), A("*", I("atr"), tol))), cmp(P("close"), ">", I("ema_fast")),
                         cmp(P("close"), ">", P("open")), cmp(I("rsi"), ">", C(50))),
                     AND(cmp(I("ema_fast"), "<", I("ema_slow")), cmp(P("high"), ">=", A("-", I("ema_fast"), A("*", I("atr"), tol))), cmp(P("close"), "<", I("ema_fast")),
                         cmp(P("close"), "<", P("open")), cmp(I("rsi"), "<", C(50)))))
    # 6 Bollinger reversal
    bb = [ind("bb", "bbands", {"length": 20, "multiplier": 2.0}), ind("rsi", "rsi", {"length": 14})]
    out.append(_spec("bollinger_reversal", "Bollinger Band Reversal", "Previous candle closed outside a Bollinger Band with RSI 14 oversold (below 30) / overbought (above 70) and the current candle closes back inside the bands.", bb,
                     AND(cmp(P("close", offset=1), "<", I("bb", "lower", 1)), cmp(P("close"), ">=", I("bb", "lower")), cmp(P("close"), "<", I("bb", "upper")), cmp(I("rsi", offset=1), "<", C(30))),
                     AND(cmp(P("close", offset=1), ">", I("bb", "upper", 1)), cmp(P("close"), "<=", I("bb", "upper")), cmp(P("close"), ">", I("bb", "lower")), cmp(I("rsi", offset=1), ">", C(70)))))
    # 7 Donchian breakout
    dc = [ind("dc", "donchian", {"length": 20}), ind("rvol", "rvol", {"length": 20})]
    out.append(_spec("donchian_breakout", "Donchian Channel Breakout", "Close crosses the PREVIOUS candle's 20-period Donchian channel (current candle excluded, so no look-ahead) with relative volume at least 1.5.", dc,
                     AND(cmp(P("close"), "crosses_above", I("dc", "upper", 1)), cmp(I("rvol"), ">=", C(1.5))),
                     AND(cmp(P("close"), "crosses_below", I("dc", "lower", 1)), cmp(I("rvol"), ">=", C(1.5)))))
    # 8 ADX + MACD
    am = [ind("adx", "adx", {"length": 14}), ind("macd", "macd", {"fast": 12, "slow": 26, "signal": 9}), ind("ema_trend", "ema", {"length": 50})]
    out.append(_spec("adx_macd_momentum", "ADX + MACD Momentum", "MACD line crosses its signal line while ADX 14 is above 20 and price is on the right side of EMA 50.", am,
                     AND(cmp(I("adx", "adx"), ">", C(20)), cmp(I("macd", "macd"), "crosses_above", I("macd", "signal")), cmp(P("close"), ">", I("ema_trend"))),
                     AND(cmp(I("adx", "adx"), ">", C(20)), cmp(I("macd", "macd"), "crosses_below", I("macd", "signal")), cmp(P("close"), "<", I("ema_trend")))))
    # 9 Squeeze expansion breakout (volatility compression -> expansion; blueprint eq_compression_expansion)
    sq = [ind("bb", "bbands", {"length": 20, "multiplier": 2.0}), ind("kc", "keltner", {"length": 20, "multiplier": 1.5, "atr_length": 10}),
          ind("rvol", "rvol", {"length": 20}), ind("vwap", "vwap")]
    out.append(_spec("squeeze_expansion", "Squeeze Expansion Breakout",
                     "Bollinger Bands (20, 2) sat inside the Keltner Channel (20, 1.5) on the previous candle - a volatility squeeze - and the close now breaks the previous "
                     "Bollinger band with relative volume at least 1.5 and price on the breakout side of session VWAP. Trades compression-to-expansion, not an established trend.", sq,
                     AND(cmp(I("bb", "upper", 1), "<", I("kc", "upper", 1)), cmp(I("bb", "lower", 1), ">", I("kc", "lower", 1)),
                         cmp(P("close"), "crosses_above", I("bb", "upper", 1)), cmp(I("rvol"), ">=", C(1.5)), cmp(P("close"), ">", I("vwap"))),
                     AND(cmp(I("bb", "upper", 1), "<", I("kc", "upper", 1)), cmp(I("bb", "lower", 1), ">", I("kc", "lower", 1)),
                         cmp(P("close"), "crosses_below", I("bb", "lower", 1)), cmp(I("rvol"), ">=", C(1.5)), cmp(P("close"), "<", I("vwap"))),
                     note="The squeeze test uses the PREVIOUS candle's bands, so the breakout candle never defines its own compression."))
    # 10 Previous-day level break (blueprint eq_gap_go_fade continuation side / day-level breakout)
    pdl = [ind("pday", "prev_day", {}), ind("rvol", "rvol", {"length": 20}), ind("vwap", "vwap")]
    out.append(_spec("prev_day_level_break", "Previous-Day Level Break",
                     "Close crosses above the previous session's high (long) or below its low (short) with relative volume at least 1.2 and session VWAP agreement. "
                     "A day-level acceptance play: the level comes from completed prior-session data only.", pdl,
                     AND(cmp(P("close"), "crosses_above", I("pday", "high")), cmp(I("rvol"), ">=", C(1.2)), cmp(P("close"), ">", I("vwap"))),
                     AND(cmp(P("close"), "crosses_below", I("pday", "low")), cmp(I("rvol"), ">=", C(1.2)), cmp(P("close"), "<", I("vwap")))))
    # 11 Inside-bar breakout (blueprint eq_nr7_inside_bar)
    ib = [ind("rvol", "rvol", {"length": 20}), ind("ema_trend", "ema", {"length": 50})]
    out.append(_spec("inside_bar_breakout", "Inside-Bar Breakout",
                     "The previous candle was an inside bar (its range sat inside the candle before it - contraction) and the close now breaks that inside bar's high (long) "
                     "or low (short) with relative volume at least 1.2, in the direction of EMA 50.", ib,
                     AND(cmp(P("high", offset=1), "<", P("high", offset=2)), cmp(P("low", offset=1), ">", P("low", offset=2)),
                         cmp(P("close"), "crosses_above", P("high", offset=1)), cmp(I("rvol"), ">=", C(1.2)), cmp(P("close"), ">", I("ema_trend"))),
                     AND(cmp(P("high", offset=1), "<", P("high", offset=2)), cmp(P("low", offset=1), ">", P("low", offset=2)),
                         cmp(P("close"), "crosses_below", P("low", offset=1)), cmp(I("rvol"), ">=", C(1.2)), cmp(P("close"), "<", I("ema_trend")))))
    # 12 Choppiness-gated Keltner trend ride (blueprint regime engine: trade expansion only in trending tape)
    kt = [ind("kc", "keltner", {"length": 20, "multiplier": 2.0, "atr_length": 10}), ind("chop", "choppiness", {"length": 14}), ind("ema_trend", "ema", {"length": 50}),
          ind("rvol", "rvol", {"length": 20})]
    out.append(_spec("choppiness_keltner_trend", "Choppiness-Gated Keltner Ride",
                     "Close crosses outside the Keltner Channel (20, 2) in the direction of EMA 50, but ONLY while Choppiness(14) is below 42 (trending tape) and relative "
                     "volume is at least 1.2. The choppiness gate is the explicit no-trade regime: ranging markets are suppressed rather than faded.", kt,
                     AND(cmp(I("chop"), "<", C(42)), cmp(P("close"), "crosses_above", I("kc", "upper")), cmp(P("close"), ">", I("ema_trend")), cmp(I("rvol"), ">=", C(1.2))),
                     AND(cmp(I("chop"), "<", C(42)), cmp(P("close"), "crosses_below", I("kc", "lower")), cmp(P("close"), "<", I("ema_trend")), cmp(I("rvol"), ">=", C(1.2))),
                     note="Choppiness below ~38-42 marks a trending regime; above ~61.8 marks a range. The 42 gate is editable in the Builder."))
    # 13 Range-regime edge rejection (blueprint eq_range_mean_reversion, regime-gated opposite of 12)
    rr = [ind("kc", "keltner", {"length": 20, "multiplier": 2.0, "atr_length": 10}), ind("chop", "choppiness", {"length": 14}), ind("stoch", "stoch", {"k_length": 14, "k_smooth": 3, "d_length": 3}),
          ind("shape", "candle_shape", {})]
    out.append(_spec("range_edge_rejection", "Range Edge Rejection",
                     "ONLY while Choppiness(14) is above 55 (balanced/ranging tape): the candle probes beyond a Keltner (20, 2) band, closes back in the trade direction "
                     "(directional body) and Stochastic %K crosses back from the extreme within the last 3 candles. Fades range edges; suppressed in trends.", rr,
                     AND(cmp(I("chop"), ">", C(55)), cmp(P("low"), "<=", I("kc", "lower")), cmp(I("shape", "direction"), ">", C(0)),
                         cmp(I("stoch", "k"), "crosses_above", C(20), 3)),
                     AND(cmp(I("chop"), ">", C(55)), cmp(P("high"), ">=", I("kc", "upper")), cmp(I("shape", "direction"), "<", C(0)),
                         cmp(I("stoch", "k"), "crosses_below", C(80), 3))))
    return out


STARTERS: list[dict[str, Any]] = _catalog()


def _with_options() -> None:
    from app.services.options_strategy import starter_definition   # late import: options_strategy builds on the helpers above
    STARTERS.append(starter_definition())


_with_options()
