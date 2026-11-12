"""Causality harness. Nothing downstream is trustworthy until this passes."""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd


def assert_causal(df: pd.DataFrame, feature_fn: Callable[[pd.DataFrame], pd.Series | pd.DataFrame], n_samples: int = 200, seed: int = 0) -> int:
    """For ``n_samples`` random indices i, recompute ``feature_fn(df[0:i+1])`` and assert EXACT equality (NaN == NaN) with the vectorised
    full-series value at i. Returns the number of indices checked. Raises AssertionError naming the first offending index and column."""
    full = feature_fn(df)
    full = full.to_frame() if isinstance(full, pd.Series) else full
    rng = np.random.default_rng(seed)
    lo = min(30, len(df) - 1)
    picks = np.sort(rng.choice(np.arange(lo, len(df)), size=min(n_samples, len(df) - lo), replace=False))
    for i in picks:
        part = feature_fn(df.iloc[: i + 1])
        part = part.to_frame() if isinstance(part, pd.Series) else part
        got, want = part.iloc[-1].to_numpy(dtype=float), full.iloc[i].to_numpy(dtype=float)
        same = (got == want) | (np.isnan(got) & np.isnan(want))
        if not same.all():
            col = full.columns[int(np.argmin(same))]
            raise AssertionError(f"lookahead: column {col!r} at index {i} ({df.index[i]}) is {want[int(np.argmin(same))]!r} on the full series but {got[int(np.argmin(same))]!r} when only bars 0..{i} exist")
    return len(picks)
