"""Evaluation phases, run in order: 0 base rate, 1 signal edge, 2 full simulation (anchored walk-forward), 3 nulls.

Primary metric = mean R per trade with its standard error. Compounded equity and max drawdown are diagnostics only and are never used to rank combinations.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from scalp_hold.barriers import BarrierCfg, label_triple_barrier, stop_fraction
from scalp_hold.data import bar_minutes
from scalp_hold.features import FeatureCfg, Seasonal, compute, fit_seasonal, sigma_h, tod_bucket
from scalp_hold.sim import SIGNAL_ORDER, SimCfg, SimResult, simulate
from scalp_hold.signals import SignalCfg, compute_signals
from scalp_hold.venues import VenueProfile, index_end_date, round_trip_pct

R_GRID = (1.5, 2.0, 2.6, 3.0)
H_GRID = (15, 30, 45, 60, 120)
K_GRID = (0.75, 1.0, 1.25)
PARTIAL_GRID = (0, 30, 60)
SLIP_MULTS = (1, 2, 3)
MAX_COMBOS = 500
MIN_TRADES_FOLD = 500


@dataclass
class Grid:
    r: tuple = R_GRID
    H: tuple = H_GRID
    k_vol: tuple = K_GRID

    def combos(self, types=SIGNAL_ORDER):
        return list(itertools.product(types, self.r, self.H, self.k_vol))


QUICK_GRID = Grid(r=(2.0,), H=(30, 60), k_vol=(1.0,))


@dataclass
class Prepared:
    df: pd.DataFrame
    f: pd.DataFrame
    sig: pd.DataFrame
    venue: VenueProfile
    symbol: str
    slip_bps: float
    bm: int


def prepare_instrument(df: pd.DataFrame, venue: VenueProfile, symbol: str, slip_bps: float, fcfg: FeatureCfg = FeatureCfg(), scfg: SignalCfg = SignalCfg()) -> Prepared:
    f = compute(df, venue, fcfg)
    return Prepared(df, f, compute_signals(df, f, scfg), venue, symbol, slip_bps, bar_minutes(df.index))


def _se(x: np.ndarray) -> float:
    return float(np.std(x, ddof=1) / math.sqrt(len(x))) if len(x) > 1 else float("nan")


# ───────────────────────── Phase 0 ─────────────────────────
def phase0(p: Prepared, grid: Grid = Grid(), equity: float = 10_000.0, bcfg: BarrierCfg = BarrierCfg(), seasonal: Seasonal | None = None) -> pd.DataFrame:
    """Unconditional triple-barrier sweep over (s, r, H, k_vol) on ALL bars, long and short pooled, with per-cell cost arithmetic."""
    seasonal = seasonal or fit_seasonal(p.df, p.venue)           # descriptive baseline only: this phase makes no out-of-sample claim
    a = {c: p.df[c].to_numpy(float) for c in ("open", "high", "low", "close")}
    cost_rt = round_trip_pct(p.venue, p.symbol, float(np.nanmedian(a["close"])), equity * 2, on=index_end_date(p.df.index) if p.venue.is_india else None)
    tick = float(p.venue.tick_size)
    buckets = tod_bucket(p.df.index, p.venue)
    rows = []
    for k_vol, r, H in itertools.product(grid.k_vol, grid.r, grid.H):
        b = replace(bcfg, k_vol=k_vol)
        sh = sigma_h(p.f["sigma_bar"], p.df.index, p.venue, H, seasonal).to_numpy()
        # Phase 0 measures what the market offers UNCONDITIONALLY: raw volatility-implied stop only. No cost floor, tick floor or s_cap here;
        # tradeability (would the fee floor / s_cap have admitted the bar?) is reported as a separate overlay column.
        s_use = np.where(np.isfinite(b.k_vol * sh / r), b.k_vol * sh / r, np.nan)
        _s_floored, skip = stop_fraction(sh, r, cost_rt, tick, a["close"], b)
        fin = np.isfinite(s_use)
        tradeable_frac = float((~skip[fin]).mean()) if fin.any() else float("nan")
        H_bars = max(1, H // p.bm)
        lab = np.concatenate([label_triple_barrier(a["open"], a["high"], a["low"], s_use, r, H_bars, d, tick, p.slip_bps)[:, None] for d in (1, -1)], axis=1)
        wins, stops, tmo = np.nansum(lab == 1), np.nansum(lab == -1), np.nansum(lab == 0)
        n = wins + stops + tmo
        if n == 0:
            continue
        s_med = float(np.nanmedian(s_use))
        f_ = cost_rt / s_med
        p_base = wins / n
        row = {"instrument": p.symbol, "k_vol": k_vol, "r": r, "H": H, "n": int(n), "p_baseline": p_base, "timeout_rate": tmo / n, "theory_1_over_r1": 1 / (r + 1), "f": f_,
               "p_star": (1 + f_) / (r + 1), "p_star_over_base": ((1 + f_) / (r + 1)) / p_base if p_base > 0 else float("inf"), "s_median": s_med,
               "tradeable_frac": tradeable_frac}
        for bucket in np.unique(buckets):
            m = (buckets == bucket)[:, None] & np.isfinite(lab)
            nb = m.sum()
            row[f"p_bucket_{int(bucket)}"] = float((lab[m] == 1).sum() / nb) if nb else float("nan")
        rows.append(row)
    return pd.DataFrame(rows)


def print_phase0(table: pd.DataFrame) -> None:
    cols = ["instrument", "k_vol", "r", "H", "n", "p_baseline", "theory_1_over_r1", "timeout_rate", "f", "p_star", "p_star_over_base"]
    print(table[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\np*/p_baseline > 1 means the cost-adjusted break-even win rate is HIGHER than an uninformed entry achieves: a signal must add at least that much edge.")


# ───────────────────────── Phase 1 ─────────────────────────
def phase1(p: Prepared, grid: Grid = Grid(), equity: float = 10_000.0, bcfg: BarrierCfg = BarrierCfg(), seasonal: Seasonal | None = None) -> pd.DataFrame:
    """edge = p(win | signal) - p_baseline at the same (s, r, H), using NON-OVERLAPPING samples only (a sample blocks the next H bars)."""
    seasonal = seasonal or fit_seasonal(p.df, p.venue)
    a = {c: p.df[c].to_numpy(float) for c in ("open", "high", "low", "close")}
    cost_rt = round_trip_pct(p.venue, p.symbol, float(np.nanmedian(a["close"])), equity * 2, on=index_end_date(p.df.index) if p.venue.is_india else None)
    tick = float(p.venue.tick_size)
    rows = []
    for k_vol, r, H in itertools.product(grid.k_vol, grid.r, grid.H):
        b = replace(bcfg, k_vol=k_vol)
        sh = sigma_h(p.f["sigma_bar"], p.df.index, p.venue, H, seasonal).to_numpy()
        s, skip = stop_fraction(sh, r, cost_rt, tick, a["close"], b)
        s_use = np.where(skip, np.nan, s)
        H_bars = max(1, H // p.bm)
        labs = {d: label_triple_barrier(a["open"], a["high"], a["low"], s_use, r, H_bars, d, tick, p.slip_bps) for d in (1, -1)}
        base_all = np.concatenate([labs[1], labs[-1]])
        base_all = base_all[np.isfinite(base_all)]
        if not len(base_all):
            continue
        p_base = float((base_all == 1).mean())
        for stype in SIGNAL_ORDER:
            wins = n = 0
            for d, side in ((1, "long"), (-1, "short")):
                idx = np.flatnonzero(p.sig[f"{stype}_{side}"].to_numpy() & np.isfinite(labs[d]))
                last = -10**9
                for i in idx:
                    if i < last + H_bars:
                        continue                                          # overlapping sample: dropped
                    last = i
                    n += 1
                    wins += labs[d][i] == 1
            if n < 2:
                rows.append({"instrument": p.symbol, "signal": stype, "k_vol": k_vol, "r": r, "H": H, "n": n, "p_win": float("nan"), "p_baseline": p_base, "edge": float("nan"), "t_stat": float("nan")})
                continue
            p_win = wins / n
            se = math.sqrt(max(p_base * (1 - p_base), 1e-12) / n)
            rows.append({"instrument": p.symbol, "signal": stype, "k_vol": k_vol, "r": r, "H": H, "n": n, "p_win": p_win, "p_baseline": p_base, "edge": p_win - p_base, "t_stat": (p_win - p_base) / se})
    return pd.DataFrame(rows)


# ───────────────────────── Phase 2 ─────────────────────────
def anchored_folds(n_bars: int, n_folds: int = 5) -> list[tuple[slice, slice]]:
    """Anchored (expanding-train) walk-forward: the series is cut into n_folds+1 equal segments; fold j trains on segments 0..j-1 and tests on segment j."""
    edges = np.linspace(0, n_bars, n_folds + 2).astype(int)
    return [(slice(0, edges[j]), slice(edges[j], edges[j + 1])) for j in range(1, n_folds + 1)]


def _slice(p: Prepared, sl: slice) -> Prepared:
    return replace(p, df=p.df.iloc[sl], f=p.f.iloc[sl], sig=p.sig.iloc[sl])


def run_combo(p: Prepared, combo: tuple, seasonal: Seasonal, base: SimCfg, bcfg: BarrierCfg, **kw) -> SimResult:
    stype, r, H, k_vol = combo
    return simulate(p.df, p.f, p.sig, p.venue, p.symbol, replace(base, r=r, H=float(H)), slip_bps=p.slip_bps, seasonal=seasonal, barrier_cfg=replace(bcfg, k_vol=k_vol),
                    types=(stype,), **kw)


def grid_search(train: Prepared, grid: Grid, seasonal: Seasonal, base: SimCfg, bcfg: BarrierCfg, min_trades: int = 30) -> tuple[tuple | None, pd.DataFrame, int]:
    """Pick the combination with the best TRAIN mean R (never equity or drawdown). Returns (best, table, combos_evaluated)."""
    combos = grid.combos()
    if len(combos) > MAX_COMBOS:
        raise ValueError(f"{len(combos)} combinations exceeds the cap of {MAX_COMBOS}")
    rows = []
    for c in combos:
        res = run_combo(train, c, seasonal, base, bcfg)
        t = res.trades
        rows.append({"combo": c, "n": len(t), "mean_R": float(t["R"].mean()) if len(t) else float("nan"), "se": _se(t["R"].to_numpy()) if len(t) else float("nan")})
    table = pd.DataFrame(rows)
    ok = table[table["n"] >= min_trades].dropna(subset=["mean_R"])
    best = tuple(ok.sort_values("mean_R", ascending=False).iloc[0]["combo"]) if len(ok) else None
    return best, table, len(combos)


@dataclass
class Phase2Result:
    folds: pd.DataFrame
    trades: pd.DataFrame
    total_combos_evaluated: int
    chosen: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def phase2(p: Prepared, grid: Grid = Grid(), n_folds: int = 5, slip_mults: tuple = SLIP_MULTS, base: SimCfg | None = None, bcfg: BarrierCfg = BarrierCfg(),
           waiver_variants: tuple = (False,), partial_grid: tuple = PARTIAL_GRID) -> Phase2Result:
    base = base or SimCfg()
    rows, all_trades, chosen, warnings = [], [], [], []
    total = 0
    for j, (tr, te) in enumerate(anchored_folds(len(p.df), n_folds), 1):
        train, test = _slice(p, tr), _slice(p, te)
        seasonal = fit_seasonal(train.df, p.venue)                    # fitted on the training fold ONLY, frozen for the test fold
        best, table, n_eval = grid_search(train, grid, seasonal, base, bcfg)
        total += n_eval
        if best is None:
            warnings.append(f"fold {j}: no combination reached the minimum train trade count")
            continue
        chosen.append({"fold": j, "combo": best, "seasonal_fitted_through": seasonal.fitted_through})
        for waiver, partial, mult in itertools.product(waiver_variants, partial_grid, slip_mults):
            venue = p.venue
            if venue.has_funding:
                from scalp_hold.venues import crypto_delta
                venue = crypto_delta(waiver_enabled=waiver)
            tp = replace(test, venue=venue)
            res = run_combo(tp, best, seasonal, replace(base, partial_pct=float(partial), slip_mult=float(mult)), bcfg)
            t = res.trades.assign(fold=j, waiver=waiver, partial_pct=partial, slip_mult=mult) if len(res.trades) else res.trades
            if len(t):
                all_trades.append(t)
            r_ = res.trades["R"].to_numpy() if len(res.trades) else np.array([])
            rows.append({"fold": j, "combo": best, "waiver": waiver, "partial_pct": partial, "slip_mult": mult, "n": len(r_), "mean_R": float(r_.mean()) if len(r_) else float("nan"),
                         "se_R": _se(r_), "below_min_lot_rate": res.below_min_lot_rate, "total_combos_evaluated": total})
            if len(r_) < MIN_TRADES_FOLD and mult == slip_mults[0] and partial == partial_grid[0]:
                warnings.append(f"fold {j}: only {len(r_)} test trades (< {MIN_TRADES_FOLD}); mean R is imprecise")
    folds = pd.DataFrame(rows)
    return Phase2Result(folds, pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame(), total, chosen, sorted(set(warnings)))


# ───────────────────────── Phase 3: nulls ─────────────────────────
def matched_random_control(p: Prepared, strat: pd.DataFrame, seasonal: Seasonal, base: SimCfg, seeds: int = 50) -> dict:
    """Random entry bar and direction with the strategy's EXACT stop/target/horizon geometry. Returns the control mean-R distribution and the strategy's percentile."""
    if strat.empty:
        return {"strategy_mean_R": float("nan"), "percentile": float("nan"), "control_means": []}
    eligible = np.flatnonzero((p.f["bsa"].to_numpy() >= 20) & np.isfinite(p.f["atr"].to_numpy()))
    eligible = eligible[eligible < len(p.df) - 2]
    geoms = strat[["s", "target", "H_bars"]].to_numpy()
    means = []
    for seed in range(seeds):
        rng = np.random.default_rng(seed)
        picks = np.sort(rng.choice(eligible, size=min(len(geoms), len(eligible)), replace=False))
        order = rng.permutation(len(geoms))[: len(picks)]
        forced = [{"i": int(i), "d": int(rng.choice([1, -1])), "s": float(geoms[g][0]), "target_pct": float(geoms[g][1]), "H_bars": int(geoms[g][2]), "stype": "X"} for i, g in zip(picks, order)]
        res = simulate(p.df, p.f, p.sig, p.venue, p.symbol, base, slip_bps=p.slip_bps, seasonal=seasonal, forced=forced)
        if len(res.trades):
            means.append(float(res.trades["R"].mean()))
    mean_strat = float(strat["R"].mean())
    pct = float(np.mean([m < mean_strat for m in means])) * 100 if means else float("nan")
    return {"strategy_mean_R": mean_strat, "percentile": pct, "control_means": means}


def sign_flip_control(p: Prepared, chosen: list[dict], seasonal_by_fold: dict[int, Seasonal], base: SimCfg, bcfg: BarrierCfg) -> dict:
    """Every signal inverted, same geometry rules. A strategy with real directional edge should do worse flipped."""
    parts = []
    for c in chosen:
        j = c["fold"]
        _, te = anchored_folds(len(p.df), 5)[j - 1]
        res = run_combo(_slice(p, te), c["combo"], seasonal_by_fold[j], base, bcfg, flip=True)
        if len(res.trades):
            parts.append(res.trades["R"].to_numpy())
    r = np.concatenate(parts) if parts else np.array([])
    return {"n": len(r), "mean_R": float(r.mean()) if len(r) else float("nan"), "se_R": _se(r)}


def block_bootstrap_surrogate(df: pd.DataFrame, venue: VenueProfile, seed: int) -> pd.DataFrame:
    """Surrogate series: whole sessions (1-day blocks) of RELATIVE candles are shuffled with replacement and re-stitched with price continuity.
    Intraday shape and volume are preserved, cross-day order is destroyed. Tagged by the caller as is_surrogate=True; never reported as data."""
    from scalp_hold.data import session_keys
    rng = np.random.default_rng(seed)
    keys = session_keys(df.index, venue).to_numpy()
    days = [g for _, g in df.groupby(keys, sort=True)]
    full_len = max(len(g) for g in days)
    days = [g for g in days if len(g) >= 0.9 * full_len] or days
    out, price = [], float(df["close"].iloc[0])
    base_index = df.index
    pos = 0
    while pos < len(df):
        g = days[int(rng.integers(len(days)))]
        scale = price / float(g["open"].iloc[0])
        block = g[["open", "high", "low", "close"]] * scale
        block["volume"] = g["volume"].to_numpy()
        take = min(len(block), len(df) - pos)
        block = block.iloc[:take].copy()
        block.index = base_index[pos:pos + take]
        price = float(block["close"].iloc[-1])
        out.append(block)
        pos += take
    return pd.concat(out)


def phase3(p: Prepared, p2: Phase2Result, grid: Grid = Grid(), seeds: int = 50, bootstrap: int = 50, base: SimCfg | None = None, bcfg: BarrierCfg = BarrierCfg()) -> dict:
    base = base or SimCfg()
    seasonal_by_fold = {}
    for j, (tr, _) in enumerate(anchored_folds(len(p.df), 5), 1):
        seasonal_by_fold[j] = fit_seasonal(p.df.iloc[tr], p.venue)
    strat = p2.trades[(p2.trades["slip_mult"] == 1) & (p2.trades["partial_pct"] == 0)] if len(p2.trades) else p2.trades
    if "waiver" in strat:
        strat = strat[strat["waiver"] == False]                            # noqa: E712
    final_seasonal = seasonal_by_fold[max(seasonal_by_fold)]
    control = matched_random_control(p, strat, final_seasonal, base, seeds)
    flip = sign_flip_control(p, p2.chosen, seasonal_by_fold, base, bcfg)
    boot_best, boot_total = [], 0
    for b in range(bootstrap):
        sur = block_bootstrap_surrogate(p.df, p.venue, seed=1000 + b)
        sp = prepare_instrument(sur, p.venue, p.symbol, p.slip_bps)
        res = phase2(sp, grid, slip_mults=(1,), partial_grid=(0,), base=base, bcfg=bcfg)
        boot_total += res.total_combos_evaluated
        boot_best.append(float(res.trades["R"].mean()) if len(res.trades) else float("nan"))
    arr = np.array([x for x in boot_best if np.isfinite(x)])
    strat_mean = float(strat["R"].mean()) if len(strat) else float("nan")
    return {"matched_random": control, "sign_flip": flip, "bootstrap": {"is_surrogate": True, "n": len(arr), "best_combo_test_meanR": boot_best,
            "strategy_percentile": float(np.mean(arr < strat_mean) * 100) if len(arr) else float("nan"), "total_combos_evaluated": boot_total}}


# ───────────────────────── reports ─────────────────────────
def run_report(trades: pd.DataFrame, res_meta: dict | None = None) -> dict[str, object]:
    """Exit-reason distribution, avg R by time bucket and by signal type for a set of trades."""
    if trades.empty:
        return {"n": 0}
    return {
        "n": len(trades), "mean_R": float(trades["R"].mean()), "se_R": _se(trades["R"].to_numpy()),
        "exit_reasons": trades["exit_reason"].value_counts(normalize=True).round(4).to_dict(),
        "avgR_by_tod_bucket": trades.groupby("tod_bucket")["R"].mean().round(4).to_dict(),
        "avgR_by_signal_type": trades.groupby("signal_type")["R"].mean().round(4).to_dict(),
        "fee_R_mean": float(trades["fee_R"].mean()),
        "stop_vs_liquidation": {"max_s": float(trades["s"].max()), "median_s": float(trades["s"].median())},
        **(res_meta or {}),
    }
