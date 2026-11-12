"""Analyse captured Dhan Company Info responses against stored prices/statements. Read-only; writes evidence.json.

    cd backend && python scripts/dhan_company_info_analyse.py
"""
import json
import statistics
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Instrument, InstrumentFundamental, InstrumentProviderIdentifier, InstrumentSnapshot, MarketBar  # noqa: E402

BASE = Path(__file__).resolve().parents[2] / "diagnostics" / "dhan_company_info"
RAW = BASE / "raw"
SECS = {"RELIANCE": "2885", "HDFCBANK": "1333", "SBIN": "3045", "UJJIVANSFB": "15228", "SBILIFE": "21808", "PNBHOUSING": "18908", "BHARTIARTL": "10604",
        "BEL": "383", "JSWSTEEL": "11723", "NTPC": "11630", "EASEMYTRIP": "2792", "DALBHARAT": "8075", "IPCALAB": "1633", "GODREJCP": "10099",
        "BHARATFORG": "422", "JPL": "766830", "PREMIERPOL": "3908"}
CR = Decimal("10000000")


def load(name):
    return json.loads((RAW / f"{name}.json").read_text())


def flat(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from flat(v, f"{path}.{k}" if path else k)
    else:
        yield path, x


evidence: dict = {"per_security": {}, "consistency": {}, "edges": {}, "inventory": {}}
with Session(engine) as db:
    for symbol, sid in SECS.items():
        combined = load(f"{symbol}_COMBINED")["response_json"]["data"]
        co, ra, sh = combined["CO"], combined["RATIOS"], combined["SHP"]
        row = {"CO": co, "RATIOS": ra, "SHP": sh}
        inst_id = db.scalar(select(InstrumentProviderIdentifier.instrument_id).where(
            InstrumentProviderIdentifier.provider == "dhan", InstrumentProviderIdentifier.provider_instrument_id.in_([f"{sid}|NSE_EQ", sid])))
        row["db"] = None
        if inst_id:
            bar = db.scalar(select(MarketBar).where(MarketBar.instrument_id == inst_id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False)).order_by(MarketBar.timestamp.desc()).limit(1))
            periods = db.scalars(select(InstrumentFundamental).where(InstrumentFundamental.instrument_id == inst_id).order_by(InstrumentFundamental.period_end.desc())).all()
            snap = db.scalar(select(InstrumentSnapshot).where(InstrumentSnapshot.instrument_id == inst_id))
            shares = next((p.shares_outstanding for p in periods if p.shares_outstanding), None)
            annual = next((p for p in periods if p.period_type == "annual" and p.revenue), None)
            q4 = [p for p in periods if p.period_type == "quarterly" and p.eps is not None][:4]
            row["db"] = {"price": str(bar.close) if bar else None, "price_date": bar.timestamp.date().isoformat() if bar else None,
                         "shares": str(shares) if shares else None, "snapshot_mcap_cr": str(snap.market_cap / CR) if snap and snap.market_cap else None,
                         "snapshot_pe": str(snap.pe) if snap and snap.pe is not None else None, "snapshot_pb": str(snap.pb) if snap and snap.pb is not None else None,
                         "annual_period": annual.period_end.isoformat() if annual else None, "annual_revenue_cr": str(annual.revenue / CR) if annual else None,
                         "annual_opm_pct": str(annual.operating_margin) if annual and annual.operating_margin is not None else None,
                         "annual_equity_cr": str(annual.total_equity / CR) if annual and annual.total_equity else None,
                         "annual_debt_cr": str(annual.total_debt / CR) if annual and annual.total_debt is not None else None,
                         "ttm_eps_4q": str(sum(p.eps for p in q4)) if len(q4) == 4 else None}
        evidence["per_security"][symbol] = row
        # separate vs combined consistency
        for group in ("CO", "RATIOS", "SHP"):
            sep = load(f"{symbol}_{group}")
            sep_data = (sep["response_json"] or {}).get("data", {})
            evidence["consistency"].setdefault(symbol, {})[group] = {
                "separate_status": sep["status_code"], "separate_group_keys": sorted(sep_data.keys()),
                "values_equal_combined": sep_data.get(group) == combined[group]}
    for name in sorted(p.stem for p in RAW.glob("edge_*.json")):
        r = load(name)
        evidence["edges"][name] = {"body": r["request"]["body"], "status": r["status_code"], "response": r.get("response_json", r.get("response_text"))}
(BASE / "evidence.json").write_text(json.dumps(evidence, indent=1, default=str))

print("== separate vs combined consistency ==")
bad = [(s, g, v) for s, d in evidence["consistency"].items() for g, v in d.items() if not v["values_equal_combined"] or v["separate_group_keys"] != [g]]
print("mismatches:", bad or "none (every separate request returned only its own group with values identical to the combined request)")

print("\n== checks vs stored data (price date, price, MCAP Cr: dhan | price x shares | yahoo) ==")
rows = []
for s, r in evidence["per_security"].items():
    d, co, ra, sh = r["db"], r["CO"], r["RATIOS"], r["SHP"]
    price = Decimal(d["price"]) if d and d["price"] else None
    out = {"sym": s, "price_date": d and d["price_date"]}
    if price and d["shares"]:
        out["mcap_dhan_vs_price_x_shares"] = round(co["MARKET_CAP"] / float(price * Decimal(d["shares"]) / CR), 3)
    if d and d["snapshot_mcap_cr"]:
        out["mcap_dhan_vs_yahoo"] = round(co["MARKET_CAP"] / float(Decimal(d["snapshot_mcap_cr"])), 3)
    if price:
        out["pb_vs_price_over_bookvalue"] = round(ra["PB_RATIO"] / float(price / Decimal(str(co["BOOK_VALUE"]))), 3) if co["BOOK_VALUE"] else None
        out["pe_vs_price_over_eps"] = round(ra["PE_RATIO"] / float(price / Decimal(str(co["EPS"]))), 3) if co["EPS"] else None
        out["price_in_52w"] = co["FIFTY_TWO_WEEK_LOW"] <= float(price) <= co["FIFTY_TWO_WEEK_HIGH"]
    if d and d["snapshot_pe"]:
        out["pe_dhan_vs_yahoo"] = round(ra["PE_RATIO"] / float(d["snapshot_pe"]), 3)
    if d and d["snapshot_pb"]:
        out["pb_dhan_vs_yahoo"] = round(ra["PB_RATIO"] / float(d["snapshot_pb"]), 3)
    if d and d["ttm_eps_4q"]:
        out["eps_dhan_vs_ttm_eps_4q"] = round(co["EPS"] / float(d["ttm_eps_4q"]), 3)
    if d and d["annual_revenue_cr"] and d["annual_opm_pct"]:
        out["ebitda_dhan_vs_rev_x_opm"] = round(co["EBITDA"] / (float(d["annual_revenue_cr"]) * float(d["annual_opm_pct"]) / 100), 3) if float(d["annual_opm_pct"]) else None
    if d and d["annual_equity_cr"] and d["annual_debt_cr"] and float(d["annual_equity_cr"]) > 0:
        out["debt_to_equity_dhan_vs_stored"] = round(ra["DEBT_TO_EQUITY"] / (float(d["annual_debt_cr"]) / float(d["annual_equity_cr"])), 3) if float(d["annual_debt_cr"]) else None
    out["shp_sum"] = round(sh["PROMOTER_HOLDING"] + sh["DII_HOLDING"] + sh["FII_HOLDING"] + sh["PUBLIC_HOLDING"], 2)
    out["chg_fii_plus_dii"] = round((sh["CHANGE_IN_FII_HOLDING"] or 0) + (sh["CHANGE_IN_DII_HOLDING"] or 0), 2) if sh["CHANGE_IN_FII_HOLDING"] is not None and sh["CHANGE_IN_DII_HOLDING"] is not None else None
    out["implied_sector_pe"] = round(ra["PE_RATIO"] / ra["PE_TO_SECTOR_PE"], 2) if ra["PE_TO_SECTOR_PE"] else None
    out["sector"] = co["SECTOR"]
    out["industry"] = co["INDUSTRY"]
    rows.append(out)
for o in rows:
    print({k: v for k, v in o.items()})
print("\n== edge cases ==")
for n, e in evidence["edges"].items():
    print(n, e["status"], json.dumps(e["response"])[:260])
print("\nmedians (ratio near 1.0 means agreement):")
for key in ("mcap_dhan_vs_price_x_shares", "mcap_dhan_vs_yahoo", "pb_vs_price_over_bookvalue", "pe_vs_price_over_eps", "pe_dhan_vs_yahoo", "pb_dhan_vs_yahoo", "eps_dhan_vs_ttm_eps_4q", "ebitda_dhan_vs_rev_x_opm", "debt_to_equity_dhan_vs_stored"):
    vals = [o[key] for o in rows if o.get(key) is not None]
    print(f"{key:34s} n={len(vals):2d}", f"median={statistics.median(vals):.3f} min={min(vals):.3f} max={max(vals):.3f}" if vals else "")
