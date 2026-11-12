"""scalp_hold as a Strategy Library entry: valid definition, editable params, engine routing, never scanned."""
import json

import pytest

from app.services import intraday_python as ipy
from app.services.engine_strategies import build_definition, is_engine, source_text
from app.services.intraday_strategy import validate_definition
from scalp_hold.app_adapter import DEFAULT_PARAMS, run_scalp_hold
from scalp_hold.data import load_candles


def test_definition_is_a_valid_python_path_strategy_with_every_spec_parameter_editable():
    defn, text = build_definition(["00000000-0000-0000-0000-000000000001"])
    issues, req = validate_definition(defn)
    assert req is not None and not [i for i in issues if i["level"] == "error"]
    assert ipy.screen_source(text) == []                                   # passes the same AST screen as any uploaded Python strategy
    assert is_engine(defn) and defn.name == "scalp_hold" and defn.decision_timeframe == "5m"
    params = defn.source.provenance["params"]
    for key in ("r", "H_minutes", "k_vol", "theta_feeR", "s_cap_mult", "min_tick_mult", "r_min", "risk_pct", "daily_loss_R", "max_trades_day", "partial_pct", "entry_mode",
                "scalper_offer_enabled", "tds_pct"):
        assert key in params
    assert params["scalper_offer_enabled"] is False and params["tds_pct"] == 0.0           # unverified config is off by default (spec hard rule 5)
    assert str(DEFAULT_PARAMS["risk_pct"]) in source_text()


def test_engine_run_on_the_golden_fixture_returns_the_documented_shape():
    df = load_candles("tests/fixtures/golden_crypto_500.csv")
    out = run_scalp_hold({"BTCUSD": (df, "crypto")}, "crypto", "single", {"bar_minutes": 5}, equity=10_000, max_lev=None, slip_bps=1.0, max_missing_pct=100.0)
    assert out["mode"] == "scalp_hold" and out["is_surrogate"] is False
    assert out["instruments"][0]["venue"] == "CRYPTO_DELTA" and out["instruments"][0]["traded_from"] > out["instruments"][0]["start"]
    assert out["instruments"][0]["phase0"] is not None and "hypothetical" in out["note"]
    json.dumps(out)                                                         # the whole result is JSON-serialisable for storage


def test_instruments_with_too_little_clean_data_are_refused_with_a_reason_not_padded():
    df = load_candles("tests/fixtures/golden_crypto_500.csv").iloc[:100]
    out = run_scalp_hold({"BTCUSD": (df, "crypto")}, "crypto", "single", {}, equity=10_000, max_lev=None, slip_bps=1.0, max_missing_pct=5.0)
    assert out["instruments"] == [] and "only" in out["refused_instruments"][0]["reason"]


def test_only_single_and_walk_forward_are_supported():
    df = load_candles("tests/fixtures/golden_crypto_500.csv")
    with pytest.raises(ValueError, match="single"):
        run_scalp_hold({"BTCUSD": (df, "crypto")}, "crypto", "sensitivity", {}, equity=1, max_lev=None, slip_bps=0, max_missing_pct=5)


def test_scanning_refuses_engine_strategies():
    from uuid import uuid4

    from app.services import scan_service

    assert "backtest-only" in open(scan_service.__file__).read()
    assert scan_service.run_strategy_scan.__doc__ is not None and uuid4() is not None


def test_per_run_overrides_are_validated_and_recorded():
    from scalp_hold.app_adapter import coerce_params
    assert coerce_params({"r": "2.5", "no_entry_hours_ist": "11,12,13", "scalper_offer_enabled": "false", "H_minutes": "60"}) == {"r": 2.5, "no_entry_hours_ist": "11,12,13", "scalper_offer_enabled": False, "H_minutes": 60}
    with pytest.raises(ValueError, match="unknown parameter"):
        coerce_params({"bogus": 1})
    with pytest.raises(ValueError, match="not a valid value"):
        coerce_params({"r": "two"})
    df = load_candles("tests/fixtures/golden_crypto_500.csv")
    out = run_scalp_hold({"BTCUSD": (df, "crypto")}, "crypto", "single", {}, equity=10_000, max_lev=None, slip_bps=1.0, max_missing_pct=100.0, overrides={"signal_types": "B", "H_minutes": "60"})
    assert out["param_overrides"] == {"signal_types": "B", "H_minutes": 60} and out["params"]["H_minutes"] == 60 and out["params"]["r"] == 2.0
