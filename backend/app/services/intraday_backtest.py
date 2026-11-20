"""Event-driven historical simulation over 1-minute bars. Produces statistics about the PAST only (AGENTS.md): no live state,
no orders, no paper portfolio.

Execution model (all of it is echoed in every result under ``assumptions``)
- A signal exists at the CLOSE of a base bar (it may use higher-timeframe candles completed by then). Entry fills at the OPEN of the next
  bar, but only if that bar directly follows (same session, gap <= ``max_fill_delay_minutes``); otherwise the signal is skipped and counted.
- Slippage moves every market/stop fill against the trader: ``slippage_model='bps'`` (default, price x bps, NOT rounded to a tick - rounding a
  2 bps move up to a whole tick overstated slippage 10-20x on low-priced shares) or ``'ticks'`` (N adverse ticks). Limit (target) fills get no
  slippage and never better than the target, except a gap open through it, which fills at that open.
- Stop-loss / target / trailing are simulation parameters. Gap through a stop fills at the open. If one 1-minute bar touches both stop and
  target the STOP is assumed first (1-minute is the finest data held, so the ambiguity cannot be resolved lower) and the trade is flagged.
- Trailing stops use the highest favourable price up to the previous bar, so a bar never trails on information from inside itself.
- Equity intraday positions are forced flat at ``square_off`` (default 15:15 IST); entries stop at ``no_entry_after`` (default 15:00).
  Crypto runs continuously and receives no session rules.
- One position per instrument; ``max_concurrent`` caps open positions across instruments; sizing never exceeds free capital x leverage.
- Money (fills, charges, P&L) is Decimal; price detection uses floats from the stored candles.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.services.transaction_costs import FeeSchedule, compute_charges, schedule_for_market, trade_executions
from app.services.intraday_strategy import ENGINE_VERSION, EvalResult, StrategyDefinition, explain
from app.services.intraday_indicators import INDICATOR_LIBRARY_VERSION
from app.services.timeframes import MINUTE, MarketProfile, local_minutes

D = Decimal
BPS = D(10000)
MIN_TRADES_RELIABLE = 30
MIN_DAYS_RATIOS = 30
MAX_EQUITY_POINTS = 1500
DEFAULT_TICK = {"equity_in": D("0.05"), "crypto": D("0.01")}
DEFAULT_QTY_STEP = {"equity_in": D("1"), "crypto": D("0.001")}


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime | None = None
    end: datetime | None = None
    initial_capital: Decimal = Field(default=D("1000000"), gt=0)
    max_concurrent: int = Field(default=5, ge=1, le=100)
    slippage_bps: Decimal = Field(default=D("2"), ge=0, le=500)
    slippage_model: str = Field(default="bps", pattern="^(bps|ticks)$")
    slippage_ticks: int = Field(default=1, ge=0, le=20)
    cost_profile: str | None = None
    cost_overrides: dict[str, Decimal] = Field(default_factory=dict)
    direction: str = Field(default="both", pattern="^(both|long|short)$")
    leverage: Decimal = Field(default=D("1"), ge=1, le=20)
    tick_size: Decimal | None = Field(default=None, gt=0)
    qty_step: Decimal | None = Field(default=None, gt=0)
    max_fill_delay_minutes: int = Field(default=5, ge=1, le=60)
    max_missing_pct: float = Field(default=5.0, ge=0, le=100)
    capture_explanations: int = Field(default=200, ge=0, le=2000)


@dataclass
class InstrumentRun:
    instrument_id: str
    symbol: str
    profile: MarketProfile
    frame: pd.DataFrame            # clean 1m (time/open/high/low/close/volume floats)
    result: EvalResult
    quality: dict[str, Any]


def _d(x: Any) -> Decimal:
    return D(repr(float(x))) if not isinstance(x, Decimal) else x  # via string, never Decimal(float)


def _q(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).to_integral_value(rounding=rounding) * tick


def _plain(x: Decimal) -> str:
    """Stable plain-decimal string (no exponent) for stored/displayed money and quantities."""
    return format(x.normalize(), "f")


def dataset_hash(runs: list[InstrumentRun]) -> str:
    h = hashlib.sha256()
    for r in sorted(runs, key=lambda x: x.instrument_id):
        h.update(r.instrument_id.encode())
        f = r.frame
        h.update(f["time"].dt.as_unit("ns").astype("int64").to_numpy().tobytes())
        for c in ("open", "high", "low", "close", "volume"):
            h.update(f[c].to_numpy(float).tobytes())
    return h.hexdigest()


class _Pos:
    __slots__ = ("side", "qty", "entry_raw", "entry_fill", "entry_time", "signal_time", "entry_idx", "stop", "stop_src", "target", "trail_dist",
                 "extreme", "margin", "stop0", "slip_in", "ambiguous", "explain", "day")


class _Engine:
    def __init__(self, defn: StrategyDefinition, runs: list[InstrumentRun], cfg: BacktestConfig):
        self.defn, self.cfg, self.runs = defn, cfg, sorted(runs, key=lambda r: r.instrument_id)
        self.market = defn.market
        self.costs: FeeSchedule = schedule_for_market(self.market, cfg.cost_profile)
        self.tick = cfg.tick_size or DEFAULT_TICK[self.market]
        self.step = cfg.qty_step or DEFAULT_QTY_STEP[self.market]
        self.slip = cfg.slippage_bps / BPS
        self.allow_long = defn.constraints.allow_long and cfg.direction in ("both", "long")
        self.allow_short = defn.constraints.allow_short and cfg.direction in ("both", "short")
        eq = self.market == "equity_in"
        self.sq = self._mins(defn.session.square_off or ("15:15" if eq else None)) if eq else None
        self.no_entry = self._mins(defn.session.no_entry_after or ("15:00" if eq else None)) if eq else None
        self.cash = cfg.initial_capital
        self.margin_used = D(0)
        self.trades: list[dict[str, Any]] = []
        self.skipped: dict[str, int] = {}
        self.warnings: list[str] = []

    @staticmethod
    def _mins(hhmm: str | None) -> int | None:
        return None if hhmm is None else int(hhmm[:2]) * 60 + int(hhmm[3:])

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    # ───── charges / fills ─────
    def fill(self, raw: Decimal, side: str, *, slip: bool) -> Decimal:
        """Executed price: the raw price moved adversely by the slippage model (statutory charges are separate and computed on this price)."""
        if not slip:
            return raw
        if self.cfg.slippage_model == "ticks":
            move = self.tick * self.cfg.slippage_ticks
            return raw + move if side == "buy" else raw - move
        factor = 1 + self.slip if side == "buy" else 1 - self.slip
        return (raw * factor).quantize(D("0.0001"), ROUND_HALF_UP)

    def px(self, x: Decimal) -> str:
        s = format(x.quantize(D("0.0001"), ROUND_HALF_UP), "f")
        while s.endswith("0") and len(s.split(".")[1]) > 2:
            s = s[:-1]
        return s

    # ───── main loop ─────
    def run(self) -> dict[str, Any]:
        n_inst = len(self.runs)
        arr = []
        for r in self.runs:
            f = r.frame.reset_index(drop=True)
            t_ns = f["time"].dt.as_unit("ns").astype("int64").to_numpy()
            day, mod = local_minutes(f["time"], r.profile)
            dayi = pd.factorize(day)[0]
            rel = mod.to_numpy() - 0
            gap_ok = np.zeros(len(f), dtype=bool)
            if len(f) > 1:
                dt = np.diff(t_ns) // (60 * 10**9)
                gap_ok[1:] = (dt <= self.cfg.max_fill_delay_minutes) & ((dayi[1:] == dayi[:-1]) if self.market == "equity_in" else True)
            arr.append(dict(t=t_ns, o=f["open"].to_numpy(float), h=f["high"].to_numpy(float), l=f["low"].to_numpy(float), c=f["close"].to_numpy(float),
                            day=dayi, mod=rel, ok=gap_ok, sig=r.result.signals, atr=r.result.atr, lvl=getattr(r.result, "stop_levels", None) or {}, times=f["time"], n=len(f)))
        if not arr:
            return self.finish([], [], [])
        timeline = np.unique(np.concatenate([a["t"] for a in arr]))
        ptr = [0] * n_inst
        pos: list[_Pos | None] = [None] * n_inst
        last_close: list[Decimal | None] = [None] * n_inst
        trades_today: list[dict[int, int]] = [dict() for _ in range(n_inst)]
        equity_t: list[int] = []
        equity_v: list[float] = []
        for T in timeline:
            for k in range(n_inst):
                a = arr[k]
                i = ptr[k]
                if i >= a["n"] or a["t"][i] != T:
                    continue
                ptr[k] += 1
                self.step_bar(k, i, a, pos, trades_today[k])
                last_close[k] = _d(a["c"][i])
                last = i + 1 >= a["n"]
                p = pos[k]
                if p is not None and not last and self.market == "equity_in" and a["day"][i + 1] != a["day"][i]:
                    self.close(k, a, i, pos, _d(a["c"][i]), "session_end", slip=True, at_close=True)
                elif p is not None and last:
                    self.close(k, a, i, pos, _d(a["c"][i]), "end_of_data", slip=True, at_close=True)
            eq = self.cash
            for k in range(n_inst):
                p = pos[k]
                if p is not None and last_close[k] is not None:
                    eq += (last_close[k] - p.entry_fill) * p.qty * (1 if p.side == "long" else -1)
            equity_t.append(int(T))
            equity_v.append(float(eq))
        return self.finish(equity_t, equity_v, [r for r in self.runs])

    def step_bar(self, k: int, i: int, a: dict[str, Any], pos: list[_Pos | None], today: dict[int, int]) -> None:
        defn = self.defn
        o, h, l = a["o"][i], a["h"][i], a["l"][i]
        # 1. decisions taken at the close of bar i-1 execute at the open of bar i
        prev_ok = bool(a["ok"][i]) and i > 0
        sig = a["sig"]
        long_e = prev_ok and sig["long_entry"][i - 1]
        short_e = prev_ok and sig["short_entry"][i - 1]
        long_x = prev_ok and sig["long_exit"][i - 1]
        short_x = prev_ok and sig["short_exit"][i - 1]
        p = pos[k]
        if p is not None:
            want_exit = (p.side == "long" and (long_x or (short_e and self.allow_short))) or (p.side == "short" and (short_x or (long_e and self.allow_long)))
            if want_exit:
                self.close(k, a, i, pos, _d(o), "signal_exit", slip=True)
            elif self.sq is not None and a["mod"][i] >= self.sq:
                self.close(k, a, i, pos, _d(o), "square_off", slip=True)
        if pos[k] is None and prev_ok:
            if long_e and short_e:
                self.skip("conflicting_long_and_short_signal")
            elif (long_e and self.allow_long) or (short_e and self.allow_short):
                side = "long" if long_e else "short"
                if self.no_entry is not None and a["mod"][i] >= self.no_entry:
                    self.skip("after_no_entry_time")
                elif defn.constraints.max_trades_per_day is not None and today.get(int(a["day"][i]), 0) >= defn.constraints.max_trades_per_day:
                    self.skip("max_trades_per_day")
                elif sum(1 for q in pos if q is not None) >= self.cfg.max_concurrent:
                    self.skip("max_concurrent_positions")
                elif self.sq is not None and a["mod"][i] >= self.sq:
                    self.skip("after_square_off")
                else:
                    self.open(k, a, i, pos, side, today)
        elif pos[k] is None and (sig["long_entry"][i - 1] or sig["short_entry"][i - 1]) and i > 0 and not a["ok"][i]:
            self.skip("no_contiguous_next_bar")
        # 2. intrabar stop / target on bar i
        p = pos[k]
        if p is not None:
            self.intrabar(k, a, i, pos, o, h, l)
        p = pos[k]
        # 3. trail uses the extreme through bar i for FUTURE bars only
        if p is not None and p.trail_dist is not None:
            if p.side == "long":
                p.extreme = max(p.extreme, _d(h))
                cand = _q(p.extreme - p.trail_dist, self.tick, ROUND_FLOOR)
                if p.stop is None or cand > p.stop:
                    p.stop, p.stop_src = cand, "trailing_stop"
            else:
                p.extreme = min(p.extreme, _d(l))
                cand = _q(p.extreme + p.trail_dist, self.tick, ROUND_CEILING)
                if p.stop is None or cand < p.stop:
                    p.stop, p.stop_src = cand, "trailing_stop"

    def stop_distance(self, rule: Any, price: Decimal, a: dict[str, Any], i: int) -> Decimal | None:
        if rule.mode == "none" or rule.value <= 0:
            return None
        if rule.mode == "percent":
            return price * D(str(rule.value)) / 100
        atr = a["atr"].get(rule.atr_length)
        v = None if atr is None or i - 1 < 0 else atr[i - 1]
        return None if v is None or math.isnan(v) else D(repr(float(v))) * D(str(rule.value))

    def open(self, k: int, a: dict[str, Any], i: int, pos: list[_Pos | None], side: str, today: dict[int, int]) -> None:
        d = self.defn
        raw = _d(a["o"][i])
        entry = self.fill(raw, "buy" if side == "long" else "sell", slip=True)
        if d.stop.mode == "level":
            levels = a["lvl"].get(side)
            raw_level = None if levels is None or i - 1 < 0 else levels[i - 1]       # the signal candle's level, known at its close
            if raw_level is None or math.isnan(raw_level):
                return self.skip("stop_level_unavailable")
            buffer = D(str(d.stop.value)) / 100
            level = D(repr(float(raw_level))) * (1 - buffer if side == "long" else 1 + buffer)
            if (side == "long" and level >= entry) or (side == "short" and level <= entry):
                return self.skip("stop_level_beyond_entry")                           # price already through the level: no valid stop
            sd = abs(entry - level)
        else:
            sd = self.stop_distance(d.stop, entry, a, i)
        if d.stop.mode != "none" and sd is None:
            return self.skip("stop_distance_unavailable")
        sz = d.sizing
        equity = self.cash
        if sz.method == "fixed_quantity":
            qty = D(str(sz.value))
        elif sz.method == "fixed_notional":
            qty = D(str(sz.value)) / entry
        elif sz.method == "percent_equity":
            qty = equity * D(str(sz.value)) / 100 / entry
        else:
            if not sd or sd <= 0:
                return self.skip("risk_sizing_needs_stop")
            qty = equity * D(str(sz.value)) / 100 / sd
        qty = (qty / self.step).to_integral_value(rounding=ROUND_FLOOR) * self.step
        if qty <= 0:
            return self.skip("quantity_below_minimum")
        margin = entry * qty / self.cfg.leverage
        if margin > self.cash - self.margin_used:
            return self.skip("insufficient_capital")
        p = _Pos()
        p.side, p.qty, p.entry_raw, p.entry_fill = side, qty, raw, entry
        p.entry_time, p.signal_time, p.entry_idx, p.margin, p.day = a["times"].iloc[i], a["times"].iloc[i - 1] + MINUTE, i, margin, int(a["day"][i])
        sign = 1 if side == "long" else -1
        p.stop = _q(entry - sign * sd, self.tick, ROUND_HALF_UP) if sd else None
        p.stop0, p.stop_src = p.stop, "stop_loss"
        t = d.target
        td = None
        if t.mode == "rr":
            td = sd * D(str(t.value)) if sd and t.value > 0 else None
        elif t.mode != "none":
            td = self.stop_distance(t, entry, a, i)
        p.target = _q(entry + sign * td, self.tick, ROUND_HALF_UP) if td else None
        tr = self.stop_distance(d.trail, entry, a, i)
        p.trail_dist = tr
        p.extreme = entry
        p.slip_in = abs(entry - raw) * qty
        p.ambiguous = False
        p.explain = None
        if self.cfg.capture_explanations > len(self.trades):
            sig_rule = "long_entry" if side == "long" else "short_entry"
            p.explain = explain(self.runs[k].result, sig_rule, i - 1)
        self.margin_used += margin
        pos[k] = p
        today[int(a["day"][i])] = today.get(int(a["day"][i]), 0) + 1

    def intrabar(self, k: int, a: dict[str, Any], i: int, pos: list[_Pos | None], o: float, h: float, l: float) -> None:
        p = pos[k]
        long = p.side == "long"
        stop = float(p.stop) if p.stop is not None else None
        tgt = float(p.target) if p.target is not None else None
        if stop is not None and ((long and o <= stop) or (not long and o >= stop)):
            return self.close(k, a, i, pos, _d(o), "stop_gap", slip=True)
        if tgt is not None and ((long and o >= tgt) or (not long and o <= tgt)):
            return self.close(k, a, i, pos, _d(o), "target_gap", slip=False, maker=True)
        hit_stop = stop is not None and ((long and l <= stop) or (not long and h >= stop))
        hit_tgt = tgt is not None and ((long and h >= tgt) or (not long and l <= tgt))
        if hit_stop:
            p.ambiguous = bool(hit_tgt)
            return self.close(k, a, i, pos, p.stop, p.stop_src, slip=True)
        if hit_tgt:
            return self.close(k, a, i, pos, p.target, "target", slip=False, maker=True)

    def close(self, k: int, a: dict[str, Any], i: int, pos: list[_Pos | None], raw: Decimal, reason: str, *, slip: bool,
              maker: bool = False, at_close: bool = False) -> None:
        p = pos[k]
        long = p.side == "long"
        exit_side = "sell" if long else "buy"
        fill = self.fill(raw, exit_side, slip=slip)
        sign = D(1) if long else D(-1)
        gross = (raw - p.entry_raw) * p.qty * sign
        slip_cost = p.slip_in + abs(fill - raw) * p.qty
        # ONE call to the central cost engine for the whole trade, on the actual executed prices and sides (entry order + exit order)
        costs = compute_charges(trade_executions(p.side, p.qty, p.entry_fill, fill, liquidity_exit="maker" if maker else "taker"), self.costs, overrides=self.cfg.cost_overrides)
        charges = costs.total
        net = (fill - p.entry_fill) * p.qty * sign - charges
        self.cash += net
        self.margin_used -= p.margin
        end_time = a["times"].iloc[i] + (MINUTE if at_close else pd.Timedelta(0))
        risk = abs(p.entry_fill - p.stop0) * p.qty if p.stop0 is not None else None
        local = end_time.tz_convert(self.runs[k].profile.tz)
        entry_local = p.entry_time.tz_convert(self.runs[k].profile.tz)
        self.trades.append({
            "instrument_id": self.runs[k].instrument_id, "symbol": self.runs[k].symbol, "side": p.side, "qty": _plain(p.qty),
            "signal_time": p.signal_time.isoformat(), "entry_time": p.entry_time.isoformat(), "exit_time": end_time.isoformat(),
            "entry_raw": _plain(p.entry_raw), "entry_fill": self.px(p.entry_fill), "exit_raw": _plain(raw), "exit_fill": self.px(fill), "slippage_model": self.cfg.slippage_model,
            "exit_reason": reason, "stop_initial": None if p.stop0 is None else self.px(p.stop0), "target": None if p.target is None else self.px(p.target),
            "gross_pnl": str(gross.quantize(D("0.01"), ROUND_HALF_UP)), "slippage_cost": str(slip_cost.quantize(D("0.01"), ROUND_HALF_UP)),
            "charges": str(charges.quantize(D("0.01"), ROUND_HALF_UP)), "cost_breakdown": costs.as_dict(),
            "net_pnl": str(net.quantize(D("0.01"), ROUND_HALF_UP)),
            "r_multiple": None if not risk else str((net / risk).quantize(D("0.01"), ROUND_HALF_UP)),
            "holding_minutes": int((end_time - p.entry_time) / MINUTE), "ambiguous_stop_and_target_same_bar": p.ambiguous,
            "entry_minute_local": entry_local.hour * 60 + entry_local.minute, "exit_day": local.strftime("%Y-%m-%d"), "explain": p.explain})
        pos[k] = None

    # ───── results ─────
    def finish(self, et: list[int], ev: list[float], runs: list[InstrumentRun]) -> dict[str, Any]:
        from app.services.intraday_metrics import summarize
        return summarize(self, et, ev)


def run_backtest(defn: StrategyDefinition, runs: list[InstrumentRun], cfg: BacktestConfig) -> dict[str, Any]:
    """Run the simulation. Callers must already have cleaned/validated each instrument's 1-minute data."""
    from app.services.intraday_metrics import assumptions
    eng = _Engine(defn, runs, cfg)
    out = eng.run()
    out["assumptions"] = assumptions(eng)
    out["versions"] = {"engine": ENGINE_VERSION, "indicator_library": INDICATOR_LIBRARY_VERSION}
    out["dataset"] = {"sha256": dataset_hash(runs), "instruments": [
        {"instrument_id": r.instrument_id, "symbol": r.symbol, "bars": int(len(r.frame)),
         "start": r.frame["time"].iloc[0].isoformat() if len(r.frame) else None, "end": r.frame["time"].iloc[-1].isoformat() if len(r.frame) else None,
         "quality": r.quality} for r in sorted(runs, key=lambda x: x.instrument_id)]}
    return out
