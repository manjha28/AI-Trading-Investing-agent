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


# ---- the kind is stored on the instrument row (test data: a hand-made master and a throw-away sqlite file) ----
import json
import subprocess
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Instrument


def _inst(symbol, **kw):
    return Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=datetime.now(timezone.utc), **kw)


def test_sync_stores_fund_and_stock_and_leaves_unknown_unclassified(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/k.db")
    Base.metadata.create_all(eng)
    with Session(eng) as db:
        db.add_all([_inst("NIFTYBEES"), _inst("TCS"), _inst("NOTINMASTER")])
        db.commit()
        out = ik.sync_kinds(db, rows=MASTER)
        kinds = {i.symbol: i.instrument_kind for i in db.query(Instrument)}
    assert kinds == {"NIFTYBEES": "fund", "TCS": "stock", "NOTINMASTER": None} and out["updated"] == 2


def test_a_fresh_process_without_the_master_still_excludes_a_known_etf(tmp_path):
    db_file = tmp_path / "fresh.db"
    eng = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(eng)
    with Session(eng) as db:
        db.add_all([_inst("NIFTYBEES"), _inst("TCS")])
        db.commit()
        ik.sync_kinds(db, rows=MASTER)
    code = (
        "import json; from sqlalchemy import create_engine; from sqlalchemy.orm import Session\n"
        "from app.broker_adapters import DhanAdapter; from app.models import Instrument; from app.services import instrument_kind as ik\n"
        f"assert DhanAdapter._scrip_master_cache is None\n"
        f"eng = create_engine('sqlite:///{db_file}')\n"
        "with Session(eng) as db:\n"
        "    print(json.dumps({i.symbol: ik.is_fund(i) for i in db.query(Instrument)}))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
                       env={**__import__("os").environ, "DATABASE_URL": "sqlite://"})
    assert r.returncode == 0, r.stderr[-500:]
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"NIFTYBEES": True, "TCS": False}


def test_a_fresh_process_research_universe_excludes_a_known_etf(tmp_path):
    """The research universe (the one the nightly verdict job iterates) is built in a NEW process with no Dhan master loaded: the ETF on the watchlist is left out because its kind is stored on the row."""
    from app.models import User, Watchlist, WatchlistInstrument
    db_file = tmp_path / "universe.db"
    eng = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(eng)
    uid = None
    with Session(eng) as db:
        u = User(email="kind@example.invalid", password_hash="x", email_verified=True, created_at=datetime.now(timezone.utc))
        db.add(u)
        etf, stock = _inst("NIFTYBEES"), _inst("TCS")
        db.add_all([etf, stock])
        db.flush()
        wl = Watchlist(user_id=u.id, name="w", created_at=datetime.now(timezone.utc))
        db.add(wl)
        db.flush()
        db.add_all([WatchlistInstrument(user_id=u.id, watchlist_id=wl.id, instrument_id=i.id, created_at=datetime.now(timezone.utc)) for i in (etf, stock)])
        db.commit()
        ik.sync_kinds(db, rows=MASTER)
        uid = str(u.id)
    code = ("import json, uuid\nfrom sqlalchemy import create_engine\nfrom sqlalchemy.orm import Session\nfrom app.broker_adapters import DhanAdapter\nfrom app.research_daily import user_universe\n"
            "assert DhanAdapter._scrip_master_cache is None\n"
            f"with Session(create_engine('sqlite:///{db_file}')) as db:\n    print(json.dumps([i.symbol for i in user_universe(db, uuid.UUID('{uid}'), include_master=False)]))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]), env={**__import__("os").environ, "DATABASE_URL": "sqlite://"})
    assert r.returncode == 0, r.stderr[-600:]
    assert json.loads(r.stdout.strip().splitlines()[-1]) == ["TCS"]
