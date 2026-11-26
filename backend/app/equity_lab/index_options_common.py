"""Shared loading, calendar and cost code for the index option studies (research only: nothing here can reach an order).

Floats are used throughout: these are statistics over downloaded candles, not stored or displayed account money.
Lot sizes, expiry weekdays and older statutory rates are from published circulars as recalled, NOT re-verified here; the reports say so.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "index_options"
REPORTS = ROOT / "reports"
MINUTES = 375                                              # 09:15 .. 15:29
UNDERLYINGS = {"NIFTY": "WEEK", "SENSEX": "WEEK", "BANKNIFTY": "MONTH"}
SLIPPAGE = {"half spread 0.5%": ("pct", 0.005), "half spread 1.5%": ("pct", 0.015), "Rs 1 a unit": ("rs", 1.0)}


def slot(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m - 555


def _read(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    f = pd.read_pickle(path)
    if not len(f):
        return None
    local = pd.to_datetime(f["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    f = f.assign(day=local.dt.date, slot=local.dt.hour * 60 + local.dt.minute - 555)
    return f[(f.slot >= 0) & (f.slot < MINUTES)].drop_duplicates(["ts"] + (["strike"] if "strike" in f else []))


def index_minutes(symbol: str) -> pd.DataFrame:
    parts = [_read(p) for p in sorted((DATA / "index" / symbol).glob("*.pkl.gz"))]
    return pd.concat([p for p in parts if p is not None], ignore_index=True).sort_values("ts")


def months_available(underlying: str, offset: str = "ATM") -> list[str]:
    base = DATA / "options" / underlying / UNDERLYINGS[underlying]
    return sorted(p.name.split(".")[0] for p in (base / f"{offset}_CE").glob("*.pkl.gz") if (base / f"{offset}_PE" / p.name).exists())


def option_month(underlying: str, month: str, max_offset: int) -> dict[date, dict]:
    """{day: {"strikes": array, "CE"/"PE": {field: array[minute, strike]}, "atm": {"CE"/"PE": array[minute]}, "oi_vol": ...}} for one month."""
    base = DATA / "options" / underlying / UNDERLYINGS[underlying]
    offsets = ["ATM"] + [f"ATM{s}{n}" for n in range(1, max_offset + 1) for s in "+-"]
    sides = {}
    atm = {}
    for side in ("CE", "PE"):
        frames = []
        for off in offsets:
            f = _read(base / f"{off}_{side}" / f"{month}.pkl.gz")
            if f is None:
                continue
            if off == "ATM":
                atm[side] = f[["day", "slot", "strike"]]
            frames.append(f)
        if not frames:
            return {}
        sides[side] = pd.concat(frames, ignore_index=True).drop_duplicates(["day", "slot", "strike"])
    out: dict[date, dict] = {}
    groups = {side: dict(tuple(f.groupby("day"))) for side, f in sides.items()}
    atms = {side: dict(tuple(f.groupby("day"))) for side, f in atm.items()}
    for day in sorted(set(groups["CE"]) & set(groups["PE"])):
        strikes = np.unique(np.concatenate([groups["CE"][day].strike.values, groups["PE"][day].strike.values]))
        strikes = strikes[np.isfinite(strikes)]
        entry: dict = {"strikes": strikes, "atm": {}}
        for side in ("CE", "PE"):
            g = groups[side][day]
            g = g[np.isfinite(g.strike)]
            rows, cols = g.slot.values.astype(int), np.searchsorted(strikes, g.strike.values)
            fields = {}
            for name in ("open", "high", "low", "close", "volume", "oi", "iv"):
                a = np.full((MINUTES, len(strikes)), np.nan, dtype="float32")
                if name in g:
                    a[rows, cols] = g[name].values
                fields[name] = a
            entry[side] = fields
            k = np.full(MINUTES, np.nan)
            if day in atms.get(side, {}):
                k[atms[side][day].slot.values.astype(int)] = atms[side][day].strike.values
            entry["atm"][side] = k
        out[day] = entry
    return out


# ───────────────────────── calendar and contract terms (see module note) ─────────────────────────
def lot_size(underlying: str, day: date) -> int:
    table = {"NIFTY": [(date(2021, 1, 1), 75), (date(2021, 7, 30), 50), (date(2024, 4, 26), 25), (date(2024, 11, 20), 75), (date(2026, 1, 1), 65)],
             "BANKNIFTY": [(date(2021, 1, 1), 25), (date(2023, 7, 1), 15), (date(2024, 11, 20), 30), (date(2025, 7, 1), 35), (date(2026, 1, 1), 30)],
             "SENSEX": [(date(2021, 1, 1), 10), (date(2024, 11, 20), 20)]}[underlying]
    return [size for since, size in table if day >= since][-1]


def expiry_weekday(underlying: str, day: date) -> int:
    if underlying == "SENSEX":
        return 4 if day < date(2025, 1, 1) else 1 if day < date(2025, 9, 1) else 3
    return 3 if day < date(2025, 9, 1) else 1


def expiry_days(underlying: str, days: list[date]) -> set[date]:
    """Trading days on which the contract in use expires: the expiry weekday, or the last trading day before it in that week (month for BANKNIFTY)."""
    out: set[date] = set()
    keyed: dict[tuple, list[date]] = {}
    for d in days:
        keyed.setdefault((d.year, d.month) if underlying == "BANKNIFTY" else d.isocalendar()[:2], []).append(d)
    for group in keyed.values():
        wd = expiry_weekday(underlying, group[0])
        eligible = [d for d in group if d.weekday() <= wd]
        if underlying == "BANKNIFTY":
            eligible = [d for d in group if d.weekday() == wd]
        if eligible:
            out.add(max(eligible))
    return out


def rates(underlying: str, day: date) -> dict[str, float]:
    stt = 0.0005 if day < date(2023, 4, 1) else 0.000625 if day < date(2024, 10, 1) else 0.001 if day < date(2026, 4, 1) else 0.0015
    old = day < date(2024, 10, 1)
    exchange = (0.0005 if old else 0.000325) if underlying == "SENSEX" else (0.0005 if old else 0.0003503)
    return {"brokerage": 20.0, "stt_sell": stt, "exchange": exchange, "sebi": 0.000001, "stamp_buy": 0.00003, "gst": 0.18}


def charges(underlying: str, day: date, fills: list[tuple[int, float, float]]) -> float:
    """Rupees for a list of orders (+1 buy / -1 sell, price per unit, units): brokerage per order, STT on sells, exchange, SEBI, stamp on buys, GST."""
    r = rates(underlying, day)
    total = 0.0
    for side, price, qty in fills:
        value = price * qty
        service = r["brokerage"] + value * (r["exchange"] + r["sebi"])
        total += service * (1 + r["gst"]) + value * (r["stt_sell"] if side < 0 else r["stamp_buy"])
    return total


def slipped(price: float, side: int, case: tuple[str, float]) -> float:
    kind, amount = case
    step = price * amount if kind == "pct" else amount
    return price + step if side > 0 else max(price - step, 0.05)


def years_to_expiry(underlying: str, day: date, minute: int) -> float:
    """Trading time left to the contract's expiry (weekday rule; holidays ignored). Only used to estimate missing prices and attribute losses."""
    ahead = next(day + timedelta(days=n) for n in range(0, 40) if (day + timedelta(days=n)).weekday() == expiry_weekday(underlying, day)
                 and (underlying != "BANKNIFTY" or (day + timedelta(days=n + 7)).month != (day + timedelta(days=n)).month))
    return max((np.busday_count(day, ahead) * MINUTES + (MINUTES - minute)) / (252 * MINUTES), 15 / (252 * MINUTES))


_erf = np.vectorize(math.erf)


def bs_price(spot: np.ndarray, strike: float, iv: np.ndarray, years: float, call: bool) -> np.ndarray:
    sq = np.maximum(iv, 0.03) * math.sqrt(years)
    d1 = (np.log(spot / strike) + 0.5 * sq * sq) / sq
    n = lambda x: 0.5 * (1 + _erf(x / math.sqrt(2)))
    price = spot * n(d1) - strike * n(d1 - sq)
    return price if call else price - spot + strike


def priced(f: dict, col: int, a: int, b: int, spot: np.ndarray, strike: float, years: float, call: bool):
    """(open, high, low, close, estimated mask) for one strike from minute a to b. Dhan only returns strikes near the CURRENT price, so after a large
    move a bought or sold strike has no print: it is then ESTIMATED from the index with the strike's last seen implied volatility, never frozen at an old price."""
    o, h, low, close = (f[k][a:b + 1, col].astype(float) for k in ("open", "high", "low", "close"))
    missing = ~np.isfinite(close)
    if missing.any():
        iv = pd.Series(f["iv"][a:b + 1, col].astype(float) / 100).replace(0, np.nan).ffill().bfill().fillna(0.15).to_numpy()
        index = pd.Series(spot[a:b + 1]).ffill().bfill().to_numpy()
        est = np.maximum(bs_price(index, strike, iv, years, call), 0.05)
        o, h, low, close = (np.where(missing, est, x) for x in (o, h, low, close))
    o, h, low = (np.where(np.isfinite(x), x, close) for x in (o, h, low))
    return o, h, low, close, missing


def splits(days: list[date]) -> tuple[date, date]:
    """Last day of train (50%) and of validation (next 25%); everything after is the final check."""
    days = sorted(days)
    return days[len(days) // 2 - 1], days[(3 * len(days)) // 4 - 1]


def summary(net: np.ndarray, pct: np.ndarray, hold: np.ndarray) -> dict:
    if not len(net):
        return {"trades": 0, "net_per_trade": None, "net_pct_premium": None, "win_rate": None, "profit_factor": None, "max_drawdown": None, "avg_minutes": None}
    equity = np.cumsum(net)
    gains, losses = net[net > 0].sum(), -net[net < 0].sum()
    return {"trades": int(len(net)), "net_per_trade": round(float(net.mean()), 1), "net_pct_premium": round(float(pct.mean()), 3), "win_rate": round(float((net > 0).mean() * 100), 1),
            "profit_factor": round(float(gains / losses), 3) if losses > 0 else None, "max_drawdown": round(float((equity - np.maximum.accumulate(np.maximum(equity, 0))).min()), 0),
            "avg_minutes": round(float(hold.mean()), 1)}


def day_bootstrap(days: np.ndarray, values: np.ndarray, n: int = 3000, seed: int = 7) -> tuple[float, float]:
    """90% interval of the mean per trade, resampling whole days (trades within a day are not independent)."""
    uniq, inverse = np.unique(days, return_inverse=True)
    sums, counts = np.bincount(inverse, weights=values), np.bincount(inverse)
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(uniq), size=(n, len(uniq)))
    means = sums[picks].sum(axis=1) / counts[picks].sum(axis=1)
    return float(np.quantile(means, 0.05)), float(np.quantile(means, 0.95))


def table(rows: list[dict], columns: list[str]) -> str:
    head = "| " + " | ".join(columns) + " |\n|" + "---|" * len(columns)
    return "\n".join([head] + ["| " + " | ".join("—" if r.get(c) is None else str(r.get(c)) for c in columns) + " |" for r in rows])
