"""Every feature is recomputed on a growing prefix and must match the full-series value exactly."""
import numpy as np
import pandas as pd
import pytest

from scalp_hold.causality import assert_causal
from scalp_hold.data import load_candles, validate
from scalp_hold.features import FeatureCfg, Seasonal, feature_functions, fit_seasonal, htf_bias
from scalp_hold.venues import CRYPTO_DELTA, NSE_INDEX_FUT

FIX = "tests/fixtures/"


@pytest.fixture(scope="module", params=["crypto", "india"])
def case(request):
    if request.param == "crypto":
        df, venue = load_candles(FIX + "golden_crypto_500.csv"), CRYPTO_DELTA
    else:
        df, venue = load_candles(FIX + "golden_india_500.csv"), NSE_INDEX_FUT
    validate(df, venue)
    return df, venue


def test_every_feature_is_causal(case):
    df, venue = case
    seasonal = fit_seasonal(df.iloc[:200], venue)
    for name, fn in feature_functions(venue, FeatureCfg(), seasonal).items():
        n = 40 if name == "volume_profile" else 150
        assert assert_causal(df, fn, n_samples=n, seed=1) > 0, name


def test_harness_catches_a_lookahead_feature(case):
    df, _ = case
    leaky = lambda d: d["close"].shift(-1)                         # noqa: E731  uses bar i+1
    with pytest.raises(AssertionError, match="lookahead"):
        assert_causal(df, leaky, n_samples=20)
    centered = lambda d: d["close"].rolling(5, center=True).mean()  # noqa: E731
    with pytest.raises(AssertionError, match="lookahead"):
        assert_causal(df, centered, n_samples=20)


def test_htf_must_be_longer_than_chart_timeframe(case):
    df, venue = case
    with pytest.raises(ValueError, match="HTF"):
        htf_bias(df, venue, FeatureCfg(htf_minutes=5))


def test_seasonal_fitted_on_training_only_differs_from_full_series_fit(case):
    df, venue = case
    train = fit_seasonal(df.iloc[:250], venue)
    full = fit_seasonal(df, venue)
    assert train.factors != full.factors and train.fitted_through < full.fitted_through       # fitting on everything would leak the test fold
    assert Seasonal().factor(np.array([3, 4])).tolist() == [1.0, 1.0]


def test_volume_profile_uses_prior_sessions_for_its_grid(case):
    df, venue = case
    from scalp_hold.features import session_bin_steps
    steps = session_bin_steps(df, venue, FeatureCfg())
    first_session = steps.iloc[0]
    assert np.isnan(first_session)                                 # no completed prior session yet: no profile rather than a peeked grid
    assert steps.notna().any()


def test_no_atr_floor_on_sigma(case):
    from scalp_hold.features import vwap_sigma
    df, venue = case
    flat = df.copy()
    flat[["open", "high", "low", "close"]] = 100.0
    assert (vwap_sigma(flat, venue)["sigma"].dropna() == 0).all()
