"""Order-flow engine: trade classification, per-bar delta/CVD, feed validation, divergence, absorption.

Never fabricates aggression: candle volume alone yields ``flow=None`` (unavailable), not zero and not an estimate.
Classification hierarchy: provider aggressor -> at/above fresh ask (buy) -> at/below fresh bid (sell) ->
tick rule (inside spread or no fresh quote) -> unclassified. A stale quote is never used to classify.
Inferring direction from depth quantities alone is not done.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from .config import DeltaCfg
from .profile import ZERO, VolumeProfile
from .vtypes import BAR, Bar, BarFlow, DataLevel, DeltaQuality, Quote, Side, Trade


@dataclass(frozen=True)
class Classified:
    trade: Trade
    direction: Side | None
    method: DeltaQuality
    quote_age_ms: int | None
    confidence: float


def classify_trade(trade: Trade, quote: Quote | None, prev_price: Decimal | None, prev_dir: Side | None,
                   cfg: DeltaCfg) -> Classified:
    if trade.aggressor is not None:
        return Classified(trade, trade.aggressor, DeltaQuality.EXCHANGE_CONFIRMED, None, 1.0)
    fresh = None
    age_ms = None
    if quote is not None:
        age_ms = int((trade.ts - quote.ts).total_seconds() * 1000)
        if 0 <= age_ms <= cfg.quote_max_age_ms and quote.bid > 0 and quote.ask >= quote.bid:
            fresh = quote
    if fresh is not None:
        if trade.price >= fresh.ask:
            return Classified(trade, Side.BUY, DeltaQuality.QUOTE_CLASSIFIED, age_ms, 0.9)
        if trade.price <= fresh.bid:
            return Classified(trade, Side.SELL, DeltaQuality.QUOTE_CLASSIFIED, age_ms, 0.9)
    if cfg.tick_rule_enabled and prev_price is not None:
        if trade.price > prev_price:
            return Classified(trade, Side.BUY, DeltaQuality.TICK_ESTIMATED, age_ms, 0.6)
        if trade.price < prev_price:
            return Classified(trade, Side.SELL, DeltaQuality.TICK_ESTIMATED, age_ms, 0.6)
        if prev_dir is not None:
            return Classified(trade, prev_dir, DeltaQuality.TICK_ESTIMATED, age_ms, 0.5)
    return Classified(trade, None, DeltaQuality.UNCLASSIFIED, age_ms, 0.0)


@dataclass
class FlowReport:
    bars_flow: dict[datetime, BarFlow]
    vol_at: dict[datetime, dict[int, Decimal]]
    duplicates: int = 0
    out_of_order: int = 0
    classified: list[Classified] = field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        tot = sum((f.total for f in self.bars_flow.values()), ZERO)
        cls = sum((f.classified for f in self.bars_flow.values()), ZERO)
        return float(cls / tot * 100) if tot > 0 else 0.0


def build_flow(trades: list[Trade], quotes: list[Quote], tick: Decimal, cfg: DeltaCfg) -> FlowReport:
    """Aggregate trades into 5-minute buckets. Dedupes only on provider ``trade_id`` (never on ts/price/qty)."""
    seen: set[str] = set()
    quotes = sorted(quotes, key=lambda q: q.ts)
    qts = [q.ts for q in quotes]
    rep = FlowReport({}, {})
    prev_price: Decimal | None = None
    prev_dir: Side | None = None
    last_ts: datetime | None = None
    acc: dict[datetime, list[Decimal]] = {}
    for t in trades:
        if t.trade_id is not None:
            if t.trade_id in seen:
                rep.duplicates += 1
                continue
            seen.add(t.trade_id)
        if last_ts is not None and t.ts < last_ts:
            rep.out_of_order += 1   # kept for diagnostics; classified in arrival order is unsafe, so left unclassified
            c = Classified(t, None, DeltaQuality.UNCLASSIFIED, None, 0.0)
        else:
            k = bisect_right(qts, t.ts) - 1
            q = quotes[k] if k >= 0 else None
            c = classify_trade(t, q, prev_price, prev_dir, cfg)
            last_ts = t.ts
            prev_price = t.price
            if c.direction is not None:
                prev_dir = c.direction
        rep.classified.append(c)
        mins = t.ts.minute - t.ts.minute % 5
        key = t.ts.replace(minute=mins, second=0, microsecond=0)
        a = acc.setdefault(key, [ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO])  # buy, sell, unclassified, ex, quote, tick, count
        if c.direction is Side.BUY:
            a[0] += t.qty
        elif c.direction is Side.SELL:
            a[1] += t.qty
        else:
            a[2] += t.qty
        idx = {DeltaQuality.EXCHANGE_CONFIRMED: 3, DeltaQuality.QUOTE_CLASSIFIED: 4, DeltaQuality.TICK_ESTIMATED: 5}.get(c.method)
        if idx is not None:
            a[idx] += t.qty
        i = int((t.price / tick).to_integral_value())
        v = rep.vol_at.setdefault(key, {})
        v[i] = v.get(i, ZERO) + t.qty
    for key, a in acc.items():
        q = (DeltaQuality.EXCHANGE_CONFIRMED if a[3] > 0 and a[4] == 0 and a[5] == 0 else
             DeltaQuality.TICK_ESTIMATED if a[5] > 0 else
             DeltaQuality.QUOTE_CLASSIFIED if a[4] > 0 else DeltaQuality.UNCLASSIFIED)
        rep.bars_flow[key] = BarFlow(a[0], a[1], a[2], q)
    return rep


# ---------------------------------------------------------------- feed validation / reconciliation
@dataclass
class FeedObservation:
    ts: datetime
    cum_volume: Decimal
    ltq: Decimal | None
    ltt: datetime | None   # last trade time reported by the provider


@dataclass
class FeedReport:
    observations: int = 0
    exact_increments: int = 0       # delta-volume == LTQ with a new LTT: classifiable last print
    unallocated_volume: Decimal = ZERO
    allocated_volume: Decimal = ZERO
    resets: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)

    @property
    def unallocated_pct(self) -> float:
        t = self.allocated_volume + self.unallocated_volume
        return float(self.unallocated_volume / t * 100) if t > 0 else 0.0

    @property
    def trade_exact(self) -> bool:
        return (self.observations > 1 and self.unallocated_volume == 0 and self.resets == 0
                and self.out_of_order == 0 and not self.gaps)


def validate_feed(obs: list[FeedObservation], *, gap_seconds: int = 60) -> FeedReport:
    """dV = cum_t - cum_{t-1}. dV == LTQ with a new LTT -> allocatable to the last price. dV > LTQ -> the excess is
    UNALLOCATED (never assigned to the last price). A backwards counter is a discontinuity, not negative volume."""
    r = FeedReport()
    prev: FeedObservation | None = None
    for o in obs:
        r.observations += 1
        if prev is None:
            prev = o
            continue
        if o.ts < prev.ts:
            r.out_of_order += 1
            continue
        if (o.ts - prev.ts).total_seconds() > gap_seconds:
            r.gaps.append((prev.ts, o.ts))
        dv = o.cum_volume - prev.cum_volume
        if dv < 0:
            r.resets += 1
        elif dv == 0 and o.ltt == prev.ltt:
            r.duplicates += 1
        else:
            ltq = o.ltq if o.ltq is not None else ZERO
            if o.ltt is not None and o.ltt != prev.ltt and ltq > 0 and dv == ltq:
                r.exact_increments += 1
                r.allocated_volume += dv
            else:
                alloc = min(dv, ltq) if (o.ltt is not None and o.ltt != prev.ltt) else ZERO
                r.allocated_volume += alloc
                r.unallocated_volume += dv - alloc
        prev = o
    return r


def reconcile_volume(reconstructed: Decimal, provider_cumulative: Decimal) -> float | None:
    """volume_reconciliation_pct; None when the provider total is unknown (never assume 100)."""
    if provider_cumulative <= 0:
        return None
    return float(reconstructed / provider_cumulative * 100)


# ---------------------------------------------------------------- series
def cvd_series(bars: list[Bar]) -> list[float | None]:
    """Running session CVD; None for bars without order flow (breaks the chain: later values stay None)."""
    out: list[float | None] = []
    run = 0.0
    ok = True
    for b in bars:
        if b.flow is None or not ok:
            ok = False
            out.append(None)
            continue
        run += float(b.flow.delta)
        out.append(run)
    return out


def flow_valid(b: Bar, cfg: DeltaCfg) -> bool:
    return b.flow is not None and b.flow.coverage_pct >= cfg.minimum_classified_volume_coverage


def window_delta_ratio(bars: list[Bar], cfg: DeltaCfg) -> tuple[float | None, str]:
    """Delta ratio over bars; invalid unless every bar carries flow and aggregate coverage passes the threshold."""
    if not bars or any(b.flow is None for b in bars):
        return None, "order flow unavailable"
    buy = sum((b.flow.buy for b in bars), ZERO)      # type: ignore[union-attr]
    sell = sum((b.flow.sell for b in bars), ZERO)    # type: ignore[union-attr]
    unc = sum((b.flow.unclassified for b in bars), ZERO)  # type: ignore[union-attr]
    tot = buy + sell + unc
    cov = float((buy + sell) / tot * 100) if tot > 0 else 0.0
    if cov < cfg.minimum_classified_volume_coverage:
        return None, f"classified coverage {cov:.1f}% < {cfg.minimum_classified_volume_coverage}%"
    return float((buy - sell) / (buy + sell)), "ok"


@dataclass(frozen=True)
class Pivot:
    idx: int            # bar index of the pivot
    confirmed_at: int   # first bar index at which the pivot is observable (idx + right)
    price: float
    cvd: float
    kind: str           # "high" | "low"


def pivots(bars: list[Bar], cvd: list[float | None], upto: int, cfg: DeltaCfg) -> list[Pivot]:
    """Confirmed pivots visible at bar ``upto`` (a pivot needs ``pivot_right`` later bars; no look-ahead)."""
    L, R = cfg.pivot_left, cfg.pivot_right
    out: list[Pivot] = []
    for i in range(L, upto - R + 1):
        c = cvd[i]
        if c is None:
            continue
        hi = float(bars[i].high)
        lo = float(bars[i].low)
        if all(hi > float(bars[j].high) for j in range(i - L, i)) and all(hi >= float(bars[j].high) for j in range(i + 1, i + R + 1)):
            out.append(Pivot(i, i + R, hi, c, "high"))
        if all(lo < float(bars[j].low) for j in range(i - L, i)) and all(lo <= float(bars[j].low) for j in range(i + 1, i + R + 1)):
            out.append(Pivot(i, i + R, lo, c, "low"))
    return out


def divergence(bars: list[Bar], cvd: list[float | None], upto: int, atr: float, cfg: DeltaCfg) -> dict[str, Pivot | None]:
    """Latest bearish (higher price high, lower CVD high) / bullish (lower price low, higher CVD low) divergence
    among pivots confirmed by bar ``upto``. Result carries the later pivot so callers can check its confirmation time."""
    ps = pivots(bars, cvd, upto, cfg)
    res: dict[str, Pivot | None] = {"bearish": None, "bullish": None}
    for kind, key, better in (("high", "bearish", lambda a, b: a.price > b.price and a.cvd < b.cvd - cfg.min_cvd_difference),
                              ("low", "bullish", lambda a, b: a.price < b.price and a.cvd > b.cvd + cfg.min_cvd_difference)):
        ks = [p for p in ps if p.kind == kind]
        for a, b in zip(ks[-2:][1:], ks[-2:][:1]):
            if a.idx - b.idx >= cfg.min_swing_separation_bars and abs(a.price - b.price) >= cfg.min_price_excursion_atr * atr \
                    and better(a, b):
                res[key] = a
    return res


def absorption(bars: list[Bar], upto: int, atr: float, *, percentile: float, window: int, history: int,
               max_progress_atr: float, cfg: DeltaCfg) -> dict[str, object]:
    """Causal absorption candidate at bar ``upto``.

    Bearish candidate: window delta is >= rolling percentile of past window deltas, price progress over the window
    <= max_progress_atr*ATR, AND the latest completed bar then closes below the window's opening reference (rejection).
    Bullish mirrors. High delta alone is never labelled absorption; the price response must confirm."""
    out: dict[str, object] = {"bearish": False, "bullish": False, "confirmed": False, "reason": ""}
    if upto < window + 1 or any(b.flow is None for b in bars[max(0, upto - history):upto + 1]):
        out["reason"] = "order flow unavailable in history window"
        return out
    def wdelta(end: int) -> float:
        return float(sum((bars[k].flow.delta for k in range(end - window + 1, end + 1)), ZERO))  # type: ignore[union-attr]
    # candidate window ends one bar before the response bar
    e = upto - 1
    hist = [wdelta(j) for j in range(max(window - 1, e - history), e)]
    if len(hist) < 10:
        out["reason"] = "insufficient delta history"
        return out
    d = wdelta(e)
    pos = sorted(x for x in hist if x > 0)
    neg = sorted(-x for x in hist if x < 0)
    start = bars[e - window + 1]
    progress = abs(float(bars[e].close - start.open))
    quiet = progress <= max_progress_atr * atr
    def pct(vals: list[float], x: float) -> bool:
        return bool(vals) and x >= vals[min(len(vals) - 1, int(len(vals) * percentile / 100))]
    if d > 0 and pct(pos, d) and quiet and bars[upto].close < start.open:
        out["bearish"] = out["confirmed"] = True
    if d < 0 and pct(neg, -d) and quiet and bars[upto].close > start.open:
        out["bullish"] = out["confirmed"] = True
    return out
