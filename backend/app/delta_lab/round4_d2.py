"""Round 4 / Part D2: intraday crypto families on the most liquid Delta perpetuals, 1-hour bars. Hypothetical research; decision support, not investment advice.

COSTS (Delta India, per side, checked 24 Nov 2026 against the product record `taker_commission_rate`; maker 0.02% is Delta's published schedule): taker 0.05%, maker 0.02%,
18% GST on the fees, slippage ASSUMED (no order-book recording exists for these symbols): BTC/ETH 1 bp, others 4 bps per taker fill; funding at 00/08/16 UTC from the FUNDING series
(value in percent per 8 h; a long pays a positive rate). Entry modes: `taker` = next bar open + slippage; `maker` = post-only limit at the signal bar's close (best bid/ask proxy), filled only
if the next bar trades THROUGH it, otherwise a missed fill (recorded with what it would have earned as a taker). Stops fill at the stop or the bar open if it gapped (a stop hit is assumed
before a same-bar target); exits at the time limit are taker fills at the bar close.
SPLITS (frozen before any result was looked at): train = first 50%, validation = next 25%, HOLDOUT = latest 25% of the calendar span. Variants are chosen on train+validation only; each
finalist touches the holdout ONCE, with frozen parameters (hash recorded). Data: Delta India 1h last-price candles and funding for the 10 most liquid perpetuals by 24 h turnover.
Not testable here (stated, not faked): book imbalance (only a day or two of recorded book), open interest (no history), delisted contracts (Delta lists no history for them)."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from . import data

DATA = data.DATA_DIR
TAKER, MAKER, GST = 0.0005, 0.0002, 0.18
SLIP_BPS = {"BTCUSD": 1.0, "ETHUSD": 1.0}
DEFAULT_SLIP = 4.0
N_PRIOR_VARIANTS = 148
Z = NormalDist()


@dataclass
class Sym:
    name: str
    t: np.ndarray; o: np.ndarray; h: np.ndarray; l: np.ndarray; c: np.ndarray; v: np.ndarray
    atr: np.ndarray
    funding: dict[int, float]                     # funding time -> rate as a fraction per 8 h


def sym_from_frames(name: str, df: pd.DataFrame, fd: pd.DataFrame) -> Sym:
    """Same arrays as `load`, from frames already in memory (the live scanner uses this)."""
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = df["volume"].astype(float).to_numpy()
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    funding = {int(t): float(x) / 100.0 for t, x in zip(fd["time"], fd["close"]) if int(t) % 28800 == 0}
    return Sym(name, df["time"].to_numpy(np.int64), o, h, l, c, v, atr, funding)


def load(name: str) -> Sym | None:
    try:
        df = pd.read_pickle(DATA / f"d2_{name}_1h.pkl")
        fd = pd.read_pickle(DATA / f"d2_FUNDING_{name}_1h.pkl")
    except FileNotFoundError:
        return None
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = df["volume"].astype(float).to_numpy()
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    funding = {int(t): float(x) / 100.0 for t, x in zip(fd["time"], fd["close"]) if int(t) % 28800 == 0}
    return Sym(name, df["time"].to_numpy(np.int64), o, h, l, c, v, atr, funding)


# ---------------------------------------------------------------- execution
def _fees_bps(maker: bool) -> float:
    return (MAKER if maker else TAKER) * 1e4 * (1 + GST)


def run(s: Sym, entries: list[dict], mode: str) -> tuple[list[dict], list[dict]]:
    """entries: {i, side(+1/-1), stop, target|None, hold}. i = signal bar (known at its close). One position at a time. Returns (trades, missed)."""
    slip = SLIP_BPS.get(s.name, DEFAULT_SLIP)
    n = len(s.t)
    trades: list[dict] = []
    missed: list[dict] = []
    busy = -1
    for e in sorted(entries, key=lambda x: x["i"]):
        i, side, stop, target, hold = e["i"], e["side"], e["stop"], e.get("target"), e["hold"]
        j = i + 1
        if i <= busy or j >= n - 1:
            continue
        if mode == "maker":
            limit = s.c[i]
            filled = (s.l[j] < limit) if side > 0 else (s.h[j] > limit)
            if not filled:
                ref = s.o[j]
                missed.append({"i": i, "t": int(s.t[i]), "would_net_bps": _simulate(s, e, ref, ref * (1 + side * slip / 1e4), False, j, n, slip)["net_bps"]})
                continue
            px, ent_maker = limit, True
        else:
            px, ent_maker = s.o[j] * (1 + side * slip / 1e4), False
        if (side > 0 and stop >= px) or (side < 0 and stop <= px):
            continue                                                               # a stop on the wrong side of the entry is not a trade
        tr = _simulate(s, e, s.o[j] if not ent_maker else limit, px, ent_maker, j, n, slip)
        tr.update(symbol=s.name, t=int(s.t[i]), side=side, family=e.get("family", ""))
        busy = tr["exit_i"]
        trades.append(tr)
    return trades, missed


def _simulate(s: Sym, e: dict, ref_entry: float, entry_px: float, ent_maker: bool, j: int, n: int, slip: float) -> dict:
    side, stop, target, hold = e["side"], e["stop"], e.get("target"), e["hold"]
    exit_px, exit_i, reason, exit_maker = None, min(n - 1, j + hold), "time", False
    for k in range(j, min(n - 1, j + hold) + 1):
        hit_stop = s.l[k] <= stop if side > 0 else s.h[k] >= stop
        if hit_stop:
            base = min(stop, s.o[k]) if (side > 0 and k > j) else max(stop, s.o[k]) if (side < 0 and k > j) else stop
            exit_px, exit_i, reason = base * (1 - side * slip / 1e4), k, "stop"
            break
        if target is not None and k > j and ((s.h[k] > target) if side > 0 else (s.l[k] < target)):
            exit_px, exit_i, reason, exit_maker = target, k, "target", True
            break
    if exit_px is None:
        exit_px = s.c[exit_i] * (1 - side * slip / 1e4)
    gross_bps = side * (exit_px - entry_px) / entry_px * 1e4
    fee_bps = _fees_bps(ent_maker) + _fees_bps(exit_maker)
    slip_note = (0 if ent_maker else slip) + (0 if exit_maker else slip)
    t_in, t_out = int(s.t[j]), int(s.t[exit_i])
    funding = 0.0
    for ts in range((t_in // 28800 + 1) * 28800, t_out + 1, 28800):
        funding += s.funding.get(ts, 0.0) * 1e4 * side                              # bps of notional; a long pays a positive rate
    net = gross_bps - fee_bps - funding
    return {"entry_px": entry_px, "exit_px": exit_px, "stop": stop, "t_in": t_in, "t_out": t_out, "net_bps": net, "gross_bps": gross_bps + slip_note, "fees_bps": fee_bps, "slip_bps": slip_note, "funding_bps": funding, "exit_i": exit_i, "reason": reason, "maker_entry": ent_maker}


# ---------------------------------------------------------------- families
def _prior_ext(x: np.ndarray, n: int, fn) -> np.ndarray:
    s = pd.Series(x).shift(1).rolling(n)
    return (s.max() if fn == "max" else s.min()).to_numpy()


def f1_sweep(s: Sym, N: int, vol_k: float, K: int, R: float) -> list[dict]:
    hi, lo = _prior_ext(s.h, N, "max"), _prior_ext(s.l, N, "min")
    vmed = pd.Series(s.v).rolling(20).median().shift(1).to_numpy()
    out = []
    for i in range(N + 20, len(s.t) - 2):
        for side, level, ext in ((1, lo[i - K + 1], np.min(s.l[i - K + 1:i + 1])), (-1, hi[i - K + 1], np.max(s.h[i - K + 1:i + 1]))):
            swept = ext < level if side > 0 else ext > level
            inside = s.c[i] > level if side > 0 else s.c[i] < level
            spike = np.max(s.v[i - K + 1:i + 1]) > vol_k * vmed[i] if vmed[i] > 0 else False
            if swept and inside and spike:
                stop = ext - side * 0.25 * s.atr[i]
                risk = abs(s.c[i] - stop)
                out.append({"i": i, "side": side, "stop": stop, "target": s.c[i] + side * R * risk, "hold": 24, "family": "F1_sweep"})
                break
    return out


def f2_funding(s: Sym, pct: float, stall: int, hold: int, stop_atr: float) -> list[dict]:
    keys = sorted(s.funding)
    rates = pd.Series([s.funding[k] for k in keys], index=keys)
    rank = rates.rolling(90).rank(pct=True)
    out = []
    for ts, r in rank.items():
        if not np.isfinite(r):
            continue
        idx = int(np.searchsorted(s.t, ts))
        if idx >= len(s.t) - 2 or idx < stall + 1:
            continue
        stalled_up = s.c[idx] <= s.c[idx - stall]
        stalled_dn = s.c[idx] >= s.c[idx - stall]
        if r >= pct and stalled_up:                                                   # crowded longs, price not rising: short
            out.append({"i": idx, "side": -1, "stop": s.c[idx] + stop_atr * s.atr[idx], "target": None, "hold": hold, "family": "F2_funding"})
        elif r <= 1 - pct and stalled_dn:
            out.append({"i": idx, "side": 1, "stop": s.c[idx] - stop_atr * s.atr[idx], "target": None, "hold": hold, "family": "F2_funding"})
    return out


SESSION_HOURS = {"asia": 1, "europe": 7, "us": 14}


def f4_session(s: Sym, session: str, hold: int, follow: bool) -> list[dict]:
    out = []
    hr = ((s.t // 3600) % 24)
    for i in np.where(hr == SESSION_HOURS[session])[0]:
        if i < 2 or i >= len(s.t) - 2:
            continue
        d = np.sign(s.c[i] - s.o[i])                                                  # direction of the first session hour (known at its close)
        if d == 0:
            continue
        side = int(d if follow else -d)
        out.append({"i": int(i), "side": side, "stop": s.c[i] - side * 2.0 * s.atr[i], "target": None, "hold": hold, "family": "F4_session"})
    return out


def f6_ema(s: Sym, ema_n: int, rr: float, direction: int) -> list[dict]:
    """The alert-candle rule of app.crypto_delta.forward on hourly bars. Unlike the live replay (stop order filled at the alert level inside the trigger bar), entry here waits for the trigger bar's close: slower and more conservative."""
    ema = pd.Series(s.c).ewm(span=ema_n, adjust=False).mean().to_numpy()
    out, sell, buy = [], None, None
    for i in range(ema_n + 5, len(s.t) - 2):
        if sell is not None and s.l[i] < sell[0]:
            if direction in (-1, 0):                                                  # decided at the CLOSE of the trigger bar (no look-ahead), entered at the next open
                stop = sell[1]
                out.append({"i": i, "side": -1, "stop": stop, "target": s.c[i] - rr * (stop - s.c[i]), "hold": 24, "family": "F6_5ema"})
            sell = None
        if buy is not None and s.h[i] > buy[0]:
            if direction in (0, 1):
                stop = buy[1]
                out.append({"i": i, "side": 1, "stop": stop, "target": s.c[i] + rr * (s.c[i] - stop), "hold": 24, "family": "F6_5ema"})
            buy = None
        if s.l[i] > ema[i]:
            sell = (s.l[i], s.h[i])
        if s.h[i] < ema[i]:
            buy = (s.h[i], s.l[i])
    return out


GRIDS: dict[str, list[tuple]] = {
    "F1_sweep": list(product((24, 48, 96), (1.5, 2.0), (1, 2), (1.5, 2.0))),
    "F2_funding": list(product((0.9, 0.95), (6, 12), (24, 48), (1.5, 2.5))),
    "F4_session": list(product(("asia", "europe", "us"), (2, 4), (True, False))),
    "F6_5ema": list(product((5,), (2.0, 3.0), (-1, 0))),
}
GEN: dict[str, Callable] = {"F1_sweep": f1_sweep, "F2_funding": f2_funding, "F4_session": f4_session, "F6_5ema": f6_ema}


# ---------------------------------------------------------------- statistics
def stats(trades: list[dict], missed: list[dict] | None = None) -> dict:
    r = np.array([t["net_bps"] for t in trades], float)
    if len(r) == 0:
        return {"trades": 0}
    g = np.array([t["gross_bps"] for t in trades], float)
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    cum = np.cumsum(r)
    out = {"trades": int(len(r)), "gross_bps_per_trade": float(g.mean()), "net_bps_per_trade": float(r.mean()), "profit_factor": float(wins / losses) if losses > 0 else None,
           "max_drawdown_bps": float((np.maximum.accumulate(cum) - cum).max()), "win_rate": float((r > 0).mean())}
    if missed is not None:
        out["missed_fills"] = len(missed)
        out["missed_would_net_bps"] = float(np.mean([m["would_net_bps"] for m in missed])) if missed else None
    return out


def boot_ci(r: np.ndarray, n: int = 2000, seed: int = 11) -> tuple[float, float]:
    if len(r) < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    m = rng.choice(r, size=(n, len(r)), replace=True).mean(axis=1)
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def dsr(returns: np.ndarray, n_variants: int, var_sr: float) -> float:
    T = len(returns)
    if T < 10 or returns.std(ddof=1) == 0:
        return float("nan")
    sr = returns.mean() / returns.std(ddof=1)
    gam = 0.5772156649
    sr0 = math.sqrt(var_sr) * ((1 - gam) * Z.inv_cdf(1 - 1 / n_variants) + gam * Z.inv_cdf(1 - 1 / (n_variants * math.e)))
    c = returns - returns.mean()
    skew, kurt = float((c ** 3).mean() / returns.std() ** 3), float((c ** 4).mean() / returns.std() ** 4)
    return float(Z.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(max(1e-9, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))))


def param_hash(family: str, params: tuple, mode: str) -> str:
    return hashlib.sha256(json.dumps([family, list(params), mode], default=str).encode()).hexdigest()[:16]


def split_edges(syms: list[Sym]) -> tuple[int, int, int, int]:
    lo = min(int(s.t[0]) for s in syms)
    hi = max(int(s.t[-1]) for s in syms)
    span = hi - lo
    return lo, lo + int(span * 0.50), lo + int(span * 0.75), hi


# ---------------------------------------------------------------- F5 cross-sectional (market neutral)
def f5_cross(syms: list[Sym], lookback: int, hold: int, reverse: bool, k: int = 2, window: tuple[int, int] | None = None, mode: str = "taker", rng: np.random.Generator | None = None) -> tuple[list[dict], list[dict]]:
    """Every `hold` hours rank the symbols by their past `lookback`-hour return; long the top k, short the bottom k (reverse flips it), equal weight per leg, held `hold` hours.
    Each leg pays the taker costs of a round trip (the maker mode is not applied: rebalances are time-driven, not signal-limited). `rng` shuffles the ranks (the random baseline)."""
    slip = {s.name: SLIP_BPS.get(s.name, DEFAULT_SLIP) for s in syms}
    maps = {s.name: {int(t): i for i, t in enumerate(s.t)} for s in syms}
    t0 = max(int(s.t[0]) for s in syms) if len(syms) < 1 else min(int(s.t[0]) for s in syms)
    t1 = max(int(s.t[-1]) for s in syms)
    legs: list[dict] = []
    for ts in range((t0 // 3600 + lookback) * 3600, t1 - hold * 3600, hold * 3600):
        if window and not (window[0] <= ts < window[1]):
            continue
        rows = []
        for s in syms:
            i, j = maps[s.name].get(ts), maps[s.name].get(ts + hold * 3600)
            p = maps[s.name].get(ts - lookback * 3600)
            if i is None or j is None or p is None:
                continue
            rows.append((s, i, j, s.c[i] / s.c[p] - 1))
        if len(rows) < 6:
            continue
        order = list(rng.permutation(len(rows))) if rng is not None else sorted(range(len(rows)), key=lambda x: rows[x][3])
        longs, shorts = (order[:k], order[-k:]) if reverse else (order[-k:], order[:k])
        for side, picks in ((1, longs), (-1, shorts)):
            for x in picks:
                s, i, j, _ = rows[x]
                entry = s.o[i + 1] * (1 + side * slip[s.name] / 1e4) if i + 1 < len(s.t) else s.c[i]
                exit_px = s.c[j] * (1 - side * slip[s.name] / 1e4)
                gross = side * (exit_px - entry) / entry * 1e4
                fee = _fees_bps(False) * 2
                funding = sum(s.funding.get(f, 0.0) for f in range((int(s.t[i]) // 28800 + 1) * 28800, int(s.t[j]) + 1, 28800)) * 1e4 * side
                legs.append({"symbol": s.name, "t": int(s.t[i]), "side": side, "net_bps": gross - fee - funding, "gross_bps": gross + 2 * slip[s.name], "exit_i": j})
    return legs, []


GRID_F5 = list(product((6, 24, 72), (1, 4, 24), (False, True)))


def run_all() -> dict:
    names = json.load(open(DATA / "d2_symbols.json"))
    syms = [x for x in (load(n) for n in names) if x is not None]
    lo, e_train, e_val, hi = split_edges(syms)
    cuts = {"train": [lo, e_train], "validation": [e_train, e_val], "holdout": [e_val, hi]}
    iso = lambda t: pd.Timestamp(t, unit="s").strftime("%Y-%m-%d")  # noqa: E731
    variants: list[dict] = []
    best: dict[tuple[str, str], dict] = {}
    cache: dict[tuple, tuple[list, list]] = {}

    def evaluate(fam: str, params: tuple, mode: str) -> tuple[list[dict], list[dict]]:
        key = (fam, params, mode)
        if key in cache:
            return cache[key]
        if fam == "F5_cross":
            tr, ms = f5_cross(syms, params[0], params[1], params[2])
        else:
            tr, ms = [], []
            for s in syms:
                a, b = run(s, GEN[fam](s, *params), mode)
                tr += a; ms += b
        cache[key] = (tr, ms)
        return tr, ms

    def within(tr, a, b):
        return [t for t in tr if a <= t["t"] < b]

    families = {**GRIDS, "F5_cross": GRID_F5}
    for fam, grid in families.items():
        for params in grid:
            for mode in (("taker",) if fam == "F5_cross" else ("taker", "maker")):
                tr, ms = evaluate(fam, params, mode)
                trn, val = within(tr, *cuts["train"]), within(tr, *cuts["validation"])
                st_t, st_v = stats(trn), stats(val)
                rec = {"family": fam, "params": list(params), "mode": mode, "train": st_t, "validation": st_v}
                rets = np.array([t["net_bps"] for t in trn + val], float)
                rec["sharpe_trade_trainval"] = float(rets.mean() / rets.std(ddof=1)) if len(rets) > 30 and rets.std(ddof=1) > 0 else None
                variants.append(rec)
                if st_t.get("trades", 0) + st_v.get("trades", 0) >= 60 and st_v.get("trades", 0) >= 20:
                    score = st_v["net_bps_per_trade"]                                      # chosen on VALIDATION net, never on the holdout
                    if (fam, mode) not in best or score > best[(fam, mode)]["score"]:
                        best[(fam, mode)] = {"score": score, "params": params, "rec": rec}
    n_total = N_PRIOR_VARIANTS + len(variants)
    srs = [v["sharpe_trade_trainval"] for v in variants if v["sharpe_trade_trainval"] is not None]
    var_sr = float(np.var(srs, ddof=1)) if len(srs) > 2 else 0.01
    finalists = []
    h0, h1 = cuts["holdout"]
    for (fam, mode), b in sorted(best.items()):
        tr, ms = evaluate(fam, b["params"], mode)                                          # the single holdout look
        hold = within(tr, h0, h1)
        hold_missed = [m for m in ms if h0 <= m["t"] < h1]
        r = np.array([t["net_bps"] for t in hold], float)
        st = stats(hold, hold_missed if mode == "maker" else None)
        thirds = [h0 + (h1 - h0) * k // 3 for k in range(4)]
        subs = [float(np.mean([t["net_bps"] for t in within(hold, thirds[k], thirds[k + 1])])) if within(hold, thirds[k], thirds[k + 1]) else None for k in range(3)]
        per_sym = {}
        for t in hold:
            per_sym.setdefault(t["symbol"], []).append(t["net_bps"])
        lo90, hi90 = boot_ci(r)
        # random baseline: same family structure with shuffled ranks / random-bar entries, mean net over 10 seeds
        base = []
        for seed in range(10):
            rng = np.random.default_rng(seed)
            if fam == "F5_cross":
                rl, _ = f5_cross(syms, b["params"][0], b["params"][1], b["params"][2], window=(h0, h1), rng=rng)
                base.append(np.mean([t["net_bps"] for t in rl]) if rl else np.nan)
            else:
                rl = []
                for s in syms:
                    idx = [i for i in range(60, len(s.t) - 30) if h0 <= s.t[i] < h1]
                    pick = rng.choice(idx, size=min(len(idx), max(1, len([t for t in hold if t["symbol"] == s.name]))), replace=False) if idx else []
                    ents = [{"i": int(i), "side": int(rng.choice([-1, 1])), "stop": 0.0, "target": None, "hold": 12} for i in pick]
                    for e in ents:
                        e["stop"] = s.c[e["i"]] - e["side"] * 1.5 * s.atr[e["i"]]
                    rl += run(s, ents, mode)[0]
                base.append(np.mean([t["net_bps"] for t in rl]) if rl else np.nan)
        pf = st.get("profit_factor")
        wins_sym = sum(1 for v in per_sym.values() if len(v) >= 10 and np.mean(v) > 0)
        is_event = fam == "F5_cross"
        d = dsr(r, n_total, var_sr) if len(r) else float("nan")
        checks = {"profit_factor_ge_1.3": bool(pf is not None and pf >= 1.3), "trades_ge_100": st.get("trades", 0) >= 100, "positive_in_2_of_3_subperiods": sum(1 for x in subs if x is not None and x > 0) >= 2,
                  "bootstrap90_lower_above_0": bool(np.isfinite(lo90) and lo90 > 0), "deflated_prob_ge_0.9": bool(np.isfinite(d) and d >= 0.9), "works_on_2_symbols": wins_sym >= 2,
                  "random_baseline_loses": bool(np.nanmean(base) < 0)}
        finalists.append({"family": fam, "mode": mode, "params": list(b["params"]), "param_hash": param_hash(fam, b["params"], mode), "chosen_on": "validation net bps per trade", "validation": b["rec"]["validation"], "train": b["rec"]["train"],
                          "holdout": st, "holdout_bootstrap90_net_bps": [lo90, hi90], "holdout_thirds_net_bps": subs, "per_symbol_holdout_net_bps": {k: [len(v), float(np.mean(v))] for k, v in per_sym.items()},
                          "deflated_probability": d, "random_baseline_mean_net_bps": float(np.nanmean(base)), "checks": checks, "passed": all(checks.values()), "event_universe": is_event})
    return {"cuts": {k: [iso(a), iso(b)] for k, (a, b) in cuts.items()}, "symbols": [s.name for s in syms], "symbol_history_start": {s.name: iso(int(s.t[0])) for s in syms}, "variants_this_round": len(variants),
            "variants_total_with_prior": n_total, "variance_of_sharpe_across_variants": var_sr, "finalists": finalists, "variants": variants,
            "not_testable": {"F2_open_interest_filter": "no open-interest history is published; the funding + stall part was tested", "F3_book_imbalance_trade_flow": "only a day or two of recorded book exists",
                             "delisted_perps": "Delta publishes no candles for delisted contracts"}}


if __name__ == "__main__":
    out = run_all()
    path = Path(__file__).resolve().parents[3] / "reports" / "round4_d2_results.json"
    path.write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps({k: out[k] for k in ("cuts", "symbols", "variants_this_round", "variants_total_with_prior")}, indent=1))
    for f in out["finalists"]:
        print(f["family"], f["mode"], f["params"], "holdout", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in f["holdout"].items()}, "boot90", [round(x, 2) for x in f["holdout_bootstrap90_net_bps"]],
              "DSR", round(f["deflated_probability"], 3) if f["deflated_probability"] == f["deflated_probability"] else None, "rand", round(f["random_baseline_mean_net_bps"], 2), "PASSED" if f["passed"] else "no", [k for k, v in f["checks"].items() if not v])
