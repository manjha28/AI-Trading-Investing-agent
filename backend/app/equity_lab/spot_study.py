"""Does any spot-derived feature set predict the next 15-60 minutes by more than the cost of trading it? (research only: nothing here can reach an order)

Step 2 of the owner's spec of 27 Nov 2026: single and paired feature rules, then logistic regression and gradient boosting trained walk-forward.
Choice is made on the validation split; the final split is evaluated for the ONE chosen candidate only.
Run from backend/:  PYTHONPATH=../diagnostics/research_lab/.pylibs python -m app.equity_lab.spot_study
"""
from __future__ import annotations

import itertools
import json
import math
from datetime import date

import numpy as np
import pandas as pd

from app.delta_lab import lit_core as K
from app.equity_lab import index_options_common as c
from app.equity_lab.spot_features import HORIZONS, INDICES, build

TRAIN_END, VALID_END = date(2024, 1, 29), date(2025, 6, 26)
PRIOR_TRIALS = 1800 + 6144
TOPS = (0.10, 0.05, 0.02)
CATEGORIES = ("or15_position", "or30_position", "swing_5m", "swing_15m", "weekday", "expiry_day")
MIN_TRAIN, MIN_VALID = 200, 100
REPORT = "2026-11-27-spot-driven-option-strategy.md"
SOURCE_NOTE = """Scripts: `backend/app/equity_lab/spot_fetch.py`, `spot_features.py`, `spot_study.py`."""


def futures_cost_pct(day: date) -> float:
    """Round trip on an index future in % of notional: STT on the sale at the rate of the day, exchange, stamp, brokerage, 0.01% slippage a side. Rates as recalled."""
    stt = 0.0125 if day < date(2024, 10, 1) else 0.02 if day < date(2026, 4, 1) else 0.05
    return stt + 0.0045 + 0.002 + 0.003 + 0.02


def breakeven(data: pd.DataFrame) -> pd.DataFrame:
    """Index move (%) at which a bought ATM option / 2-step debit spread breaks even, from actual premiums on the TRAIN split.

    Net premium points are regressed on the index move in the trade's direction: pnl = a + b * move, break-even = -a / b."""
    rows = []
    train = data[data.day <= TRAIN_END]
    for underlying, h, kind, slip in itertools.product(INDICES, HORIZONS, ("atm", "spread"), ("lo", "hi")):
        g = train[train.underlying == underlying]
        move = np.concatenate([g[f"ret_{h}"].to_numpy(), -g[f"ret_{h}"].to_numpy()])
        pnl = np.concatenate([g[f"{kind}_CE_{h}_{slip}"].to_numpy(), g[f"{kind}_PE_{h}_{slip}"].to_numpy()])
        ok = np.isfinite(move) & np.isfinite(pnl) & (np.abs(move) < 3)
        if ok.sum() < 1000:
            continue
        b, a = np.polyfit(move[ok], pnl[ok], 1)
        rows.append({"underlying": underlying, "horizon": h, "instrument": kind, "slippage": "0.5%" if slip == "lo" else "1.5%", "trades": int(ok.sum()),
                     "avg_net_points": round(float(pnl[ok].mean()), 2), "points_per_1pct_move": round(float(b), 1), "breakeven_move_pct": round(float(-a / b), 4) if b > 0 else None})
    return pd.DataFrame(rows)


# ───────────────────────── evaluation of a set of signals ─────────────────────────
def non_overlapping(idx: np.ndarray, group: np.ndarray, bar: np.ndarray, h: int) -> np.ndarray:
    """Keep a signal only if the previous kept one in the same index and day has finished."""
    order = idx[np.lexsort((bar[idx], group[idx]))]
    keep, last_group, free = [], -1, 0
    for i in order:
        if group[i] != last_group:
            last_group, free = group[i], 0
        if bar[i] >= free:
            keep.append(i)
            free = bar[i] + h // 5
    return np.array(keep, dtype=int)


class Book:
    """The table as arrays, with the hurdles each signal is judged against."""

    def __init__(self, data: pd.DataFrame, hurdles: pd.DataFrame):
        self.data = data
        self.day = data.day.to_numpy()
        self.day_id = pd.factorize(self.day)[0]
        self.index_id = data.underlying.map({u: i for i, u in enumerate(INDICES)}).to_numpy()
        self.group = self.day_id * len(INDICES) + self.index_id
        self.bar = data.bar.to_numpy()
        self.split = np.where(self.day <= TRAIN_END, 0, np.where(self.day <= VALID_END, 1, 2))
        self.ret = {h: data[f"ret_{h}"].to_numpy(dtype=float) for h in HORIZONS}
        futures = np.array([futures_cost_pct(d) for d in self.day])
        self.hurdle: dict[int, dict[str, np.ndarray]] = {}
        for h in HORIZONS:
            self.hurdle[h] = {"futures": futures}
            for kind in ("atm", "spread"):
                table = hurdles[(hurdles.horizon == h) & (hurdles.instrument == kind) & (hurdles.slippage == "0.5%")]
                per = dict(zip(table.underlying, table.breakeven_move_pct))
                self.hurdle[h][kind] = np.array([per.get(u) or np.nan for u in data.underlying], dtype=float)
        self.valid_days = np.unique(self.day_id[self.split == 1])

    def stats(self, idx: np.ndarray, direction: np.ndarray, h: int, thin: bool = True, boot: bool = False) -> dict:
        """Signed index move after the signals in ``idx`` (``direction`` is aligned with the whole table)."""
        idx = idx[np.isfinite(self.ret[h][idx]) & (direction[idx] != 0)]
        if thin and len(idx):
            idx = non_overlapping(idx, self.group, self.bar, h)
        if not len(idx):
            return {"signals": 0}
        gross = direction[idx] * self.ret[h][idx]
        out = {"signals": int(len(idx)), "hit_rate": round(float((gross > 0).mean() * 100), 1), "avg_move_pct": round(float(gross.mean()), 4)}
        for name, hurdle in self.hurdle[h].items():
            net = gross - hurdle[idx]
            net = net[np.isfinite(net)]
            out[f"net_{name}"] = round(float(net.mean()), 4) if len(net) else None
            out[f"beats_{name}_pct"] = round(float((gross - hurdle[idx] > 0).mean() * 100), 1)
            if boot and len(net) > 30:
                ok = np.isfinite(gross - hurdle[idx])
                lo, hi = c.day_bootstrap(self.day_id[idx][ok], net)
                out[f"ci90_{name}"] = f"{lo:.4f} to {hi:.4f}"
                out[f"low_{name}"] = lo
        return out

    def daily(self, idx: np.ndarray, direction: np.ndarray, h: int, hurdle: str, days: np.ndarray) -> np.ndarray:
        """Summed net % per day over ``days`` (0 on days with no signal), for Sharpe ratios."""
        net = direction[idx] * self.ret[h][idx] - self.hurdle[h][hurdle][idx]
        ok = np.isfinite(net)
        total = np.bincount(self.day_id[idx][ok], weights=net[ok], minlength=int(self.day_id.max()) + 1)
        return total[days]


def day_cluster(day: np.ndarray, gross: np.ndarray, n_days: int) -> tuple[float, float]:
    """(mean per signal, 90% lower bound with whole days as the unit)."""
    sums, counts = np.bincount(day, weights=gross, minlength=n_days), np.bincount(day, minlength=n_days)
    on = counts > 0
    mean = sums[on].sum() / counts[on].sum()
    k = int(on.sum())
    if k < 20:
        return float(mean), -9.0
    resid = sums[on] - mean * counts[on]
    se = math.sqrt(k / (k - 1) * float((resid ** 2).sum())) / counts[on].sum()
    return float(mean), float(mean - 1.645 * se)


# ───────────────────────── single features and pairs ─────────────────────────
def codes(values: np.ndarray, train: np.ndarray, name: str, buckets: int) -> tuple[np.ndarray, list[str]]:
    if name in CATEGORIES:
        levels = np.unique(values[train & np.isfinite(values)])
        out = np.full(len(values), -1)
        for i, level in enumerate(levels):
            out[values == level] = i
        return out, [f"= {level:g}" for level in levels]
    edges = np.unique(np.nanquantile(values[train], np.linspace(0, 1, buckets + 1)[1:-1]))
    out = np.where(np.isfinite(values), np.searchsorted(edges, values, side="right"), -1)
    marks = [-np.inf, *edges, np.inf]
    return out, [f"[{marks[i]:.4g}, {marks[i + 1]:.4g})" for i in range(len(marks) - 1)]


def rules(book: Book, features: list[str]) -> pd.DataFrame:
    """Every single-feature bucket and every pair of tercile buckets. Direction comes from the train split only."""
    train = book.split == 0
    single = {f: codes(book.data[f].to_numpy(dtype=float), train, f, 5) for f in features}
    pair = {f: codes(book.data[f].to_numpy(dtype=float), train, f, 3) for f in features}
    combos = [((f,), single) for f in features] + [(p, pair) for p in itertools.combinations(features, 2)]
    pre = book.split < 2                                                   # the final split is never touched here
    n_days = int(book.day_id.max()) + 1
    out = []
    for h in HORIZONS:
        ret = book.ret[h]
        usable = pre & np.isfinite(ret)
        rows = np.flatnonzero(usable)
        r, split, day = ret[rows], book.split[rows], book.day_id[rows]
        for names, table in combos:
            arrays = [table[f][0][rows] for f in names]
            sizes = [len(table[f][1]) for f in names]
            key = arrays[0].copy()
            valid = arrays[0] >= 0
            for a, size in zip(arrays[1:], sizes[1:]):
                key = key * size + a
                valid &= a >= 0
            order = np.argsort(np.where(valid, key, -1), kind="stable")
            ordered = np.where(valid, key, -1)[order]
            cells = int(np.prod(sizes))
            bounds = np.searchsorted(ordered, np.arange(cells + 1))
            for cell in range(cells):
                at = order[bounds[cell]:bounds[cell + 1]]
                tr, va = at[split[at] == 0], at[split[at] == 1]
                rest, picks = cell, []
                for size in sizes[::-1]:
                    picks.append(rest % size)
                    rest //= size
                label = " & ".join(f"{f} {table[f][1][p]}" for f, p in zip(names, picks[::-1]))
                row = {"horizon": h, "rule": label, "features": "|".join(names), "cells": "|".join(map(str, picks[::-1])), "train_n": len(tr), "valid_n": len(va)}
                if len(tr) >= MIN_TRAIN and len(va) >= MIN_VALID:
                    sign = 1.0 if r[tr].mean() >= 0 else -1.0
                    mean, low = day_cluster(day[va], sign * r[va], n_days)
                    daily = np.bincount(day[va], weights=sign * r[va] - book.hurdle[h]["futures"][rows][va], minlength=n_days)[book.valid_days]
                    row.update({"direction": int(sign), "train_move_pct": round(float(abs(r[tr].mean())), 4), "train_hit": round(float((sign * r[tr] > 0).mean() * 100), 1),
                                "valid_move_pct": round(mean, 4), "valid_low90_pct": round(low, 4), "valid_hit": round(float((sign * r[va] > 0).mean() * 100), 1),
                                "valid_daily_sharpe_after_futures_cost": round(float(daily.mean() / daily.std()), 4) if daily.std() > 0 else None})
                out.append(row)
        print("rules done for", h, flush=True)
    return pd.DataFrame(out)


def rule_signal(book: Book, features: list[str], row: pd.Series) -> np.ndarray:
    names, cells = row["features"].split("|"), [int(x) for x in str(row["cells"]).split("|")]
    on = np.ones(len(book.data), dtype=bool)
    for name, cell in zip(names, cells):
        on &= codes(book.data[name].to_numpy(dtype=float), book.split == 0, name, 5 if len(names) == 1 else 3)[0] == cell
    return np.where(on, float(row["direction"]), 0.0)


# ───────────────────────── walk-forward models ─────────────────────────
def make(algo: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if algo == "logistic":
        return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), LogisticRegression(C=0.05, max_iter=500))
    return HistGradientBoostingClassifier(max_depth=3, max_iter=120, learning_rate=0.03, min_samples_leaf=500, l2_regularization=1.0, early_stopping=False, random_state=7)


def month_key(days: np.ndarray) -> np.ndarray:
    return np.array([d.year * 12 + d.month - 1 for d in days])


def walk_forward(book: Book, x: np.ndarray, algo: str, target: str, h: int, months: list[int], key: np.ndarray, importance: bool = False):
    """Train on the previous 12 months, predict the next month. Returns (score per row, per-row threshold for each TOPS, probability, importance)."""
    from sklearn.metrics import log_loss
    n = len(book.data)
    score, prob = np.full(n, np.nan), np.full(n, np.nan)
    cut = {q: np.full(n, np.nan) for q in TOPS}
    gains = np.zeros(x.shape[1])
    counted = 0
    rng = np.random.default_rng(7)
    ret, move = book.ret[h], book.hurdle[h]["spread"]
    for month in months:
        train = np.flatnonzero((key >= month - 12) & (key < month) & np.isfinite(ret))
        test = np.flatnonzero((key == month) & np.isfinite(ret))
        if not len(test) or len(train) < 5000:
            continue
        if target == "sign":
            labels = [ret[train] > 0]
        else:                                                              # a move bigger than the debit-spread break-even, up or down
            limit = np.nan_to_num(move[train], nan=float(np.nanmedian(move)))
            labels = [ret[train] > limit, ret[train] < -limit]
        if any(y.sum() < 100 or (~y).sum() < 100 for y in labels):
            continue
        models = [make(algo).fit(x[train], y) for y in labels]
        p_in = [m.predict_proba(x[train])[:, 1] for m in models]
        p_out = [m.predict_proba(x[test])[:, 1] for m in models]
        inside = p_in[0] - 0.5 if target == "sign" else p_in[0] - p_in[1]
        outside = p_out[0] - 0.5 if target == "sign" else p_out[0] - p_out[1]
        score[test], prob[test] = outside, p_out[0]
        for q in TOPS:
            cut[q][test] = np.quantile(np.abs(inside), 1 - q)             # threshold from the training window only
        inner = book.split[test] == 1                                      # a month can straddle a split: validation rows only
        if importance and inner.any():
            rows = test[inner]
            y = ret[rows] > (0 if target == "sign" else np.nan_to_num(move[rows], nan=float(np.nanmedian(move))))
            p_valid = p_out[0][inner]
            if y.any() and (~y).any():
                base = log_loss(y, p_valid, labels=[False, True])
                shuffled = x[rows].copy()
                for j in range(x.shape[1]):
                    keep = shuffled[:, j].copy()
                    shuffled[:, j] = rng.permutation(keep)
                    gains[j] += log_loss(y, models[0].predict_proba(shuffled)[:, 1], labels=[False, True]) - base
                    shuffled[:, j] = keep
                counted += 1
    return score, cut, prob, gains / max(counted, 1)


def signal_from(score: np.ndarray, cut: np.ndarray) -> np.ndarray:
    return np.where(np.isfinite(score) & (np.abs(score) >= cut), np.sign(score), 0.0)


def models(book: Book, features: list[str]):
    x = book.data[features].to_numpy(dtype="float32")
    key = month_key(book.day)
    pre = sorted(set(key[book.split < 2]))
    months = [m for m in pre if m >= min(pre) + 12]
    pre_x = book.split < 2
    rows, runs, importances, calibration, slice_rows = [], {}, [], [], []
    for algo, target, h in itertools.product(("logistic", "boosting"), ("sign", "big move"), HORIZONS):
        score, cut, prob, gains = walk_forward(book, x, algo, target, h, months, key, importance=True)
        kept = [j for j in range(len(features)) if gains[j] > 0]
        for j, f in enumerate(features):
            importances.append({"model": algo, "target": target, "horizon": h, "feature": f, "logloss_gain_on_validation": round(float(gains[j]), 6), "kept": gains[j] > 0})
        variants = [("all features", list(range(len(features))), score, cut, prob)]
        if len(kept) >= 2:
            s2, c2, p2, _ = walk_forward(book, x[:, kept], algo, target, h, months, key)
            variants.append(("helpful features", kept, s2, c2, p2))
        for subset, cols, s, ct, p in variants:
            # the walk-forward months include the validation split's last month; rows after VALID_END are blanked so the final split stays unseen
            s = np.where(pre_x, s, np.nan)
            for q in TOPS:
                direction = signal_from(s, ct[q])
                name = f"{algo} | {target} | {subset} ({len(cols)}) | top {int(q * 100)}%"
                row = {"model": name, "horizon": h}
                for split, label in ((0, "train_oos"), (1, "valid")):
                    idx = np.flatnonzero((book.split == split) & (direction != 0))
                    got = book.stats(idx, direction, h)
                    row.update({f"{label}_{k}": v for k, v in got.items()})
                    if split == 1 and got["signals"]:
                        thin = non_overlapping(idx[np.isfinite(book.ret[h][idx])], book.group, book.bar, h)
                        gross = direction[thin] * book.ret[h][thin]
                        row["valid_low90_pct"] = round(day_cluster(book.day_id[thin], gross, int(book.day_id.max()) + 1)[1], 4)
                        daily = book.daily(thin, direction, h, "futures", book.valid_days)
                        row["valid_daily_sharpe_after_futures_cost"] = round(float(daily.mean() / daily.std()), 4) if daily.std() > 0 else None
                rows.append(row)
                runs[(name, h)] = {"algo": algo, "target": target, "h": h, "cols": cols, "top": q, "direction": direction}
            if target == "sign":
                ok = np.isfinite(p) & pre_x & np.isfinite(book.ret[h])
                bins = pd.qcut(p[ok], 10, labels=False, duplicates="drop")
                for b in np.unique(bins):
                    sel = bins == b
                    calibration.append({"model": algo, "features": subset, "horizon": h, "decile": int(b) + 1, "rows": int(sel.sum()),
                                        "predicted_up_pct": round(float(p[ok][sel].mean() * 100), 1), "actual_up_pct": round(float((book.ret[h][ok][sel] > 0).mean() * 100), 1)})
                at = np.flatnonzero(ok)
                slices = {"index": np.array(INDICES)[book.index_id[at]], "year": np.array([str(d.year) for d in book.day[at]]),
                          "time of day": time_of_day(book.bar[at])}
                called_up = p[at] > 0.5
                went_up = book.ret[h][at] > 0
                for title, labels in slices.items():
                    for label in sorted(set(labels)):
                        sel = labels == label
                        slice_rows.append({"model": algo, "features": subset, "horizon": h, "by": title, "slice": label, "rows": int(sel.sum()),
                                           "predicted_up_pct": round(float(p[at][sel].mean() * 100), 1), "actual_up_pct": round(float(went_up[sel].mean() * 100), 1),
                                           "hit_rate_pct": round(float((called_up[sel] == went_up[sel]).mean() * 100), 1)})
        print("model done", algo, target, h, "kept", len(kept), flush=True)
    return pd.DataFrame(rows), runs, pd.DataFrame(importances), pd.DataFrame(calibration), pd.DataFrame(slice_rows), months


def time_of_day(bar: np.ndarray) -> np.ndarray:
    return np.select([bar < 9, bar < 33, bar < 57], ["09:15-10:00", "10:00-12:00", "12:00-14:00"], "14:00-15:30")


def breakdown(book: Book, direction: np.ndarray, h: int, splits: tuple[int, ...]) -> dict[str, pd.DataFrame]:
    idx = np.flatnonzero(np.isin(book.split, splits) & (direction != 0) & np.isfinite(book.ret[h]))
    idx = non_overlapping(idx, book.group, book.bar, h) if len(idx) else idx
    when = time_of_day(book.bar[idx])
    slices = {"index": np.array(INDICES)[book.index_id[idx]], "year": np.array([str(d.year) for d in book.day[idx]]), "time of day": when}
    out = {}
    for title, labels in slices.items():
        out[title] = pd.DataFrame([{title: label, **book.stats(idx[labels == label], direction, h, thin=False)} for label in sorted(set(labels))])
    return out


def md(frame: pd.DataFrame) -> str:
    if not len(frame):
        return "None."
    rows = frame.astype(object).where(frame.notna(), None).to_dict("records")
    return c.table(rows, [x for x in frame.columns if not x.startswith("low_")])


def main() -> None:
    data, features, coverage = build()
    hurdles = breakeven(data)
    book = Book(data, hurdles)
    runs_dir = c.REPORTS / "delta_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rule_table = rules(book, features)
    rule_table.to_csv(runs_dir / "spot_rules.csv", index=False)
    model_table, runs, importance, calibration, calibration_slices, months = models(book, features)
    model_table.to_csv(runs_dir / "spot_models.csv", index=False)
    importance.to_csv(runs_dir / "spot_importance.csv", index=False)
    calibration.to_csv(runs_dir / "spot_calibration.csv", index=False)
    calibration_slices.to_csv(runs_dir / "spot_calibration_slices.csv", index=False)

    tested = rule_table.dropna(subset=["valid_low90_pct"])
    best_rules = tested.sort_values("valid_low90_pct", ascending=False).head(40)
    shortlist = []
    for _, row in best_rules.iterrows():                                    # re-judge the leaders without overlapping signals
        direction = rule_signal(book, features, row)
        h = int(row.horizon)
        idx = np.flatnonzero((book.split == 1) & (direction != 0) & np.isfinite(book.ret[h]))
        thin = non_overlapping(idx, book.group, book.bar, h)
        if len(thin) < MIN_VALID:
            continue
        _, low = day_cluster(book.day_id[thin], direction[thin] * book.ret[h][thin], int(book.day_id.max()) + 1)
        shortlist.append({"kind": "rule", "name": row.rule, "h": h, "low": low, "direction": direction, "valid": book.stats(idx, direction, h, boot=True)})
    model_ok = model_table[(model_table.valid_signals >= MIN_VALID) & model_table.valid_low90_pct.notna()]
    for _, row in model_ok.sort_values("valid_low90_pct", ascending=False).head(10).iterrows():
        run = runs[(row.model, row.horizon)]
        idx = np.flatnonzero((book.split == 1) & (run["direction"] != 0))
        shortlist.append({"kind": "model", "name": row.model, "h": int(row.horizon), "low": float(row.valid_low90_pct), "direction": run["direction"], "run": run,
                          "valid": book.stats(idx, run["direction"], int(row.horizon), boot=True)})
    shortlist.sort(key=lambda s: s["low"], reverse=True)
    chosen = shortlist[0]
    h = chosen["h"]

    # ── the final split, once, for the chosen candidate only ──
    direction = chosen["direction"].copy()
    if chosen["kind"] == "model":
        run = chosen["run"]
        key = month_key(book.day)
        later = sorted(set(key[book.split == 2]))
        x = data[features].to_numpy(dtype="float32")[:, run["cols"]]
        score, cut, _, _ = walk_forward(book, x, run["algo"], run["target"], h, later, key)
        direction = np.where(book.split == 2, signal_from(score, cut[run["top"]]), direction)
    final_idx = np.flatnonzero((book.split == 2) & (direction != 0))
    final = book.stats(final_idx, direction, h, boot=True)
    passed = {name: final.get(f"low_{name}") is not None and final[f"low_{name}"] > 0 for name in ("futures", "atm", "spread")}

    new_trials = len(rule_table) + len(model_table)
    total = PRIOR_TRIALS + new_trials
    sharpes = pd.concat([rule_table.valid_daily_sharpe_after_futures_cost, model_table.valid_daily_sharpe_after_futures_cost]).dropna().to_numpy(dtype=float)
    var_sr = float(sharpes.var())
    valid_idx = np.flatnonzero((book.split == 1) & (direction != 0) & np.isfinite(book.ret[h]))
    dsr = {}
    for split, label, idx in ((1, "validation", valid_idx), (2, "final", final_idx[np.isfinite(book.ret[h][final_idx])])):
        thin = non_overlapping(idx, book.group, book.bar, h) if len(idx) else idx
        daily = book.daily(thin, direction, h, "futures", np.unique(book.day_id[book.split == split]))
        got = K.dsr_prob(daily, total, var_sr)
        dsr[label] = None if got is None else round(float(got), 4)

    verdict = "YES" if any(passed.values()) else "NO"
    show = lambda s: {"candidate": s["name"], "horizon": s["h"], **{k: v for k, v in s["valid"].items() if not k.startswith("low_")}}
    lines = ["# Spot-driven features: do they predict beyond costs?", "", "Research only. Decision support, not investment advice. Hypothetical; no order was placed and nothing was deployed.", "",
             "## Verdict", "", f"**{verdict}** — " + ("the chosen candidate's final-check 90% interval is above zero against: " + ", ".join(k for k, v in passed.items() if v) + ". Step 3 has NOT been run yet."
                                                      if verdict == "YES" else "no spot feature set beat the cost of trading it out of sample. Step 3 (instrument choice) was not run, and no paper test is proposed."), "",
             f"- Chosen on validation: `{chosen['name']}`, {h}-minute horizon.",
             f"- Final check (after {VALID_END}), non-overlapping signals: " + json.dumps({k: v for k, v in final.items() if not k.startswith("low_")}),
             f"- Deflated Sharpe (probability the true Sharpe beats the best expected from luck; futures hurdle, N = {total:,}): validation {dsr['validation']}, final {dsr['final']}.",
             f"- Tried here: {len(rule_table):,} rule cells ({len(tested):,} had enough trades to be judged) and {len(model_table)} model settings. Running total {total:,}.", "",
             "`net_*` is the average signed index move minus that instrument's break-even move, in % of the index. Above zero means it paid for itself.", "",
             "## Data and method", ""]
    for u, cv in coverage.items():
        lines += [f"- **{u}**: {cv['days'][0]} days, {cv['days'][1]} to {cv['days'][2]}. Constituents found {cv['names']} of {cv['wanted']}.",
                  f"  - Volume proxies (days, first, last): constituents {cv['cons']}, ATM option volume {cv['opt']}, nearest future {cv['fut']}.",
                  f"  - Rank correlation of same-time relative volume (value, bars): constituents/options {cv['corr_cons_opt']}, constituents/futures {cv['corr_cons_fut']}, options/futures {cv['corr_opt_fut']}."]
    lines += ["- The futures proxy covers only a few weeks (expired contracts cannot be downloaded), so it is compared above but not modelled.",
              "- The constituent proxy is summed traded value (price × shares), so cheap shares do not dominate. VWAP of the index is weighted by it.",
              "- Constituents and weights are today's, as recalled and not verified (survivorship bias). SENSEX uses NSE listings. Share prices are Dhan intraday, not re-adjusted here.",
              "- Option features use ATM±4 strikes (BANKNIFTY monthly: ±2 only). SENSEX options start in May 2023.",
              f"- Splits: train to {TRAIN_END}, validation to {VALID_END}, final after. Models: 12-month rolling window, next month predicted, from {months[0] // 12}-{months[0] % 12 + 1:02d}.",
              "- Rules: direction and bucket edges from train only; screened on validation with whole days as the unit; leaders re-judged without overlapping signals.",
              "- The break-even move that labels the `big move` target was estimated on the whole train split, so early walk-forward months see a cost level (not a price) from later.",
              "- Lot sizes, expiry weekdays and statutory rates are as recalled, not re-verified. Slippage is assumed: Dhan has no historical bid/ask.",
              "- The final period is not untouched history: the earlier scalp study already looked at it for a different strategy. Here it was evaluated for one candidate only.",
              "- The `big move` target uses the debit-spread break-even, which came out HIGHER than the ATM option's (very high for BANKNIFTY). It was not re-run with the ATM hurdle. All bars are Dhan data; Yahoo was not used.", "",
              "## Break-even move (train split, actual premiums)", "", md(hurdles), "",
              f"Index futures hurdle (assumed, round trip): {futures_cost_pct(date(2023, 1, 1)):.4f}% before Oct 2024, {futures_cost_pct(date(2025, 1, 1)):.4f}% to Mar 2026, {futures_cost_pct(date(2026, 5, 1)):.4f}% after.", "",
              "## Leaders on validation (non-overlapping)", "", md(pd.DataFrame([show(s) for s in shortlist[:15]])), "",
              "## Best single-feature buckets on validation (overlapping signals)", "",
              md(tested[~tested.features.str.contains("|", regex=False)].sort_values("valid_low90_pct", ascending=False).head(15)[["horizon", "rule", "direction", "train_n", "train_move_pct", "valid_n", "valid_move_pct", "valid_low90_pct", "valid_hit"]]), "",
              "## All model settings", "",
              md(model_table[[x for x in ("model", "horizon", "train_oos_signals", "train_oos_hit_rate", "train_oos_avg_move_pct", "valid_signals", "valid_hit_rate", "valid_avg_move_pct", "valid_low90_pct", "valid_net_futures", "valid_net_spread", "valid_net_atm") if x in model_table]]), "",
              "## Chosen candidate: out-of-sample breakdown", "", "All splits after the rule or window was learned (train-split rows of a rule are in sample)."]
    for title, frame in breakdown(book, direction, h, (0, 1, 2) if chosen["kind"] == "model" else (1, 2)).items():
        lines += ["", f"### By {title}", "", md(frame)]
    best_model = next((s for s in shortlist if s["kind"] == "model"), None)
    if best_model and best_model is not chosen:
        lines += ["", f"## Best model on validation (`{best_model['name']}`, {best_model['h']} min): up to {VALID_END}, final period not touched"]
        for title, frame in breakdown(book, best_model["direction"], best_model["h"], (0, 1)).items():
            lines += ["", f"### By {title}", "", md(frame)]
    top = importance.groupby("feature").agg(avg_gain=("logloss_gain_on_validation", "mean"), kept_in_settings=("kept", "sum")).sort_values("avg_gain", ascending=False).reset_index()
    top["avg_gain"] = top.avg_gain.round(6)
    lines += ["", "## Feature importance (permutation, validation months; gain in log loss, above zero helps)", "", md(top), "",
              "## Calibration of the direction models (walk-forward, up to validation end)", "", md(calibration[calibration.features == "all features"]), "",
              "### By index, year and time of day (all features)", "", md(calibration_slices[calibration_slices.features == "all features"]), "",
              "## Every rule and model tried", "",
              "- `reports/delta_runs/spot_rules.csv` (every rule cell, including those with too few trades)", "- `reports/delta_runs/spot_models.csv`",
              "- `reports/delta_runs/spot_importance.csv`, `reports/delta_runs/spot_calibration.csv`, `reports/delta_runs/spot_calibration_slices.csv`", "", SOURCE_NOTE, ""]
    (c.REPORTS / REPORT).write_text("\n".join(lines))
    print("VERDICT", verdict, chosen["name"], h, json.dumps(final, default=str), "dsr", dsr, "trials", total, flush=True)


if __name__ == "__main__":
    main()
