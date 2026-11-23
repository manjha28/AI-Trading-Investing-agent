"""Universe rank and rank-based confidence. Frames are TEST DATA (random walks), never runtime data."""
import numpy as np
import pandas as pd

from app.services import research_confidence
from app.services.universe_rank import composite_percentiles


def _panel(n=60, bars=300, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=bars)
    drift = np.linspace(-0.0005, 0.001, n)
    vol = np.linspace(0.04, 0.008, n)                       # stock 0 is the most volatile and weakest; stock n-1 calm and strongest
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(drift, vol, (bars, n)), axis=0)), index=idx, columns=[f"s{i}" for i in range(n)])
    return close, close * 1.01, pd.DataFrame(rng.uniform(1e5, 2e5, (bars, n)), index=idx, columns=close.columns)


def test_calm_strong_stocks_rank_above_volatile_weak_ones():
    close, high, volume = _panel()
    t = composite_percentiles(close, high, volume)
    assert t.loc["s59", "composite_pct"] > t.loc["s0", "composite_pct"]
    assert t["composite_pct"].between(0, 100).all()


def test_missing_history_gives_unknown_not_a_default():
    close, high, volume = _panel()
    close.iloc[:, 3] = np.nan
    assert pd.isna(composite_percentiles(close, high, volume).loc["s3", "composite_pct"])


def test_rank_replaces_the_legacy_formula_when_available_and_falls_back_when_not():
    horizon = {"recommendation": "BUY", "score": 80, "confidence": 70}
    rank = {"status": "available", "percentile": 83.4, "as_of": "2026-11-20", "universe_size": 300,
            "members": [{"key": "lowvol", "label": "Low volatility (60d)", "percentile": 90.0}, {"key": "mom_12_1", "label": "12-1 month momentum", "percentile": 70.0}]}
    c = research_confidence.buy_confidence(horizon, None, 2, 65, 3.0, rank)
    assert c["score"] == 83.4 and c["band"] == "High" and c["basis"] == "universe_rank" and "not a probability" in c["note"]
    low = research_confidence.buy_confidence(horizon, None, 2, 65, 3.0, {**rank, "percentile": 30.0})
    assert low["band"] == "Low"
    legacy = research_confidence.buy_confidence(horizon, None, 2, 65, 3.0, {"status": "unavailable", "reason": "x"})
    assert legacy["basis"] == "legacy_formula" and "unavailable" in legacy["note"]
