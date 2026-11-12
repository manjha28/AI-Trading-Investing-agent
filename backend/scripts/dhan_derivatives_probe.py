"""Live Dhan connectivity probe across derivative categories, using the EXISTING stored connection.
Representative contracts are DERIVED from the public instrument master (nothing hardcoded). Prints status codes and payload SHAPES only: never tokens/headers.

    python scripts/dhan_derivatives_probe.py --master /path/api-scrip-master-detailed.csv --out ../diagnostics/dhan_derivatives_probe.json
"""
import argparse
import csv
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.brokers import _fresh_tokens  # noqa: E402
from app.broker_adapters import DhanAdapter  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import BrokerCredential  # noqa: E402


def shape(p, depth=0):
    if isinstance(p, dict):
        return {k: (shape(v, depth + 1) if depth < 2 else type(v).__name__) for k, v in list(p.items())[:16]}
    if isinstance(p, list):
        return f"list[{len(p)}]" + (f" of {type(p[0]).__name__}" if p else "")
    return type(p).__name__


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.master, newline="")))
    adapter = DhanAdapter()
    with Session(engine) as db:
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        if cred is None:
            print(json.dumps({"error": "no Dhan connection stored"}))
            return
        try:
            tokens = _fresh_tokens(db, adapter, cred)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"token unavailable ({type(e).__name__})"}))
            return
    headers = adapter.account_headers(tokens["access_token"], tokens)
    results: list[dict] = []

    def call(name, path, body, wait=1.2, note=""):
        time.sleep(wait)
        t = time.monotonic()
        rec = {"name": name, "path": path, "request": {k: v for k, v in body.items() if k not in ("securityId",)} | {"securityId": body.get("securityId")}, "note": note}
        try:
            r = httpx.post(f"https://api.dhan.co{path}", json=body, headers=headers, timeout=40)
            try:
                data = r.json()
            except ValueError:
                data = None
            rec.update(http=r.status_code, ms=int((time.monotonic() - t) * 1000), shape=shape(data))
            if isinstance(data, dict):
                inner = data.get("data", data)
                arrays = {k: len(v) for k, v in (inner.items() if isinstance(inner, dict) else []) if isinstance(v, list)}
                if arrays:
                    rec["array_lengths"] = arrays
                if r.status_code != 200 or data.get("status") == "failure":
                    rec["error"] = {k: str(v)[:200] for k, v in data.items() if k in ("errorType", "errorCode", "errorMessage", "message", "status", "remarks")} or str(data)[:200]
                if isinstance(inner, dict) and isinstance(inner.get("timestamp"), list) and inner["timestamp"]:
                    rec["first_ts"], rec["last_ts"] = inner["timestamp"][0], inner["timestamp"][-1]
            rec["_payload"] = data
        except Exception as e:  # noqa: BLE001
            rec.update(http=None, error=type(e).__name__)
        results.append(rec)
        return rec

    by = lambda **kw: [r for r in rows if all(r.get(k) == v for k, v in kw.items())]  # noqa: E731
    today = date.today()

    def underlying_for(deriv_row):
        """Cash/index security that the chain API calls the underlying: derived by symbol from the master, not hardcoded."""
        sym, exch = deriv_row["UNDERLYING_SYMBOL"], deriv_row["EXCH_ID"]
        idx = [r for r in rows if r["INSTRUMENT"] == "INDEX" and r["EXCH_ID"] == exch and sym in (r["UNDERLYING_SYMBOL"], r["SYMBOL_NAME"])]
        if idx:
            return idx[0]["SECURITY_ID"], "IDX_I", "index"
        eq = [r for r in rows if r["INSTRUMENT"] == "EQUITY" and r["EXCH_ID"] == exch and sym in (r["UNDERLYING_SYMBOL"], r["SYMBOL_NAME"]) and r["SERIES"] in ("EQ", "A", "B", "T", "X", "XT")]
        if eq:
            return eq[0]["SECURITY_ID"], f"{exch}_EQ", "stock"
        return deriv_row["UNDERLYING_SECURITY_ID"], f"{exch}_FNO", "master-underlying-id"

    seg_name = {("NSE", "D"): "NSE_FNO", ("BSE", "D"): "BSE_FNO", ("MCX", "M"): "MCX_COMM", ("NSE", "M"): "NSE_COMM"}
    nearest = lambda lst: sorted(lst, key=lambda r: r["SM_EXPIRY_DATE"])[0]  # noqa: E731
    picks = {
        "NSE index futures (NIFTY)": nearest([r for r in by(EXCH_ID="NSE", INSTRUMENT="FUTIDX") if r["UNDERLYING_SYMBOL"] == "NIFTY"]),
        "NSE stock futures (RELIANCE)": nearest([r for r in by(EXCH_ID="NSE", INSTRUMENT="FUTSTK") if r["UNDERLYING_SYMBOL"] == "RELIANCE"]),
        "BSE index futures (SENSEX)": nearest([r for r in by(EXCH_ID="BSE", INSTRUMENT="FUTIDX") if r["UNDERLYING_SYMBOL"] == "SENSEX"]),
        "MCX commodity futures": nearest(by(EXCH_ID="MCX", INSTRUMENT="FUTCOM")),
    }
    for label, row in picks.items():
        seg = seg_name[(row["EXCH_ID"], row["SEGMENT"])]
        call(f"intraday 1m candles, {label}", "/v2/charts/intraday", {"securityId": row["SECURITY_ID"], "exchangeSegment": seg, "instrument": row["INSTRUMENT"], "interval": "1", "oi": True,
                                                                         "fromDate": f"{today - timedelta(days=4):%Y-%m-%d} 09:15:00", "toDate": f"{today:%Y-%m-%d} 15:30:00"}, note=f"{row['SYMBOL_NAME']} expires {row['SM_EXPIRY_DATE']}")
        call(f"daily candles, {label}", "/v2/charts/historical", {"securityId": row["SECURITY_ID"], "exchangeSegment": seg, "instrument": row["INSTRUMENT"], "expiryCode": 0, "oi": True,
                                                                     "fromDate": f"{today - timedelta(days=30):%Y-%m-%d}", "toDate": f"{today:%Y-%m-%d}"}, note=row["SYMBOL_NAME"])
    # option chains: index, stock, BSE index, MCX
    chains = {"NSE index options (NIFTY)": ("NSE", "OPTIDX", "NIFTY"), "NSE stock options (RELIANCE)": ("NSE", "OPTSTK", "RELIANCE"), "BSE index options (SENSEX)": ("BSE", "OPTIDX", "SENSEX")}
    mcx_opt = by(EXCH_ID="MCX", INSTRUMENT="OPTFUT")
    if mcx_opt:
        chains["MCX commodity options"] = ("MCX", "OPTFUT", nearest(mcx_opt)["UNDERLYING_SYMBOL"])
    for label, (exch, ins, sym) in chains.items():
        cand = [r for r in by(EXCH_ID=exch, INSTRUMENT=ins) if r["UNDERLYING_SYMBOL"] == sym]
        if not cand:
            results.append({"name": f"option chain, {label}", "error": "no such contracts in the master"})
            continue
        row = nearest(cand)
        uid, useg, kind = underlying_for(row)
        if exch == "MCX":
            uid, useg = row["UNDERLYING_SECURITY_ID"], "MCX_COMM"
        exp = call(f"expiry list, {label}", "/v2/optionchain/expirylist", {"UnderlyingScrip": int(uid), "UnderlyingSeg": useg}, wait=3.2, note=f"underlying {sym} id {uid} ({kind})")
        expiries = (exp.get("_payload") or {}).get("data") if isinstance(exp.get("_payload"), dict) else None
        if isinstance(expiries, list) and expiries:
            call(f"option chain, {label}", "/v2/optionchain", {"UnderlyingScrip": int(uid), "UnderlyingSeg": useg, "Expiry": expiries[0]}, wait=3.2, note=f"expiry {expiries[0]}")
    # rolling (expired) options: index and stock, plus unsupported probes
    for label, (exch, ins, sym, seg) in {"NSE index (NIFTY)": ("NSE", "OPTIDX", "NIFTY", "NSE_FNO"), "NSE stock (RELIANCE)": ("NSE", "OPTSTK", "RELIANCE", "NSE_FNO"), "BSE index (SENSEX)": ("BSE", "OPTIDX", "SENSEX", "BSE_FNO"),
                                         "MCX options": ("MCX", "OPTFUT", None, "MCX_COMM")}.items():
        cand = [r for r in by(EXCH_ID=exch, INSTRUMENT=ins) if sym is None or r["UNDERLYING_SYMBOL"] == sym]
        if not cand:
            continue
        row = nearest(cand)
        uid, _, _ = underlying_for(row)
        for opt in ("CALL", "PUT"):
            call(f"rolling options 1m, {label} ATM {opt}", "/v2/charts/rollingoption", {"exchangeSegment": seg, "interval": "1", "securityId": int(uid), "instrument": ins, "expiryFlag": "WEEK" if exch != "MCX" else "MONTH", "expiryCode": 1,
                                                                                         "strike": "ATM", "drvOptionType": opt, "requiredData": ["open", "high", "low", "close", "volume", "oi", "iv", "spot", "strike"],
                                                                                         "fromDate": f"{today - timedelta(days=20):%Y-%m-%d}", "toDate": f"{today - timedelta(days=15):%Y-%m-%d}"}, wait=1.2, note=f"{sym or row['UNDERLYING_SYMBOL']} underlying id {uid}")
            if opt == "CALL" and label.startswith("NSE index"):
                break
    # expired futures: any already-expired derivative still listed in the master (currency) is the only way to obtain an expired security id
    exp_rows = sorted((r for r in rows if r["INSTRUMENT"].startswith("FUT") and r["SM_EXPIRY_DATE"] and r["SM_EXPIRY_DATE"] < f"{today:%Y-%m-%d}"), key=lambda r: r["SM_EXPIRY_DATE"])
    results.append({"name": "expired futures still listed in the master", "count": len(exp_rows), "categories": sorted({f"{r['EXCH_ID']}/{r['SEGMENT']}/{r['INSTRUMENT']}" for r in exp_rows})})
    if exp_rows:
        r0 = exp_rows[-1]
        call("intraday candles, an EXPIRED future from the master", "/v2/charts/intraday", {"securityId": r0["SECURITY_ID"], "exchangeSegment": {"C": "NSE_CURRENCY" if r0["EXCH_ID"] == "NSE" else "BSE_CURRENCY"}.get(r0["SEGMENT"], "NSE_FNO"),
                                                                                               "instrument": r0["INSTRUMENT"], "interval": "1", "oi": True, "fromDate": f"{date.fromisoformat(r0['SM_EXPIRY_DATE']) - timedelta(days=3):%Y-%m-%d} 09:15:00",
                                                                                               "toDate": f"{r0['SM_EXPIRY_DATE']} 15:30:00"}, note=f"{r0['SYMBOL_NAME']} expired {r0['SM_EXPIRY_DATE']}")
    clean = [{k: v for k, v in r.items() if k != "_payload"} for r in results]
    text = json.dumps(clean, indent=1, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)


if __name__ == "__main__":
    main()
