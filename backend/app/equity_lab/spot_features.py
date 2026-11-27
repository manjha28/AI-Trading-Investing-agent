"""Spot-derived features and targets on completed 5-minute index bars (research only).

Bar b covers minutes b*5 .. b*5+4. Everything in its row is known when it completes; the trade would start at the open of minute (b+1)*5.
Index 5-minute bars are built from the downloaded Dhan 1-minute bars. Floats: these are research statistics, not account money.
"""
from __future__ import annotations

import warnings
from datetime import date

import numpy as np
import pandas as pd

from app.equity_lab import index_options_common as c
from app.equity_lab.spot_fetch import OUT as CONSTITUENTS
from app.equity_lab.spot_fetch import WEIGHTS

BARS = 75
HORIZONS = (15, 30, 60)
INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")
SPREAD_STEPS = 2
PROXIES = ("cons", "opt", "fut")


def _div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.asarray(a, dtype=float) / np.asarray(b, dtype=float)
    return np.where(np.isfinite(out), out, np.nan)


def five_minute(minutes: np.ndarray) -> np.ndarray:
    """(days, 375, [o,h,l,c]) -> (days, 75, [o,h,l,c])."""
    x = minutes.reshape(len(minutes), BARS, 5, 4)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        high, low = np.nanmax(x[..., 1], axis=2), np.nanmin(x[..., 2], axis=2)
    first = pd.DataFrame(x[..., 0].reshape(-1, 5)).bfill(axis=1).iloc[:, 0].to_numpy().reshape(len(minutes), BARS)
    last = pd.DataFrame(x[..., 3].reshape(-1, 5)).ffill(axis=1).iloc[:, -1].to_numpy().reshape(len(minutes), BARS)
    return np.stack([first, high, low, last], axis=2)


def constituents(symbol: str, days: list[date]) -> dict[str, np.ndarray] | None:
    f = c._read(CONSTITUENTS / f"{symbol}.pkl.gz")
    if f is None:
        return None
    pos = {d: i for i, d in enumerate(days)}
    f = f[f.day.isin(pos)]
    rows, cols = f.day.map(pos).to_numpy(), (f.slot // 5).to_numpy()
    out = {}
    for name in ("open", "high", "low", "close", "volume"):
        a = np.full((len(days), BARS), np.nan)
        a[rows, cols] = f[name].to_numpy()
        out[name] = a
    return out


def breadth(underlying: str, days: list[date]) -> tuple[np.ndarray, dict[str, np.ndarray], int]:
    """(summed constituent traded value per bar, breadth features, number of names found)."""
    shape = (len(days), BARS)
    value, have = np.zeros(shape), np.zeros(shape)
    adv, dec, seen, wabove, wseen = (np.zeros(shape) for _ in range(5))
    names = 0
    for symbol, weight in WEIGHTS[underlying].items():
        x = constituents(symbol, days)
        if x is None:
            continue
        names += 1
        close, vol = x["close"], np.nan_to_num(x["volume"])
        typical = np.nan_to_num((x["high"] + x["low"] + close) / 3)
        vwap = _div(np.cumsum(typical * vol, axis=1), np.cumsum(vol, axis=1))
        last = pd.DataFrame(close).ffill(axis=1).iloc[:, -1]
        prior = last.ffill().shift(1).to_numpy()[:, None]
        ok = np.isfinite(close)
        value += typical * vol
        have += ok
        moved = ok & np.isfinite(prior)
        adv += moved & (close > prior)
        dec += moved & (close < prior)
        seen += moved
        wseen += weight * (ok & np.isfinite(vwap))
        wabove += weight * (ok & (close > vwap))
    enough = have >= 0.7 * max(names, 1)
    value = np.where(enough, value, np.nan)
    share = np.where(enough, _div(wabove, wseen), np.nan)
    feats = {"adv_dec": np.where(enough, _div(adv - dec, seen), np.nan), "above_vwap_share": share,
             "above_vwap_share_chg": share - pd.DataFrame(share).shift(6, axis=1).to_numpy()}
    return value, feats, names


def same_time_ratio(volume: np.ndarray) -> np.ndarray:
    """Bar volume / the average of the same bar of the previous 20 days."""
    usual = pd.DataFrame(volume).rolling(20, min_periods=10).mean().shift(1).to_numpy()
    return _div(volume, usual)


def option_day(cube: dict, minutes: np.ndarray) -> dict[str, np.ndarray]:
    """Option-derived context per bar, from the last minute of the bar."""
    out = {k: np.full(BARS, np.nan) for k in ("ce_oi_chg", "pe_oi_chg", "oi_tilt_chg", "pcr", "pcr_chg", "atm_iv", "iv_chg_day", "iv_chg_30m", "opt_volume")}
    strikes = cube["strikes"]
    if len(strikes) < 2:
        return out
    step = float(np.median(np.diff(strikes)))
    vol = np.nansum(cube["CE"]["volume"], axis=1) + np.nansum(cube["PE"]["volume"], axis=1)
    out["opt_volume"] = np.where(vol.reshape(BARS, 5).sum(axis=1) > 0, vol.reshape(BARS, 5).sum(axis=1), np.nan)
    oi = {s: pd.DataFrame(cube[s]["oi"]).ffill(limit=5).to_numpy() for s in ("CE", "PE")}
    iv = {s: cube[s]["iv"] for s in ("CE", "PE")}
    first_iv = np.nan
    atm_iv = np.full(BARS, np.nan)
    for b in range(BARS):
        m = b * 5 + 4
        k = cube["atm"]["CE"][m]
        if not np.isfinite(k):
            continue
        col = int(np.searchsorted(strikes, k))
        near = np.abs(strikes - k) <= 2 * step + 1e-6
        m0 = max(m - 30, 4)
        for side, name in (("CE", "ce_oi_chg"), ("PE", "pe_oi_chg")):
            now, then = oi[side][m, near], oi[side][m0, near]
            both = np.isfinite(now) & np.isfinite(then)
            if both.any() and then[both].sum() > 0 and m > m0:
                out[name][b] = now[both].sum() / then[both].sum() - 1
        ce, pe = np.nansum(oi["CE"][m]), np.nansum(oi["PE"][m])
        if ce > 0 and pe > 0:
            out["pcr"][b] = pe / ce
        values = [v for v in (iv["CE"][m, col], iv["PE"][m, col]) if np.isfinite(v) and v > 0]
        if values:
            atm_iv[b] = float(np.mean(values))
            if not np.isfinite(first_iv):
                first_iv = atm_iv[b]
    out["oi_tilt_chg"] = out["pe_oi_chg"] - out["ce_oi_chg"]
    out["pcr_chg"] = out["pcr"] - pd.Series(out["pcr"]).shift(6).to_numpy()
    out["atm_iv"], out["iv_chg_day"] = atm_iv, atm_iv - first_iv
    out["iv_chg_30m"] = atm_iv - pd.Series(atm_iv).shift(6).to_numpy()
    return out


def option_trades(underlying: str, day: date, cube: dict) -> dict[str, np.ndarray]:
    """Net premium points per unit (0.5% and 1.5% slippage, charges for one lot) of buying at each bar's next open and selling h minutes later."""
    strikes, lot = cube["strikes"], c.lot_size(underlying, day)
    out = {f"{kind}_{side}_{h}_{slip}": np.full(BARS, np.nan) for kind in ("atm", "spread") for side in ("CE", "PE") for h in HORIZONS for slip in ("lo", "hi")}
    if len(strikes) < 2:
        return out
    step = float(np.median(np.diff(strikes)))
    for side, away in (("CE", 1), ("PE", -1)):
        opens, closes = cube[side]["open"], cube[side]["close"]
        for b in range(BARS - 3):
            e = b * 5 + 5
            k = cube["atm"][side][e]
            if not np.isfinite(k):
                continue
            col = int(np.searchsorted(strikes, k))
            sold = np.flatnonzero(np.isclose(strikes, k + away * SPREAD_STEPS * step))
            for h in HORIZONS:
                x = e + h - 1
                if x >= c.MINUTES:
                    continue
                buy, sell = float(opens[e, col]), float(closes[x, col])
                if not (buy >= 1 and np.isfinite(sell)):
                    continue
                short = (float(opens[e, sold[0]]), float(closes[x, sold[0]])) if len(sold) else (np.nan, np.nan)
                for slip, amount in (("lo", 0.005), ("hi", 0.015)):
                    fills = [(1, buy * (1 + amount), lot), (-1, sell * (1 - amount), lot)]
                    net = sell * (1 - amount) - buy * (1 + amount) - c.charges(underlying, day, fills) / lot
                    out[f"atm_{side}_{h}_{slip}"][b] = net
                    if np.isfinite(short[0]) and np.isfinite(short[1]) and short[0] > 0:
                        legs = [(-1, short[0] * (1 - amount), lot), (1, short[1] * (1 + amount), lot)]
                        out[f"spread_{side}_{h}_{slip}"][b] = net + short[0] * (1 - amount) - short[1] * (1 + amount) - c.charges(underlying, day, legs) / lot
    return out


def index_frame(underlying: str, vix: dict[date, np.ndarray]) -> tuple[pd.DataFrame, dict]:
    index = c.index_minutes(underlying)
    days = sorted(index.day.unique())
    pos = {d: i for i, d in enumerate(days)}
    m = np.full((len(days), c.MINUTES, 4), np.nan)
    m[index.day.map(pos).to_numpy(), index.slot.to_numpy()] = index[["open", "high", "low", "close"]].to_numpy()
    full = np.isfinite(m[..., 3]).sum(axis=1) >= 300                      # drops muhurat and broken sessions
    days, m = [d for d, ok in zip(days, full) if ok], m[full]
    n = len(days)
    bars = five_minute(m)
    o, h, low, close = (bars[..., i] for i in range(4))
    filled = pd.DataFrame(close).ffill(axis=1).to_numpy()
    day_open = pd.DataFrame(o).bfill(axis=1).iloc[:, 0].to_numpy()
    high_so_far, low_so_far = np.fmax.accumulate(h, axis=1), np.fmin.accumulate(low, axis=1)
    pdh, pdl, pdc = (np.concatenate([[np.nan], x[:-1]])[:, None] for x in (high_so_far[:, -1], low_so_far[:, -1], filled[:, -1]))
    flat = pd.Series(filled.ravel())
    fh, fl = pd.Series(h.ravel()), pd.Series(low.ravel())
    pct = lambda ref: 100 * (_div(close, ref) - 1)
    f: dict[str, np.ndarray] = {"dist_prev_high": pct(pdh), "dist_prev_low": pct(pdl), "dist_prev_close": pct(pdc),
                                "gap": np.repeat((100 * (day_open[:, None] / pdc - 1)), BARS, axis=1)}
    for minutes, first in ((15, 3), (30, 6)):
        top, bottom = np.nanmax(h[:, :first], axis=1)[:, None], np.nanmin(low[:, :first], axis=1)[:, None]
        where = np.where(close > top, 1.0, np.where(close < bottom, -1.0, 0.0))
        mid = pct((top + bottom) / 2)
        where[:, :first - 1], mid[:, :first - 1] = np.nan, np.nan
        f[f"or{minutes}_position"], f[f"or{minutes}_mid_dist"] = where, mid
    value, wide, names = breadth(underlying, days)
    typical = (h + low + filled) / 3
    weight = np.nan_to_num(value)
    vwap = _div(np.nancumsum(np.nan_to_num(typical) * weight, axis=1), np.cumsum(weight, axis=1))
    f["vwap_dist"] = pct(vwap)
    f["vwap_slope"] = 100 * (_div(vwap, pd.DataFrame(vwap).shift(6, axis=1).to_numpy()) - 1)
    for length in (5, 20, 50):
        f[f"ma{length}_dist"] = pct(flat.rolling(length).mean().to_numpy().reshape(n, BARS))
    prior_close = flat.shift(1)
    tr = pd.concat([fh - fl, (fh - prior_close).abs(), (fl - prior_close).abs()], axis=1).max(axis=1)
    atr = 100 * tr.rolling(14, min_periods=10).mean() / flat
    f["atr_pct"] = atr.to_numpy().reshape(n, BARS)
    f["atr_rank"] = atr.rolling(1500, min_periods=500).rank(pct=True).to_numpy().reshape(n, BARS)
    grid = np.arange(BARS)[None, :]
    for name, series, running in (("since_day_high", h, high_so_far), ("since_day_low", low, low_so_far)):
        at = np.where(series == running, grid, -1)
        f[name] = (grid - np.maximum.accumulate(at, axis=1)) * 5.0
    f["swing_5m"] = np.where(fh > fh.shift(1).rolling(6).max(), 1.0, np.where(fl < fl.shift(1).rolling(6).min(), -1.0, 0.0)).reshape(n, BARS)
    h15, l15 = fh.rolling(3).max(), fl.rolling(3).min()
    f["swing_15m"] = np.where(h15 > fh.shift(3).rolling(12).max(), 1.0, np.where(l15 < fl.shift(3).rolling(12).min(), -1.0, 0.0)).reshape(n, BARS)
    for length in (1, 6):
        f[f"return_{length}bar"] = (100 * (flat / flat.shift(length) - 1)).to_numpy().reshape(n, BARS)
    f.update(wide)
    # options: context, volume proxy and trade results, month by month to keep memory small
    option: dict[str, np.ndarray] = {}
    for month in c.months_available(underlying):
        for day, cube in c.option_month(underlying, month, 4).items():
            if day not in pos or day not in set(days):
                continue
            i = days.index(day)
            for source in (option_day(cube, m[i]), option_trades(underlying, day, cube)):
                for key, values in source.items():
                    option.setdefault(key, np.full((n, BARS), np.nan))[i] = values
    optvol = option.pop("opt_volume")
    futvol = np.full((n, BARS), np.nan)
    at = {d: i for i, d in enumerate(days)}
    for path in sorted((c.DATA / "futures" / underlying).glob("*.pkl.gz"), reverse=True):       # nearer expiries overwrite later ones
        x = c._read(path)
        if x is None:
            continue
        expiry = date.fromisoformat(path.name.split(".")[0])
        for (day, bar), v in x[x.day <= expiry].groupby(["day", x.slot // 5]).volume.sum().items():
            if day in at:
                futvol[at[day], bar] = v
    volumes = {"cons": value, "opt": optvol, "fut": futvol}
    direction = np.sign(close - o)
    for name in ("cons", "opt"):                                          # futures history is too short to model
        v = volumes[name]
        ratio = same_time_ratio(v)
        f[f"rvol_{name}"] = ratio
        r = pd.Series(ratio.ravel())
        f[f"rvol_trend_{name}"] = _div(r.rolling(3).mean(), r.shift(3).rolling(3).mean()).reshape(n, BARS)
        flow = pd.Series(np.nan_to_num(v * direction).ravel()).rolling(6).sum()
        total = pd.Series(np.nan_to_num(v).ravel()).rolling(6).sum()
        f[f"updown_{name}"] = _div(flow, total).reshape(n, BARS)
    for key in ("ce_oi_chg", "pe_oi_chg", "oi_tilt_chg", "pcr", "pcr_chg", "atm_iv", "iv_chg_day", "iv_chg_30m"):
        f[key] = option.pop(key)
    level = np.full((n, BARS), np.nan)
    for i, d in enumerate(days):
        if d in vix:
            level[i] = vix[d]
    f["vix"] = level
    f["vix_chg_day"] = level - pd.DataFrame(level).bfill(axis=1).iloc[:, 0].to_numpy()[:, None]
    f["minutes"] = np.repeat(((grid + 1) * 5.0), n, axis=0)
    f["weekday"] = np.repeat(np.array([d.weekday() for d in days], dtype=float)[:, None], BARS, axis=1)
    expiry = c.expiry_days(underlying, days)
    f["expiry_day"] = np.repeat(np.array([float(d in expiry) for d in days])[:, None], BARS, axis=1)
    features = list(f)
    for hz in HORIZONS:
        target = np.full((n, BARS), np.nan)
        for b in range(BARS):
            e = b * 5 + 5
            if e + hz - 1 < c.MINUTES:
                target[:, b] = 100 * (m[:, e + hz - 1, 3] / m[:, e, 0] - 1)
        f[f"ret_{hz}"] = target
    f.update(option)
    f["spot"] = close
    frame = pd.DataFrame({k: np.asarray(v, dtype="float32").ravel() for k, v in f.items()})
    frame["day"] = np.repeat(np.array(days, dtype=object), BARS)
    frame["bar"] = np.tile(np.arange(BARS), n)
    frame["underlying"] = underlying
    both = np.isfinite(volumes["cons"]) & np.isfinite(volumes["opt"])
    coverage = {"names": names, "wanted": len(WEIGHTS[underlying])}
    for name, v in volumes.items():
        on = np.isfinite(v).any(axis=1)
        have = [d for d, ok in zip(days, on) if ok]
        coverage[name] = (len(have), str(have[0]) if have else None, str(have[-1]) if have else None)
    ratios = {name: same_time_ratio(v) for name, v in volumes.items()}
    for a, b in (("cons", "opt"), ("cons", "fut"), ("opt", "fut")):
        ok = np.isfinite(ratios[a]) & np.isfinite(ratios[b])
        coverage[f"corr_{a}_{b}"] = (round(float(pd.Series(ratios[a][ok]).rank().corr(pd.Series(ratios[b][ok]).rank())), 2), int(ok.sum())) if ok.sum() > 100 else None
    coverage["days"] = (len(days), str(days[0]), str(days[-1]), int(both.sum()))
    frame = frame[np.isfinite(frame.spot) & (frame.bar < BARS - 3)]
    return frame, {"features": features, "coverage": coverage}


def build() -> tuple[pd.DataFrame, list[str], dict]:
    v = c.index_minutes("INDIAVIX")
    vix = {}
    for day, g in v.groupby("day"):
        a = np.full(c.MINUTES, np.nan)
        a[g.slot.to_numpy()] = g.close.to_numpy()
        vix[day] = pd.Series(a).ffill().to_numpy()[4::5]
    frames, coverage, features = [], {}, []
    for underlying in INDICES:
        frame, info = index_frame(underlying, vix)
        frames.append(frame)
        features, coverage[underlying] = info["features"], info["coverage"]
        print(underlying, len(frame), "rows", info["coverage"], flush=True)
    data = pd.concat(frames, ignore_index=True)
    data = data.sort_values(["day", "bar", "underlying"], kind="stable").reset_index(drop=True)
    return data, features, coverage
