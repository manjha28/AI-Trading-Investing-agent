"""Volume profile on integer tick indices (no float price comparisons).

Documented rules
- POC: highest-volume level. Ties -> level closest to the volume-weighted mean price; still tied -> lower price.
- Value area: start at POC; repeatedly add the adjacent level (one bin above vs one bin below) with greater volume
  until >= pct of total volume is included. Tie -> upper side. If one side is exhausted the other is taken.
- HVN/LVN: smoothed profile (centred moving average), local maxima with smoothed volume >= percentile threshold and
  prominence >= fraction of the peak are HVNs; LVNs are the minimum between adjacent HVNs when <= ratio*lower neighbour peak.
- Candle-only data is spread uniformly over the bar's tick range and the profile is labelled ``approximate``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from .vtypes import Bar, D, DataLevel, Side

ZERO = Decimal(0)


def tick_index(price: Decimal, tick: Decimal) -> int:
    return int((price / tick).to_integral_value(rounding=ROUND_HALF_EVEN))


def value_area(weights: dict[int, Decimal], pct: float, poc: int | None = None) -> tuple[int, int, Decimal] | None:
    """Return (val_idx, vah_idx, included_volume) or None for an empty distribution."""
    total = sum(weights.values(), ZERO)
    if not weights or total <= 0:
        return None
    if poc is None:
        poc = choose_poc(weights)
    lo = hi = poc
    inc = weights.get(poc, ZERO)
    target = total * D(pct) / Decimal(100)
    mn, mx = min(weights), max(weights)
    while inc < target and (lo > mn or hi < mx):
        up = weights.get(hi + 1, ZERO) if hi < mx else None
        dn = weights.get(lo - 1, ZERO) if lo > mn else None
        if up is None:
            lo -= 1; inc += dn  # type: ignore[operator]
        elif dn is None:
            hi += 1; inc += up
        elif up >= dn:
            hi += 1; inc += up
        else:
            lo -= 1; inc += dn
    return lo, hi, inc


def choose_poc(weights: dict[int, Decimal]) -> int:
    top = max(weights.values())
    tied = sorted(i for i, v in weights.items() if v == top)
    if len(tied) == 1:
        return tied[0]
    total = sum(weights.values(), ZERO)
    mean = sum((D(i) * v for i, v in weights.items()), ZERO) / total
    return min(tied, key=lambda i: (abs(D(i) - mean), i))


@dataclass(frozen=True)
class Node:
    kind: str          # "HVN" | "LVN"
    low_idx: int
    high_idx: int
    peak_idx: int
    strength: float


@dataclass(frozen=True)
class ProfileStats:
    poc: int
    val: int
    vah: int
    total: Decimal
    included: Decimal


class VolumeProfile:
    def __init__(self, tick: Decimal, *, level: DataLevel = DataLevel.C_CANDLE):
        if tick <= 0:
            raise ValueError("tick must be positive")
        self.tick = tick
        self.level = level
        self.vol: dict[int, Decimal] = {}
        self.buy: dict[int, Decimal] = {}
        self.sell: dict[int, Decimal] = {}
        self.approximate = level is DataLevel.C_CANDLE
        self.unallocated = ZERO

    # ---- construction
    def idx(self, price: Decimal) -> int:
        return tick_index(price, self.tick)

    def price(self, idx: int) -> Decimal:
        return self.tick * idx

    def add(self, price: Decimal, qty: Decimal, side: Side | None = None) -> None:
        if qty < 0:
            raise ValueError("negative quantity")
        i = self.idx(price)
        self.vol[i] = self.vol.get(i, ZERO) + qty
        if side is Side.BUY:
            self.buy[i] = self.buy.get(i, ZERO) + qty
        elif side is Side.SELL:
            self.sell[i] = self.sell.get(i, ZERO) + qty

    def add_range(self, low: Decimal, high: Decimal, qty: Decimal) -> None:
        """Approximation for candle data: uniform over [low, high] ticks; volume is conserved exactly."""
        lo, hi = self.idx(low), self.idx(high)
        n = hi - lo + 1
        share = qty / n
        acc = ZERO
        for k, i in enumerate(range(lo, hi + 1)):
            part = share if k < n - 1 else qty - acc
            acc += part
            self.vol[i] = self.vol.get(i, ZERO) + part

    def add_bar(self, bar: Bar) -> None:
        if bar.vol_at is not None:
            for i, v in bar.vol_at.items():
                self.vol[i] = self.vol.get(i, ZERO) + v
        else:
            self.add_range(bar.low, bar.high, bar.volume)

    def merged(self, other: "VolumeProfile") -> "VolumeProfile":
        if other.tick != self.tick:
            raise ValueError("cannot merge profiles with different tick sizes (different contracts)")
        out = VolumeProfile(self.tick, level=self.level if self.approximate or not other.approximate else other.level)
        out.approximate = self.approximate or other.approximate
        for src in (self, other):
            for i, v in src.vol.items():
                out.vol[i] = out.vol.get(i, ZERO) + v
        return out

    # ---- queries
    @property
    def total(self) -> Decimal:
        return sum(self.vol.values(), ZERO)

    def stats(self, pct: float) -> ProfileStats | None:
        va = value_area(self.vol, pct)
        if va is None:
            return None
        poc = choose_poc(self.vol)
        return ProfileStats(poc, va[0], va[1], self.total, va[2])

    def volume_above(self, price: Decimal) -> Decimal:
        i = self.idx(price)
        return sum((v for k, v in self.vol.items() if k > i), ZERO)

    def nodes(self, *, smoothing: int = 3, hvn_percentile: float = 80.0, min_prominence: float = 0.10,
              lvn_max_ratio: float = 0.5) -> list[Node]:
        if len(self.vol) < 3:
            return []
        lo, hi = min(self.vol), max(self.vol)
        idxs = list(range(lo, hi + 1))
        raw = [float(self.vol.get(i, ZERO)) for i in idxs]
        half = smoothing // 2
        sm = []
        for k in range(len(raw)):
            w = raw[max(0, k - half): k + half + 1]
            sm.append(sum(w) / len(w))
        ranked = sorted(sm)
        thr = ranked[min(len(ranked) - 1, int(len(ranked) * hvn_percentile / 100))]
        peaks = [k for k in range(len(sm)) if sm[k] >= thr and sm[k] > 0
                 and (k == 0 or sm[k] > sm[k - 1]) and (k == len(sm) - 1 or sm[k] >= sm[k + 1])]
        gmax = max(sm)
        pre, suf = list(sm), list(sm)
        for q in range(1, len(sm)):
            pre[q] = min(pre[q - 1], sm[q])
        for q in range(len(sm) - 2, -1, -1):
            suf[q] = min(suf[q + 1], sm[q])
        hvns: list[tuple[int, int, int, float]] = []
        for k in peaks:
            l = k
            while l > 0 and sm[l - 1] >= sm[k] / 2 and sm[l - 1] <= sm[l] + 1e-12:
                l -= 1
            r = k
            while r < len(sm) - 1 and sm[r + 1] >= sm[k] / 2 and sm[r + 1] <= sm[r] + 1e-12:
                r += 1
            prominence = (sm[k] - max(pre[k], suf[k])) / gmax if gmax else 0.0
            if prominence >= min_prominence:
                hvns.append((l, r, k, sm[k] / gmax))
        out = [Node("HVN", idxs[l], idxs[r], idxs[k], s) for l, r, k, s in hvns]
        for (l1, r1, k1, _), (l2, r2, k2, _) in zip(hvns, hvns[1:]):
            seg = sm[k1:k2 + 1]
            m = min(seg)
            if m <= lvn_max_ratio * min(sm[k1], sm[k2]):
                mi = k1 + seg.index(m)
                a = mi
                while a > k1 and sm[a - 1] <= min(sm[k1], sm[k2]) * lvn_max_ratio * 1.5:
                    a -= 1
                b = mi
                while b < k2 and sm[b + 1] <= min(sm[k1], sm[k2]) * lvn_max_ratio * 1.5:
                    b += 1
                out.append(Node("LVN", idxs[a], idxs[b], idxs[mi], 1.0 - m / min(sm[k1], sm[k2])))
        return sorted(out, key=lambda n: (n.peak_idx, n.kind))


def profile_from_bars(bars: list[Bar], tick: Decimal) -> VolumeProfile:
    level = DataLevel.A_TRADE if bars and all(b.vol_at is not None for b in bars) else DataLevel.C_CANDLE
    p = VolumeProfile(tick, level=level)
    for b in bars:
        p.add_bar(b)
    p.approximate = any(b.vol_at is None for b in bars)
    return p
