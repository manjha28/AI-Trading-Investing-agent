"""Pine import error diagnosis. The script below is TEST DATA written to exercise the constructs a stateful strategy uses; it is not a user file."""
from app.services.pine_import import import_pine

STATEFUL = """//@version=5
strategy("test stateful", overlay=true)
len = input.int(20, "len")
useBias = input.bool(true, "bias")
var float cum = 0.0
var float[] profile = array.new_float(10, 0.0)
f(x) =>
    x * 2
for i = 0 to 9
    cum := cum + array.get(profile, i)
    profile.set(i, cum)
bias = request.security(syminfo.tickerid, "D", close)
score = cum + f(close) + bias
long = score > cum and close > ta.sma(close, len)
if long
    strategy.entry("L", strategy.long)
"""


def test_cascades_collapse_under_their_root_cause_and_blocking_constructs_route_to_python():
    r = import_pine(STATEFUL, "t.pine")
    d = r["diagnosis"]
    assert not r["can_import"]
    assert d["total_errors"] == len(r["errors"]) >= 8
    assert d["root_cause_count"] < d["total_errors"]            # follow-ons are not counted as roots
    assert d["follow_on_count"] >= 3
    keys = {g["key"] for g in d["groups"]}
    assert {"state", "functions", "loops"} <= keys
    state = next(g for g in d["groups"] if g["key"] == "state")
    assert state["example_line"] == 5 and state["follow_on_count"] >= 1
    assert d["verdict"].startswith("Cannot import: needs ") and "persistent state" in d["verdict"] and "Python strategy path" in d["verdict"]
    assert d["route"]["to"] == "python" and any("20 second / 512 MB" in x for x in d["route"]["limits"])
    # the original per-line errors are all still reported
    assert all("line" in e and "message" in e for e in r["errors"])


def test_non_blocking_failures_get_a_verdict_without_the_python_route():
    r = import_pine('//@version=5\nstrategy("t")\na = close > open ? 1 : 0\nif close > open\n    strategy.entry("L", strategy.long)\n', "t.pine")
    d = r["diagnosis"]
    assert d["route"] is None and "ternary" in d["verdict"]


def test_importable_script_has_no_verdict():
    r = import_pine('//@version=5\nstrategy("t")\nif ta.crossover(close, ta.sma(close, 5))\n    strategy.entry("L", strategy.long)\n', "t.pine")
    assert r["can_import"] and r["diagnosis"]["verdict"] is None and r["diagnosis"]["groups"] == []
