"""Depth-aware execution (paper and backtest only): maker-first entries, size capped by visible depth, stops placed away from obvious clusters, and a cost comparison.

It sits BESIDE the one risk gate (risk.py): the order it produces still has to pass `risk.evaluate`/sizing (cross-margin liquidation check, stop before liquidation,
size from the owner's own balance, balance in use means paper). Prices and sizes are Decimal. Books are [[price, size], ...] best first.

What this module will never do: place an order meant to move price, or one the strategy intends to cancel before it fills (no spoofing, layering or wash trading).
A resting limit exists only because the signal wants that fill; it is repriced or removed only because the signal became invalid or the window ended.
The live engine is NOT connected to this module: paper and backtest use it; going live needs a separate owner approval."""
from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal

D = Decimal
Side = Literal["long", "short"]
Book = Sequence[Sequence[Any]]
GST = D("0.18")


def _d(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else D(str(x))


def walk_book(levels: Book, qty: Decimal) -> Decimal | None:
    """Average fill price for `qty` contracts through the visible levels; None when the visible depth cannot fill it (the order is then not assumed to fill)."""
    need, cost = qty, D(0)
    for price, size in levels:
        take = min(need, _d(size))
        cost += take * _d(price)
        need -= take
        if need <= 0:
            return cost / qty
    return None


# ---------------- 2. size versus depth ----------------
@dataclass(frozen=True)
class SizeDecision:
    requested: int
    allowed: int
    cut: bool
    visible_depth: Decimal
    share: Decimal
    note: str


def cap_size(side: Side, bids: Book, asks: Book, requested: int, *, share: Decimal = D("0.10"), top_levels: int = 5, within_bps: Decimal | None = None) -> SizeDecision:
    """Cap an order at `share` of the visible depth it would trade against (the asks for a long, the bids for a short): default 10% of the top 5 levels,
    or of the depth within `within_bps` of mid when given. Logs (returns) when the cap cuts the size; an unknown book allows nothing."""
    book = asks if side == "long" else bids
    if not book or not bids or not asks:
        return SizeDecision(requested, 0, True, D(0), share, "no visible book: size 0")
    if within_bps is not None:
        mid = (_d(bids[0][0]) + _d(asks[0][0])) / 2
        lim = mid * within_bps / D(10000)
        depth = sum((_d(s) for p, s in book if abs(_d(p) - mid) <= lim), D(0))
        basis = f"depth within {within_bps} bps of mid"
    else:
        depth = sum((_d(s) for _, s in list(book)[:top_levels]), D(0))
        basis = f"top {top_levels} levels"
    allowed = int((depth * share).to_integral_value(rounding=ROUND_FLOOR))
    allowed = min(requested, allowed)
    cut = allowed < requested
    return SizeDecision(requested, allowed, cut, depth, share, (f"size cut {requested} -> {allowed}: {share:.0%} of {basis} ({depth})" if cut else f"size {requested} within {share:.0%} of {basis}"))


# ---------------- 3. stop placement ----------------
@dataclass(frozen=True)
class StopPlacement:
    stop: Decimal
    base_stop: Decimal
    moved_for: list[str]
    distance_from_extreme: Decimal            # how far past the swing extreme the stop sits (recorded in the trade row)
    round_number_clearance: Decimal | None
    wall_price: Decimal | None


def round_step(price: Decimal) -> Decimal:
    """The 'obvious' round-number grid for a price: 1 for prices under 10, 10 under 100, 100 under 10,000, 1,000 above (a coarse human-visible grid)."""
    if price < 10:
        return D("0.5")
    if price < 100:
        return D(1)
    if price < 1000:
        return D(10)
    if price < 20000:
        return D(100)
    return D(500)


def liquidity_wall(levels: Book, n: int = 20, factor: Decimal = D(3)) -> Decimal | None:
    """Price of the biggest resting level among the first n, if it is at least `factor` x the median level size (a visible wall where stops tend to cluster)."""
    rows = [(_d(p), _d(s)) for p, s in list(levels)[:n]]
    if len(rows) < 5:
        return None
    med = D(str(statistics.median(float(s) for _, s in rows)))
    price, size = max(rows, key=lambda r: r[1])
    return price if med > 0 and size >= factor * med else None


def place_stop(side: Side, swing_extreme: Decimal, atr: Decimal, tick: Decimal, *, buffer_atr: Decimal = D("0.25"), book_beyond: Book | None = None, round_tolerance_ticks: int = 20, wall_buffer_atr: Decimal = D("0.15")) -> StopPlacement:
    """Stop beyond the swing extreme plus an ATR buffer, then kept off round numbers and off the far side of a visible liquidity wall (placed PAST it with a buffer).
    `book_beyond` is the side of the book the stop sits in (bids for a long's stop, asks for a short's). The result is always at least as protective-distance as the base stop."""
    sign = D(-1) if side == "long" else D(1)                       # a long's stop is below, a short's above
    base = swing_extreme + sign * buffer_atr * atr
    stop, notes = base, []
    wall = liquidity_wall(book_beyond, 20) if book_beyond else None
    if wall is not None and ((side == "long" and stop >= wall - tick * 3 and stop <= wall + tick * 3) or (side == "short" and stop <= wall + tick * 3 and stop >= wall - tick * 3)):
        stop = wall + sign * wall_buffer_atr * atr
        notes.append(f"moved past the liquidity wall at {wall}")
    step = round_step(stop)
    nearest = (stop / step).to_integral_value() * step
    if abs(stop - nearest) <= tick * round_tolerance_ticks and abs(stop - nearest) <= step / 4:
        stop = nearest + sign * max(tick * round_tolerance_ticks, wall_buffer_atr * atr)
        notes.append(f"moved off the round number {nearest}")
    stop = (stop / tick).to_integral_value(rounding=ROUND_FLOOR if side == "long" else ROUND_CEILING) * tick     # round AWAY from price: never tighter than intended
    clearance = abs(stop - ((stop / round_step(stop)).to_integral_value() * round_step(stop)))
    return StopPlacement(stop, base, notes, abs(stop - swing_extreme), clearance, wall)


# ---------------- 1. maker first ----------------
@dataclass
class BookStep:
    """One look at the market at time t: the book, plus the trade prints since the previous step ((price, aggressor 'buy'|'sell'))."""
    t: float
    bids: Book
    asks: Book
    prints: Sequence[tuple[Decimal, str]] = ()


@dataclass
class EntryOutcome:
    result: Literal["maker_filled", "taker_filled", "missed"]
    price: Decimal | None
    reprices: int
    reason: str
    expected_taker_cost_bps: Decimal | None
    log: list[str] = field(default_factory=list)


def taker_cost_bps(side: Side, bids: Book, asks: Book, qty: Decimal) -> Decimal | None:
    """Expected cost of a market order now: (average walked price - mid) / mid in bps, i.e. half-spread plus impact at this size. Fees are added separately."""
    if not bids or not asks:
        return None
    mid = (_d(bids[0][0]) + _d(asks[0][0])) / 2
    avg = walk_book(asks if side == "long" else bids, qty)
    if avg is None or mid <= 0:
        return None
    return ((avg - mid) if side == "long" else (mid - avg)) / mid * D(10000)


def maker_first_entry(side: Side, qty: Decimal, steps: Iterable[BookStep], signal_valid: Callable[[float], bool], *, max_reprices: int = 3, window_s: float = 30.0,
                      max_taker_cost_bps: Decimal = D(3)) -> EntryOutcome:
    """Post-only limit at the best bid (long) / best ask (short) and let it rest; reprice up to `max_reprices` times inside `window_s` seconds while the signal is
    still valid. A resting limit fills only when the market trades THROUGH it (an opposite-side print at or beyond the limit); touching is not a fill.
    When the window ends unfilled and the signal is still valid, take liquidity only if (spread + impact at this size + the taker fee) is under `max_taker_cost_bps`;
    otherwise the trade is skipped as a missed fill. Every choice is logged."""
    log: list[str] = []
    limit: Decimal | None = None
    reprices, start = 0, None
    last: BookStep | None = None
    for step in steps:
        start = step.t if start is None else start
        last = step
        if not signal_valid(step.t):
            return EntryOutcome("missed", None, reprices, "signal no longer valid: resting order removed", None, log + [f"t={step.t}: signal invalid, stop quoting"])
        if limit is not None:                                       # did the market trade through our resting order since the last look?
            for price, aggressor in step.prints:
                if (side == "long" and aggressor == "sell" and price <= limit) or (side == "short" and aggressor == "buy" and price >= limit):
                    log.append(f"t={step.t}: filled as maker at {limit}")
                    return EntryOutcome("maker_filled", limit, reprices, "filled as maker", None, log)
        if not step.bids or not step.asks:
            continue
        best = _d(step.bids[0][0]) if side == "long" else _d(step.asks[0][0])
        if step.t - start >= window_s:
            break
        if limit is None:
            limit = best
            log.append(f"t={step.t}: post-only {side} limit at {limit}")
        elif best != limit and reprices < max_reprices:
            limit, reprices = best, reprices + 1
            log.append(f"t={step.t}: repriced to {limit} ({reprices}/{max_reprices})")
    if last is None or not signal_valid(last.t):
        return EntryOutcome("missed", None, reprices, "no valid signal at the end of the window", None, log)
    cost = taker_cost_bps(side, last.bids, last.asks, qty)
    if cost is None:
        return EntryOutcome("missed", None, reprices, "missed fill: the visible book cannot fill this size", None, log + ["no taker fallback: book too thin"])
    if cost <= max_taker_cost_bps:                                   # spread + impact at this size; the fee is the same for every taker and is charged on top
        avg = walk_book(last.asks if side == "long" else last.bids, qty)
        log.append(f"taker fallback: expected cost {cost:.2f} bps <= limit {max_taker_cost_bps} bps")
        return EntryOutcome("taker_filled", avg, reprices, "taker fallback within the cost limit", cost, log)
    return EntryOutcome("missed", None, reprices, f"missed fill: taker cost {cost:.2f} bps over the {max_taker_cost_bps} bps limit", cost, log)


# ---------------- 5. measurement ----------------
def entry_cost_bps(kind: Literal["maker", "taker"], side: Side, bids: Book, asks: Book, qty: Decimal, *, maker_fee: Decimal = D("0.0002"), taker_fee: Decimal = D("0.0005")) -> Decimal | None:
    """Expected one-side entry cost in bps of notional: fee + 18% GST on the fee, plus (for a taker) half-spread and impact. A maker pays the fee only, and is assumed filled."""
    fee = (maker_fee if kind == "maker" else taker_fee) * D(10000) * (1 + GST)
    if kind == "maker":
        return fee
    c = taker_cost_bps(side, bids, asks, qty)
    return None if c is None else fee + c


def summarise_savings(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """events: {'saving_bps': Decimal|None (taker cost minus maker-first cost where it filled), 'missed': bool, 'missed_net_bps': Decimal|None (what the skipped trade earned)}.
    Returns the median and 90th percentile saving, the missed-fill rate, and what the missed trades would have earned."""
    savings = sorted(float(e["saving_bps"]) for e in events if e.get("saving_bps") is not None and not e.get("missed"))
    missed = [e for e in events if e.get("missed")]
    p90 = savings[min(len(savings) - 1, max(0, math.ceil(0.9 * len(savings)) - 1))] if savings else None
    earned = [float(e["missed_net_bps"]) for e in missed if e.get("missed_net_bps") is not None]
    return {"signals": len(events), "filled": len(savings), "median_saving_bps": statistics.median(savings) if savings else None, "p90_saving_bps": p90,
            "missed_fill_rate": (len(missed) / len(events)) if events else None, "missed_trades_mean_net_bps": (sum(earned) / len(earned)) if earned else None, "missed_trades_counted": len(earned)}
