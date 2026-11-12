"""Versioned, source-independent intraday strategy definition + a data-driven (no eval / no dynamic code) evaluator.

The visual builder, the Pine importer and the Python importer all produce a ``StrategyDefinition``. Conditions are a typed tree
evaluated with numpy over the 1-minute base index; higher-timeframe values are exposed only after their candle completed
(``timeframes.align_to_base``), and conditions are sampled only on bars that complete a candle of ``decision_timeframe``.
Stop/target/trailing rules are *historical-simulation* parameters only; they are never part of a live signal.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services import intraday_indicators as ti
from app.services.timeframes import (MINUTE, TIMEFRAME_MINUTES, MarketProfile, aggregate, align_to_base, decision_mask,
                                     local_minutes, minutes_of)

SCHEMA_VERSION = "intraday-strategy/1"
ENGINE_VERSION = "intraday-engine/1.0.0"
MAX_DEPTH, MAX_NODES, MAX_INDICATORS = 6, 200, 40
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4")
COMPARE_OPS = (">", ">=", "<", "<=", "==", "!=", "crosses_above", "crosses_below")
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ───────────────────────── operands & conditions ─────────────────────────
class IndOperand(_Base):
    kind: Literal["indicator"] = "indicator"
    ref: str
    output: str | None = None
    offset: int = Field(default=0, ge=0, le=50)


class PriceOperand(_Base):
    kind: Literal["price"] = "price"
    field: Literal["open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"] = "close"
    timeframe: str = "1m"
    offset: int = Field(default=0, ge=0, le=50)


class ConstOperand(_Base):
    kind: Literal["const"] = "const"
    value: float


class ArithOperand(_Base):
    kind: Literal["arith"] = "arith"
    op: Literal["+", "-", "*", "/"]
    left: "Operand"
    right: "Operand"


Operand = Annotated[Union[IndOperand, PriceOperand, ConstOperand, ArithOperand], Field(discriminator="kind")]
ArithOperand.model_rebuild()


class Compare(_Base):
    type: Literal["compare"] = "compare"
    left: Operand
    op: Literal[">", ">=", "<", "<=", "==", "!=", "crosses_above", "crosses_below"]
    right: Operand
    within: int = Field(default=1, ge=1, le=100)
    label: str | None = Field(default=None, max_length=120)


class TimeWindow(_Base):
    type: Literal["time_window"] = "time_window"
    start: str
    end: str
    label: str | None = Field(default=None, max_length=120)

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM (24h, market-local)")
        return v


class Group(_Base):
    type: Literal["group"] = "group"
    op: Literal["and", "or", "not"]
    children: list["Condition"] = Field(min_length=1)
    label: str | None = Field(default=None, max_length=120)


Condition = Annotated[Union[Compare, TimeWindow, Group], Field(discriminator="type")]
Group.model_rebuild()


# ───────────────────────── definition ─────────────────────────
class IndicatorDecl(_Base):
    alias: str
    indicator: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeframe: str = "1m"


class StopRule(_Base):
    mode: Literal["none", "percent", "atr"] = "none"
    value: float = Field(default=0, ge=0, le=1000)
    atr_length: int = Field(default=14, ge=1, le=200)


class TargetRule(_Base):
    mode: Literal["none", "percent", "atr", "rr"] = "none"
    value: float = Field(default=0, ge=0, le=1000)
    atr_length: int = Field(default=14, ge=1, le=200)


class TrailRule(_Base):
    mode: Literal["none", "percent", "atr"] = "none"
    value: float = Field(default=0, ge=0, le=1000)
    atr_length: int = Field(default=14, ge=1, le=200)


class Sizing(_Base):
    method: Literal["fixed_quantity", "fixed_notional", "percent_equity", "risk_percent"] = "percent_equity"
    value: float = Field(default=10.0, gt=0)


class SessionRules(_Base):
    no_entry_after: str | None = None   # market-local HH:MM (equity only by default)
    square_off: str | None = None       # market-local HH:MM, forced exit in simulation (equity only)


class Constraints(_Base):
    allow_long: bool = True
    allow_short: bool = False
    max_trades_per_day: int | None = Field(default=None, ge=1, le=1000)


class PythonSpec(_Base):
    source_sha256: str
    entrypoint: str = "Strategy"


class Source(_Base):
    type: Literal["visual", "pine", "python", "legacy", "starter"] = "visual"
    artifact: dict[str, Any] | None = None   # filename, sha256, size (the text itself is stored separately)
    provenance: dict[str, Any] = Field(default_factory=dict)


_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class OptionsUnderlying(_Base):
    """Settings that GENERATE the underlying signal rules (``options_strategy.build_underlying_rules``). The generated indicators/conditions are
    stored in the definition like any other strategy; these values are kept so the Builder can regenerate them."""
    underlying: str = Field(default="NIFTY", min_length=1, max_length=32)
    signal_source: Literal["futures", "spot"] = "futures"       # the instrument in `universe`; futures carry real traded volume
    spot_instrument_id: str | None = None                       # index level used for ATM mapping (futures price is a labelled proxy otherwise)
    trend_timeframe: str = "5m"
    entry_timeframe: str = "1m"
    ema_fast: int = Field(default=9, ge=2, le=200)
    ema_slow: int = Field(default=21, ge=3, le=400)
    ema_pullback_alt: int = Field(default=20, ge=2, le=400)
    ema_slope_bars: int = Field(default=3, ge=2, le=10)
    adx_length: int = Field(default=14, ge=2, le=100)
    adx_min: float = Field(default=18.0, ge=0, le=100)
    rsi_length: int = Field(default=14, ge=2, le=100)
    rsi_long_min: float = Field(default=52.0, ge=0, le=100)
    rsi_short_max: float = Field(default=48.0, ge=0, le=100)
    atr_length: int = Field(default=14, ge=2, le=100)
    pullback_atr: float = Field(default=0.25, ge=0, le=5)
    pullback_lookback: int = Field(default=3, ge=1, le=20)
    volume_length: int = Field(default=20, ge=2, le=200)
    volume_multiple: float = Field(default=1.2, ge=0, le=20)
    max_candle_range_atr: float = Field(default=1.5, ge=0, le=20)
    max_vwap_extension_atr: float = Field(default=3.0, ge=0, le=50)
    swing_left: int = Field(default=3, ge=1, le=20)
    swing_right: int = Field(default=3, ge=1, le=20)
    start_time: str = "09:30"
    last_entry_time: str = "14:45"


class OptionsSelectionSpec(_Base):
    allow_ce: bool = True
    allow_pe: bool = True
    strike_choices: list[Literal["atm", "itm1"]] = Field(default_factory=lambda: ["atm", "itm1"], min_length=1)
    delta_min: float = Field(default=0.45, ge=0, le=1)
    delta_max: float = Field(default=0.60, ge=0, le=1)
    delta_preferred: float = Field(default=0.55, ge=0, le=1)
    expiry_rank: int = Field(default=0, ge=0, le=10)             # 0 = nearest eligible expiry
    allow_expiry_day: bool = False
    max_spread_pct: float = Field(default=0.75, gt=0, le=100)
    quote_max_age_seconds: float = Field(default=15.0, gt=0, le=3600)
    min_volume: int = Field(default=0, ge=0)
    min_depth_lots: float = Field(default=1.0, ge=0, le=1000)
    allow_calculated_greeks: bool = True
    risk_free_rate_pct: float = Field(default=6.5, ge=0, le=50)


class OptionsExecutionSpec(_Base):
    price_mode: Literal["quote_accurate", "indicative_ohlc"] = "quote_accurate"
    premium_stop_pct: float = Field(default=12.0, gt=0, lt=100)
    target_r: float = Field(default=1.5, gt=0, le=20)             # target premium gain = premium_stop_pct x target_r
    trailing_activation_r: float | None = Field(default=None, gt=0, le=20)
    trailing_distance_pct: float | None = Field(default=None, gt=0, lt=100)
    max_hold_minutes: float = Field(default=5.0, gt=0, le=240)
    underlying_invalidation: bool = True
    opposite_signal_exit: bool = True
    thesis_exit: bool = True                                      # exit when long_exit/short_exit rules fire
    slippage_ticks: int = Field(default=0, ge=0, le=20)
    tick_size: float = Field(default=0.05, gt=0)
    entry_latency_seconds: float = Field(default=1.0, ge=0, le=60)
    max_entry_wait_seconds: float = Field(default=30.0, gt=0, le=600)
    ohlc_half_spread_pct: float = Field(default=0.5, ge=0, le=20)  # indicative mode only: assumed half-spread paid each way
    fee_schedule: str = "dhan_nse_options_2026_11"


class OptionsRiskSpec(_Base):
    risk_per_trade_pct: float = Field(default=0.25, gt=0, le=100)
    daily_loss_limit_pct: float = Field(default=0.75, gt=0, le=100)
    max_positions: int = Field(default=1, ge=1, le=1)             # overlapping positions are not supported
    sizing_method: Literal["risk_percent", "fixed_lots"] = "risk_percent"
    fixed_lots: int = Field(default=1, ge=1, le=1000)


class EventBlackout(_Base):
    start: str      # ISO-8601 with offset
    end: str
    label: str = Field(default="", max_length=80)


class OptionsSpec(_Base):
    strategy_type: Literal["options_buying"] = "options_buying"
    underlying: OptionsUnderlying = Field(default_factory=OptionsUnderlying)
    selection: OptionsSelectionSpec = Field(default_factory=OptionsSelectionSpec)
    execution: OptionsExecutionSpec = Field(default_factory=OptionsExecutionSpec)
    risk: OptionsRiskSpec = Field(default_factory=OptionsRiskSpec)
    event_blackouts: list[EventBlackout] = Field(default_factory=list, max_length=200)   # user-supplied only; no event calendar is invented


class StrategyDefinition(_Base):
    schema_version: str = SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    market: Literal["equity_in", "crypto"] = "equity_in"
    universe: list[str] = Field(default_factory=list, max_length=200)
    base_timeframe: Literal["1m"] = "1m"
    decision_timeframe: str = "1m"
    indicators: list[IndicatorDecl] = Field(default_factory=list)
    long_entry: Condition | None = None
    short_entry: Condition | None = None
    long_exit: Condition | None = None
    short_exit: Condition | None = None
    sizing: Sizing = Field(default_factory=Sizing)
    stop: StopRule = Field(default_factory=StopRule)
    target: TargetRule = Field(default_factory=TargetRule)
    trail: TrailRule = Field(default_factory=TrailRule)
    session: SessionRules = Field(default_factory=SessionRules)
    constraints: Constraints = Field(default_factory=Constraints)
    python: PythonSpec | None = None
    options: OptionsSpec | None = None
    source: Source = Field(default_factory=Source)


def canonical_json(defn: StrategyDefinition | dict[str, Any]) -> str:
    data = defn.model_dump(mode="json") if isinstance(defn, StrategyDefinition) else defn
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def definition_hash(defn: StrategyDefinition | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(defn).encode()).hexdigest()


# ───────────────────────── validation ─────────────────────────
@dataclass
class Requirements:
    timeframes: list[str]
    indicators: list[dict[str, Any]]
    warmup_base_minutes: int
    nodes: int


def _walk(cond: Condition | None, depth: int = 1):
    if cond is None:
        return
    yield cond, depth
    if isinstance(cond, Group):
        for child in cond.children:
            yield from _walk(child, depth + 1)


def _operands(op: Any):
    yield op
    if isinstance(op, ArithOperand):
        yield from _operands(op.left)
        yield from _operands(op.right)


def rules(defn: StrategyDefinition) -> dict[str, Condition | None]:
    return {"long_entry": defn.long_entry, "short_entry": defn.short_entry, "long_exit": defn.long_exit, "short_exit": defn.short_exit}


def validate_definition(defn: StrategyDefinition) -> tuple[list[dict[str, str]], Requirements | None]:
    """(issues, requirements). Any issue with level 'error' blocks saving."""
    issues: list[dict[str, str]] = []

    def err(msg: str, where: str = "") -> None:
        issues.append({"level": "error", "message": msg, "where": where})

    def warn(msg: str, where: str = "") -> None:
        issues.append({"level": "warning", "message": msg, "where": where})

    if defn.decision_timeframe not in TIMEFRAME_MINUTES:
        err(f"unsupported decision timeframe {defn.decision_timeframe!r}", "decision_timeframe")
    aliases: dict[str, IndicatorDecl] = {}
    if len(defn.indicators) > MAX_INDICATORS:
        err(f"at most {MAX_INDICATORS} indicators")
    warm = 1
    timeframes = {defn.decision_timeframe}
    for decl in defn.indicators:
        where = f"indicators.{decl.alias}"
        if not ALIAS_RE.match(decl.alias):
            err("alias must be lowercase letters/digits/underscore, starting with a letter", where)
        if decl.alias in aliases:
            err("duplicate alias", where)
        aliases[decl.alias] = decl
        if decl.timeframe not in TIMEFRAME_MINUTES:
            err(f"unsupported timeframe {decl.timeframe!r}", where)
            continue
        timeframes.add(decl.timeframe)
        try:
            warm = max(warm, ti.warmup_bars(decl.indicator, decl.params) * minutes_of(decl.timeframe))
        except ti.IndicatorError as e:
            err(str(e), where)
    nodes = 0
    has_entry = False
    for name, cond in rules(defn).items():
        if cond is None:
            continue
        if name.endswith("entry"):
            has_entry = True
        for node, depth in _walk(cond):
            nodes += 1
            if depth > MAX_DEPTH:
                err(f"condition nesting deeper than {MAX_DEPTH}", name)
            if isinstance(node, Group) and node.op == "not" and len(node.children) != 1:
                err("a NOT group takes exactly one condition", name)
            if isinstance(node, TimeWindow) and node.start == node.end:
                err("time window start equals end", name)
            if isinstance(node, Compare):
                for side in (node.left, node.right):
                    for o in _operands(side):
                        if isinstance(o, IndOperand):
                            decl = aliases.get(o.ref)
                            if decl is None:
                                err(f"unknown indicator alias {o.ref!r}", name)
                            elif decl.indicator in ti.REGISTRY:
                                outs = ti.REGISTRY[decl.indicator].outputs
                                if (o.output or outs[0]) not in outs:
                                    err(f"{decl.indicator} has no output {o.output!r}; available: {list(outs)}", name)
                        elif isinstance(o, PriceOperand):
                            if o.timeframe not in TIMEFRAME_MINUTES:
                                err(f"unsupported timeframe {o.timeframe!r}", name)
                            else:
                                timeframes.add(o.timeframe)
                                warm = max(warm, (o.offset + 1) * minutes_of(o.timeframe))
    if nodes > MAX_NODES:
        err(f"at most {MAX_NODES} condition nodes")
    if defn.python is None and not has_entry:
        err("a strategy needs at least one entry condition (long_entry or short_entry)")
    if defn.python is not None and any(rules(defn).values()):
        err("a Python strategy must not also carry builder conditions")
    if defn.long_entry is not None and not defn.constraints.allow_long:
        warn("long_entry is defined but longs are disabled", "constraints")
    if defn.short_entry is not None and not defn.constraints.allow_short:
        warn("short_entry is defined but shorts are disabled; it will not trade", "constraints")
    if defn.market == "crypto" and (defn.session.square_off or defn.session.no_entry_after):
        warn("Indian equity session rules are not applied to 24/7 crypto: square_off / no_entry_after are ignored", "session")
    for key, value in (("no_entry_after", defn.session.no_entry_after), ("square_off", defn.session.square_off)):
        if value is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            err("time must be HH:MM", f"session.{key}")
    if defn.sizing.method == "risk_percent" and defn.stop.mode == "none":
        err("risk_percent sizing needs a stop-loss rule to measure risk", "sizing")
    if defn.target.mode == "rr" and defn.stop.mode == "none":
        err("a risk/reward target needs a stop-loss rule", "target")
    if defn.sizing.method == "percent_equity" and defn.sizing.value > 100:
        err("percent_equity cannot exceed 100", "sizing")
    if defn.options is not None:
        o = defn.options
        if defn.python is not None:
            err("an options strategy cannot also be a Python strategy", "options")
        for key, tf in (("trend_timeframe", o.underlying.trend_timeframe), ("entry_timeframe", o.underlying.entry_timeframe)):
            if tf not in TIMEFRAME_MINUTES:
                err(f"unsupported timeframe {tf!r}", f"options.underlying.{key}")
        if o.underlying.entry_timeframe != defn.decision_timeframe:
            err("the decision timeframe must equal the options entry timeframe", "options.underlying.entry_timeframe")
        if o.underlying.trend_timeframe in TIMEFRAME_MINUTES and o.underlying.entry_timeframe in TIMEFRAME_MINUTES and minutes_of(o.underlying.trend_timeframe) < minutes_of(o.underlying.entry_timeframe):
            err("the trend timeframe must not be shorter than the entry timeframe", "options.underlying.trend_timeframe")
        for key, v in (("start_time", o.underlying.start_time), ("last_entry_time", o.underlying.last_entry_time)):
            if not _HHMM.fullmatch(v):
                err("time must be HH:MM", f"options.underlying.{key}")
        if o.underlying.ema_fast >= o.underlying.ema_slow:
            err("ema_fast must be shorter than ema_slow", "options.underlying")
        sel = o.selection
        if not sel.delta_min <= sel.delta_preferred <= sel.delta_max:
            err("delta_min <= delta_preferred <= delta_max is required", "options.selection")
        if not (sel.allow_ce or sel.allow_pe):
            err("enable at least one of CE / PE", "options.selection")
        if o.execution.trailing_distance_pct is None and o.execution.trailing_activation_r is not None:
            err("trailing activation needs a trailing distance", "options.execution")
        if o.execution.price_mode == "indicative_ohlc":
            warn("indicative OHLC mode: premiums are candle prices plus an assumed half-spread, NOT executable quotes", "options.execution")
        if defn.stop.mode != "none" or defn.target.mode != "none" or defn.trail.mode != "none":
            warn("underlying stop/target/trail are ignored for options strategies: the premium stop/target in options.execution apply", "options")
        for b in o.event_blackouts:
            try:
                if datetime.fromisoformat(b.start) >= datetime.fromisoformat(b.end):
                    err("blackout start must be before end", "options.event_blackouts")
            except ValueError:
                err("blackout times must be ISO-8601 with offset", "options.event_blackouts")
    if any(i["level"] == "error" for i in issues):
        return issues, None
    inds = [{"alias": d.alias, "indicator": d.indicator, "params": ti.resolve_params(d.indicator, d.params), "timeframe": d.timeframe} for d in defn.indicators]
    return issues, Requirements(sorted(timeframes, key=minutes_of), inds, int(warm), nodes)


def apply_overrides(defn: StrategyDefinition, overrides: dict[str, float]) -> StrategyDefinition:
    """Return a copy with ``alias.param`` values replaced (used by sensitivity and walk-forward; never mutates a saved version)."""
    data = defn.model_dump(mode="json")
    by_alias = {d["alias"]: d for d in data["indicators"]}
    for key, value in overrides.items():
        alias, _, param = key.partition(".")
        if alias not in by_alias or not param:
            raise ValueError(f"unknown override {key!r}")
        by_alias[alias]["params"][param] = value
    return StrategyDefinition.model_validate(data)


def tunable_parameters(defn: StrategyDefinition) -> list[dict[str, Any]]:
    out = []
    for d in defn.indicators:
        resolved = ti.resolve_params(d.indicator, d.params)
        for name, spec in ti.REGISTRY[d.indicator].params.items():
            if spec.kind in ("int", "float"):
                out.append({"key": f"{d.alias}.{name}", "value": resolved[name], "kind": spec.kind, "min": spec.low, "max": spec.high})
    return out


# ───────────────────────── evaluation ─────────────────────────
@dataclass
class Node:
    kind: str
    label: str
    result: np.ndarray                      # bool, decision space
    left: np.ndarray | None = None
    right: np.ndarray | None = None
    op: str = ""
    children: list["Node"] = field(default_factory=list)


@dataclass
class EvalResult:
    base_times: pd.Series
    decision_idx: np.ndarray                # base indices that complete a decision-timeframe candle
    signals: dict[str, np.ndarray]          # rule name -> bool over the BASE index (True only on decision bars)
    trees: dict[str, Node]
    atr: dict[int, np.ndarray]              # atr_length -> 1m ATR over the base index (for stop/target/trail distances)
    frames: dict[str, pd.DataFrame]
    warnings: list[str]


def describe_operand(op: Any, decls: dict[str, IndicatorDecl]) -> str:
    if isinstance(op, ConstOperand):
        return f"{op.value:g}"
    if isinstance(op, PriceOperand):
        return f"{op.field}@{op.timeframe}" + (f"[-{op.offset}]" if op.offset else "")
    if isinstance(op, IndOperand):
        d = decls.get(op.ref)
        out = f".{op.output}" if op.output and d is not None and len(ti.REGISTRY[d.indicator].outputs) > 1 else ""
        params = ",".join(str(v) for v in ti.resolve_params(d.indicator, d.params).values() if not isinstance(v, str)) if d else ""
        return f"{d.indicator.upper() if d else op.ref}({params}){out}@{d.timeframe if d else '?'}" + (f"[-{op.offset}]" if op.offset else "")
    return f"({describe_operand(op.left, decls)} {op.op} {describe_operand(op.right, decls)})"


class _Evaluator:
    def __init__(self, defn: StrategyDefinition, base: pd.DataFrame, profile: MarketProfile):
        self.defn, self.base, self.profile = defn, base.reset_index(drop=True), profile
        self.decls = {d.alias: d for d in defn.indicators}
        self.frames: dict[str, pd.DataFrame] = {}
        self.cache: dict[Any, np.ndarray] = {}
        self.n = len(self.base)
        self.dec = np.flatnonzero(decision_mask(self.base["time"], self.frame(defn.decision_timeframe)))

    def frame(self, tf: str) -> pd.DataFrame:
        if tf not in self.frames:
            self.frames[tf] = aggregate(self.base, tf, self.profile)
        return self.frames[tf]

    def _aligned(self, tf: str, key: tuple, values: np.ndarray, offset: int) -> np.ndarray:
        ck = (tf, key, offset)
        if ck not in self.cache:
            frame = self.frame(tf)
            shifted = ti.shift(values, offset) if offset else values
            tmp = pd.DataFrame({"bar_end": frame["bar_end"], "v": shifted})
            self.cache[ck] = align_to_base(tmp, ["v"], self.base["time"])["v"]
        return self.cache[ck]

    def price(self, op: PriceOperand) -> np.ndarray:
        f = self.frame(op.timeframe)
        if f.empty:
            return np.full(self.n, np.nan)
        return self._aligned(op.timeframe, ("price", op.field), ti.src(f, op.field), op.offset)

    def indicator(self, op: IndOperand) -> np.ndarray:
        d = self.decls[op.ref]
        out = op.output or ti.REGISTRY[d.indicator].outputs[0]
        f = self.frame(d.timeframe)
        if f.empty:
            return np.full(self.n, np.nan)
        ck = ("ind", d.alias)
        if ck not in self.cache:
            self.cache[ck] = ti.compute(d.indicator, f, d.params, ti.Ctx(self.profile, d.timeframe))
        return self._aligned(d.timeframe, ("ind", d.alias, out), self.cache[ck][out], op.offset)

    def operand(self, op: Any) -> np.ndarray:
        if isinstance(op, ConstOperand):
            return np.full(self.n, op.value)
        if isinstance(op, PriceOperand):
            return self.price(op)
        if isinstance(op, IndOperand):
            return self.indicator(op)
        a, b = self.operand(op.left), self.operand(op.right)
        with np.errstate(divide="ignore", invalid="ignore"):
            return {"+": a + b, "-": a - b, "*": a * b, "/": np.where(b != 0, a / b, np.nan)}[op.op]

    def node(self, cond: Condition) -> Node:
        d = self.dec
        m = len(d)
        if isinstance(cond, Group):
            kids = [self.node(c) for c in cond.children]
            stack = np.vstack([k.result for k in kids]) if kids else np.zeros((1, m), bool)
            res = stack.all(axis=0) if cond.op == "and" else stack.any(axis=0) if cond.op == "or" else ~stack[0]
            return Node("group", cond.label or cond.op.upper(), res, op=cond.op, children=kids)
        if isinstance(cond, TimeWindow):
            local_close = (self.base["time"].iloc[d] + MINUTE).dt.tz_convert(self.profile.tz) if m else self.base["time"].iloc[:0]
            mod = (local_close.dt.hour * 60 + local_close.dt.minute).to_numpy() if m else np.array([], dtype=int)
            s, e = (int(x[:2]) * 60 + int(x[3:]) for x in (cond.start, cond.end))
            res = (mod >= s) & (mod < e) if s < e else (mod >= s) | (mod < e)
            return Node("time", cond.label or f"time {cond.start}-{cond.end}", res)
        lv, rv = self.operand(cond.left)[d], self.operand(cond.right)[d]
        both = ~(np.isnan(lv) | np.isnan(rv))
        with np.errstate(invalid="ignore"):
            if cond.op in ("crosses_above", "crosses_below"):
                pl, pr = np.r_[np.nan, lv[:-1]], np.r_[np.nan, rv[:-1]]
                ok = both & ~(np.isnan(pl) | np.isnan(pr))
                res = ok & ((pl <= pr) & (lv > rv) if cond.op == "crosses_above" else (pl >= pr) & (lv < rv))
            else:
                tol = 1e-9 * np.maximum(1.0, np.abs(rv))
                res = both & {">": lv > rv, ">=": lv >= rv, "<": lv < rv, "<=": lv <= rv,
                              "==": np.abs(lv - rv) <= tol, "!=": np.abs(lv - rv) > tol}[cond.op]
        if cond.within > 1 and m:
            res = np.convolve(res.astype(int), np.ones(cond.within, dtype=int), mode="full")[:m] > 0
        text = f"{describe_operand(cond.left, self.decls)} {cond.op.replace('_', ' ')} {describe_operand(cond.right, self.decls)}"
        if cond.within > 1:
            text += f" (within last {cond.within} decision bars)"
        return Node("compare", cond.label or text, res, lv, rv, cond.op)


def evaluate(defn: StrategyDefinition, base: pd.DataFrame, profile: MarketProfile) -> EvalResult:
    """Evaluate the builder conditions over clean 1-minute bars. Returns boolean signals on the base index (information up to that bar's close)."""
    issues, req = validate_definition(defn)
    if req is None:
        raise ValueError("; ".join(i["message"] for i in issues if i["level"] == "error"))
    ev = _Evaluator(defn, base, profile)
    signals = {k: np.zeros(ev.n, dtype=bool) for k in ("long_entry", "short_entry", "long_exit", "short_exit")}
    trees: dict[str, Node] = {}
    for name, cond in rules(defn).items():
        if cond is None or len(ev.dec) == 0:
            continue
        tree = ev.node(cond)
        trees[name] = tree
        signals[name][ev.dec] = tree.result
    lengths = {defn.stop.atr_length, defn.target.atr_length, defn.trail.atr_length}
    atr = {n: ti.compute("atr", ev.base, {"length": n}, ti.Ctx(profile, "1m"))["value"] for n in lengths
           if defn.stop.mode == "atr" or defn.target.mode == "atr" or defn.trail.mode == "atr"}
    return EvalResult(ev.base["time"], ev.dec, signals, trees, atr, ev.frames, [])


def explain(result: EvalResult, rule: str, base_index: int) -> list[dict[str, Any]]:
    """Condition-by-condition breakdown of ``rule`` at a decision bar: satisfied and unsatisfied leaves with their operand values."""
    tree = result.trees.get(rule)
    if tree is None:
        return []
    j = int(np.searchsorted(result.decision_idx, base_index))
    if j >= len(result.decision_idx) or result.decision_idx[j] != base_index:
        return []
    out: list[dict[str, Any]] = []

    def walk(n: Node, depth: int) -> None:
        item = {"label": n.label, "satisfied": bool(n.result[j]), "depth": depth, "kind": n.kind}
        if n.left is not None:
            item["left"] = None if np.isnan(n.left[j]) else round(float(n.left[j]), 6)
            item["right"] = None if np.isnan(n.right[j]) else round(float(n.right[j]), 6)
        out.append(item)
        for c in n.children:
            walk(c, depth + 1)
    walk(tree, 0)
    return out
