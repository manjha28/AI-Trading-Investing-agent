from app.dhan_execution import policy
from app.services.intraday_strategy import StrategyDefinition, validate_definition

BASE = {"name": "x", "universe": [], "decision_timeframe": "5m"}


def issues(extra):
    return [i["message"] for i in validate_definition(StrategyDefinition.model_validate({**BASE, **extra}))[0]]


def test_always_true_condition_and_missing_stop_are_flagged():
    always = {"type": "compare", "left": {"kind": "price", "field": "close", "timeframe": "5m"}, "op": ">", "right": {"kind": "const", "value": 0}}
    found = issues({"long_entry": always})
    assert any("always true" in m for m in found) and any("no stop-loss" in m for m in found)
    real = {**always, "right": {"kind": "const", "value": 100}}
    assert not any("always true" in m for m in issues({"long_entry": real, "stop": {"mode": "percent", "value": 1}}))


def test_own_strategies_can_never_be_on_the_live_allowlist():
    assert not any(key.startswith("own:") for key in policy.ALLOWED_STRATEGY_KEYS)
