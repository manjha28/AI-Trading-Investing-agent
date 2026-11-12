"""Sandbox worker. Runs in a separate interpreter started with `python -I -S` (no site-packages, no env, no user site).

Reads one JSON request on stdin, writes one JSON response on stdout. User code is exec'd with a whitelist of builtins and a
whitelist of module proxies. This is defence in depth for a single-user tool, NOT container-grade isolation: the parent also
applies an AST screen, a scrubbed environment, an empty temp cwd, rlimits and a wall-clock kill. See app/services/intraday_python.py.
"""
import builtins
import json
import resource
import sys
import types

ALLOWED_MODULES = ("math", "statistics", "itertools", "collections", "functools", "datetime", "decimal", "fractions", "typing",
                   "dataclasses", "enum")
SAFE_BUILTINS = ("abs all any bool dict divmod enumerate filter float format int isinstance issubclass iter len list map max min next "
                 "object pow property range repr reversed round set slice sorted staticmethod classmethod str sum super tuple zip "
                 "ArithmeticError AssertionError AttributeError Exception IndexError KeyError KeyboardInterrupt LookupError NotImplementedError "
                 "OverflowError RuntimeError StopIteration TypeError ValueError ZeroDivisionError True False None").split()


def _limits(cpu_seconds, memory_bytes):
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (8, 8))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ValueError, OSError):
        pass


def _proxy(name):
    mod = __import__(name)
    return types.SimpleNamespace(**{k: v for k, v in vars(mod).items() if not k.startswith("_") and not isinstance(v, types.ModuleType)})


class View:
    """Read-only sequence limited to bars up to the current one: no future access is possible."""
    def __init__(self, values, cursor):
        self._v, self._c = values, cursor

    def __len__(self):
        return self._c[0] + 1

    def __getitem__(self, k):
        i = self._c[0]
        if isinstance(k, slice):
            raise TypeError("slicing is not supported; index with [-1], [-2], ...")
        if k > 0:
            raise IndexError("only the current bar and earlier bars are visible; use negative indexes")
        j = i + k
        if j < 0:
            raise IndexError("not enough history")
        return self._v[j]


class Ctx:
    def __init__(self, data, cursor):
        self._c = cursor
        for name in ("open", "high", "low", "close", "volume"):
            setattr(self, name, View(data[name], cursor))
        self.time = View(data["time"], cursor)
        self.ind = {a: {o: View(v, cursor) for o, v in outs.items()} for a, outs in data["indicators"].items()}
        self.position = "flat"

    @property
    def index(self):
        return self._c[0]


def main():
    req = json.loads(sys.stdin.read())
    _limits(int(req["cpu_seconds"]), int(req["memory_bytes"]))

    real_import = builtins.__import__
    proxies = {}

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".")[0]
        if level != 0 or name not in ALLOWED_MODULES:
            raise ImportError("import of %r is not allowed in a strategy" % name)
        if top not in proxies:
            proxies[top] = _proxy(top)
        return proxies[top]

    safe = {k: getattr(builtins, k) for k in SAFE_BUILTINS if hasattr(builtins, k)}
    safe["__import__"] = guarded_import
    safe["__build_class__"] = builtins.__build_class__
    safe["__name__"] = "strategy"
    ns = {"__builtins__": safe, "__name__": "strategy"}
    exec(compile(req["source"], "<strategy>", "exec"), ns)
    cls = ns.get(req["entrypoint"])
    if cls is None:
        raise ValueError("entrypoint class %r not found" % req["entrypoint"])
    if req["mode"] == "describe":
        meta = {k: getattr(cls, k, None) for k in ("NAME", "DESCRIPTION", "DECISION_TIMEFRAME", "INDICATORS", "PARAMS", "ALLOW_SHORT", "MARKETS")}
        json.dump({"ok": True, "meta": meta}, sys.stdout)
        return
    data = req["data"]
    n = len(data["time"])
    cursor = [0]
    ctx = Ctx(data, cursor)
    strat = cls(dict(req.get("params") or {}))
    out = {k: [False] * n for k in ("long_entry", "short_entry", "long_exit", "short_exit")}
    for i in range(n):
        cursor[0] = i
        acts = strat.on_bar(ctx)
        if acts is None:
            continue
        if isinstance(acts, str):
            acts = [acts]
        for a in acts:
            if a == "enter_long":
                out["long_entry"][i] = True
                ctx.position = "long"
            elif a == "enter_short":
                out["short_entry"][i] = True
                ctx.position = "short"
            elif a == "exit_long" or (a == "exit" and ctx.position == "long"):
                out["long_exit"][i] = True
                ctx.position = "flat"
            elif a == "exit_short" or (a == "exit" and ctx.position == "short"):
                out["short_exit"][i] = True
                ctx.position = "flat"
            else:
                raise ValueError("unknown action %r (use enter_long, enter_short, exit_long, exit_short, exit)" % (a,))
    json.dump({"ok": True, "signals": out}, sys.stdout)


try:
    main()
except BaseException as e:  # noqa: BLE001 - report every failure to the parent as data
    line = None
    tb = e.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<strategy>":
            line = tb.tb_lineno
        tb = tb.tb_next
    msg = "%s: %s" % (type(e).__name__, e)
    if isinstance(e, MemoryError):
        msg = "MemoryError: memory limit exceeded"
    sys.stdout.write(json.dumps({"ok": False, "error": msg[:500], "line": line}))
