"""Pine Script v5 import: a small, explicitly bounded subset translated into the internal StrategyDefinition.

This is a TRANSLATOR for a documented subset, not a Pine runtime. Anything outside the subset is reported with its line number and
blocks the import; nothing is silently ignored. Display-only calls (plot, bgcolor...) are listed as ignored. Imported strategies are
NEVER claimed to behave identically to TradingView: fills, repainting and order handling follow this application's backtester.

Supported (see ``SUPPORT_MATRIX``): //@version=5; strategy(); input.int/float/bool; series close/open/high/low/volume/hl2/hlc3/ohlc4;
history reference x[n]; + - * / arithmetic; comparisons; and/or/not; ta.sma/ema/wma/hma/rsi/atr/stdev/macd/bb/stoch/cci/mfi/roc/mom/
supertrend/dmi/highest(high)/lowest(low)/vwap/change/crossover/crossunder; request.security(syminfo.tickerid, "<minutes>", expr);
strategy.entry(id, strategy.long|short [, when=]); strategy.close(id [, when=]); strategy.close_all(); `if cond` blocks around them.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.intraday_strategy import (ArithOperand, Compare, ConstOperand, Group, IndOperand, IndicatorDecl, PriceOperand, Source,
                                            StrategyDefinition, validate_definition)
from app.services.timeframes import TIMEFRAME_MINUTES

SUPPORTED_VERSION = 5
SUPPORT_MATRIX = {
    "supported": ["//@version=5", "strategy(name, ...)", "input.int / input.float / input.bool", "close open high low volume hl2 hlc3 ohlc4",
                  "series history x[n]", "arithmetic + - * /", "comparisons and / or / not",
                  "ta.sma ema wma hma rsi atr stdev macd bb stoch cci mfi roc mom supertrend dmi vwap change crossover crossunder",
                  "ta.highest(high, n) / ta.lowest(low, n)", "request.security(syminfo.tickerid, \"5\", expr) (completed candles only)",
                  "strategy.entry(id, strategy.long|short, when=)", "strategy.close(id, when=)", "strategy.close_all()", "if <cond> blocks around strategy calls"],
    "partial": ["strategy() sizing/commission arguments (ignored: use the backtest configuration)", "qty= on strategy.entry (ignored)",
                "request.security (always completed candles; TradingView realtime behaviour can differ)",
                "plot/plotshape/bgcolor/hline/alertcondition (display only; ignored and listed)"],
    "unsupported": ["//@version other than 5", "indicator() scripts", "var/varip, :=, loops, user functions, arrays/maps/matrices, ternary ?:", "strategy.exit, stop/limit orders, strategy.order",
                    "strategy.risk.*, strategy.position_size and other strategy.* state", "na/nz, session/time/date builtins, daily-and-higher timeframes",
                    "libraries and imports", "any ta.* function not listed above"],
}
DISPLAY_ONLY = ("plot", "plotshape", "plotchar", "plotcandle", "plotbar", "bgcolor", "barcolor", "hline", "fill", "alertcondition", "alert", "label.new", "line.new", "box.new", "table.new")
SERIES = ("close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4")

TOKEN_RE = re.compile(r"""\s*(?:(?P<num>\d+\.\d*|\.\d+|\d+)|(?P<str>"[^"]*"|'[^']*')|(?P<id>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)|(?P<op>:=|==|!=|<=|>=|[-+*/%<>()\[\],=?:]))""")


class PineError(Exception):
    def __init__(self, line: int, message: str):
        super().__init__(message)
        self.line, self.message = line, message


@dataclass
class Report:
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, line: int, status: str, construct: str, message: str = "") -> None:
        self.items.append({"line": line, "status": status, "construct": construct, "message": message})


# ───────────────────────── lexing / parsing ─────────────────────────
def _tokens(text: str, line: int) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise PineError(line, f"unexpected character {text[pos:].strip()[:1]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


class Parser:
    def __init__(self, toks: list[tuple[str, str]], line: int):
        self.t, self.i, self.line = toks, 0, line

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def eat(self, val: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or (val is not None and tok[1] != val):
            raise PineError(self.line, f"expected {val!r}" if val else "unexpected end of line")
        self.i += 1
        return tok

    def accept(self, val: str) -> bool:
        tok = self.peek()
        if tok and tok[1] == val and tok[0] in ("op", "id"):
            self.i += 1
            return True
        return False

    def done(self) -> bool:
        return self.i >= len(self.t)

    # precedence: or < and < not < comparison < +- < */% < unary < postfix
    def expr(self):
        left = self.and_()
        while self.accept("or"):
            left = ("or", left, self.and_())
        return left

    def and_(self):
        left = self.not_()
        while self.accept("and"):
            left = ("and", left, self.not_())
        return left

    def not_(self):
        if self.accept("not"):
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        left = self.add()
        tok = self.peek()
        if tok and tok[1] in ("<", "<=", ">", ">=", "==", "!="):
            self.i += 1
            return ("cmp", tok[1], left, self.add())
        if tok and tok[1] == "?":
            raise PineError(self.line, "ternary '?:' is not supported")
        return left

    def add(self):
        left = self.mul()
        while (tok := self.peek()) and tok[1] in ("+", "-"):
            self.i += 1
            left = ("arith", tok[1], left, self.mul())
        return left

    def mul(self):
        left = self.unary()
        while (tok := self.peek()) and tok[1] in ("*", "/", "%"):
            self.i += 1
            if tok[1] == "%":
                raise PineError(self.line, "modulo '%' is not supported")
            left = ("arith", tok[1], left, self.unary())
        return left

    def unary(self):
        if self.accept("-"):
            return ("arith", "-", ("num", 0.0), self.unary())
        if self.accept("+"):
            return self.unary()
        return self.postfix()

    def postfix(self):
        node = self.atom()
        while self.peek() and self.peek()[1] == "[":
            self.eat("[")
            idx = self.expr()
            self.eat("]")
            node = ("index", node, idx)
        return node

    def atom(self):
        kind, val = self.eat()
        if kind == "num":
            return ("num", float(val))
        if kind == "str":
            return ("str", val[1:-1])
        if val == "(":
            node = self.expr()
            self.eat(")")
            return node
        if val == "[":
            raise PineError(self.line, "array / tuple literals are not supported")
        if kind == "id":
            if self.peek() and self.peek()[1] == "(":
                self.eat("(")
                args, kwargs = [], {}
                while not (self.peek() and self.peek()[1] == ")"):
                    if self.peek() and self.peek()[0] == "id" and self.i + 1 < len(self.t) and self.t[self.i + 1][1] == "=":
                        key = self.eat()[1]
                        self.eat("=")
                        kwargs[key] = self.expr()
                    else:
                        args.append(self.expr())
                    if not self.accept(","):
                        break
                self.eat(")")
                return ("call", val, args, kwargs)
            return ("id", val)
        raise PineError(self.line, f"unexpected token {val!r}")


def _logical_lines(text: str) -> list[tuple[int, int, str]]:
    """(first line number, indent, text) after removing comments and joining lines inside unclosed brackets."""
    out, buf, start, indent, depth = [], "", 0, 0, 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"//.*$", "", raw) if not re.search(r"""["'][^"']*//""", raw) else raw
        if not line.strip() and depth == 0:
            continue
        if depth == 0:
            start = n
            indent = len(line) - len(line.lstrip(" \t")) + 3 * line[: len(line) - len(line.lstrip(" \t"))].count("\t")
            buf = ""
        buf += " " + line.strip()
        depth += line.count("(") + line.count("[") - line.count(")") - line.count("]")
        if depth <= 0:
            out.append((start, indent, buf.strip()))
            depth = 0
    if depth > 0:
        raise PineError(start, "unbalanced brackets")
    return out


# ───────────────────────── translation ─────────────────────────
class Translator:
    def __init__(self, decision_tf: str, report: Report):
        self.tf0, self.report = decision_tf, report
        self.vars: dict[str, Any] = {}
        self.decls: dict[str, IndicatorDecl] = {}
        self.params: dict[str, Any] = {}
        self.entries: dict[str, list[Any]] = {"long_entry": [], "short_entry": [], "long_exit": [], "short_exit": []}
        self.id_dir: dict[str, str] = {}
        self.name = "Imported Pine strategy"
        self.security_used = False

    # operands --------------------------------------------------------------
    def decl(self, indicator: str, params: dict[str, Any], tf: str) -> str:
        key = indicator + "_" + "_".join(f"{v}".replace(".", "p") for v in params.values()) + f"_{tf}"
        alias = re.sub(r"[^a-z0-9_]", "_", key.lower())[:32]
        n = 0
        base = alias
        while alias in self.decls and (self.decls[alias].indicator != indicator or self.decls[alias].params != params or self.decls[alias].timeframe != tf):
            n += 1
            alias = f"{base[:28]}_{n}"
        self.decls[alias] = IndicatorDecl(alias=alias, indicator=indicator, params=params, timeframe=tf)
        return alias

    def num(self, node, line: int) -> float:
        v = self.operand(node, line, self.tf0)
        if not isinstance(v, ConstOperand):
            raise PineError(line, "this argument must be a constant or an input.* value")
        return v.value

    def source(self, node, line: int, tf: str) -> str:
        if node[0] == "id" and node[1] in SERIES:
            return node[1]
        raise PineError(line, "only close/open/high/low/volume/hl2/hlc3/ohlc4 can be used as an indicator source")

    def shifted(self, op, n: int, line: int):
        if isinstance(op, (PriceOperand, IndOperand)):
            return op.model_copy(update={"offset": op.offset + n})
        if isinstance(op, ConstOperand):
            return op
        if isinstance(op, ArithOperand):
            return ArithOperand(op=op.op, left=self.shifted(op.left, n, line), right=self.shifted(op.right, n, line))
        raise PineError(line, "history reference [n] is only supported on series values")

    def operand(self, node, line: int, tf: str):
        k = node[0]
        if k == "num":
            return ConstOperand(value=node[1])
        if k == "id":
            name = node[1]
            if name in self.vars:
                v = self.vars[name]
                if isinstance(v, (Compare, Group)):
                    raise PineError(line, f"{name!r} is a condition, not a number")
                return v if tf == self.tf0 or isinstance(v, ConstOperand) else self.retime(v, tf)
            if name in SERIES:
                return PriceOperand(field=name, timeframe=tf)
            if name in ("ta.vwap", "vwap"):
                return IndOperand(ref=self.decl("vwap", {}, tf), output="value")
            raise PineError(line, f"unknown or unsupported identifier {name!r}")
        if k == "index":
            idx = self.operand(node[2], line, tf)
            if not isinstance(idx, ConstOperand) or idx.value < 0 or idx.value != int(idx.value):
                raise PineError(line, "history index must be a non-negative integer constant")
            return self.shifted(self.operand(node[1], line, tf), int(idx.value), line)
        if k == "arith":
            return ArithOperand(op=node[1], left=self.operand(node[2], line, tf), right=self.operand(node[3], line, tf))
        if k == "call":
            return self.call_operand(node, line, tf)
        raise PineError(line, "this expression is not supported here")

    def retime(self, op, tf: str):
        if isinstance(op, PriceOperand):
            return op.model_copy(update={"timeframe": tf})
        if isinstance(op, IndOperand):
            d = self.decls[op.ref]
            return op.model_copy(update={"ref": self.decl(d.indicator, d.params, tf)})
        if isinstance(op, ArithOperand):
            return ArithOperand(op=op.op, left=self.retime(op.left, tf), right=self.retime(op.right, tf))
        return op

    def call_operand(self, node, line: int, tf: str):
        _, name, args, kw = node
        if name in ("input.int", "input.float", "input"):
            if not args and "defval" not in kw:
                raise PineError(line, "input needs a default value")
            return ConstOperand(value=self.num(args[0] if args else kw["defval"], line))
        if name == "input.bool":
            raise PineError(line, "boolean inputs are not supported; use input.int/input.float")
        if name == "request.security":
            return self.security(node, line)
        spec = {
            "ta.sma": ("sma", ("source", "length")), "ta.ema": ("ema", ("source", "length")), "ta.wma": ("wma", ("source", "length")),
            "ta.hma": ("hma", ("source", "length")), "ta.rsi": ("rsi", ("source", "length")), "ta.stdev": ("stdev", ("source", "length")),
            "ta.roc": ("roc", ("source", "length")), "ta.mom": ("momentum", ("source", "length")),
        }
        if name in spec:
            ind, order = spec[name]
            if len(args) != 2 and not (len(args) + len(kw) == 2):
                raise PineError(line, f"{name}(source, length) takes two arguments")
            a = list(args) + [kw[k] for k in order if k in kw]
            return IndOperand(ref=self.decl(ind, {"length": int(self.num(a[1], line)), "source": self.source(a[0], line, tf)}, tf), output="value")
        if name == "ta.atr":
            return IndOperand(ref=self.decl("atr", {"length": int(self.num(args[0], line))}, tf), output="value")
        if name in ("ta.highest", "ta.lowest"):
            a = list(args)
            if len(a) != 2 or a[0] != ("id", "high" if name == "ta.highest" else "low"):
                raise PineError(line, f"only {name}(high|low, n) is supported (highest of highs / lowest of lows)")
            return IndOperand(ref=self.decl("donchian", {"length": int(self.num(a[1], line))}, tf), output="upper" if name == "ta.highest" else "lower")
        if name == "ta.change":
            n = int(self.num(args[1], line)) if len(args) > 1 else 1
            cur = self.operand(args[0], line, tf)
            return ArithOperand(op="-", left=cur, right=self.shifted(cur, n, line))
        if name == "ta.cci":
            if args[0] != ("id", "hlc3"):
                raise PineError(line, "ta.cci is supported only with the hlc3 source")
            return IndOperand(ref=self.decl("cci", {"length": int(self.num(args[1], line))}, tf), output="value")
        if name == "ta.mfi":
            if args[0] != ("id", "hlc3"):
                raise PineError(line, "ta.mfi is supported only with the hlc3 source")
            return IndOperand(ref=self.decl("mfi", {"length": int(self.num(args[1], line))}, tf), output="value")
        if name == "ta.stoch":
            if len(args) != 4 or [a for a in args[:3]] != [("id", "close"), ("id", "high"), ("id", "low")]:
                raise PineError(line, "only ta.stoch(close, high, low, length) is supported")
            return IndOperand(ref=self.decl("stoch", {"k_length": int(self.num(args[3], line)), "k_smooth": 1, "d_length": 1}, tf), output="k")
        raise PineError(line, f"{name}() is not supported")

    def security(self, node, line: int):
        _, _, args, kw = node
        if len(args) < 3 or args[0] != ("id", "syminfo.tickerid"):
            raise PineError(line, "only request.security(syminfo.tickerid, \"<minutes>\", expression) is supported")
        if args[1][0] != "str":
            raise PineError(line, "request.security timeframe must be a string literal")
        raw = args[1][1]
        if not raw.isdigit() or f"{int(raw)}m" not in TIMEFRAME_MINUTES:
            raise PineError(line, f"timeframe {raw!r} is not supported (supported minutes: {', '.join(k[:-1] for k in TIMEFRAME_MINUTES)})")
        la = kw.get("lookahead") or (args[4] if len(args) > 4 else None)
        if la is not None and la != ("id", "barmerge.lookahead_off"):
            raise PineError(line, "lookahead=barmerge.lookahead_on reads unfinished candles (look-ahead bias) and is rejected")
        self.security_used = True
        self.report.add(line, "partial", "request.security", "evaluated on completed higher-timeframe candles only; TradingView realtime values can differ")
        return self.operand(args[2], line, f"{int(raw)}m")

    # conditions ------------------------------------------------------------
    def cond(self, node, line: int, tf: str | None = None):
        tf = tf or self.tf0
        k = node[0]
        if k in ("and", "or"):
            return Group(op=k, children=[self.cond(node[1], line, tf), self.cond(node[2], line, tf)])
        if k == "not":
            return Group(op="not", children=[self.cond(node[1], line, tf)])
        if k == "cmp":
            return Compare(left=self.operand(node[2], line, tf), op=node[1], right=self.operand(node[3], line, tf))
        if k == "id" and node[1] in self.vars and isinstance(self.vars[node[1]], (Compare, Group)):
            return self.vars[node[1]]
        if k == "call" and node[1] in ("ta.crossover", "ta.crossunder"):
            return Compare(left=self.operand(node[2][0], line, tf), op="crosses_above" if node[1] == "ta.crossover" else "crosses_below",
                           right=self.operand(node[2][1], line, tf))
        if k == "call" and node[1] == "request.security":
            self.report.add(line, "partial", "request.security", "evaluated on completed higher-timeframe candles only")
            raw = node[2][1][1] if len(node[2]) > 1 and node[2][1][0] == "str" else ""
            if not raw.isdigit() or f"{int(raw)}m" not in TIMEFRAME_MINUTES:
                raise PineError(line, f"timeframe {raw!r} is not supported")
            return self.cond(node[2][2], line, f"{int(raw)}m")
        raise PineError(line, "expected a boolean condition (comparison, and/or/not, ta.crossover/crossunder)")

    # statements ------------------------------------------------------------
    def strategy_call(self, name: str, args, kw, line: int, guard):
        def with_guard(c):
            if guard is None:
                return c
            return Group(op="and", children=[guard, c])
        when = self.cond(kw["when"], line) if "when" in kw else None
        base = None if when is None and guard is None else (with_guard(when) if when is not None else guard)
        if name == "strategy.entry":
            if len(args) < 2 or args[0][0] != "str" or args[1][0] != "id" or args[1][1] not in ("strategy.long", "strategy.short"):
                raise PineError(line, "strategy.entry(\"id\", strategy.long|strategy.short [, when=]) is the supported form")
            for bad in ("limit", "stop", "oca_name", "oca_type", "alert_message"):
                if bad in kw:
                    raise PineError(line, f"strategy.entry argument {bad!r} (pending/limit/stop orders) is not supported")
            if "qty" in kw or len(args) > 2:
                self.report.add(line, "partial", "strategy.entry qty", "quantity ignored: sizing comes from the strategy sizing setting and backtest configuration")
            if base is None:
                raise PineError(line, "an unconditional strategy.entry has no condition: put it inside `if <condition>` or add when=")
            side = "long_entry" if args[1][1] == "strategy.long" else "short_entry"
            self.id_dir[args[0][1]] = "long" if side == "long_entry" else "short"
            self.entries[side].append(base)
            self.report.add(line, "supported", name)
        elif name == "strategy.close":
            if not args or args[0][0] != "str" or args[0][1] not in self.id_dir:
                raise PineError(line, "strategy.close(\"id\") must name an id used by an earlier strategy.entry")
            if base is None:
                raise PineError(line, "an unconditional strategy.close has no condition")
            self.entries["long_exit" if self.id_dir[args[0][1]] == "long" else "short_exit"].append(base)
            self.report.add(line, "supported", name)
        elif name == "strategy.close_all":
            if base is None:
                raise PineError(line, "an unconditional strategy.close_all has no condition")
            self.entries["long_exit"].append(base)
            self.entries["short_exit"].append(base)
            self.report.add(line, "supported", name)
        else:
            raise PineError(line, f"{name}() is not supported" + (" (stop/limit exits cannot be translated safely; configure stop/target in the strategy risk settings instead)" if name == "strategy.exit" else ""))


def _join(conds: list[Any]):
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else Group(op="or", children=conds)



# ───────────────────────── error diagnosis (reporting only; never changes what is translated) ─────────────────────────
PYTHON_PATH_LIMITS = [
    "Only the decision timeframe's OHLCV is visible; higher timeframes must be declared as library indicators, not read as raw candles.",
    "Only indicators in the trusted library registry are available.",
    "Output is four boolean signals (long/short entry and exit): no stop or limit orders and no strategy.exit.",
    "Each run has a 20 second / 512 MB budget in an isolated interpreter.",
]
# (key, label, blocking: cannot be expressed in the rule DSL at all)
CAUSES: dict[str, tuple[str, bool]] = {
    "state": ("var / persistent state", True), "loops": ("loops (for / while)", True), "arrays": ("arrays / maps / matrices", True),
    "functions": ("user-defined functions", True), "ternary": ("ternary ?:", False), "math": ("math.* functions", False),
    "bool_input": ("boolean inputs", False), "blocks": ("multi-line blocks / indentation", False), "security": ("request.security forms", False),
    "orders": ("strategy.exit / stop / limit orders", False), "ta": ("unsupported ta.* functions", False), "identifier": ("unknown identifiers", False),
    "script": ("script type / version", False), "other": ("other unsupported syntax", False),
}


def _classify(message: str, src: str = "") -> str:
    head = re.match(r"^(var|varip|for|while|switch)\b", src)
    if head:
        return {"var": "state", "varip": "state", "for": "loops", "while": "loops", "switch": "blocks"}[head.group(1)]
    if re.match(r"^(?:export\s+)?[A-Za-z_]\w*\s*\([^)]*\)\s*=>", src):
        return "functions"
    if re.match(r"^[A-Za-z_]\w*\s*:=", src):
        return "state"
    m = message.lower()
    if "'var'" in m or "'varip'" in m or "':='" in m:
        return "state"
    if "'for'" in m or "'while'" in m:
        return "loops"
    if "array" in m or re.search(r"\b(matrix|map)\.", m) or "'type'" in m or "'method'" in m:
        return "arrays"
    if "function definition" in m or "=>" in m:
        return "functions"
    if "ternary" in m:
        return "ternary"
    if "math." in m:
        return "math"
    if "boolean inputs" in m:
        return "bool_input"
    if "unexpected indentation" in m or "'switch'" in m:
        return "blocks"
    if "request.security" in m or "lookahead" in m or "timeframe" in m:
        return "security"
    if "strategy." in m or "stop/limit" in m or "pending" in m:
        return "orders"
    if re.search(r"\bta\.\w+\(\) is not supported|only ta\.", m):
        return "ta"
    if "unknown or unsupported identifier" in m:
        return "identifier"
    if "indicator/library" in m or "version" in m or "no strategy.entry" in m:
        return "script"
    return "other"


def _bound_names(src: str) -> list[str]:
    """Names a (possibly failing) statement would have defined, so later uses can be recognised as follow-on errors."""
    m = re.match(r"^(?:var|varip)\s+(?:[A-Za-z_]\w*(?:\[\])?\s+)?([A-Za-z_]\w*)\s*=", src)
    if m:
        return [m.group(1)]
    m = re.match(r"^for\s+([A-Za-z_]\w*)\s*=", src)
    if m:
        return [m.group(1)]
    m = re.match(r"^(?:export\s+)?([A-Za-z_]\w*)\s*\([^)]*\)\s*=>", src)
    if m:
        return [m.group(1)]
    m = re.match(r"^\[([^\]]*)\]\s*=", src)
    if m:
        return [x.strip() for x in m.group(1).split(",") if x.strip()]
    m = re.match(r"^(?:[A-Za-z_]\w*\s+)?([A-Za-z_]\w*)\s*(?::=|=(?!=))", src)
    return [m.group(1)] if m else []


def diagnose(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Group errors by cause, collapse follow-on errors under their origin and produce one verdict. Pure reporting."""
    roots: dict[str, list[dict[str, Any]]] = {}
    follow: dict[str, int] = {}
    for e in errors:
        origin = e.get("follow_on_of")
        if origin:
            follow[origin] = follow.get(origin, 0) + 1
        else:
            roots.setdefault(e.get("cause") or _classify(e["message"]), []).append(e)
    groups = []
    for key, items in sorted(roots.items(), key=lambda kv: (-len(kv[1]) - follow.get(kv[0], 0), kv[0])):
        label, blocking = CAUSES[key]
        first = min(items, key=lambda x: x["line"] or 0)
        groups.append({"key": key, "label": label, "blocking": blocking, "count": len(items), "follow_on_count": follow.get(key, 0),
                       "example_line": first["line"], "example_message": first["message"]})
    for key, n in follow.items():          # follow-ons whose origin group has no root of its own (cannot normally happen)
        if key not in roots:
            groups.append({"key": key, "label": CAUSES[key][0], "blocking": CAUSES[key][1], "count": 0, "follow_on_count": n, "example_line": None, "example_message": ""})
    blocking = [g for g in groups if g["blocking"]]
    root_total = sum(g["count"] for g in groups)
    if not errors:
        verdict = None
    elif blocking:
        names = " and ".join(", ".join(g["label"] for g in blocking).rsplit(", ", 1)) if len(blocking) > 1 else blocking[0]["label"]
        verdict = f"Cannot import: needs {names}. Not expressible in the rule DSL; use the Python strategy path."
    else:
        verdict = f"Cannot import: {root_total} unsupported construct{'s' if root_total != 1 else ''} ({', '.join(g['label'] for g in groups[:3])}{'…' if len(groups) > 3 else ''})."
    return {"verdict": verdict, "total_errors": len(errors), "root_cause_count": root_total, "follow_on_count": sum(follow.values()), "groups": groups,
            "route": ({"to": "python", "reason": "The rule DSL cannot express persistent state, arrays, loops or user-defined functions.",
                       "limits": PYTHON_PATH_LIMITS} if blocking else None)}


def import_pine(text: str, filename: str, market: str = "equity_in", decision_timeframe: str = "5m") -> dict[str, Any]:
    """Parse + translate. Always returns a report; ``definition`` is present only when the whole script is importable."""
    report = Report()
    errors: list[dict[str, Any]] = []
    sha = hashlib.sha256(text.encode()).hexdigest()
    result: dict[str, Any] = {"filename": filename, "sha256": sha, "supported_version": SUPPORTED_VERSION, "support_matrix": SUPPORT_MATRIX,
                              "equivalence_validated": False, "equivalence_note": "Not validated against TradingView. Fills, repainting, order handling and costs follow this "
                              "application's backtester, so results can differ. Treat the translation as a starting point and review it."}
    if len(text.encode()) > 200_000:
        errors.append({"line": None, "message": "file larger than 200 KB"})
    m = re.search(r"//\s*@version\s*=\s*(\d+)", text)
    version = int(m.group(1)) if m else None
    result["version"] = version
    if version != SUPPORTED_VERSION:
        errors.append({"line": next((i for i, l in enumerate(text.splitlines(), 1) if "@version" in l), 1),
                       "message": f"Pine version {version if version else 'unknown'} is not supported; only //@version=5 is translated"})
    tr = Translator(decision_timeframe, report)
    guard = None
    if not errors:
        try:
            lines = _logical_lines(text)
        except PineError as e:
            lines = []
            errors.append({"line": e.line, "message": e.message})
        block_guard: Any = None
        block_indent = -1
        failed_names: dict[str, str] = {}      # name that failed to bind -> cause key of the root failure
        head_failure: str | None = None        # cause of the last failed top-level statement (its indented body fails as a follow-on)
        for line, indent, src in lines:
            if indent == 0:
                head_failure = None
            try:
                if indent > 0 and block_guard is None:
                    raise PineError(line, "unexpected indentation (only `if` blocks are supported)")
                if indent == 0:
                    block_guard, block_indent = None, -1
                head = src.split("(")[0].strip().split()[0] if src else ""
                if head in ("var", "varip", "for", "while", "switch", "import", "export", "method", "type", "enum") or re.match(r"^[A-Za-z_]\w*\s*\(.*\)\s*=>", src) or "=>" in src:
                    raise PineError(line, f"{head or 'function definition'!r} is not supported")
                if src.startswith("indicator(") or src.startswith("library("):
                    raise PineError(line, "this is an indicator/library script; only strategy() scripts are supported")
                if src.startswith("//@"):
                    continue
                if any(src.startswith(d + "(") for d in DISPLAY_ONLY):
                    report.add(line, "partial", src.split("(")[0], "display-only; ignored")
                    continue
                if src.startswith("strategy("):
                    p = Parser(_tokens(src, line), line)
                    node = p.expr()
                    for a in node[2]:
                        if a[0] == "str":
                            tr.name = a[1][:120]
                            break
                    if "title" in node[3] and node[3]["title"][0] == "str":
                        tr.name = node[3]["title"][1][:120]
                    ignored = [k for k in node[3] if k not in ("title", "shorttitle", "overlay", "format", "precision")]
                    report.add(line, "partial" if ignored else "supported", "strategy()",
                               ("ignored arguments: " + ", ".join(ignored) + " (use the backtest configuration)") if ignored else "")
                    continue
                if src.startswith("if "):
                    p = Parser(_tokens(src[3:], line), line)
                    block_guard = tr.cond(p.expr(), line)
                    block_indent = indent
                    report.add(line, "supported", "if <condition>")
                    continue
                if re.match(r"^\[[^\]]*\]\s*=", src):
                    names_part, rhs = src.split("=", 1)
                    names = [x.strip() for x in names_part.strip()[1:-1].split(",")]
                    p = Parser(_tokens(rhs, line), line)
                    node = p.expr()
                    if node[0] != "call":
                        raise PineError(line, "tuple assignment needs a function call on the right")
                    _tuple_assign(tr, names, node, line)
                    report.add(line, "supported", "tuple assignment " + node[1])
                    continue
                if ":=" in src:
                    raise PineError(line, "reassignment ':=' is not supported")
                am = re.match(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.+)$", src)
                if am:
                    p = Parser(_tokens(am.group(2), line), line)
                    node = p.expr()
                    if not p.done():
                        raise PineError(line, "ternary '?:' is not supported" if p.peek()[1] == "?" else "unexpected trailing tokens")
                    name = am.group(1)
                    if node[0] in ("and", "or", "not", "cmp") or (node[0] == "call" and node[1] in ("ta.crossover", "ta.crossunder")):
                        tr.vars[name] = tr.cond(node, line)
                    else:
                        tr.vars[name] = tr.operand(node, line, tr.tf0)
                        if node[0] == "call" and node[1] in ("input.int", "input.float", "input"):
                            tr.params[name] = tr.vars[name].value
                    report.add(line, "supported", f"{name} = ...")
                    continue
                p = Parser(_tokens(src, line), line)
                node = p.expr()
                if node[0] == "call" and node[1].startswith("strategy."):
                    tr.strategy_call(node[1], node[2], node[3], line, block_guard if indent > 0 else None)
                    continue
                raise PineError(line, "this statement is not supported")
            except (PineError, IndexError, ValueError, KeyError) as e:  # malformed argument lists raise the builtin types
                message = e.message if isinstance(e, PineError) else f"could not translate this statement ({type(e).__name__})"
                cause = _classify(message, src)
                origin = None
                if cause == "blocks" and head_failure is not None:
                    origin = head_failure
                elif cause in ("identifier", "other"):
                    used = [n for n in re.findall(r"[A-Za-z_]\w*", src) if n in failed_names]
                    if used:
                        origin = failed_names[used[0]]
                entry: dict[str, Any] = {"line": line, "message": message, "cause": cause}
                if origin:
                    entry["follow_on_of"] = origin
                errors.append(entry)
                for name in _bound_names(src):
                    failed_names.setdefault(name, origin or cause)
                if indent == 0:
                    head_failure = origin or cause

    result["items"] = report.items
    result["errors"] = errors
    result["diagnosis"] = diagnose(errors)
    result["warnings"] = [i for i in report.items if i["status"] == "partial"]
    result["parameters"] = tr.params
    result["detected"] = {"indicators": [d.model_dump() for d in tr.decls.values()],
                          "entries": {k: len(v) for k, v in tr.entries.items()}}
    result["compatibility"] = "unsupported" if errors else ("partial" if result["warnings"] else "full")
    if not errors and not (tr.entries["long_entry"] or tr.entries["short_entry"]):
        errors.append({"line": None, "message": "no strategy.entry found: nothing to trade"})
        result["compatibility"] = "unsupported"
    result["can_import"] = not errors
    result["definition"] = None
    if not errors:
        try:
            defn = StrategyDefinition(
                name=tr.name, market=market, decision_timeframe=decision_timeframe, indicators=list(tr.decls.values()),
                long_entry=_join(tr.entries["long_entry"]), short_entry=_join(tr.entries["short_entry"]),
                long_exit=_join(tr.entries["long_exit"]), short_exit=_join(tr.entries["short_exit"]),
                constraints={"allow_long": bool(tr.entries["long_entry"]), "allow_short": bool(tr.entries["short_entry"])},
                source=Source(type="pine", artifact={"filename": filename, "sha256": sha, "size": len(text.encode())},
                              provenance={"pine_version": version, "compatibility": result["compatibility"], "inputs": tr.params, "equivalence_validated": False}))
            issues, req = validate_definition(defn)
            if req is None:
                for i in issues:
                    if i["level"] == "error":
                        errors.append({"line": None, "message": i["message"]})
                result["can_import"] = False
                result["compatibility"] = "unsupported"
            else:
                result["definition"] = defn.model_dump(mode="json")
        except Exception as e:  # noqa: BLE001 - surface any model error as a report entry
            errors.append({"line": None, "message": str(e)[:300]})
            result["can_import"] = False
    return result


def _tuple_assign(tr: Translator, names: list[str], node, line: int) -> None:
    _, fn, args, kw = node
    tf = tr.tf0
    if fn == "ta.macd":
        if len(args) != 4:
            raise PineError(line, "ta.macd(source, fast, slow, signal) takes four arguments")
        params = {"fast": int(tr.num(args[1], line)), "slow": int(tr.num(args[2], line)), "signal": int(tr.num(args[3], line)), "source": tr.source(args[0], line, tf)}
        outs = ("macd", "signal", "hist")
        ind = "macd"
    elif fn == "ta.bb":
        params = {"length": int(tr.num(args[1], line)), "multiplier": tr.num(args[2], line), "source": tr.source(args[0], line, tf)}
        outs, ind = ("middle", "upper", "lower"), "bbands"
    elif fn == "ta.dmi":
        n, sm = int(tr.num(args[0], line)), int(tr.num(args[1], line))
        if n != sm:
            raise PineError(line, "ta.dmi is supported only when the DI length equals the ADX smoothing length")
        params, outs, ind = {"length": n}, ("plus_di", "minus_di", "adx"), "adx"
    elif fn == "ta.supertrend":
        params, outs, ind = {"multiplier": tr.num(args[0], line), "length": int(tr.num(args[1], line))}, ("line", "direction"), "supertrend"
    else:
        raise PineError(line, f"tuple result of {fn}() is not supported")
    if len(names) != len(outs):
        raise PineError(line, f"{fn} returns {len(outs)} values, {len(names)} names given")
    alias = tr.decl(ind, params, tf)
    for nm, out in zip(names, outs):
        if nm == "_":
            continue
        op = IndOperand(ref=alias, output=out)
        if fn == "ta.supertrend" and out == "direction":  # Pine: -1 = uptrend; the library uses +1 = uptrend
            op = ArithOperand(op="-", left=ConstOperand(value=0.0), right=op)
        tr.vars[nm] = op
