"""Pine subset importer and Python sandbox security."""
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from app.services import intraday_python as ip
from app.services.intraday_strategy import StrategyDefinition, evaluate, validate_definition
from app.services.pine_import import import_pine
from app.services.timeframes import EQUITY_IN

GOOD_PINE = '''//@version=5
strategy("EMA RSI", overlay=true, default_qty_type=strategy.percent_of_equity, commission_value=0.1)
fastLen = input.int(9, "Fast")
slowLen = input.int(21, "Slow")
fast = ta.ema(close, fastLen)
slow = ta.ema(close, slowLen)
rsi = ta.rsi(close, 14)
htf = request.security(syminfo.tickerid, "15", ta.ema(close, 50))
longCond = ta.crossover(fast, slow) and rsi > 50 and close > htf
[m, s, h] = ta.macd(close, 12, 26, 9)
if longCond
    strategy.entry("L", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("L")
plot(fast)
'''


def codes(report):
    return {(e["line"], e["message"]) for e in report["errors"]}


def test_supported_pine_translates_and_reports_partial_items():
    r = import_pine(GOOD_PINE, "ema.pine")
    assert r["can_import"] and r["compatibility"] == "partial" and r["equivalence_validated"] is False
    statuses = {(i["construct"], i["status"]) for i in r["items"]}
    assert ("request.security", "partial") in statuses and ("plot", "partial") in statuses and ("strategy.entry", "supported") in statuses
    assert any("default_qty_type" in i["message"] for i in r["items"] if i["construct"] == "strategy()")
    d = StrategyDefinition.model_validate(r["definition"])
    assert validate_definition(d)[1] is not None and d.source.type == "pine" and d.source.artifact["sha256"] == r["sha256"]
    tfs = {i.timeframe for i in d.indicators}
    assert tfs == {"5m", "15m"} and "Not validated against TradingView" in r["equivalence_note"]


def test_imported_pine_strategy_runs_in_the_engine():
    r = import_pine(GOOD_PINE, "ema.pine")
    d = StrategyDefinition.model_validate(r["definition"])
    t = pd.date_range("2025-01-06 03:45", periods=375 * 3, freq="min", tz="UTC")
    c = 100 + np.cumsum(np.random.default_rng(4).normal(0, 0.15, len(t)))
    f = pd.DataFrame({"time": t, "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 500.0})
    f = pd.concat([f.iloc[:375], f.iloc[375:]])  # (3 contiguous sessions would need day gaps; session logic ignores gaps between days)
    assert evaluate(d, f.reset_index(drop=True), EQUITY_IN).signals["long_entry"].dtype == bool


@pytest.mark.parametrize("src,line,fragment", [
    ("//@version=5\nstrategy('x')\nvar x = 0\n", 3, "'var' is not supported"),
    ("//@version=5\nstrategy('x')\nfor i = 0 to 3\n    a = 1\n", 3, "'for' is not supported"),
    ("//@version=5\nstrategy('x')\nx = ta.tr\n", 3, "unknown or unsupported identifier"),
    ("//@version=5\nstrategy('x')\nx = ta.supertrend2(3,10)\n", 3, "not supported"),
    ("//@version=5\nstrategy('x')\nx = close > open ? 1 : 0\n", 3, "ternary"),
    ("//@version=5\nstrategy('x')\nif close > open\n    strategy.exit('e','L', stop=close*0.99)\n", 4, "strategy.exit() is not supported"),
    ("//@version=5\nstrategy('x')\nif close > open\n    strategy.entry('L', strategy.long, limit=close)\n", 4, "limit"),
    ("//@version=5\nstrategy('x')\nstrategy.entry('L', strategy.long)\n", 3, "unconditional"),
    ("//@version=5\nindicator('x')\n", 2, "indicator/library"),
    ("//@version=5\nstrategy('x')\nx = request.security(syminfo.tickerid, 'D', close)\n", 3, "timeframe"),
    ("//@version=5\nstrategy('x')\nx = request.security(syminfo.tickerid, '15', close, lookahead=barmerge.lookahead_on)\n", 3, "look-ahead"),
    ("//@version=5\nstrategy('x')\nx = nz(close)\n", 3, "not supported"),
    ("//@version=5\nstrategy('x')\nf(a) => a + 1\n", 3, "not supported"),
    ("//@version=5\nstrategy('x')\nx := 1\n", 3, "not supported"),
])
def test_unsupported_constructs_are_rejected_with_line_numbers(src, line, fragment):
    r = import_pine(src, "x.pine")
    assert not r["can_import"] and r["definition"] is None and r["compatibility"] == "unsupported"
    assert any(e["line"] == line and fragment in e["message"] for e in r["errors"]), r["errors"]


def test_pine_version_and_empty_scripts_are_rejected():
    assert "version 4 is not supported" in import_pine("//@version=4\nstrategy('x')", "a.pine")["errors"][0]["message"]
    assert not import_pine("strategy('x')", "a.pine")["can_import"]
    r = import_pine("//@version=5\nstrategy('x')\nplot(close)\n", "a.pine")
    assert not r["can_import"] and any("no strategy.entry" in e["message"] for e in r["errors"])


def test_pine_supertrend_direction_sign_is_inverted_for_pine_convention():
    src = "//@version=5\nstrategy('x')\n[st, dir] = ta.supertrend(3, 10)\nif dir < 0\n    strategy.entry('L', strategy.long)\n"
    d = import_pine(src, "s.pine")["definition"]
    assert '"op": "-"' in __import__("json").dumps(d["long_entry"])


# ───────────── Python sandbox ─────────────
@pytest.mark.parametrize("src,fragment", [
    ("import os\n", "import of 'os'"),
    ("import subprocess\n", "import of 'subprocess'"),
    ("from socket import socket\n", "not allowed"),
    ("import math\nx = math.__loader__\n", "underscore attribute"),
    ("x = ().__class__.__bases__\n", "underscore attribute"),
    ("x = open('/etc/passwd')\n", "use of 'open'"),
    ("x = getattr(object, 'a')\n", "use of 'getattr'"),
    ("eval('1')\n", "use of 'eval'"),
    ("exec('1')\n", "use of 'exec'"),
    ("x = __import__('os')\n", "__import__"),
    ("def f(:\n", "syntax error"),
])
def test_static_screen_blocks_dangerous_code_with_line_numbers(src, fragment):
    errs = ip.screen_source(src)
    assert errs and fragment in errs[0]["message"] and errs[0]["line"] is not None


def test_template_screens_clean_and_builds_a_valid_definition():
    assert ip.screen_source(ip.TEMPLATE) == []
    d, meta = ip.build_definition(ip.TEMPLATE, "t.py", "equity_in")
    assert d.source.type == "python" and d.python.source_sha256 and validate_definition(d)[1] is not None
    assert [i.alias for i in d.indicators] == ["fast", "slow", "rsi"] and meta["DECISION_TIMEFRAME"] == "5m"


def frames():
    t = pd.date_range("2025-01-06 03:45", periods=375 * 2, freq="min", tz="UTC")
    c = 100 + np.cumsum(np.random.default_rng(9).normal(0, 0.2, len(t)))
    return pd.DataFrame({"time": t, "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 500.0})


def test_python_strategy_produces_signals_matching_builder_semantics():
    f = frames()
    d, _ = ip.build_definition(ip.TEMPLATE, "t.py", "equity_in")
    res = ip.evaluate_python(d, ip.TEMPLATE, f, EQUITY_IN)
    assert res.signals["long_entry"].sum() > 0
    assert set(np.flatnonzero(res.signals["long_entry"])) <= set(res.decision_idx.tolist())
    # prefix invariance: the sandbox only ever sees completed candles, so cutting the future changes nothing earlier
    part = ip.evaluate_python(d, ip.TEMPLATE, f.iloc[:500].reset_index(drop=True), EQUITY_IN)
    assert np.array_equal(res.signals["long_entry"][:500], part.signals["long_entry"])


def test_python_cannot_peek_at_the_future():
    src = ip.TEMPLATE.replace('return "enter_long"', 'return "enter_long" if ctx.close[1] else None').replace("if ctx.position == \"flat\" and f[-2]", "if ctx.close[1] or ctx.position == \"flat\" and f[-2]")
    d, _ = ip.build_definition(ip.TEMPLATE, "t.py", "equity_in")
    d = d.model_copy(update={"python": d.python.model_copy(update={"source_sha256": __import__("hashlib").sha256(src.encode()).hexdigest()})})
    with pytest.raises(ip.SandboxError, match="only the current bar"):
        ip.evaluate_python(d, src, frames(), EQUITY_IN)


def raw(src, *, t=3, mem=256 * 1024 * 1024):
    payload = {"mode": "run", "source": src, "entrypoint": "Strategy", "params": {},
               "data": {"time": ["a"] * 3, "open": [1] * 3, "high": [1] * 3, "low": [1] * 3, "close": [1] * 3, "volume": [1] * 3, "indicators": {}}}
    return ip._run_worker(payload, t, mem)


BODY = "class Strategy:\n def __init__(self, p): pass\n def on_bar(self, ctx):\n"


@pytest.mark.parametrize("code,fragment", [
    ("  import os\n", "not allowed"),
    ("  import socket\n", "not allowed"),
    ("  import math\n  return math.os\n", "no attribute"),
    ("  return open('/etc/passwd')\n", "not defined"),
    ("  return eval('1')\n", "not defined"),
    ("  return __import__('os')\n", "not allowed"),
    ("  import statistics\n  return statistics.random\n", "no attribute"),
    ("  return exec('x=1')\n", "not defined"),
])
def test_runtime_whitelist_blocks_bypasses_even_without_the_static_screen(code, fragment):
    with pytest.raises(ip.SandboxError, match=fragment):
        raw(BODY + code)


def test_resource_limits_kill_runaway_code():
    with pytest.raises(ip.SandboxError, match="time limit|killed|CPU"):
        raw(BODY + "  while True:\n   pass\n", t=2)
    with pytest.raises(ip.SandboxError, match="[Mm]emory"):
        raw(BODY + "  x = [0] * (10 ** 10)\n", mem=128 * 1024 * 1024)
    with pytest.raises(ip.SandboxError, match="unknown action"):
        raw(BODY + "  return 'buy_everything'\n")


def test_worker_is_started_with_scrubbed_environment_and_isolated_interpreter(monkeypatch):
    monkeypatch.setenv("BROKER_SECRET", "must-not-leak")
    seen = {}
    real = subprocess.Popen

    def spy(args, **kw):
        seen.update(args=args, env=kw.get("env"), cwd=kw.get("cwd"), session=kw.get("start_new_session"))
        return real(args, **kw)

    monkeypatch.setattr(ip.subprocess, "Popen", spy)
    raw(BODY + "  return None\n")
    assert seen["env"] == {} and "-I" in seen["args"] and "-S" in seen["args"] and seen["session"] is True
    assert seen["cwd"] != os.getcwd() and os.path.basename(seen["cwd"]).startswith("sandbox_")


def test_bad_output_and_hash_mismatch_are_rejected():
    d, _ = ip.build_definition(ip.TEMPLATE, "t.py", "equity_in")
    with pytest.raises(ip.SandboxError, match="hash"):
        ip.evaluate_python(d, ip.TEMPLATE + "\n# edited\n", frames(), EQUITY_IN)
    with pytest.raises(ip.SandboxError):
        ip.build_definition("import os\n", "x.py", "equity_in")
    with pytest.raises(ip.SandboxError, match="not found"):
        ip.describe("class Other:\n pass\n")
