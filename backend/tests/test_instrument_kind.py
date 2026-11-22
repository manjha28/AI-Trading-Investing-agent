"""Fund vs stock classification from the Dhan master ISIN (test data: a hand-made master)."""
from types import SimpleNamespace

from app.services import instrument_kind as ik

MASTER = [{"EXCH_ID": "NSE", "SEGMENT": "E", "UNDERLYING_SYMBOL": "NIFTYBEES", "ISIN": "INF204KB14I2"}, {"EXCH_ID": "NSE", "SEGMENT": "E", "UNDERLYING_SYMBOL": "TCS", "ISIN": "INE467B01029"},
          {"EXCH_ID": "BSE", "SEGMENT": "E", "UNDERLYING_SYMBOL": "BSEFUND", "ISIN": "INF000000001"}, {"EXCH_ID": "NSE", "SEGMENT": "E", "UNDERLYING_SYMBOL": "NOISIN", "ISIN": ""}]


def test_funds_are_found_by_isin_prefix_and_stocks_and_unknowns_are_not():
    ik.reset_cache()
    assert ik.fund_symbols(loader=lambda: MASTER) == {"NIFTYBEES"}
    assert ik.is_fund(SimpleNamespace(symbol="niftybees")) and not ik.is_fund(SimpleNamespace(symbol="TCS")) and not ik.is_fund(SimpleNamespace(symbol="NOISIN"))
    ik.reset_cache()


def test_an_unreachable_master_never_marks_anything_a_fund_and_keeps_the_previous_copy():
    ik.reset_cache()

    def boom():
        raise RuntimeError("down")
    assert ik.fund_symbols(loader=boom) == frozenset()
    ik.reset_cache()
    assert ik.fund_symbols(loader=lambda: MASTER) == {"NIFTYBEES"}
    ik._cache = (0.0, ik._cache[1])                   # expired
    assert ik.fund_symbols(loader=boom) == {"NIFTYBEES"}
    ik.reset_cache()
