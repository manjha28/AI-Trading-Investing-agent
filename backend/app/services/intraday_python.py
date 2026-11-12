"""Python strategy import: static screening, isolated execution and signal generation.

Security model (single-user tool; defence in depth, NOT a container boundary - stated honestly in the UI and report):
1. Static AST screen with line numbers: allow-listed imports only, no underscore attributes, no eval/exec/open/getattr/etc.
2. The code runs in a separate interpreter (``python -I -S``) with a scrubbed environment (no secrets, no PATH), an empty temp cwd,
   its own session, rlimits (CPU, address space, no file writes, 8 fds, no forks) and a wall-clock kill.
3. Inside the worker, user code only sees whitelisted builtins and proxies of allow-listed stdlib modules (no module attributes).
4. Data in is OHLCV + indicator series precomputed by the trusted library; history is exposed through read-only views limited to the
   current bar, so a strategy cannot read the future. Data out is four boolean lists, schema-checked here.
The main process never imports or executes the uploaded code.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.services import intraday_indicators as ti
from app.services.intraday_strategy import (IndicatorDecl, PythonSpec, Source, StrategyDefinition, EvalResult, _Evaluator)
from app.services.timeframes import MarketProfile

WORKER = Path(__file__).resolve().parent.parent / "sandbox" / "worker.py"
ALLOWED_IMPORTS = ("math", "statistics", "itertools", "collections", "functools", "datetime", "decimal", "fractions", "typing", "dataclasses", "enum")
BANNED_NAMES = {"eval", "exec", "compile", "open", "globals", "locals", "vars", "getattr", "setattr", "delattr", "__import__", "input", "breakpoint",
                "dir", "type", "memoryview", "exit", "quit", "help", "id", "hash", "bytearray", "bytes", "callable", "classmethod_"}
MAX_SOURCE_BYTES = 100_000
DEFAULT_TIMEOUT = 20
DEFAULT_MEMORY = 512 * 1024 * 1024
MAX_OUTPUT = 20_000_000

TEMPLATE = '''"""Example: EMA crossover confirmed by RSI. Runs inside the Signals sandbox.

Interface
  class Strategy:
      NAME, DESCRIPTION            optional strings
      DECISION_TIMEFRAME = "5m"    timeframe whose completed candles drive on_bar
      INDICATORS = [...]           indicators computed by the trusted library: {"alias", "indicator", "params", "timeframe"}
      PARAMS = {...}               optional defaults, shown as editable parameters
      ALLOW_SHORT = False
      def __init__(self, params)   params = PARAMS merged with your overrides
      def on_bar(self, ctx)        called once per COMPLETED decision candle, oldest to newest
  ctx.open/high/low/close/volume   read-only history: ctx.close[-1] is the current bar, ctx.close[-2] the previous one (no positive index)
  ctx.time[-1]                     candle open time (UTC ISO string)
  ctx.ind["alias"]["output"][-1]   indicator values (None until warm-up completes)
  ctx.position                     "flat" | "long" | "short" - what THIS strategy last asked for (intent, not a fill)
  return                           None, or one/many of: "enter_long", "enter_short", "exit_long", "exit_short", "exit"
Allowed imports: math, statistics, itertools, collections, functools, datetime, decimal, fractions, typing, dataclasses, enum.
No files, network, environment or other modules. Time and memory are limited.
"""


class Strategy:
    NAME = "EMA cross + RSI filter"
    DESCRIPTION = "Long when the fast EMA crosses above the slow EMA while RSI is above its threshold."
    DECISION_TIMEFRAME = "5m"
    PARAMS = {"rsi_min": 50}
    ALLOW_SHORT = False
    INDICATORS = [
        {"alias": "fast", "indicator": "ema", "params": {"length": 9}, "timeframe": "5m"},
        {"alias": "slow", "indicator": "ema", "params": {"length": 21}, "timeframe": "5m"},
        {"alias": "rsi", "indicator": "rsi", "params": {"length": 14}, "timeframe": "5m"},
    ]

    def __init__(self, params):
        self.rsi_min = params.get("rsi_min", 50)

    def on_bar(self, ctx):
        f, s, r = ctx.ind["fast"]["value"], ctx.ind["slow"]["value"], ctx.ind["rsi"]["value"]
        if len(f) < 2 or f[-1] is None or f[-2] is None or s[-1] is None or s[-2] is None or r[-1] is None:
            return None
        if ctx.position == "flat" and f[-2] <= s[-2] and f[-1] > s[-1] and r[-1] > self.rsi_min:
            return "enter_long"
        if ctx.position == "long" and f[-1] < s[-1]:
            return "exit_long"
        return None
'''


class SandboxError(Exception):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.message, self.line = message, line


def screen_source(source: str) -> list[dict[str, Any]]:
    """Static screen. Returns line-numbered errors (empty = passed). Never executes the code."""
    errors: list[dict[str, Any]] = []
    if len(source.encode()) > MAX_SOURCE_BYTES:
        return [{"line": None, "message": f"file larger than {MAX_SOURCE_BYTES // 1000} KB"}]
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [{"line": e.lineno, "message": f"syntax error: {e.msg}"}]

    def bad(node: ast.AST, msg: str) -> None:
        errors.append({"line": getattr(node, "lineno", None), "message": msg})

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    bad(node, f"import of {a.name!r} is not allowed (allowed: {', '.join(ALLOWED_IMPORTS)})")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_IMPORTS:
                bad(node, f"import from {node.module!r} is not allowed (allowed: {', '.join(ALLOWED_IMPORTS)})")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            bad(node, f"access to underscore attribute {node.attr!r} is not allowed")
        elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or (node.id.startswith("__") and node.id not in ("__name__",))):
            bad(node, f"use of {node.id!r} is not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            bad(node, "async code is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bad(node, "global/nonlocal statements are not allowed")
    return errors


def _run_worker(request: dict[str, Any], timeout: int, memory_bytes: int) -> dict[str, Any]:
    request = {**request, "cpu_seconds": timeout, "memory_bytes": memory_bytes}
    with tempfile.TemporaryDirectory(prefix="sandbox_") as cwd:
        proc = subprocess.Popen([sys.executable, "-I", "-S", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=cwd, env={}, close_fds=True, start_new_session=True)
        try:
            out, _ = proc.communicate(json.dumps(request).encode(), timeout=timeout + 2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise SandboxError(f"strategy exceeded the {timeout}s time limit and was killed") from None
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
    if proc.returncode != 0 and not out:
        reason = {-signal.SIGKILL: "killed (memory or CPU limit)", -signal.SIGXCPU: "CPU time limit exceeded", -signal.SIGSEGV: "crashed (memory limit)"}
        raise SandboxError(f"strategy process ended abnormally: {reason.get(proc.returncode, f'exit code {proc.returncode}')}")
    if len(out) > MAX_OUTPUT:
        raise SandboxError("strategy produced too much output")
    try:
        resp = json.loads(out)
    except ValueError:
        raise SandboxError("strategy produced invalid output") from None
    if not resp.get("ok"):
        raise SandboxError(resp.get("error", "strategy failed"), resp.get("line"))
    return resp


def describe(source: str, entrypoint: str = "Strategy", timeout: int = 10) -> dict[str, Any]:
    errors = screen_source(source)
    if errors:
        raise SandboxError(errors[0]["message"], errors[0]["line"])
    return _run_worker({"mode": "describe", "source": source, "entrypoint": entrypoint}, timeout, DEFAULT_MEMORY)["meta"]


def build_definition(source: str, filename: str, market: str, name: str | None = None, entrypoint: str = "Strategy") -> tuple[StrategyDefinition, dict[str, Any]]:
    """Describe the uploaded class in the sandbox and translate its declarations into a validated StrategyDefinition."""
    meta = describe(source, entrypoint)
    decls = []
    for raw in meta.get("INDICATORS") or []:
        decls.append(IndicatorDecl.model_validate(raw))
    sha = hashlib.sha256(source.encode()).hexdigest()
    defn = StrategyDefinition(
        name=(name or meta.get("NAME") or Path(filename).stem)[:120], description=(meta.get("DESCRIPTION") or "")[:500], market=market,
        decision_timeframe=meta.get("DECISION_TIMEFRAME") or "5m", indicators=decls, python=PythonSpec(source_sha256=sha, entrypoint=entrypoint),
        constraints={"allow_long": True, "allow_short": bool(meta.get("ALLOW_SHORT"))},
        source=Source(type="python", artifact={"filename": filename, "sha256": sha, "size": len(source.encode())},
                      provenance={"params": meta.get("PARAMS") or {}, "isolation": "subprocess + AST screen + rlimits (not container-grade)"}))
    return defn, meta


def evaluate_python(defn: StrategyDefinition, source: str, base: pd.DataFrame, profile: MarketProfile, params: dict[str, Any] | None = None,
                    timeout: int = DEFAULT_TIMEOUT) -> EvalResult:
    """Run the sandboxed strategy over completed decision candles and return signals on the base index (same shape as the builder)."""
    if defn.python is None or hashlib.sha256(source.encode()).hexdigest() != defn.python.source_sha256:
        raise SandboxError("stored source does not match the strategy version's recorded hash")
    errors = screen_source(source)
    if errors:
        raise SandboxError(errors[0]["message"], errors[0]["line"])
    ev = _Evaluator(defn, base, profile)
    d = ev.dec
    n = ev.n
    signals = {k: np.zeros(n, dtype=bool) for k in ("long_entry", "short_entry", "long_exit", "short_exit")}
    if len(d) == 0:
        return EvalResult(ev.base["time"], d, signals, {}, {}, ev.frames, [])
    from app.services.intraday_strategy import IndOperand
    indicators: dict[str, dict[str, list[float | None]]] = {}
    for decl in defn.indicators:
        outs = {}
        for out in ti.REGISTRY[decl.indicator].outputs:
            arr = ev.indicator(IndOperand(ref=decl.alias, output=out))[d]
            outs[out] = [None if np.isnan(v) else float(v) for v in arr]
        indicators[decl.alias] = outs
    f = ev.base.iloc[d]
    # the strategy sees the DECISION timeframe's completed candle (not the single closing minute)
    sub = ev.frame(defn.decision_timeframe).set_index("bar_end").reindex(pd.DatetimeIndex(f["time"] + pd.Timedelta(minutes=1)))
    data = {"time": [t.isoformat() for t in sub["time"]], **{c: [float(v) for v in sub[c]] for c in ("open", "high", "low", "close", "volume")},
            "indicators": indicators}
    resp = _run_worker({"mode": "run", "source": source, "entrypoint": defn.python.entrypoint, "params": params or defn.source.provenance.get("params", {}), "data": data},
                       timeout, DEFAULT_MEMORY)
    sig = resp["signals"]
    for k in signals:
        if not isinstance(sig.get(k), list) or len(sig[k]) != len(d) or not all(isinstance(x, bool) for x in sig[k]):
            raise SandboxError("strategy output failed schema validation")
        signals[k][d] = np.array(sig[k], dtype=bool)
    return EvalResult(ev.base["time"], d, signals, {}, {}, ev.frames, [])
