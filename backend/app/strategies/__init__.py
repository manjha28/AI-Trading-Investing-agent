"""Code-defined strategy registry. Importing this package loads the library (``library.py``)."""
from app.strategies.base import ParamSpec, Rules, StrategySpec  # noqa: F401
from app.strategies.registry import REGISTRY, all_specs, get, register  # noqa: F401

from app.strategies import library  # noqa: E402,F401  (registers the shipped strategies)
