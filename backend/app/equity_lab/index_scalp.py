"""Index option-buying scalp study (owner's spec, 26 Nov 2026). Research only.

Trigger on a 5-minute index bar: volume >= multiple x average of the previous N bars, RSI crossing, close on that side of session VWAP.
Index volume is not trusted (it is zero in early years), so volume comes from two stand-ins, each reported separately:
  "options": total volume of the strikes ATM+-4 (calls and puts); available for the whole option history.
  "futures": the nearest-expiry index future still listed by Dhan; only the last weeks (expired contracts cannot be downloaded).
Entry: ATM option at the next 1-minute open. THREE lots, because one lot cannot be sold in thirds. Exits on 1-minute option candles;
when a stop and a target fall in the same minute the stop is taken. One position at a time, at most 3 trades a day.
Run from backend/:  python -m app.equity_lab.index_scalp
"""
from __future__ import annotations

import itertools
import math
from datetime import date

import numpy as np
import pandas as pd

from app.equity_lab import index_options_common as c

MULTS, LOOKBACKS, RSI_LENGTHS, CROSSES = (1.5, 2.0, 2.5, 3.0), (20, 50), (7, 14), ("50", "55/45")
STOPS = ("20%", "30%", "40%", "vwap")
TRANCHES = {"20/40/80": ("pct", (0.20, 0.40, 0.80)), "20/40/trail": ("trail", (0.20, 0.40)), "15/30/60": ("pct", (0.15, 0.30, 0.60)),
            "index 0.15/0.30/0.50%": ("index", (0.0015, 0.0030, 0.0050)), "all at 20%": ("pct", (0.20, 0.20, 0.20)), "all at 40%": ("pct", (0.40, 0.40, 0.40))}
TIMES = (15, 30, 60, 0)                                    # 0 = until 15:15
EOD = c.slot("15:15")
LAST_ENTRY = c.slot("14:30")
EXITS = list(itertools.product(STOPS, TRANCHES, TIMES))
HORIZONS = (5, 15, 30, 60)
MAX_OFFSET = 4


def rsi(close: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(close, prepend=close[0])
    up = pd.Series(np.clip(d, 0, None)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = pd.Series(np.clip(-d, 0, None)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return (100 - 100 / (1 + up / down.replace(0, np.nan))).fillna(100).where(up.notna()).to_numpy()


def futures_volume(underlying: str) -> dict[tuple[date, int], float]:
    """Volume per (day, minute) from the nearest-expiry contract that was downloaded and had traded by then."""
    out: dict[tuple[date, int], float] = {}
    for path in sorted((c.DATA / "futures" / underlying).glob("*.pkl.gz"), reverse=True):      # later expiries first, nearer ones overwrite
        f = c._read(path)
        if f is None:
            continue
        expiry = date.fromisoformat(path.name.split(".")[0])
        out.update({(d, int(s)): float(v) for d, s, v in zip(f.day, f.slot, f.volume) if d <= expiry})
    return out


def collect(underlying: str):
    """5-minute bars for every day that has option data, plus a per-day option cube cache."""
    index = c.index_minutes(underlying)
    by_day = dict(tuple(index.groupby("day")))
    cubes: dict[date, dict] = {}
    for month in c.months_available(underlying):
        for day, cube in c.option_month(underlying, month, MAX_OFFSET).items():
            if day in by_day:
                cubes[day] = cube
    fut = futures_volume(underlying)
    rows = []
    for day in sorted(cubes):
        g = by_day[day]
        m = np.full((c.MINUTES, 4), np.nan)
        m[g.slot.values] = g[["open", "high", "low", "close"]].values
        cube = cubes[day]
        optvol = np.nansum(cube["CE"]["volume"], axis=1) + np.nansum(cube["PE"]["volume"], axis=1)
        futvol = np.array([fut.get((day, s), np.nan) for s in range(c.MINUTES)])
        cube["index"] = m
        cube["years"] = c.years_to_expiry(underlying, day, c.slot("12:00"))
        for b in range(c.MINUTES // 5):
            part = m[b * 5:b * 5 + 5]
            ok = np.isfinite(part[:, 3])
            if not ok.any():
                continue
            fv = futvol[b * 5:b * 5 + 5]
            rows.append((day, b * 5 + 5, part[ok, 0][0], part[ok, 1].max(), part[ok, 2].min(), part[ok, 3][-1], float(optvol[b * 5:b * 5 + 5].sum()),
                         float(np.nansum(fv)) if np.isfinite(fv).any() else np.nan))
    bars = pd.DataFrame(rows, columns=["day", "end", "open", "high", "low", "close", "options", "futures"])
    typical = (bars.high + bars.low + bars.close) / 3
    for source in ("options", "futures"):
        v = bars[source].fillna(0)
        bars[f"vwap_{source}"] = (typical * v).groupby(bars.day).cumsum() / v.groupby(bars.day).cumsum().replace(0, np.nan)
        for n in LOOKBACKS:
            bars[f"avg_{source}_{n}"] = bars[source].rolling(n, min_periods=n).mean().shift(1)
    for n in RSI_LENGTHS:
        bars[f"rsi{n}"] = rsi(bars.close.to_numpy(), n)
    return bars, cubes


def signals(bars: pd.DataFrame, source: str, mult: float, lookback: int, length: int, cross: str) -> np.ndarray:
    """+1 bullish, -1 bearish, 0 nothing, one per bar."""
    r = bars[f"rsi{length}"].to_numpy()
    prev = np.concatenate([[np.nan], r[:-1]])
    hi, lo = (50, 50) if cross == "50" else (55, 45)
    loud = (bars[source] >= mult * bars[f"avg_{source}_{lookback}"]).to_numpy()
    above = (bars.close > bars[f"vwap_{source}"]).to_numpy()
    below = (bars.close < bars[f"vwap_{source}"]).to_numpy()
    out = np.zeros(len(bars), dtype=int)
    out[loud & above & (prev < hi) & (r >= hi)] = 1
    out[loud & below & (prev > lo) & (r <= lo)] = -1
    return out


def first(mask: np.ndarray, default: int) -> int:
    i = int(np.argmax(mask)) if mask.size else 0
    return i if mask.size and mask[i] else default


def simulate(cube: dict, entry_slot: int, direction: int, vwap_exit_slot: dict[str, int]) -> dict | None:
    """All exit rules for one entry. Returns {"entry", "strike", "iv", "exits": {exit: [(price, units, minute offset), ...]}}."""
    side = "CE" if direction > 0 else "PE"
    strike = cube["atm"][side][entry_slot]
    if not np.isfinite(strike):
        return None
    col = int(np.searchsorted(cube["strikes"], strike))
    f = cube[side]
    if not np.isfinite(f["open"][entry_slot, col]) or f["open"][entry_slot, col] < 1:
        return None
    o, h, low, last, _ = c.priced(f, col, entry_slot, EOD, cube["index"][:, 3], float(strike), cube["years"], direction > 0)
    entry = o[0]
    n = len(o)
    spot = pd.Series(cube["index"][entry_slot:EOD + 1, 3]).ffill().to_numpy()
    move = direction * (spot - cube["index"][entry_slot, 0]) / cube["index"][entry_slot, 0]
    stop_at = {}
    for s in STOPS[:3]:
        level = entry * (1 - int(s[:-1]) / 100)
        i = first(low <= level, n)
        stop_at[s] = (i, min(level, o[i]) if i < n else None)
    out: dict = {"entry": entry, "strike": float(strike), "iv": float(f["iv"][entry_slot, col]), "col": col, "exits": {}}
    for name, (kind, levels) in TRANCHES.items():
        if kind == "index":
            hits = [(first(move >= x, n), None) for x in levels]
        else:
            hits = [(first(h >= entry * (1 + x), n), entry * (1 + x)) for x in levels]
        if kind == "trail":                                # third lot: after the second target, sell if price falls 25% from its high since entry
            start = hits[1][0]
            peak = np.maximum.accumulate(h)
            j = start + 1 + first(low[start + 1:] <= 0.75 * peak[start:-1], n) if start + 1 < n else n
            hits.append((min(j, n), None if j >= n else float(min(o[j], 0.75 * peak[j - 1]))))
        for s in STOPS:
            if s == "vwap":
                j = vwap_exit_slot.get("slot", 10_000) - entry_slot
                stop_i, stop_price = (j, o[j]) if 0 < j < n else (n, None)
            else:
                stop_i, stop_price = stop_at[s]
            for minutes in TIMES:
                limit = min(minutes, n - 1) if minutes else n - 1                      # time stop, or 15:15, sold at that minute's open
                fills = []
                left = 3
                for i, price in sorted(hits, key=lambda x: x[0]):
                    if i < stop_i and i < limit and left:
                        fills.append((float(price if price is not None else last[i]), 1, i))
                        left -= 1
                if left:
                    if stop_i <= limit and stop_i < n:
                        fills.append((float(stop_price), left, stop_i))
                    else:
                        fills.append((float(o[limit]), left, limit))
                out["exits"][(s, name, minutes)] = fills
    return out


def net_of(underlying: str, day: date, trade: dict, key: tuple, case: tuple[str, float]) -> tuple[float, float, int]:
    """(net rupees for three lots, net as % of premium paid, minutes held)."""
    lot = c.lot_size(underlying, day)
    buy = c.slipped(trade["entry"], 1, case)
    fills = [(1, buy, 3 * lot)] + [(-1, c.slipped(p, -1, case), u * lot) for p, u, _ in trade["exits"][key]]
    gross = sum(-side * price * qty for side, price, qty in fills)
    net = gross - c.charges(underlying, day, fills)
    return net, 100 * net / (buy * 3 * lot), max(i for *_, i in trade["exits"][key])


def bs(spot: float, strike: float, iv: float, years: float, call: bool) -> tuple[float, float, float]:
    """(delta, vega per 1 vol point, theta per trading minute) with zero rates: used only to attribute losses."""
    if years <= 0 or iv <= 0:
        return 0.0, 0.0, 0.0
    sq = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / sq
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    cdf = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return (cdf if call else cdf - 1), spot * pdf * math.sqrt(years) / 100, -spot * pdf * iv / (2 * math.sqrt(years)) / (252 * 375)


def main() -> None:
    c.REPORTS.mkdir(exist_ok=True)
    runs = c.REPORTS / "delta_runs"
    runs.mkdir(exist_ok=True)
    vix = c.index_minutes("INDIAVIX")
    vix_minute = {(d, s): v for d, s, v in zip(vix.day, vix.slot, vix.close)}
    daily_vix = vix.groupby("day").close.last()
    pct30, pct70 = (daily_vix.rolling(252, min_periods=60).quantile(q).shift(1).to_dict() for q in (0.3, 0.7))
    events: dict[tuple, dict] = {}                          # (underlying, day, entry slot, direction, source) -> simulated trade
    variants: dict[tuple, list] = {}                        # (source, mult, lookback, rsi, cross) -> [event keys in time order]
    forward_rows = []
    all_days: set[date] = set()
    coverage = []
    for underlying in c.UNDERLYINGS:
        if not c.months_available(underlying):
            coverage.append(f"{underlying}: no option data downloaded")
            continue
        bars, cubes = collect(underlying)
        days = sorted(cubes)
        all_days.update(days)
        coverage.append(f"{underlying}: {len(days)} days, {days[0]} to {days[-1]}; futures volume on {bars[np.isfinite(bars.futures)].day.nunique()} of them")
        expiry = c.expiry_days(underlying, days)
        day_rows = {d: g for d, g in bars.groupby("day")}
        close_at = {d: pd.Series(cubes[d]["index"][:, 3]).ffill().to_numpy() for d in days}
        for source in ("options", "futures"):
            if source == "futures" and not np.isfinite(bars.futures).any():
                continue
            for mult, lookback, length, cross in itertools.product(MULTS, LOOKBACKS, RSI_LENGTHS, CROSSES):
                key = (source, mult, lookback, length, cross)
                sig = signals(bars, source, mult, lookback, length, cross)
                for i in np.flatnonzero(sig):
                    day, end, direction = bars.day.iat[i], int(bars.end.iat[i]), int(sig[i])
                    if end > LAST_ENTRY:
                        continue
                    now = close_at[day][end - 1]
                    level = vix_minute.get((day, end - 1))
                    row = {"underlying": underlying, "source": source, "mult": mult, "lookback": lookback, "rsi": length, "cross": cross, "year": day.year,
                           "vix_band": bool(level is not None and 12 <= level <= 18)}
                    for hz in HORIZONS:
                        row[f"m{hz}"] = direction * (close_at[day][min(end - 1 + hz, c.MINUTES - 1)] / now - 1) * 100
                    forward_rows.append(row)
                    ev = (underlying, day, end, direction, source)
                    if ev not in events:
                        g = day_rows[day]
                        after = g[g.end > end]
                        back = after[(after.close - after[f"vwap_{source}"]) * direction < 0]
                        trade = simulate(cubes[day], end, direction, {"slot": int(back.end.iat[0])} if len(back) else {})
                        if trade is not None:
                            lo, hi = pct30.get(day), pct70.get(day)
                            trade.update(day=day, vix=level, pct_band=None if level is None or lo != lo else bool(lo <= level <= hi), expiry=day in expiry,
                                         spot=float(now), cube_day=day)
                            minutes = trade["exits"][("30%", "20/40/80", 30)][-1][2]
                            side = "CE" if direction > 0 else "PE"
                            exit_slot = min(end + minutes, c.MINUTES - 1)
                            trade["iv_exit"] = float(cubes[day][side]["iv"][exit_slot, trade["col"]])
                            trade["spot_exit"] = float(close_at[day][exit_slot])
                            trade["years"] = c.years_to_expiry(underlying, day, end)
                        events[ev] = trade
                    if events[ev] is not None:
                        variants.setdefault(key, []).append(ev)
        del bars, cubes
    for evs in variants.values():
        evs.sort(key=lambda e: (e[1], e[2], e[0]))
    train_end, val_end = c.splits(sorted(all_days))

    # ── Part A: does the signal move the index? ──
    fwd = pd.DataFrame(forward_rows)
    fwd.to_csv(runs / "index_scalp_signal_moves.csv", index=False)
    columns = [f"m{h}" for h in HORIZONS]

    def moves(frame: pd.DataFrame, by: list[str]) -> list[dict]:
        out = []
        for keys, g in frame.groupby(by):
            keys = keys if isinstance(keys, tuple) else (keys,)
            row = dict(zip(by, keys)) | {"signals": len(g)}
            for h in HORIZONS:
                row[f"{h} min avg %"] = round(float(g[f"m{h}"].mean()), 4)
                row[f"{h} min hit %"] = round(float((g[f"m{h}"] > 0).mean() * 100), 1)
            out.append(row)
        return out

    part_a = {"By index, volume source and VIX 12-18": moves(fwd, ["underlying", "source", "vix_band"]), "By year (all signal variants)": moves(fwd, ["source", "year"]),
              "By volume multiple": moves(fwd, ["source", "mult"])}
    del columns

    # ── Part B: every variant ──
    def run_variant(evs: list, exit_key: tuple, case, keep=lambda e: True, max_per_day: int | None = 3, daily_loss: float | None = None, risk: float | None = None):
        net, pct, hold, when, meta = [], [], [], [], []
        day, count, free_at, lost = None, 0, 0, 0.0
        for ev in evs:
            trade = events[ev]
            underlying, d, end, direction, _ = ev
            if not keep(trade):
                continue
            if d != day:
                day, count, free_at, lost = d, 0, 0, 0.0
            if end < free_at or (max_per_day and count >= max_per_day) or (daily_loss and lost <= -daily_loss):
                continue
            n, p, minutes = net_of(underlying, d, trade, exit_key, case)
            if risk is not None:                           # position size so a premium stop-out risks at most `risk` rupees (whole 3-lot sets)
                stop = 0.3 if exit_key[0] == "vwap" else int(exit_key[0][:-1]) / 100
                sets = int(risk // (trade["entry"] * stop * 3 * c.lot_size(underlying, d)))
                if sets < 1:
                    continue
                n *= sets
            count, free_at, lost = count + 1, end + minutes + 1, lost + n
            net.append(n), pct.append(p), hold.append(minutes), when.append(d), meta.append((underlying, direction, trade["expiry"], d.year))
        return np.array(net), np.array(pct), np.array(hold), np.array(when), meta

    base_case = c.SLIPPAGE["half spread 0.5%"]
    rows = []
    for key, evs in variants.items():
        source, mult, lookback, length, cross = key
        for exit_key in EXITS:
            net, pct, hold, when, _ = run_variant(evs, exit_key, base_case)
            row = {"volume_source": source, "vol_multiple": mult, "vol_lookback": lookback, "rsi": length, "cross": cross, "stop": exit_key[0], "targets": exit_key[1],
                   "time_stop_min": exit_key[2] or "15:15"} | c.summary(net, pct, hold)
            for label, mask in (("train", when <= train_end), ("validation", (when > train_end) & (when <= val_end)), ("final", when > val_end)):
                row[f"{label}_trades"] = int(mask.sum()) if len(net) else 0
                row[f"{label}_net_pct"] = round(float(pct[mask].mean()), 3) if len(net) and mask.any() else None
            rows.append(row)
    result = pd.DataFrame(rows)
    result.to_csv(runs / "index_scalp_variants.csv", index=False)

    lines = ["# Index option scalp: backtest", "", "Decision support, not investment advice. Hypothetical results; no order was placed.", ""]
    lines += ["## Data", ""] + [f"- {x}" for x in coverage]
    lines += [f"- Splits by day: train to {train_end}, validation to {val_end}, final check after that.",
              "- Volume stand-ins: `options` = ATM±4 option volume; `futures` = nearest listed contract only (expired contracts cannot be downloaded, so this covers weeks, not years).",
              "- Three lots per trade (one lot cannot be split). Costs: Rs 20 an order, STT on sells at the rate of the day (0.15% from April 2026), exchange, SEBI, stamp, GST; slippage 0.5% of premium a side unless stated.",
              "- Lot sizes, expiry weekdays and older tax rates are from memory of circulars, not re-verified. Dhan has no historical bid/ask, so slippage is assumed.", ""]
    lines += ["## A. Does the signal predict the index? (no costs; move in the signal's direction)", ""]
    for title, data in part_a.items():
        lines += [f"### {title}", "", c.table(data, list(data[0].keys())) if data else "no signals", ""]

    lines += ["## B. The option trade", "", f"{len(result)} variants (every one is in `reports/delta_runs/index_scalp_variants.csv`). Added to the running total: about 1,800 + {len(result)}.", ""]
    for source, g in result.groupby("volume_source"):
        lines += [f"### Volume source: {source}", "",
                  f"- Variants with positive net after costs over all data: {int((g.net_pct_premium > 0).sum())} of {len(g)}.",
                  f"- Median variant: {g.net_pct_premium.median():.2f}% of premium per trade.", ""]
        for column in ("stop", "targets", "time_stop_min", "vol_multiple"):
            avg = g.groupby(column).net_pct_premium.mean().round(3)
            lines += [f"Average net % of premium by {column}: " + ", ".join(f"{k}: {v}" for k, v in avg.items()), ""]
        eligible = g[(g.validation_trades >= 40) & g.validation_net_pct.notna()]
        if eligible.empty:
            lines += ["No variant has 40 validation trades: no choice is made and there is no verdict for this source.", ""]
            continue
        best = eligible.loc[eligible.validation_net_pct.idxmax()]
        key = (source, best.vol_multiple, int(best.vol_lookback), int(best.rsi), best.cross)
        exit_key = (best.stop, best.targets, 0 if best.time_stop_min == "15:15" else int(best.time_stop_min))
        evs = variants[key]
        lines += ["**Chosen on validation:** " + ", ".join(f"{k}={best[k]}" for k in ("vol_multiple", "vol_lookback", "rsi", "cross", "stop", "targets", "time_stop_min")), "",
                  f"- Train {best.train_net_pct}% ({best.train_trades} trades), validation {best.validation_net_pct}% ({best.validation_trades}), **final check {best.final_net_pct}% ({best.final_trades})**."]
        net, pct, hold, when, meta = run_variant(evs, exit_key, base_case)
        final = when > val_end
        if final.sum() >= 10:
            low, high = c.day_bootstrap(when[final], pct[final])
            lines += [f"- Final check, 90% bootstrap interval of net % of premium per trade: {low:.3f} to {high:.3f}. " + ("**Above zero.**" if low > 0 else "**Not above zero: it does not beat costs with confidence.**")]
        else:
            lines += ["- Fewer than 10 final-check trades: no interval."]
        lines += ["", "Slippage cases (all data):", "", c.table([{"case": name} | c.summary(*run_variant(evs, exit_key, case)[:3]) for name, case in c.SLIPPAGE.items()],
                                                            ["case", "trades", "net_per_trade", "net_pct_premium", "win_rate", "profit_factor", "max_drawdown", "avg_minutes"]), ""]
        filters = {"no filter": lambda t: True, "VIX 12-18": lambda t: t["vix"] is not None and 12 <= t["vix"] <= 18, "VIX 13-20": lambda t: t["vix"] is not None and 13 <= t["vix"] <= 20,
                   "VIX 14-22": lambda t: t["vix"] is not None and 14 <= t["vix"] <= 22, "VIX 30th-70th pct of last year": lambda t: bool(t["pct_band"]),
                   "expiry day only": lambda t: t["expiry"], "not expiry day": lambda t: not t["expiry"]}
        lines += ["Filters:", "", c.table([{"filter": name} | c.summary(*run_variant(evs, exit_key, base_case, keep=f)[:3]) for name, f in filters.items()],
                                          ["filter", "trades", "net_per_trade", "net_pct_premium", "win_rate", "profit_factor", "max_drawdown"]), ""]
        late = [e for e in evs if e[2] >= c.slot("09:30")]
        lines += [f"Skipping the first 15 minutes: {c.summary(*run_variant(late, exit_key, base_case)[:3])}", ""]
        caps = []
        for capital, sizing, cap, loss in itertools.product((500_000, 1_000_000), ("3 lots", "1% risk"), (3, None), (True, False)):
            out = run_variant(evs, exit_key, base_case, max_per_day=cap, daily_loss=0.04 * capital if loss else None, risk=0.01 * capital if sizing == "1% risk" else None)
            stats = c.summary(*out[:3])
            caps.append({"capital": capital, "size": sizing, "max a day": cap or "no cap", "4% day stop": "on" if loss else "off", "total_net": round(float(out[0].sum())) if len(out[0]) else 0} | stats)
        lines += ["Daily loss limit, trade cap and sizing:", "", c.table(caps, ["capital", "size", "max a day", "4% day stop", "trades", "total_net", "net_per_trade", "max_drawdown"]), ""]
        parts = []
        for label, pick in (("calls", lambda m: m[1] > 0), ("puts", lambda m: m[1] < 0), ("expiry day", lambda m: m[2]), ("other days", lambda m: not m[2]),
                            *[(u, lambda m, u=u: m[0] == u) for u in c.UNDERLYINGS], *[(str(y), lambda m, y=y: m[3] == y) for y in sorted({m[3] for m in meta})]):
            mask = np.array([pick(m) for m in meta], dtype=bool)
            parts.append({"slice": label} | (c.summary(net[mask], pct[mask], hold[mask]) if len(net) else {}))
        lines += ["Breakdown:", "", c.table(parts, ["slice", "trades", "net_per_trade", "net_pct_premium", "win_rate", "profit_factor"]), ""]
        # theta and IV against the index move, on the chosen entries
        theta, iv_part, move_part, losses = [], 0.0, 0.0, 0
        for ev in evs:
            t = events[ev]
            if not (t["iv"] > 0 and t["iv_exit"] > 0):
                continue
            delta, vega, th = bs(t["spot"], t["strike"], t["iv"] / 100, t["years"], ev[3] > 0)
            theta.append(th)
            minutes = t["exits"][("30%", "20/40/80", 30)][-1][2]
            if net_of(ev[0], ev[1], t, ("30%", "20/40/80", 30), base_case)[0] < 0:
                losses += 1
                iv_part += min(vega * (t["iv_exit"] - t["iv"]), 0)
                move_part += min(delta * (t["spot_exit"] - t["spot"]), 0)
                del minutes
        if theta and losses:
            lines += [f"Theta and IV (Black-Scholes estimate, reference exit 30% stop / 30 minutes): average decay {abs(np.mean(theta)):.3f} premium points a minute at entry. "
                      f"Per losing trade, adverse index move cost {abs(move_part) / losses:.1f} points and falling IV {abs(iv_part) / losses:.1f} points.", ""]
    lines += ["## Could this have worked live for a retail trader?", "",
              "The test assumes a fill at the next minute's open after the 5-minute bar closes, and stop fills at the stop price unless the minute opened beyond it. "
              "Live, an ATM option moves several percent within that minute after a volume spike, and stop orders slip in fast moves. The 1.5% and Rs 1 rows above show how much of any edge that removes. "
              "Read the verdict from the final check and its interval, not from the best variant over all data.", ""]
    (c.REPORTS / "2026-11-26-index-option-scalp.md").write_text("\n".join(lines))
    print("variants", len(result), "events", sum(v is not None for v in events.values()))
    print("\n".join(coverage))


if __name__ == "__main__":
    main()
