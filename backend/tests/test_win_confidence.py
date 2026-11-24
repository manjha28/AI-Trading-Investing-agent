"""Measured win-probability confidence: shrinkage, matching, bands, data penalty, the fixed features. Test data: hand-made setups (never runtime data)."""
import pandas as pd
import pytest

from app.services import win_confidence as wc


def _setups(spec):                        # spec: list of (bins dict, wins, losses)
    rows = []
    for bins, w, l in spec:
        rows += [{"bins": bins, "win": True, "net_r": 1.0, "finish": "2024-01-01"}] * w + [{"bins": bins, "win": False, "net_r": -1.0, "finish": "2024-01-01"}] * l
    return rows


def _model(rows, p0, sep=False, order=("regime", "setup")):
    return {"version": "t", "horizons": {"swing": {"p0": p0, "order": list(order), "setups": rows, "cuts": {}, "validation": {"separating": sep}}}}


def test_shrinkage_is_the_beta_binomial_posterior_mean():
    assert wc.shrink(0, 0, 0.30, 50) == pytest.approx(0.30)                    # no evidence: the base rate
    assert wc.shrink(60, 100, 0.30, 50) == pytest.approx((60 + 15) / 150)       # (w + k p0) / (n + k)
    assert wc.shrink(6, 10, 0.30, 50) < 0.36                                    # thin evidence stays near the base rate


def test_beta_quantiles_are_ordered_and_bracket_the_mean():
    lo, hi = wc.interval(30, 100, 0.30, wc.ConfidenceConfig())
    assert 0 < lo < wc.shrink(30, 100, 0.30, 50) < hi < 1
    assert wc.beta_cdf(wc.beta_ppf(0.8, 7, 13), 7, 13) == pytest.approx(0.8, abs=1e-6)


def test_the_weakest_feature_is_dropped_until_enough_setups_match():
    a = {"setup": "pullback", "regime": "uptrend"}
    rows = _setups([(a, 10, 10), ({"setup": "pullback", "regime": "range"}, 30, 30)])
    m, used = wc.find_matches(rows, a, ["regime", "setup"], 30)
    assert used == ["setup"] and len(m) == 80                                    # 20 exact matches are too few: regime dropped first, then all pullbacks match
    m2, used2 = wc.find_matches(rows, a, ["regime", "setup"], 10)
    assert used2 == ["regime", "setup"] and len(m2) == 20


def test_an_unknown_feature_cannot_match_and_is_dropped_up_front():
    rows = _setups([({"setup": "pullback", "regime": "uptrend"}, 20, 20)])
    m, used = wc.find_matches(rows, {"setup": "pullback", "regime": None}, ["regime", "setup"], 10)
    assert used == ["setup"] and len(m) == 40


def test_bands_are_relative_to_the_base_rate_and_labelled_until_separating():
    b = {"setup": "pullback", "regime": "uptrend"}
    strong = _model(_setups([(b, 90, 60)]), 0.30)                                # 60% win rate on 150 matches, base 30%
    s = wc.score("swing", b, strong)
    assert s["band"] == "High" and s["calibrated"] > 40 and s["band_label"].startswith("Unproven") and "High" not in s["band_label"] and s["n_matches"] == 150
    assert wc.score("swing", b, _model(_setups([(b, 90, 60)]), 0.30, sep=True))["band_label"] == "High"
    thin = wc.score("swing", b, _model(_setups([(b, 20, 10)]), 0.30))
    assert thin["band"] == "Medium"                                              # above the base rate but under 100 matches
    low = wc.score("swing", b, _model(_setups([(b, 5, 45)]), 0.30))
    assert low["band"] == "Low"


def test_a_required_unknown_input_halves_the_evidence():
    b = {"setup": "pullback", "regime": "uptrend"}
    m = _model(_setups([(b, 90, 60)]), 0.30)
    full, halved = wc.score("swing", b, m), wc.score("swing", b, m, required_unknown=True)
    assert halved["n_effective"] == 75 and halved["calibrated"] < full["calibrated"] and halved["data_penalty_applied"]


def test_the_line_and_tooltip_state_the_number_the_base_rate_and_the_sample():
    b = {"setup": "pullback", "regime": "uptrend"}
    s = wc.score("swing", b, _model(_setups([(b, 40, 80)]), 0.30))
    assert s["line"].startswith("Measured win rate ") and "(avg setup 30%)" in s["line"] and s["line"].endswith("based on 120 similar past setups")
    assert "80% interval" in s["tooltip"] and "net R" in s["tooltip"]


def test_no_model_for_a_horizon_is_unavailable_not_a_number():
    s = wc.score("positional", {"setup": "x"}, _model([], 0.3))
    assert s["available"] is False and s["calibrated"] is None and s["line"] is None


def test_series_features_are_point_in_time_and_unknown_when_history_is_short():
    idx = pd.bdate_range("2024-01-01", periods=300)
    close = pd.Series([100 + i * 0.5 for i in range(300)], index=idx)
    f = wc.series_features(close, close * 1.01, close * 0.9 + 5)
    assert f["stack"] == 4 and f["dist52"] is not None and -0.05 < f["dist52"] <= 0 and f["rs60"] is not None
    short = wc.series_features(close.iloc[:100], close.iloc[:100], None)
    assert short == {"stack": None, "rs60": None, "dist52": None}


def test_setup_type_names_blue_sky_before_the_engine_setup():
    assert wc.setup_type({"selected_setup": "PULLBACK_ENTRY", "targets": [{"basis": "Blue sky: ATR multiple"}]}) == "blue_sky"
    assert wc.setup_type({"selected_setup": "BREAKOUT_ENTRY", "targets": [{"basis": "Resistance zone"}]}) == "breakout"
    assert wc.setup_type({"selected_setup": "NO_VALID_SETUP", "targets": []}) == "other"


def test_the_shipped_model_file_is_well_formed_when_present():
    """The stored model (built by the Phase 7b scripts from replay data) has the three horizons, base rates in (0,1), and every setup row carries bins and a win flag. Skipped before it is built."""
    m = wc.load_model()
    if not m:
        pytest.skip("win_confidence_model.json is not built yet")
    assert set(m["horizons"]) >= {"swing", "positional", "long_term"}
    for hz, blk in m["horizons"].items():
        assert 0 < blk["p0"] < 1 and blk["setups"] and blk["order"]
        assert all(isinstance(r["win"], bool) and "bins" in r for r in blk["setups"][:50])
        assert "separating" in (blk.get("validation") or {}), hz
