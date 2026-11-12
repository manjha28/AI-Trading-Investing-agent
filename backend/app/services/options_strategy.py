"""NIFTY options-buying strategy: compiles the stored underlying-signal settings into ordinary indicators + condition trees.

The result is stored in the definition like any hand-built strategy, so the visual builder, validation, backtesting and live signals all read the same
data. Thresholds live in ``definition.options.underlying`` (to regenerate) and in the generated conditions (editable directly); nothing is hidden in code.
"""
from __future__ import annotations

from typing import Any

from app.services.intraday_strategy import OptionsSpec, OptionsUnderlying
from app.services.rule_dsl import A, AND, C, I, LABEL, P, STARTER_VERSION, cmp, ind

NAME = "NIFTY Momentum Options Scalper"
KEY = "nifty_options_scalper"
DESCRIPTION = ("One-minute options-buying strategy using five-minute underlying trend confirmation, one-minute pullback continuation, dynamic CE/PE contract selection "
               "and short-duration trade management.")


def OR(*children: dict[str, Any]) -> dict[str, Any]:
    return {"type": "group", "op": "or", "children": list(children)}


def build_underlying_rules(u: OptionsUnderlying) -> dict[str, Any]:
    T, E = u.trend_timeframe, u.entry_timeframe
    inds = [
        ind("t_vwap", "vwap", {}, T), ind("t_fast", "ema", {"length": u.ema_fast}, T), ind("t_slow", "ema", {"length": u.ema_slow}, T), ind("t_adx", "adx", {"length": u.adx_length}, T),
        ind("e_fast", "ema", {"length": u.ema_fast}, E), ind("e_alt", "ema", {"length": u.ema_pullback_alt}, E), ind("e_atr", "atr", {"length": u.atr_length}, E),
        ind("e_rsi", "rsi", {"length": u.rsi_length}, E), ind("e_vwap", "vwap", {}, E), ind("e_vol", "vol_sma", {"length": u.volume_length}, E),
        ind("e_swing", "swing", {"left": u.swing_left, "right": u.swing_right}, E),
    ]
    tol = A("*", I("e_atr"), C(u.pullback_atr))
    slope_up = [cmp(I("t_slow", offset=k - 1), ">", I("t_slow", offset=k)) for k in range(1, u.ema_slope_bars)]
    slope_dn = [cmp(I("t_slow", offset=k - 1), "<", I("t_slow", offset=k)) for k in range(1, u.ema_slope_bars)]
    window = {"type": "time_window", "start": u.start_time, "end": u.last_entry_time}
    bull_regime = AND(cmp(P("close", T), ">", I("t_vwap")), cmp(I("t_fast"), ">", I("t_slow")), *slope_up, cmp(I("t_adx", "adx"), ">=", C(u.adx_min)), cmp(I("t_adx", "plus_di"), ">", I("t_adx", "minus_di")))
    bear_regime = AND(cmp(P("close", T), "<", I("t_vwap")), cmp(I("t_fast"), "<", I("t_slow")), *slope_dn, cmp(I("t_adx", "adx"), ">=", C(u.adx_min)), cmp(I("t_adx", "minus_di"), ">", I("t_adx", "plus_di")))
    long_entry = AND(
        bull_regime, window,
        OR(cmp(P("low", E), "<=", A("+", I("e_fast"), tol), u.pullback_lookback), cmp(P("low", E), "<=", A("+", I("e_alt"), tol), u.pullback_lookback)),     # pulled back to EMA9/EMA20 +- tolerance
        cmp(P("close", E), ">", I("e_swing", "low")),                                                                                                       # swing-low structure intact
        cmp(P("close", E), ">", P("open", E)), cmp(P("close", E), ">", P("high", E, 1)),                                                                  # bullish candle closing above the previous candle's high
        cmp(I("e_rsi"), ">", C(u.rsi_long_min)), cmp(I("e_rsi"), ">", I("e_rsi", offset=1)),
        cmp(P("volume", E), ">=", A("*", I("e_vol"), C(u.volume_multiple))),
        cmp(A("-", P("high", E), P("low", E)), "<=", A("*", I("e_atr"), C(u.max_candle_range_atr))),
        cmp(A("-", P("close", E), I("e_vwap")), "<=", A("*", I("e_atr"), C(u.max_vwap_extension_atr))))
    short_entry = AND(
        bear_regime, window,
        OR(cmp(P("high", E), ">=", A("-", I("e_fast"), tol), u.pullback_lookback), cmp(P("high", E), ">=", A("-", I("e_alt"), tol), u.pullback_lookback)),
        cmp(P("close", E), "<", I("e_swing", "high")),
        cmp(P("close", E), "<", P("open", E)), cmp(P("close", E), "<", P("low", E, 1)),
        cmp(I("e_rsi"), "<", C(u.rsi_short_max)), cmp(I("e_rsi"), "<", I("e_rsi", offset=1)),
        cmp(P("volume", E), ">=", A("*", I("e_vol"), C(u.volume_multiple))),
        cmp(A("-", P("high", E), P("low", E)), "<=", A("*", I("e_atr"), C(u.max_candle_range_atr))),
        cmp(A("-", I("e_vwap"), P("close", E)), "<=", A("*", I("e_atr"), C(u.max_vwap_extension_atr))))
    # thesis invalidation: the 5-minute regime that justified the trade no longer holds on a completed 5-minute candle
    long_exit = OR(cmp(P("close", T), "<", I("t_vwap")), cmp(I("t_fast"), "<", I("t_slow")))
    short_exit = OR(cmp(P("close", T), ">", I("t_vwap")), cmp(I("t_fast"), ">", I("t_slow")))
    return {"indicators": inds, "long_entry": long_entry, "short_entry": short_entry, "long_exit": long_exit, "short_exit": short_exit}


def starter_definition() -> dict[str, Any]:
    opts = OptionsSpec()
    rules = build_underlying_rules(opts.underlying)
    return {
        "schema_version": "intraday-strategy/1", "name": NAME, "description": DESCRIPTION, "market": "equity_in", "universe": [], "base_timeframe": "1m", "decision_timeframe": opts.underlying.entry_timeframe,
        **rules, "sizing": {"method": "percent_equity", "value": 10}, "stop": {"mode": "none", "value": 0, "atr_length": 14}, "target": {"mode": "none", "value": 0, "atr_length": 14},
        "trail": {"mode": "none", "value": 0, "atr_length": 14}, "session": {"no_entry_after": None, "square_off": None}, "constraints": {"allow_long": True, "allow_short": True, "max_trades_per_day": None},
        "python": None, "options": opts.model_dump(mode="json"),
        "source": {"type": "starter", "artifact": None, "provenance": {"starter_key": KEY, "starter_version": STARTER_VERSION, "label": LABEL, "performance": "not tested",
                                                                        "note": "Select a NIFTY futures instrument as the signal source once its 1-minute data is stored. Requires option contracts, quotes and Greeks to backtest; "
                                                                                "none are stored until you ingest them (see Data coverage)."}},
    }
