"""Code-defined strategy registry: every shipped strategy builds a valid, evaluable definition and carries honest evidence.
Frames used for smoke evaluation are TEST DATA."""
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.services.intraday_strategy import StrategyDefinition, evaluate, validate_definition
from app.services.starter_strategies import STARTERS
from app.services.timeframes import EQUITY_IN
from app.strategies import all_specs, get
from app.strategies.base import ParamSpec, Rules, StrategySpec
from tests.test_signals_lab_api import API, add_inst, client, engine, h, login  # noqa: F401  (fixtures + helpers)

KEYS = [s.key for s in all_specs()]


def sessions_frame(days=4, seed=3) -> pd.DataFrame:
    t = np.concatenate([pd.date_range(f"2025-01-{6 + d:02d} 03:45", periods=375, freq="min", tz="UTC") for d in range(days)])
    n = len(t)
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.15, n))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"time": t, "open": open_, "high": np.maximum(open_, close) + 0.05, "low": np.minimum(open_, close) - 0.05, "close": close,
                         "volume": rng.integers(100, 900, n).astype(float)})


def test_the_registry_ships_the_selected_strategies_including_the_five_ema():
    assert "five_ema_alert" in KEYS and len(KEYS) == len(set(KEYS)) >= 6
    assert {s.family for s in all_specs()} <= {"trend", "momentum", "mean_reversion", "breakout", "volatility", "volume"}


@pytest.mark.parametrize("key", KEYS)
def test_each_strategy_builds_a_valid_definition_from_its_defaults(key):
    spec = get(key)
    defn = StrategyDefinition.model_validate(spec.definition())
    errors = [i for i in validate_definition(defn)[0] if i["level"] == "error"]
    assert not errors, errors
    prov = defn.source.provenance
    assert prov["starter_key"] == f"reg_{key}" and prov["family"] == spec.family and prov["rationale"] and defn.name.startswith("Registry: ")
    assert defn.market == "equity_in" and defn.session.square_off == "15:15"                  # intraday: forced flat before the close, no overnight carry
    for p in spec.params:
        assert p.low <= p.default <= p.high, p.name


@pytest.mark.parametrize("key", KEYS)
def test_each_strategy_evaluates_on_one_minute_bars_without_error(key):
    defn = StrategyDefinition.model_validate(get(key).definition())
    result = evaluate(defn, sessions_frame(), EQUITY_IN)
    assert set(result.signals) >= {"long_entry", "short_entry"} and len(result.signals["long_entry"]) == 375 * 4
    if defn.stop.mode == "level":
        assert set(result.stop_levels) >= {"short"} or set(result.stop_levels) >= {"long"}


def test_the_five_ema_uses_the_alert_candle_as_its_stop_not_an_atr():
    defn = StrategyDefinition.model_validate(get("five_ema_alert").definition())
    assert defn.stop.mode == "level" and defn.stop.short_level.output == "short_stop" and defn.stop.long_level.output == "long_stop"
    assert defn.target.mode == "rr" and defn.target.value == 3.0
    assert defn.long_entry is None and defn.short_entry is not None                              # sell-only by default
    both = StrategyDefinition.model_validate(get("five_ema_alert").definition({"direction": 0, "rr": 2}))
    assert both.long_entry is not None and both.short_entry is not None and both.target.value == 2.0


def test_parameter_overrides_are_validated_against_their_bounds():
    spec = get("mfi_dip_30m")
    assert spec.resolve({"level": 30})["level"] == 30 and spec.resolve()["mfi_length"] == 14
    with pytest.raises(ValueError, match="unknown parameter"):
        spec.resolve({"nope": 1})
    with pytest.raises(ValueError, match="between"):
        spec.resolve({"level": 90})
    defn = StrategyDefinition.model_validate(spec.definition({"level": 30, "stop_atr": 2.5, "rr": 2}))
    assert defn.stop.value == 2.5 and defn.target.value == 2.0
    trailing = StrategyDefinition.model_validate(spec.definition({"rr": 0, "trail_atr": 4}))
    assert trailing.target.mode == "none" and trailing.trail.mode == "atr" and trailing.trail.value == 4.0


def test_a_spec_is_built_from_a_rules_object_and_never_carries_a_price():
    spec = StrategySpec("t", "T", "trend", "s", "r", (ParamSpec("n", 5, 1, 9, "d"),), lambda p: Rules([], None))
    assert spec.definition()["long_entry"] is None and spec.describe()["params"][0]["name"] == "n"
    for s in all_specs():       # levels are indicator outputs computed from data; nothing in a definition is a literal price
        text = str(s.definition())
        assert "entry_price" not in text and "limit_price" not in text


@pytest.mark.parametrize("key", [k for k in KEYS if get(k).evidence])
def test_recorded_evidence_is_complete_internally_consistent_and_labelled_hypothetical(key):
    e = get(key).evidence
    for field in ("symbols", "bars", "data_window", "cost_profile", "trades", "gross_pnl_before_costs", "total_charges", "total_slippage", "net_profit", "profit_factor_net", "verdict"):
        assert field in e and e[field] is not None, f"{key}: missing {field}"
    gross, charges, slip, net = (Decimal(str(e[k])) for k in ("gross_pnl_before_costs", "total_charges", "total_slippage", "net_profit"))
    assert abs(net - (gross - charges - slip)) <= Decimal("1.00")                                 # net is gross less charges and slippage
    assert e["trades"] > 0 and e["symbols_tested"] >= 1 and 0 <= e["symbols_profitable"] <= e["symbols_tested"]
    assert get(key).definition()["source"]["provenance"]["performance"] == "hypothetical backtest recorded"
    if Decimal(str(e["net_profit"])) <= 0:
        assert "not" in e["verdict"].lower() or "did not" in e["verdict"].lower()                  # a losing result is never dressed up


def test_registry_strategies_flow_into_the_starter_catalogue_and_install_idempotently(client, engine):
    login(client, engine)
    add_inst(engine, "SBIN")                                                                        # installing anchors each strategy to an eligible instrument
    catalog = client.get(f"{API}/starters").json()["catalog"]
    registry_keys = {f"reg_{k}" for k in KEYS}
    assert registry_keys <= {c["key"] for c in catalog} and len(STARTERS) >= 14 + len(KEYS)
    listing = client.get(f"{API}/registry").json()
    assert [s["key"] for s in listing["strategies"]] == [s.key for s in all_specs()] and "never real P&L" in listing["disclaimer"]
    assert all(s["installed_strategy_id"] is None for s in listing["strategies"])
    five = next(s for s in listing["strategies"] if s["key"] == "five_ema_alert")
    assert five["family"] == "mean_reversion" and five["rationale"] and {p["name"] for p in five["params"]} == {"ema", "rr", "direction", "stop_buffer_pct", "timeframe_min", "vwap_filter"}
    assert client.post(f"{API}/starters/install", headers=h(client)).status_code == 200
    after = client.get(f"{API}/registry").json()["strategies"]
    assert all(s["installed_strategy_id"] for s in after)
    again = client.post(f"{API}/starters/install", headers=h(client)).json()
    assert again["installed"] == 0                                                                  # idempotent


def test_the_five_ema_runs_on_five_or_fifteen_minute_candles_and_nothing_else():
    spec = get("five_ema_alert")
    d15, d5 = spec.definition(), spec.definition({"timeframe_min": 5})
    assert d15["decision_timeframe"] == "15m" and d5["decision_timeframe"] == "5m"
    assert d5["indicators"][0]["timeframe"] == "5m" and d5["stop"]["mode"] == "level"          # the same alert-candle stop at either timeframe
    with pytest.raises(ValueError, match="5 or 15"):
        spec.definition({"timeframe_min": 10})
    defn = StrategyDefinition.model_validate(d5)
    assert not [i for i in validate_definition(defn)[0] if i["level"] == "error"]
    assert evaluate(defn, sessions_frame(), EQUITY_IN).signals["short_entry"].dtype == bool


def test_the_two_live_allowlisted_strategies_are_in_the_registry_with_the_stop_and_target_live_orders_need():
    for key in ("five_ema_alert", "mfi_dip_30m"):
        d = StrategyDefinition.model_validate(get(key).definition())
        assert d.stop.mode != "none" and d.target.mode != "none", key              # a live bracket needs both
        assert d.session.square_off == "15:15" and d.session.no_entry_after == "14:30"


def test_the_vwap_filter_adds_a_session_vwap_side_condition_to_both_directions():
    spec = get("five_ema_alert")
    plain = StrategyDefinition.model_validate(spec.definition({"direction": 0}))
    filtered = StrategyDefinition.model_validate(spec.definition({"direction": 0, "vwap_filter": 1, "rr": 2}))
    assert [d.alias for d in plain.indicators] == ["al"] and [d.alias for d in filtered.indicators] == ["al", "vw"] and filtered.target.value == 2.0
    assert filtered.short_entry.type == "group" and filtered.long_entry.type == "group" and plain.short_entry.type == "compare"
    assert not [i for i in validate_definition(filtered)[0] if i["level"] == "error"]
    assert evaluate(filtered, sessions_frame(), EQUITY_IN).signals["short_entry"].sum() <= evaluate(plain, sessions_frame(), EQUITY_IN).signals["short_entry"].sum()   # a filter never adds signals
