"""VAR strategy engines: Acceptance Breakout (A) and Failed-Auction Reversal (B), evaluated on completed 5m bars.

Causality: ``evaluate(ctx, i)`` reads bars[:i+1] and series values at indices <= i only. Every candidate that reaches a
trigger bar yields either a Signal (all mandatory conditions pass) or a Rejection listing every failed condition.
No score can override a failed mandatory condition. Levels are computed here in Python; no model emits a price."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from . import orderflow as of
from .config import VarConfig
from .charges import round_trip_estimate
from .profile import ProfileStats, VolumeProfile, tick_index
from .structure import BALANCED, BEAR, BULL, Regime, classify_regime, relative_volume, session_close, slot_key
from .tpo import InitialBalance, TPOResult, initial_balance, tpo_profile
from .vtypes import (BAR, IST, Bar, Cond, CondResult, Contract, D, DataLevel, DeltaQuality, Rejection, Signal)


def dec(x: float) -> Decimal:
    return D(f"{x:.8f}")


def snap(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    q = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return q * tick


@dataclass
class Ctx:
    contract: Contract
    cfg: VarConfig
    bars: list[Bar]                         # current-session completed bars (index i = latest completed)
    atr: list[float | None]                 # aligned to bars
    vwap: list[float | None]                # aligned to bars
    cvd: list[float | None]
    dev_stats: list[ProfileStats | None]    # developing profile stats after each bar
    dev_prof: list[VolumeProfile]           # snapshot-free accessor: profile built to bar i is rebuilt lazily (see below)
    prev_prof: VolumeProfile | None
    prev_stats: ProfileStats | None
    comp_prof: VolumeProfile | None
    comp_stats: ProfileStats | None
    rvol_hist: dict[time, list[Decimal]]
    feed_ok: bool = True
    excluded_reason: str | None = None
    allow_unknown_spread: bool = False
    is_expiring_index: bool = False
    _nodes: list | None = None

    def comp_nodes(self) -> list:
        """Composite HVN/LVN nodes; the composite profile is fixed for the session, so compute once."""
        if self._nodes is None:
            ms = self.cfg.market_structure
            self._nodes = [] if self.comp_prof is None else self.comp_prof.nodes(
                smoothing=ms.node_smoothing_bins, hvn_percentile=ms.hvn_percentile, min_prominence=ms.node_min_prominence, lvn_max_ratio=ms.lvn_max_ratio)
        return self._nodes

    @property
    def tick(self) -> Decimal:
        return self.contract.tick_size

    def poc_traj(self) -> list[int | None]:
        return [s.poc if s else None for s in self.dev_stats]


@dataclass
class EngineState:
    fired: set[tuple] = field(default_factory=set)
    stages: set[tuple] = field(default_factory=set)      # unique pattern milestones reached (diagnostics): (engine, stage, side, key...)


def build_ctx(contract: Contract, cfg: VarConfig, bars: list[Bar], warm_bars: list[Bar], prev_session: list[Bar] | None,
              composite_sessions: list[list[Bar]], rvol_hist: dict[time, list[Decimal]], **kw) -> Ctx:
    from .profile import profile_from_bars
    from .structure import atr_series, exact_vwap_series
    allb = warm_bars + bars
    atr_all = atr_series(allb, cfg.market_structure.atr_period)
    atr = atr_all[len(warm_bars):]
    vw = exact_vwap_series(bars, contract.tick_size)
    cv = of.cvd_series(bars)
    ms = cfg.market_structure
    dev = VolumeProfile(contract.tick_size)
    stats: list[ProfileStats | None] = []
    for b in bars:
        dev.add_bar(b)
        stats.append(dev.stats(ms.value_area_pct))
    prev_prof = profile_from_bars(prev_session, contract.tick_size) if prev_session else None
    comp = None
    if composite_sessions:
        comp = VolumeProfile(contract.tick_size)
        for s in composite_sessions[-ms.composite_sessions:]:
            comp = comp.merged(profile_from_bars(s, contract.tick_size))
    return Ctx(contract, cfg, bars, atr, vw, cv, stats, [], prev_prof, prev_prof.stats(ms.value_area_pct) if prev_prof else None,
               comp, comp.stats(ms.value_area_pct) if comp else None, rvol_hist, **kw)


def developing_profile(ctx: Ctx, i: int) -> VolumeProfile:
    p = VolumeProfile(ctx.tick)
    for b in ctx.bars[: i + 1]:
        p.add_bar(b)
    return p


def volume_beyond(bar: Bar, level: Decimal, side: int, tick: Decimal) -> Decimal:
    """Traded volume beyond ``level`` (above for side=+1, below for side=-1). Exact with vol_at, else uniform over the range."""
    li = tick_index(level, tick)
    if bar.vol_at is not None:
        return sum((v for k, v in bar.vol_at.items() if (k > li if side > 0 else k < li)), Decimal(0))
    lo, hi = tick_index(bar.low, tick), tick_index(bar.high, tick)
    n = hi - lo + 1
    beyond = (max(0, hi - li) if side > 0 else max(0, li - lo))
    beyond = min(beyond, n)
    return bar.volume * beyond / n


def _cond(name: str, ok: bool | None, detail: str = "", *, value: object | None = None, dq: bool = False) -> CondResult:
    if ok is None:
        return CondResult(name, Cond.INVALID_DATA if dq else Cond.UNAVAILABLE, detail, value)
    return CondResult(name, Cond.PASS if ok else Cond.FAIL, detail, value)


# ---------------------------------------------------------------- universal filters
def universal_filters(ctx: Ctx, i: int, strategy_min_rr: float) -> list[CondResult]:
    cfg, b = ctx.cfg, ctx.bars[i]
    day = b.ts.astimezone(IST).date()
    close = session_close(day, is_expiring_index_derivative=ctx.is_expiring_index)
    h, m = (int(x) for x in cfg.session.earliest_entry.split(":"))
    earliest = datetime.combine(day, time(h, m), tzinfo=IST)
    latest = close - timedelta(minutes=cfg.session.no_entry_minutes_before_close)
    out = [
        _cond("entry_window", earliest <= b.end <= latest, f"trigger bar closes {b.end.astimezone(IST):%H:%M}; window {earliest:%H:%M}-{latest:%H:%M} IST"),
        _cond("feed_quality", ctx.feed_ok, "feed gaps / staleness flagged" if not ctx.feed_ok else "ok"),
    ]
    contiguous = all(ctx.bars[k].ts + BAR == ctx.bars[k + 1].ts for k in range(max(0, i - 8), i))
    out.append(_cond("bars_contiguous", contiguous, "missing 5m bar in the recent window" if not contiguous else "ok"))
    if ctx.excluded_reason:
        out.append(_cond("instrument_eligible", False, ctx.excluded_reason))
    sp = b.spread_ticks
    if sp is None:
        out.append(CondResult("spread", Cond.PASS if ctx.allow_unknown_spread else Cond.UNAVAILABLE,
                              "spread unknown (candle data); research mode only" if ctx.allow_unknown_spread else "spread unavailable"))
    else:
        out.append(_cond("spread", sp <= 3.0, f"spread {sp:.1f} ticks (normal <=2, reject >3)"))
    return out


def _tpo_status(ctx: Ctx, i: int, side: int, kind: str) -> tuple[str, bool | None]:
    bars = ctx.bars[: i + 1]
    t_now = tpo_profile(bars, ctx.tick, bracket_minutes=ctx.cfg.market_structure.tpo_bracket_minutes, pct=ctx.cfg.market_structure.value_area_pct)
    if t_now.poc is None:
        return "unavailable", None
    if kind == "breakout":
        j = max(0, i - 6)
        t_prev = tpo_profile(ctx.bars[: j + 1], ctx.tick, bracket_minutes=ctx.cfg.market_structure.tpo_bracket_minutes, pct=ctx.cfg.market_structure.value_area_pct)
        if t_prev.poc is None:
            return "unavailable", None
        moved = (t_now.poc - t_prev.poc) * side
        return ("supportive" if moved > 0 else "neutral" if moved == 0 else "adverse"), moved >= 0
    # reversal: little time spent beyond the old value edge
    if ctx.prev_stats is None:
        return "unavailable", None
    edge = ctx.prev_stats.vah if side < 0 else ctx.prev_stats.val
    beyond = [c for r, c in t_now.counts.items() if (r > edge if side < 0 else r < edge)]
    mx = max(beyond) if beyond else 0
    return ("supportive" if mx <= 2 else "adverse"), mx <= 2


# ---------------------------------------------------------------- targets
def _targets(ctx: Ctx, i: int, side: int, entry: Decimal, kinds: tuple[str, ...]) -> list[tuple[Decimal, str]]:
    t = ctx.tick
    out: list[tuple[Decimal, str]] = []
    dp = ctx.dev_stats[i]
    def add(idx: int | None, prof: VolumeProfile | None, label: str) -> None:
        if idx is None or prof is None:
            return
        p = prof.price(idx)
        if (p - entry) * side > t:
            out.append((p, label))
    if "dev_poc" in kinds and dp is not None:
        add(dp.poc, VolumeProfile(t), "developing POC")
    if "prev_poc" in kinds and ctx.prev_stats is not None:
        add(ctx.prev_stats.poc, ctx.prev_prof, "previous POC")
    if "comp_poc" in kinds and ctx.comp_stats is not None:
        add(ctx.comp_stats.poc, ctx.comp_prof, "composite POC")
    if "prev_edge" in kinds and ctx.prev_stats is not None and ctx.prev_prof is not None:
        add(ctx.prev_stats.vah if side > 0 else ctx.prev_stats.val, ctx.prev_prof, "previous VAH" if side > 0 else "previous VAL")
    if "comp_edge" in kinds and ctx.comp_stats is not None and ctx.comp_prof is not None:
        add(ctx.comp_stats.vah if side > 0 else ctx.comp_stats.val, ctx.comp_prof, "composite VAH" if side > 0 else "composite VAL")
    if "hvn" in kinds and ctx.comp_prof is not None:
        for n in ctx.comp_nodes():
            if n.kind == "HVN":
                add(n.peak_idx, ctx.comp_prof, "composite HVN")
    if "vwap" in kinds and ctx.vwap[i] is not None:
        v = snap(dec(ctx.vwap[i]), t, up=side < 0)
        if (v - entry) * side > t:
            out.append((v, "session VWAP"))
    # dev POC needs the actual developing profile prices: convert from tick index directly
    return sorted({(p, l) for p, l in out}, key=lambda x: (x[0] - entry) * side)


def _dev_poc_price(ctx: Ctx, i: int) -> Decimal | None:
    s = ctx.dev_stats[i]
    return None if s is None else ctx.tick * s.poc


def _collect_targets(ctx: Ctx, i: int, side: int, entry: Decimal, kinds: tuple[str, ...]) -> list[tuple[Decimal, str]]:
    t = ctx.tick
    out = [x for x in _targets(ctx, i, side, entry, tuple(k for k in kinds if k != "dev_poc"))]
    if "dev_poc" in kinds:
        p = _dev_poc_price(ctx, i)
        if p is not None and (p - entry) * side > t:
            out.append((p, "developing POC"))
    return sorted(set(out), key=lambda x: (x[0] - entry) * side)


# ---------------------------------------------------------------- shared candidate assembly
def _finish(ctx: Ctx, i: int, state: EngineState, key: tuple, setup: str, side: int, ref: Decimal, ref_src: str,
            entry: Decimal, stop: Decimal, conds: list[CondResult], tgt_kinds: tuple[str, ...], min_rr: float,
            regime: Regime, derivation: dict, delta_quality: str) -> tuple[Signal | None, Rejection | None]:
    cfg, c = ctx.cfg, ctx.contract
    t_ = ctx.tick
    risk = (entry - stop) * side
    atr = ctx.atr[i] or 0.0
    reasons: list[str] = []
    if risk <= 0:
        conds.append(_cond("stop_valid", False, "stop is not beyond entry"))
    else:
        maxstop = cfg.breakout.max_stop_atr if setup == "ACCEPTANCE_BREAKOUT" else cfg.reversal.max_stop_atr
        conds.append(_cond("stop_size", float(risk) <= maxstop * atr, f"stop distance {risk} vs {maxstop}*ATR={maxstop * atr:.2f}"))
    tg = _collect_targets(ctx, i, side, entry, tgt_kinds) if risk > 0 else []
    cap_r = D(cfg.exits.baseline_max_target_r)
    cap_price = snap(entry + side * risk * cap_r, t_, up=side < 0) if risk > 0 else entry   # toward entry so it stays reachable
    warnings: list[str] = []
    if risk > 0:
        if tg:
            first_p, first_l = tg[0]
            rr1 = float((first_p - entry) * side / risk)
            conds.append(_cond("first_target_rr", rr1 >= min_rr, f"nearest structural target {first_l} {first_p} = {rr1:.2f}R (min {min_rr}R)", value=rr1))
            final = first_p if (first_p - entry) * side <= (cap_price - entry) * side else cap_price
            targets = [final] + [p for p, _ in tg[1:] if (p - entry) * side > (final - entry) * side]
            derivation["first_target"] = f"{first_l} @ {first_p}"
        else:
            warnings.append("no structural target beyond entry; fell back to the capped R target")
            final = cap_price
            targets = [final]
            conds.append(_cond("first_target_rr", cfg.exits.baseline_max_target_r >= min_rr, f"fallback {cfg.exits.baseline_max_target_r}R"))
        final_rr = float((final - entry) * side / risk)
        # Economic filter. G = gross target reward in rupees; C = estimated round-trip charges (entry order + one exit order) from the single
        # authoritative calculator. net = G - C. Pass iff net / C >= k  (k = risk.min_net_reward_to_cost), i.e. G >= (1 + k) * C.
        # This is neither the gross R:R (reward/risk before charges) nor the net expected reward (which would also weigh win probability).
        units = Decimal(c.lot_size)
        est = round_trip_estimate(c, cfg.charges, ctx.bars[i].ts.astimezone(IST).date(), "LONG" if side > 0 else "SHORT", entry, final, units)
        gross = (final - entry) * side * units
        chg_total = est["total"]
        net = gross - chg_total
        conds.append(_cond("net_reward_after_costs", chg_total > 0 and net > 0 and net / chg_total >= D(cfg.risk.min_net_reward_to_cost),
                           f"G {gross:.2f} - C {chg_total:.2f} = net {net:.2f}; net/C = {float(net / chg_total):.2f} (need >= {cfg.risk.min_net_reward_to_cost}); "
                           f"unverified rate components: {','.join(est['unverified_components']) or 'none'}"))
        if est["unverified_components"]:
            warnings.append("unverified charge components: " + ", ".join(est["unverified_components"]))
    else:
        final = entry
        targets = []
        final_rr = 0.0
    key_fired = key in state.fired
    conds.append(_cond("not_duplicate", not key_fired, "setup already signalled" if key_fired else "first trigger for this setup"))
    conds.extend(universal_filters(ctx, i, min_rr))
    bad = [x for x in conds if not x.ok()]
    b = ctx.bars[i]
    direction = "LONG" if side > 0 else "SHORT"
    if bad:
        return None, Rejection(setup, c.symbol, direction, b.end, [f"{x.name}: {x.state.value} - {x.detail}" for x in bad], conds)
    state.fired.add(key)
    dl = ctx.bars[i].level.value
    return Signal("VAR_V" + cfg.version, setup, c.symbol, direction, b.end, regime.name, ref, ref_src, entry, stop, targets, final_rr,
                  delta_quality, dl, conds, warnings, derivation, expires_at=b.end + BAR,
                  signal_quality="VALID" if b.level is not DataLevel.C_CANDLE else "VALID_APPROXIMATE_DATA"), None


def _flow_quality(bars: list[Bar]) -> str:
    qs = {b.flow.quality for b in bars if b.flow is not None}
    if not qs or any(b.flow is None for b in bars):
        return DeltaQuality.UNAVAILABLE.value
    order = [DeltaQuality.UNCLASSIFIED, DeltaQuality.TICK_ESTIMATED, DeltaQuality.QUOTE_CLASSIFIED, DeltaQuality.EXCHANGE_CONFIRMED]
    return min(qs, key=order.index).value


# ---------------------------------------------------------------- Engine A: acceptance breakout
def _breakout_levels(ctx: Ctx, i: int, side: int) -> list[tuple[Decimal, str]]:
    t, out = ctx.tick, []
    ps, cs = ctx.prev_stats, ctx.comp_stats
    if ps and ctx.prev_prof:
        out.append((ctx.prev_prof.price(ps.vah if side > 0 else ps.val), "previous VAH" if side > 0 else "previous VAL"))
    if cs and ctx.comp_prof:
        out.append((ctx.comp_prof.price(cs.vah if side > 0 else cs.val), "composite VAH" if side > 0 else "composite VAL"))
        for n in ctx.comp_nodes():
            if n.kind == "LVN":
                out.append((ctx.comp_prof.price(n.high_idx if side > 0 else n.low_idx), "composite LVN boundary"))
    ib = initial_balance(ctx.bars[: i + 1], ctx.bars[i].end, minutes=ctx.cfg.market_structure.initial_balance_minutes)
    if ib.complete:
        out.append(((ib.high if side > 0 else ib.low), "Initial Balance high" if side > 0 else "Initial Balance low"))  # type: ignore[arg-type]
    seen, res = set(), []
    for p, s in out:
        if p is not None and (p, s) not in seen:
            seen.add((p, s)); res.append((p, s))
    return res


def eval_breakout(ctx: Ctx, i: int, state: EngineState, regime: Regime) -> tuple[list[Signal], list[Rejection]]:
    cfg, bc, t = ctx.cfg, ctx.cfg.breakout, ctx.tick
    sigs: list[Signal] = []
    rejs: list[Rejection] = []
    A, R = bc.acceptance_bars, bc.maximum_retest_bars
    atr = ctx.atr[i]
    if atr is None or atr <= 0:
        return sigs, rejs
    ib_end = ctx.bars[0].ts.replace(hour=9, minute=15) + timedelta(minutes=cfg.market_structure.initial_balance_minutes)
    for side in (1, -1):
        for ref, src in _breakout_levels(ctx, i, side):
            accepted = lambda b: (b.close >= ref + t) if side > 0 else (b.close <= ref - t)
            for j in range(i - A - R + 1, i - A + 1):
                if j < 0 or j + A - 1 >= i:
                    continue
                if not all(accepted(ctx.bars[j + k]) for k in range(A)):
                    continue
                if j > 0 and accepted(ctx.bars[j - 1]):
                    continue  # not the first acceptance
                if src.startswith("Initial Balance") and ctx.bars[j + A - 1].end < ib_end:
                    continue
                post = ctx.bars[j + A: i + 1]
                if not post:
                    continue
                if any((b.close < ref) if side > 0 else (b.close > ref) for b in post):
                    continue  # acceptance back through the level -> setup dead
                key = ("BO", side, ref, src, j)
                state.stages.add(("BO", "acceptance", side, ref, j))
                trig = ctx.bars[i]
                bullish = trig.close > trig.open if side > 0 else trig.close < trig.open
                beyond = trig.close > ref if side > 0 else trig.close < ref
                dist = lambda b: (b.low - ref) if side > 0 else (ref - b.high)
                retest_idx = [k for k in range(j + A, i + 1) if dist(ctx.bars[k]) <= dec(bc.retest_atr_distance * atr)]
                if not (bullish and beyond and retest_idx):
                    continue   # no trigger yet (pending); not a rejection
                state.stages.add(("BO", "trigger", side, ref, j))
                conds: list[CondResult] = []
                win = ctx.bars[j: j + A]
                tot = sum((b.volume for b in win), Decimal(0))
                out_v = sum((volume_beyond(b, ref, side, t) for b in win), Decimal(0))
                pct = float(out_v / tot * 100) if tot > 0 else 0.0
                conds.append(_cond("acceptance_closes", True, f"{A} consecutive closes beyond {src} {ref}"))
                conds.append(_cond("acceptance_volume", pct >= bc.minimum_outside_volume_pct,
                                   f"{pct:.1f}% of confirmation volume beyond level (min {bc.minimum_outside_volume_pct}%)", value=pct))
                want = BULL if side > 0 else BEAR
                conds.append(_cond("regime", regime.name == want, f"{regime.name}: " + "; ".join(regime.reasons)))
                vw = ctx.vwap[i]
                conds.append(_cond("vwap_side", vw is not None and ((float(trig.close) > vw) if side > 0 else (float(trig.close) < vw)),
                                   f"close {trig.close} vs VWAP {vw:.2f}" if vw is not None else "VWAP unavailable"))
                slope = regime.slope
                conds.append(_cond("vwap_slope", slope is not None and ((slope >= cfg.regime.slope_threshold) if side > 0 else (slope <= -cfg.regime.slope_threshold)),
                                   f"slope {slope:.3f} ATR" if slope is not None else "slope unavailable"))
                rv = [relative_volume(b, ctx.rvol_hist, bc.rvol_sessions, bc.rvol_min_sessions) for b in win]
                if any(x is None for x in rv):
                    conds.append(_cond("relative_volume", None, f"fewer than {bc.rvol_sessions} prior sessions for the time slot", dq=True))
                else:
                    m = sum(rv) / len(rv)  # type: ignore[arg-type]
                    conds.append(_cond("relative_volume", m >= bc.relative_volume_minimum, f"mean relative volume {m:.2f} (min {bc.relative_volume_minimum})", value=m))
                conds.append(_cond("retest", True, f"retest within {bc.retest_atr_distance}*ATR of {ref}; trigger candle closes beyond level"))
                dq = DeltaQuality.UNAVAILABLE.value
                if cfg.uses_orderflow:
                    dr, why = of.window_delta_ratio(win, cfg.delta)
                    dq = _flow_quality(ctx.bars[j: i + 1])
                    if dr is None:
                        conds.append(_cond("delta_ratio", None, why, dq=True))
                    else:
                        conds.append(_cond("delta_ratio", (dr >= bc.delta_ratio_minimum) if side > 0 else (dr <= -bc.delta_ratio_minimum),
                                           f"breakout-window delta ratio {dr:+.3f} (need {'>=' if side > 0 else '<='} {side * bc.delta_ratio_minimum:+.2f})", value=dr))
                    c0, c1 = (ctx.cvd[j - 1] if j > 0 else 0.0), ctx.cvd[i]
                    conds.append(_cond("cvd_direction", None if (c0 is None or c1 is None) else ((c1 > c0) if side > 0 else (c1 < c0)),
                                       f"CVD {c0} -> {c1}", dq=True))
                    td = trig.flow.delta if trig.flow else None
                    conds.append(_cond("trigger_delta", None if td is None else ((td > 0) if side > 0 else (td < 0)),
                                       f"trigger-candle delta {td}", dq=True))
                    div = of.divergence(ctx.bars, ctx.cvd, i, atr, cfg.delta)
                    adverse = div["bearish" if side > 0 else "bullish"]
                    conds.append(_cond("no_adverse_divergence", adverse is None, "adverse price/CVD divergence present" if adverse else "none"))
                    if cfg.uses_absorption:
                        ab = of.absorption(ctx.bars, i, atr, percentile=cfg.reversal.absorption_percentile, window=cfg.reversal.absorption_window_bars,
                                           history=cfg.reversal.absorption_history_bars, max_progress_atr=cfg.reversal.absorption_max_progress_atr, cfg=cfg.delta)
                        adv = ab["bearish" if side > 0 else "bullish"]
                        conds.append(_cond("no_adverse_absorption", not adv, "adverse absorption present" if adv else "none"))
                if cfg.uses_tpo:
                    label, ok = _tpo_status(ctx, i, side, "breakout")
                    conds.append(_cond("tpo_support", ok, f"TPO POC migration: {label}"))
                entry = trig.high + t if side > 0 else trig.low - t
                sw = ctx.bars[retest_idx[0]: i + 1]
                swing = min(b.low for b in sw) if side > 0 else max(b.high for b in sw)
                buf = max(2 * t, dec(bc.stop_buffer_atr * atr))
                stop = snap(swing - buf, t, up=False) if side > 0 else snap(swing + buf, t, up=True)
                deriv = {"reference": f"{src} {ref}", "acceptance_window": f"bars {j}..{j + A - 1}", "retest_swing": str(swing),
                         "stop_rule": f"retest swing {'low' if side > 0 else 'high'} {'-' if side > 0 else '+'} max(2*tick, {bc.stop_buffer_atr}*ATR)",
                         "atr_5m": round(atr, 4), "entry_rule": f"{'high' if side > 0 else 'low'} of trigger candle {'+' if side > 0 else '-'} 1 tick"}
                s, r = _finish(ctx, i, state, key, "ACCEPTANCE_BREAKOUT", side, ref, src, entry, stop, conds,
                               ("dev_poc", "prev_poc", "comp_poc", "hvn", "prev_edge", "comp_edge"), bc.minimum_rr, regime, deriv, dq)
                if s: sigs.append(s)
                if r: rejs.append(r)
    return sigs, rejs


# ---------------------------------------------------------------- Engine B: failed auction reversal
def eval_reversal(ctx: Ctx, i: int, state: EngineState, regime: Regime) -> tuple[list[Signal], list[Rejection]]:
    cfg, rc, t = ctx.cfg, ctx.cfg.reversal, ctx.tick
    sigs: list[Signal] = []
    rejs: list[Rejection] = []
    atr = ctx.atr[i]
    if atr is None or atr <= 0 or ctx.prev_stats is None or ctx.prev_prof is None:
        return sigs, rejs
    for side in (-1, 1):   # -1: short at VAH ; +1: long at VAL
        ref = ctx.prev_prof.price(ctx.prev_stats.vah if side < 0 else ctx.prev_stats.val)
        src = "previous VAH" if side < 0 else "previous VAL"
        # probe bars p: traded beyond ref by at least min sweep; look back over the retest window
        for p in range(max(0, i - 2 * rc.maximum_reentry_bars - 1), i):
            a_p = ctx.atr[p] or atr
            beyond = lambda b: ((b.high - ref) if side < 0 else (ref - b.low))
            if beyond(ctx.bars[p]) < max(t, dec(rc.minimum_sweep_atr * a_p)):
                continue
            if p > 0 and beyond(ctx.bars[p - 1]) >= max(t, dec(rc.minimum_sweep_atr * a_p)):
                continue   # first probe bar only
            state.stages.add(("REV", "probe", side, p))
            f = next((k for k in range(p, min(p + rc.maximum_reentry_bars, i - 1) + 1)
                      if ((ctx.bars[k].close < ref) if side < 0 else (ctx.bars[k].close > ref))), None)
            if f is None or f >= i:
                continue
            state.stages.add(("REV", "failure", side, p))
            probe = ctx.bars[p: f + 1]
            sweep = max(b.high for b in probe) if side < 0 else min(b.low for b in probe)
            ext = (sweep - ref) * (1 if side < 0 else -1)
            trig = ctx.bars[i]
            mid = ctx.bars[f + 1: i]
            if any(((b.close > ref) if side < 0 else (b.close < ref)) for b in mid):
                continue
            reject_bar_ok = (trig.close < ref) if side < 0 else (trig.close > ref)
            touch = (trig.high >= ref - dec(cfg.breakout.retest_atr_distance * atr)) if side < 0 else (trig.low <= ref + dec(cfg.breakout.retest_atr_distance * atr))
            if not (reject_bar_ok and touch and i >= f + 1):
                continue
            key = ("REV", side, ref, p)
            state.stages.add(("REV", "retest_trigger", side, p))
            conds: list[CondResult] = []
            conds.append(_cond("sweep_extension", ext <= dec(rc.maximum_sweep_atr * atr),
                               f"extension {ext} (>= {rc.minimum_sweep_atr}*ATR by construction) must be <= {rc.maximum_sweep_atr}*ATR = {rc.maximum_sweep_atr * atr:.2f}", value=float(ext)))
            two = any(((ctx.bars[k].close >= ref + t) and (ctx.bars[k + 1].close >= ref + t)) if side < 0 else
                      ((ctx.bars[k].close <= ref - t) and (ctx.bars[k + 1].close <= ref - t)) for k in range(p, f))
            conds.append(_cond("not_a_confirmed_trend_day", not two, "two consecutive closes accepted beyond value" if two else "no acceptance outside value"))
            conds.append(_cond("failure_to_accept", True, f"closed back inside value within {rc.maximum_reentry_bars} bars"))
            want = BALANCED
            if rc.balanced_regime_only:
                conds.append(_cond("regime", regime.name == want, f"{regime.name}: " + "; ".join(regime.reasons)))
            else:
                conds.append(_cond("regime", regime.name != "TRANSITION", f"{regime.name}"))
            rv = relative_volume(trig, ctx.rvol_hist, cfg.breakout.rvol_sessions, cfg.breakout.rvol_min_sessions)
            conds.append(_cond("relative_volume", None if rv is None else rv >= rc.relative_volume_minimum,
                               f"{rv:.2f} (min {rc.relative_volume_minimum})" if rv is not None else "insufficient time-of-day history", dq=True))
            conds.append(_cond("retest_rejection", True, f"retested {src} {ref} and closed back {'below' if side < 0 else 'above'}"))
            dq = DeltaQuality.UNAVAILABLE.value
            failure_kind = "n/a (variant A)"
            if cfg.uses_orderflow:
                dq = _flow_quality(ctx.bars[p: i + 1])
                dr = trig.flow.delta_ratio() if trig.flow else None
                covered = trig.flow is not None and trig.flow.coverage_pct >= cfg.delta.minimum_classified_volume_coverage
                if dr is None or not covered:
                    conds.append(_cond("delta_ratio", None, "rejection-bar delta unavailable or coverage below threshold", dq=True))
                else:
                    conds.append(_cond("delta_ratio", (dr <= -rc.rejection_delta_ratio) if side < 0 else (dr >= rc.rejection_delta_ratio),
                                       f"rejection delta ratio {dr:+.3f} (need {'<=' if side < 0 else '>='} {-side * rc.rejection_delta_ratio:+.2f})", value=dr))
                cp, cc = ctx.cvd[i - 1], ctx.cvd[i]
                conds.append(_cond("cvd_direction", None if (cp is None or cc is None) else ((cc < cp) if side < 0 else (cc > cp)), f"CVD {cp} -> {cc}", dq=True))
                kinds = []
                div = of.divergence(ctx.bars, ctx.cvd, i, atr, cfg.delta)["bearish" if side < 0 else "bullish"]
                if div is not None:
                    kinds.append("delta divergence")
                probe_flow = [b.flow for b in probe if b.flow is not None]
                if probe_flow and len(probe_flow) == len(probe):
                    pd_ = sum((x.delta for x in probe_flow), Decimal(0))
                    fd = ctx.bars[f].flow.delta if ctx.bars[f].flow else Decimal(0)
                    if (pd_ > 0 and fd < 0) if side < 0 else (pd_ < 0 and fd > 0):
                        kinds.append("delta reversal after probe")
                if cfg.uses_absorption:
                    ab = of.absorption(ctx.bars, i, atr, percentile=rc.absorption_percentile, window=rc.absorption_window_bars,
                                       history=rc.absorption_history_bars, max_progress_atr=rc.absorption_max_progress_atr, cfg=cfg.delta)
                    if ab["bearish" if side < 0 else "bullish"]:
                        kinds.append("confirmed absorption")
                failure_kind = ", ".join(kinds) if kinds else "none"
                conds.append(_cond("order_flow_failure", bool(kinds) if dq != DeltaQuality.UNAVAILABLE.value else None, failure_kind, dq=True))
            if cfg.uses_tpo:
                label, ok = _tpo_status(ctx, i, side, "reversal")
                conds.append(_cond("tpo_support", ok, f"TPO time outside old value: {label}"))
            entry = trig.low - t if side < 0 else trig.high + t
            buf = max(2 * t, dec(rc.stop_buffer_atr * atr))
            stop = snap(sweep + buf, t, up=True) if side < 0 else snap(sweep - buf, t, up=False)
            deriv = {"reference": f"{src} {ref}", "probe_bar": p, "failure_bar": f, "sweep_extreme": str(sweep), "order_flow_failure": failure_kind,
                     "stop_rule": f"sweep {'high +' if side < 0 else 'low -'} max(2*tick, {rc.stop_buffer_atr}*ATR)", "atr_5m": round(atr, 4)}
            s, r = _finish(ctx, i, state, key, "FAILED_AUCTION_REVERSAL", side, ref, src, entry, stop, conds,
                           ("dev_poc", "prev_poc"), rc.minimum_rr, regime, deriv, dq)
            if s:
                s.targets = s.targets + [p_ for p_, _ in _collect_targets(ctx, i, side, entry, ("vwap", "prev_edge")) if p_ not in s.targets]
                sigs.append(s)
            if r: rejs.append(r)
    return sigs, rejs


def evaluate(ctx: Ctx, i: int, state: EngineState) -> tuple[list[Signal], list[Rejection], Regime]:
    reg = classify_regime(ctx.bars, i, ctx.vwap, ctx.atr, ctx.prev_stats, ctx.prev_prof, ctx.poc_traj(), ctx.cfg.regime, ctx.feed_ok)
    sigs: list[Signal] = []
    rejs: list[Rejection] = []
    if "breakout" in ctx.cfg.engines:
        s, r = eval_breakout(ctx, i, state, reg); sigs += s; rejs += r
    if "reversal" in ctx.cfg.engines:
        s, r = eval_reversal(ctx, i, state, reg); sigs += s; rejs += r
    return sigs, rejs, reg
