"""VWAP, ATR, relative volume, market regime and the effective-dated session calendar. All causal."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import median

from .config import RegimeCfg
from .profile import ProfileStats, VolumeProfile
from .vtypes import IST, Bar, D


# ---------------------------------------------------------------- session calendar (effective-dated)
# (effective_from, close_time). Source: owner-supplied NSE FAQ (6 Nov 2026): 15:45 from 7 Dec 2026; expiring index
# derivatives keep 15:30 on expiry day. UNVERIFIED by this code: confirm against the NSE circular.
_CLOSE_TABLE: list[tuple[date, time]] = [(date(2000, 1, 1), time(15, 30)), (date(2026, 12, 7), time(15, 45))]
SESSION_OPEN = time(9, 15)


def session_close(day: date, *, is_expiring_index_derivative: bool = False) -> datetime:
    close = _CLOSE_TABLE[0][1]
    for eff, t in _CLOSE_TABLE:
        if day >= eff:
            close = t
    if is_expiring_index_derivative and day.year >= 2026:
        close = time(15, 30)
    return datetime.combine(day, close, tzinfo=IST)


# ---------------------------------------------------------------- indicators
def atr_series(bars: list[Bar], period: int = 14) -> list[float | None]:
    """Wilder ATR on completed bars. The previous session's last bar is NOT used here: pass a bar list that includes
    prior-session bars if a warm ATR is wanted; values before ``period`` bars are None."""
    out: list[float | None] = [None] * len(bars)
    tr: list[float] = []
    for i, b in enumerate(bars):
        h, l = float(b.high), float(b.low)
        pc = float(bars[i - 1].close) if i else None
        tr.append(h - l if pc is None else max(h - l, abs(h - pc), abs(l - pc)))
    if len(bars) < period:
        return out
    a = sum(tr[:period]) / period
    out[period - 1] = a
    for i in range(period, len(bars)):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a
    return out


def vwap_series(bars: list[Bar]) -> list[float | None]:
    """Candle-based session VWAP using typical price (H+L+C)/3: APPROXIMATE. Use exact_vwap_series with trade data."""
    out: list[float | None] = []
    pv = vv = 0.0
    for b in bars:
        v = float(b.volume)
        pv += float(b.high + b.low + b.close) / 3 * v
        vv += v
        out.append(pv / vv if vv > 0 else None)
    return out


def exact_vwap_series(bars: list[Bar], tick: Decimal) -> list[float | None]:
    """VWAP from trade-level volume-at-price (sum price*qty / sum qty); falls back per-bar to typical price."""
    out: list[float | None] = []
    pv = 0.0
    vv = 0.0
    for b in bars:
        if b.vol_at:
            for i, q in b.vol_at.items():
                pv += float(tick * i) * float(q)
                vv += float(q)
        else:
            pv += float(b.high + b.low + b.close) / 3 * float(b.volume)
            vv += float(b.volume)
        out.append(pv / vv if vv > 0 else None)
    return out


def vwap_slope(vwap: list[float | None], atr: list[float | None], i: int, lookback: int = 3) -> float | None:
    if i < lookback or vwap[i] is None or vwap[i - lookback] is None or atr[i] in (None, 0):
        return None
    return (vwap[i] - vwap[i - lookback]) / atr[i]  # type: ignore[operator]


def slot_key(ts: datetime) -> time:
    return ts.astimezone(IST).time().replace(second=0, microsecond=0)


def relative_volume(bar: Bar, history: dict[time, list[Decimal]], window: int, min_samples: int | None = None) -> float | None:
    """volume / median(volume at the same time-of-day over up to ``window`` prior sessions). None when fewer than ``min_samples``
    (default: ``window``) prior sessions exist: invalid, never a default of 1.0."""
    h = history.get(slot_key(bar.ts), [])
    need = window if min_samples is None else min_samples
    if len(h) < need:
        return None
    m = median(h[-window:])
    return float(bar.volume / m) if m > 0 else None


# ---------------------------------------------------------------- regime
BULL, BEAR, BALANCED, TRANSITION = "BULLISH_TREND", "BEARISH_TREND", "BALANCED", "TRANSITION"


@dataclass
class Regime:
    name: str
    reasons: list[str] = field(default_factory=list)
    slope: float | None = None


def classify_regime(bars: list[Bar], i: int, vwap: list[float | None], atr: list[float | None], prev: ProfileStats | None,
                    prev_prof: VolumeProfile | None, poc_traj: list[int | None], cfg: RegimeCfg,
                    feed_ok: bool = True) -> Regime:
    """Independent of any entry setup (no circularity). Evaluated on completed bars up to and including i."""
    s = vwap_slope(vwap, atr, i, cfg.slope_lookback_bars)
    if not feed_ok:
        return Regime(TRANSITION, ["feed quality failure"], s)
    if s is None or vwap[i] is None:
        return Regime(TRANSITION, ["VWAP/ATR not yet available"], s)
    w = bars[max(0, i - cfg.closes_window + 1): i + 1]
    if len(w) < cfg.closes_window:
        return Regime(TRANSITION, [f"fewer than {cfg.closes_window} completed bars"], s)
    vw = [vwap[k] for k in range(i - len(w) + 1, i + 1)]
    above = sum(1 for b, v in zip(w, vw) if v is not None and float(b.close) > v)
    below = sum(1 for b, v in zip(w, vw) if v is not None and float(b.close) < v)
    price = float(bars[i].close)
    ptraj = [p for p in poc_traj[max(0, i - cfg.closes_window + 1): i + 1] if p is not None]
    poc_move = (ptraj[-1] - ptraj[0]) if len(ptraj) >= 2 else 0
    tick = float(prev_prof.tick) if prev_prof else 0.0
    poc_move_price = poc_move * tick
    a = atr[i] or 0.0
    if price > vwap[i] and s >= cfg.slope_threshold and above >= cfg.closes_required and poc_move_price >= -cfg.balanced_poc_max_atr * a:
        return Regime(BULL, [f"price>VWAP, slope {s:.3f}>={cfg.slope_threshold}", f"{above}/{len(w)} closes above VWAP",
                             f"developing POC moved {poc_move_price:+.2f}"], s)
    if price < vwap[i] and s <= -cfg.slope_threshold and below >= cfg.closes_required and poc_move_price <= cfg.balanced_poc_max_atr * a:
        return Regime(BEAR, [f"price<VWAP, slope {s:.3f}<=-{cfg.slope_threshold}", f"{below}/{len(w)} closes below VWAP",
                             f"developing POC moved {poc_move_price:+.2f}"], s)
    if prev is not None and prev_prof is not None:
        val, vah = prev_prof.price(prev.val), prev_prof.price(prev.vah)
        inside = sum(1 for b in w if val <= b.close <= vah)
        if inside >= cfg.closes_required and abs(s) < cfg.slope_threshold and abs(poc_move_price) <= cfg.balanced_poc_max_atr * a:
            return Regime(BALANCED, [f"{inside}/{len(w)} closes inside previous value", f"|slope| {abs(s):.3f}<{cfg.slope_threshold}",
                                     f"developing POC range {abs(poc_move_price):.2f}<={cfg.balanced_poc_max_atr}*ATR"], s)
    return Regime(TRANSITION, ["conditions conflict or no regime criteria met"], s)
