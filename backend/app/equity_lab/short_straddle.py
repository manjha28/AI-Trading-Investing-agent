"""Intraday short straddle / strangle study on Dhan option candles (owner's spec, 26 Nov 2026). Research only.

One lot of each leg is sold at the open of the entry minute; strikes stay fixed all day. A leg stop triggers on that leg's 1-minute high
(filled at the stop, or at the open if it gapped past). The grid is staged (not every combination), and every variant run is listed.
A strike with no print keeps its last price; a day is skipped when a sold strike has no price at entry.
Run from backend/:  python -m app.equity_lab.short_straddle
"""
from __future__ import annotations

import itertools
from datetime import date

import numpy as np
import pandas as pd

from app.equity_lab import index_options_common as c

ENTRIES = ("09:20", "09:30", "09:45", "10:00", "10:30", "11:00", "12:00", "13:00")
EXITS = ("14:30", "15:00", "15:10", "15:20")
STOPS = (0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
MANAGEMENT = ("none", "move to cost", "re-enter once", "re-enter twice", "re-enter at cost", "trail after 30% decay", "trail after 40% decay", "trail after 50% decay",
              "target 30% of premium", "target 50% of premium")
STRIKES = ("ATM", "strangle 0.5%", "strangle 1%", "strangle 1.5%", "ATM hedged")
BASE = {"entry": "09:20", "exit": "15:10", "stop": 0.25, "manage": "none", "strikes": "ATM"}
TRAIL_BACK = 0.20                                          # trailing stop: 20% above the lowest premium seen, once the decay is reached
WING_STRIKES = 6                                           # hedged: long options this many strikes beyond the sold ones
MAX_OFFSET = 10
# Union Budget days and the 2024 general-election result: the only event dates used (RBI and global dates were not verified, so they are not tested)
EVENTS = {date(2022, 2, 1), date(2023, 2, 1), date(2024, 2, 1), date(2024, 6, 4), date(2024, 7, 23), date(2025, 2, 1), date(2026, 2, 1)}


def variants() -> list[dict]:
    """Stage 1: entry x exit x stop at the money. Stage 2: management x stop. Stage 3: strikes x stop. Combined-premium stops separately."""
    out = [dict(BASE, entry=e, exit=x, stop=s) for e, x, s in itertools.product(ENTRIES, EXITS, STOPS)]
    out += [dict(BASE, manage=m, stop=s) for m, s in itertools.product(MANAGEMENT[1:], STOPS)]
    out += [dict(BASE, strikes=k, stop=s) for k, s in itertools.product(STRIKES[1:], STOPS)]
    out += [dict(BASE, entry=e, stop=s, manage="combined stop") for e, s in itertools.product(ENTRIES, STOPS)]
    return out


def leg_path(cube: dict, side: str, strike: float, a: int, b: int):
    """(open, high, low, estimated) from entry minute ``a`` to exit minute ``b`` for one strike, or None when it has no price at entry."""
    hit = np.flatnonzero(cube["strikes"] == strike)
    if not hit.size or not np.isfinite(cube[side]["open"][a, hit[0]]):
        return None
    o, h, low, _, missing = c.priced(cube[side], int(hit[0]), a, b, cube["spot"], float(strike), cube["years"], side == "CE")
    return o, h, low, missing


def first_from(mask: np.ndarray, start: int) -> int | None:
    if start >= mask.size:
        return None
    i = int(np.argmax(mask[start:]))
    return start + i if mask[start + i] else None


def short_leg(o: np.ndarray, h: np.ndarray, low: np.ndarray, stop: float, manage: str) -> list[tuple[float, float, int, int]]:
    """[(sold at, bought back at, entry index, exit index)] for one short leg until the last index (the time exit)."""
    n = len(o) - 1
    trades, start, sold, rounds = [], 0, o[0], {"re-enter once": 1, "re-enter twice": 2, "re-enter at cost": 1}.get(manage, 0)
    while True:
        level = sold * (1 + stop)
        if manage.startswith("trail"):
            need = int(manage.split()[2].rstrip("%")) / 100
            lowest = np.minimum.accumulate(np.concatenate([[sold], low[start:-1]]))            # lows of EARLIER minutes only
            levels = np.where(lowest <= sold * (1 - need), np.minimum(level, lowest * (1 + TRAIL_BACK)), level)
            hit = first_from(h[start:n] >= levels[: n - start], 0)
            hit = None if hit is None else hit + start
            level = None if hit is None else float(levels[hit - start])
        else:
            hit = first_from(h[:n] >= level, start)
        if hit is None:
            trades.append((sold, float(o[n]), start, n))
            return trades
        trades.append((sold, float(max(level, o[hit])), start, hit))
        if not rounds:
            return trades
        rounds -= 1
        if manage == "re-enter at cost":
            back = first_from(low[:n] <= sold, hit + 1)
        else:
            back = hit + 1 if hit + 1 < n else None
        if back is None:
            return trades
        start, sold = back, float(o[back]) if manage != "re-enter at cost" else min(sold, float(o[back]))


def day_trade(cube: dict, v: dict, spot: float, step: float) -> dict | None:
    a, b = c.slot(v["entry"]), c.slot(v["exit"])
    atm = {s: cube["atm"][s][a] for s in ("CE", "PE")}
    if not (np.isfinite(atm["CE"]) and np.isfinite(atm["PE"])):
        return None
    away = {"strangle 0.5%": 0.005, "strangle 1%": 0.01, "strangle 1.5%": 0.015}.get(v["strikes"], 0.0)
    shift = round(spot * away / step) * step
    strikes = {"CE": atm["CE"] + shift, "PE": atm["PE"] - shift}
    paths = {s: leg_path(cube, s, strikes[s], a, b) for s in ("CE", "PE")}
    if any(p is None for p in paths.values()):
        return None
    premium = paths["CE"][0][0] + paths["PE"][0][0]
    combined_open = paths["CE"][0] + paths["PE"][0]
    n = b - a
    legs: list[tuple[str, float, float, int, int]] = []      # (side, sold, bought, entry, exit)
    if v["manage"] == "combined stop" or v["manage"].startswith("target"):
        if v["manage"] == "combined stop":
            hit = first_from(combined_open[:n] >= premium * (1 + v["stop"]), 1)
        else:                                               # profit target on the combined premium, with the usual leg stops
            goal = int(v["manage"].split()[1].rstrip("%")) / 100
            hit = first_from(combined_open[:n] <= premium * (1 - goal), 1)
        for s in ("CE", "PE"):
            o, h, low = paths[s][:3]
            if v["manage"] == "combined stop":
                end = n if hit is None else hit
                legs.append((s, float(o[0]), float(o[end]), 0, end))
            else:
                for sold, bought, i, j in short_leg(o, h, low, v["stop"], "none"):
                    if hit is not None and j > hit:
                        j, bought = hit, float(o[hit])
                    legs.append((s, float(sold), bought, i, j))
    else:
        base_manage = "none" if v["manage"] == "move to cost" else v["manage"]
        for s in ("CE", "PE"):
            legs += [(s, float(x[0]), x[1], x[2], x[3]) for x in short_leg(*paths[s][:3], v["stop"], base_manage)]
        if v["manage"] == "move to cost":
            stopped = sorted((leg for leg in legs if leg[4] < n), key=lambda leg: leg[4])
            if stopped:
                first_side, at = stopped[0][0], stopped[0][4]
                other = "PE" if first_side == "CE" else "CE"
                o, h = paths[other][:2]
                for k, (s, sold, bought, i, j) in enumerate(legs):
                    if s == other and j > at:
                        hit = first_from(h[:n] >= sold, at + 1)
                        if hit is not None and hit < j:
                            legs[k] = (s, sold, float(max(sold, o[hit])), i, hit)
    wings, guessed = [], sum(bool(paths[leg[0]][3][leg[4]]) for leg in legs)
    if v["strikes"] == "ATM hedged":
        for s, sign in (("CE", 1), ("PE", -1)):
            path = leg_path(cube, s, strikes[s] + sign * WING_STRIKES * step, a, b)
            if path is None:
                return None
            closed = max(leg[4] for leg in legs if leg[0] == s)      # the hedge is closed with its short leg, never kept as a free bet
            wings.append((float(path[0][0]), float(path[0][closed])))
            guessed += bool(path[3][closed])
    return {"legs": legs, "wings": wings, "premium": float(premium), "minutes": max(leg[4] for leg in legs), "estimated": guessed, "exits": len(legs) + len(wings)}


def rupees(underlying: str, day: date, trade: dict, case: tuple[str, float]) -> float:
    lot = c.lot_size(underlying, day)
    fills = []
    for _, sold, bought, _, _ in trade["legs"]:
        fills += [(-1, c.slipped(sold, -1, case), lot), (1, c.slipped(bought, 1, case), lot)]
    for paid, got in trade["wings"]:
        fills += [(1, c.slipped(paid, 1, case), lot), (-1, c.slipped(got, -1, case), lot)]
    return sum(-side * price * qty for side, price, qty in fills) - c.charges(underlying, day, fills)


def margin(underlying: str, day: date, spot: float, hedged: bool, step: float) -> float:
    """SPAN-like ESTIMATE, not Dhan's calculator: naked about 11% of one side's notional; hedged about the wing width plus 2% of notional."""
    lot = c.lot_size(underlying, day)
    return (WING_STRIKES * step + 0.02 * spot) * lot if hedged else 0.11 * spot * lot


def main() -> None:
    runs = c.REPORTS / "delta_runs"
    runs.mkdir(parents=True, exist_ok=True)
    vix = c.index_minutes("INDIAVIX")
    vix_minute = {(d, s): v for d, s, v in zip(vix.day, vix.slot, vix.close)}
    daily_vix = vix.groupby("day").close.last()
    vix_prev = daily_vix.shift(1).to_dict()
    todo = variants()
    base_case = c.SLIPPAGE["half spread 0.5%"]
    lines = ["# Intraday short straddle and strangle: backtest", "", "Decision support, not investment advice. Hypothetical results; no order was placed.", "",
             "- One lot a leg. Costs: Rs 20 an order, STT on sells at the rate of the day, exchange, SEBI, stamp, GST; slippage 0.5% of premium a side unless stated. Dhan has no historical bid/ask.",
             "- Return on margin uses a SPAN-like estimate (11% of notional naked; wing width plus 2% hedged), not Dhan's margin calculator.",
             "- Lot sizes, expiry weekdays and older tax rates are from memory of circulars, not re-verified.",
             "- Events tested: Union Budget days and the 2024 election result only. RBI and global event dates were not verified and are not tested.",
             "- The grid is staged (entry x exit x stop; management x stop; strikes x stop), not every combination.",
             "- Hedged: each bought wing is closed when its short leg is closed.",
             "- The Quantsapp figures were not checked and are not used.", ""]
    all_rows = []
    for underlying in ("NIFTY", "SENSEX"):
        months = c.months_available(underlying, f"ATM+{MAX_OFFSET}")
        if not months:
            lines += [f"## {underlying}", "", "Strikes out to ±10 were not downloaded: not tested.", ""]
            continue
        index = c.index_minutes(underlying)
        by_day = {d: g for d, g in index.groupby("day")}
        results: list[list] = [[] for _ in todo]            # per variant: (day, net, return on premium %, return on margin %, minutes)
        estimated, exits = 0, 0
        days_all: list[date] = []
        for month in months:
            for day, cube in c.option_month(underlying, month, MAX_OFFSET).items():
                if day not in by_day or day.weekday() >= 5 and day not in EVENTS:
                    continue
                g = by_day[day]
                spot_at = np.full(c.MINUTES, np.nan)
                spot_at[g.slot.values] = g.open.values
                typical = np.full(c.MINUTES, np.nan)
                typical[g.slot.values] = ((g.high + g.low + g.close) / 3).values
                volume = np.nansum(cube["CE"]["volume"], axis=1) + np.nansum(cube["PE"]["volume"], axis=1)
                with np.errstate(invalid="ignore", divide="ignore"):
                    vwap = np.nancumsum(typical * volume) / np.cumsum(volume)
                diffs = np.diff(cube["strikes"])
                step = float(np.min(diffs[diffs > 0])) if (diffs > 0).any() else 50.0
                days_all.append(day)
                cube["spot"] = spot_at
                cube["years"] = c.years_to_expiry(underlying, day, c.slot("12:00"))
                cache: dict[str, tuple] = {}
                for k, v in enumerate(todo):
                    a = c.slot(v["entry"])
                    spot = spot_at[a]
                    if not np.isfinite(spot):
                        continue
                    trade = day_trade(cube, v, float(spot), step)
                    if trade is None:
                        continue
                    if v["entry"] not in cache:
                        atm = cube["atm"]["CE"][a]
                        col = np.flatnonzero(cube["strikes"] == atm)
                        ratio = None
                        if col.size:
                            call_oi, put_oi = cube["CE"]["oi"][a, col[0]], cube["PE"]["oi"][a, col[0]]
                            ratio = float(call_oi / put_oi) if np.isfinite(call_oi) and put_oi else None
                        near = bool(np.isfinite(vwap[max(a - 1, 0)]) and abs(spot / vwap[max(a - 1, 0)] - 1) <= 0.001)
                        cache[v["entry"]] = (vix_minute.get((day, a)), near and ratio is not None and 0.8 <= ratio <= 1.25)
                    estimated, exits = estimated + trade["estimated"], exits + trade["exits"]
                    net = rupees(underlying, day, trade, base_case)
                    lot = c.lot_size(underlying, day)
                    results[k].append((day, net, 100 * net / (trade["premium"] * lot), 100 * net / margin(underlying, day, float(spot), bool(trade["wings"]), step),
                                       trade["minutes"], *cache[v["entry"]]))
        expiry = c.expiry_days(underlying, days_all)
        train_end, val_end = c.splits(days_all)
        next_event = {d for d, later in zip(sorted(days_all), sorted(days_all)[1:]) if later in EVENTS}

        def stats(items: list) -> dict:
            if not items:
                return c.summary(np.array([]), np.array([]), np.array([])) | {"net_pct_margin": None, "worst_day": None}
            net, pct, rom, hold = (np.array([x[i] for x in items], dtype=float) for i in (1, 2, 3, 4))
            return c.summary(net, pct, hold) | {"net_pct_margin": round(float(rom.mean()), 3), "worst_day": round(float(net.min()))}

        rows = []
        for v, items in zip(todo, results):
            row = {"underlying": underlying, **v} | stats(items)
            for label, keep in (("train", lambda d: d <= train_end), ("validation", lambda d: train_end < d <= val_end), ("final", lambda d: d > val_end)):
                part = stats([x for x in items if keep(x[0])])
                row[f"{label}_trades"], row[f"{label}_net"] = part["trades"], part["net_per_trade"]
            rows.append(row)
        all_rows += rows
        frame = pd.DataFrame(rows)
        lines += [f"## {underlying}", "", f"{len(days_all)} days, {min(days_all)} to {max(days_all)}. Train to {train_end}, validation to {val_end}, final check after. "
                  f"{len(todo)} variants; {int((frame.net_per_trade > 0).sum())} are positive after costs over all data.",
                  f"{100 * estimated / max(exits, 1):.1f}% of option exits had no print (strike left the downloaded range) and were estimated with Black-Scholes.", ""]
        columns = ["entry", "exit", "stop", "manage", "strikes", "trades", "net_per_trade", "net_pct_premium", "net_pct_margin", "win_rate", "profit_factor", "max_drawdown", "worst_day", "avg_minutes"]
        base_index = next(k for k, v in enumerate(todo) if v == BASE)
        lines += ["### The reference rule (09:20, 15:10, 25% leg stop, ATM)", "", c.table([rows[base_index]], columns), ""]
        for column in ("entry", "exit", "stop", "manage", "strikes"):
            avg = frame.groupby(column).net_per_trade.mean().round(0)
            lines += [f"Average net rupees a day by {column}: " + ", ".join(f"{k}: {v:.0f}" for k, v in avg.items()), ""]
        eligible = frame[(frame.validation_trades >= 40) & frame.validation_net.notna()]
        if eligible.empty:
            lines += ["No variant has 40 validation days: no choice and no verdict.", ""]
            continue
        k = int(eligible.validation_net.idxmax())
        items = results[k]
        chosen = rows[k]
        final = [x for x in items if x[0] > val_end]
        lines += ["### Chosen on validation", "", c.table([chosen], columns), "",
                  f"Train Rs {chosen['train_net']} a day, validation Rs {chosen['validation_net']}, **final check Rs {chosen['final_net']}** ({chosen['final_trades']} days)."]
        if len(final) >= 10:
            low, high = c.day_bootstrap(np.array([x[0] for x in final]), np.array([x[1] for x in final]))
            lines += [f"Final check, 90% bootstrap interval of net rupees a day: {low:.0f} to {high:.0f}. " + ("**Above zero.**" if low > 0 else "**Not above zero.**")]
        lines += [""]
        slip_rows = []
        for name, case in c.SLIPPAGE.items():
            v = todo[k]
            nets = []
            for month in months:
                for day, cube in c.option_month(underlying, month, MAX_OFFSET).items():
                    if day not in by_day or day <= val_end:
                        continue
                    g = by_day[day]
                    spot_at = np.full(c.MINUTES, np.nan)
                    spot_at[g.slot.values] = g.open.values
                    cube["spot"], cube["years"] = spot_at, c.years_to_expiry(underlying, day, c.slot("12:00"))
                    at = g[g.slot == c.slot(v["entry"])]
                    diffs = np.diff(cube["strikes"])
                    trade = None if at.empty else day_trade(cube, v, float(at.open.iat[0]), float(np.min(diffs[diffs > 0])))
                    if trade:
                        nets.append(rupees(underlying, day, trade, case))
            slip_rows.append({"case (final check only)": name, "days": len(nets), "net a day": round(float(np.mean(nets))) if nets else None})
        lines += [c.table(slip_rows, ["case (final check only)", "days", "net a day"]), ""]
        filters = {"no filter": lambda x: True, "VIX under 13": lambda x: x[5] is not None and x[5] < 13, "VIX 13-18": lambda x: x[5] is not None and 13 <= x[5] < 18,
                   "VIX 18 and over": lambda x: x[5] is not None and x[5] >= 18, "VIX up on the day": lambda x: x[5] is not None and vix_prev.get(x[0]) == vix_prev.get(x[0]) and x[5] > vix_prev.get(x[0], 1e9),
                   "VIX down on the day": lambda x: x[5] is not None and x[5] <= vix_prev.get(x[0], -1e9), "expiry day": lambda x: x[0] in expiry, "not expiry day": lambda x: x[0] not in expiry,
                   "spot near VWAP and balanced OI": lambda x: x[6], "day before an event": lambda x: x[0] in next_event, "event day (IV crush)": lambda x: x[0] in EVENTS,
                   **{name: (lambda x, n=n: x[0].weekday() == n) for n, name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"))},
                   **{str(y): (lambda x, y=y: x[0].year == y) for y in sorted({x[0].year for x in items})}}
        for title, which in (("Filters on the chosen rule", items), ("Filters on the reference rule", results[base_index])):
            lines += [f"### {title}", "", c.table([{"filter": name} | stats([x for x in which if f(x)]) for name, f in filters.items()],
                                                  ["filter", "trades", "net_per_trade", "net_pct_margin", "win_rate", "worst_day", "max_drawdown"]), ""]
    pd.DataFrame(all_rows).to_csv(runs / "short_straddle_variants.csv", index=False)
    lines += ["## Reading this", "",
              "An unhedged short straddle has open-ended loss: a stop is not a guarantee when price jumps past it, and these fills assume the stop price unless the minute opened beyond it. "
              f"All {len(all_rows)} variants are in `reports/delta_runs/short_straddle_variants.csv`; add them to the running total. Judge by the final check and its interval.", ""]
    (c.REPORTS / "2026-11-26-short-straddle.md").write_text("\n".join(lines))
    print("variants", len(all_rows))


if __name__ == "__main__":
    main()
