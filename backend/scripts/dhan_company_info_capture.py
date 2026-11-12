"""Capture raw Dhan Company Info responses for an API audit. Reuses the app's Dhan credentials, header builder and throttle.

    cd backend && python scripts/dhan_company_info_capture.py

Writes sanitised JSON (never headers, tokens or the client id) to ../diagnostics/dhan_company_info/raw/.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.market_data import _validated_dhan_tokens  # noqa: E402
from app.broker_adapters import DhanAdapter, _wait_for_dhan_market_data  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import BrokerCredential  # noqa: E402

URL = "https://api.dhan.co/v2/data/companyinfo"
OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "dhan_company_info" / "raw"
# Security ids come from this app's verified Dhan instrument mappings (instrument_provider_identifiers); none are invented.
SECURITIES = [
    ("RELIANCE", "2885", "Energy / conglomerate"), ("HDFCBANK", "1333", "Private bank"), ("SBIN", "3045", "PSU bank"),
    ("UJJIVANSFB", "15228", "Small finance bank"), ("SBILIFE", "21808", "Life insurance"), ("PNBHOUSING", "18908", "Housing finance (NBFC)"),
    ("BHARTIARTL", "10604", "Telecom"), ("BEL", "383", "Defence PSU"), ("JSWSTEEL", "11723", "Steel / metals"), ("NTPC", "11630", "Power utility"),
    ("EASEMYTRIP", "2792", "Travel services"), ("DALBHARAT", "8075", "Cement"), ("IPCALAB", "1633", "Pharma"), ("GODREJCP", "10099", "FMCG"),
    ("BHARATFORG", "422", "Auto ancillary / engineering"), ("JPL", "766830", "Technology"), ("PREMIERPOL", "3908", "Specialty chemicals"),
]
GROUPS = [["CO"], ["RATIOS"], ["SHP"], ["CO", "RATIOS", "SHP"]]
EDGE = [  # (name, body)
    ("edge_etf_combined", {"securityId": "10576", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["CO", "RATIOS", "SHP"]}),
    ("edge_invalid_security_id", {"securityId": "99999999", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["CO"]}),
    ("edge_non_numeric_security_id", {"securityId": "ABC", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["CO"]}),
    ("edge_nse_id_on_bse_segment", {"securityId": "2885", "exchangeSegment": "BSE_EQ", "instrument": "EQUITY", "metrics": ["CO"]}),
    ("edge_fno_segment", {"securityId": "2885", "exchangeSegment": "NSE_FNO", "instrument": "EQUITY", "metrics": ["CO"]}),
    ("edge_index_instrument", {"securityId": "2885", "exchangeSegment": "NSE_EQ", "instrument": "INDEX", "metrics": ["CO"]}),
    ("edge_unknown_metric_group", {"securityId": "2885", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["FOO"]}),
    ("edge_empty_metrics", {"securityId": "2885", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": []}),
    ("edge_missing_metrics_key", {"securityId": "2885", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY"}),
    ("edge_lowercase_metric", {"securityId": "2885", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["co"]}),
    ("edge_security_id_as_integer", {"securityId": 2885, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["CO"]}),
]


def main() -> int:
    with Session(engine) as db:
        credential = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        if credential is None:
            print("no Dhan credential stored")
            return 2
        tokens, error = _validated_dhan_tokens(credential)
        if error:
            print("Dhan credential unusable:", error)
            return 2
    access, client_id = tokens["access_token"], tokens["client_id"]
    headers = DhanAdapter().account_headers(access, tokens)
    secrets = [access, client_id]

    def scrub(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    OUT.mkdir(parents=True, exist_ok=True)
    stop = False
    with httpx.Client(timeout=30, trust_env=False) as client:
        def call(name: str, body: dict, *, hdrs=None) -> int | None:
            _wait_for_dhan_market_data()  # the app's existing >= 1 s spacing for Dhan data requests
            started = time.monotonic()
            try:
                response = client.post(URL, json=body, headers=hdrs or headers)
                status, text = response.status_code, response.text
                content_type = response.headers.get("content-type", "")
                retry_after = response.headers.get("retry-after")
            except httpx.HTTPError as exc:
                status, text, content_type, retry_after = None, f"transport error: {type(exc).__name__}", "", None
            record = {"name": name, "request": {"method": "POST", "url": URL, "body": body}, "status_code": status,
                      "elapsed_ms": int((time.monotonic() - started) * 1000), "content_type": content_type, "retry_after": retry_after}
            text = scrub(text)
            try:
                record["response_json"] = json.loads(text)
            except ValueError:
                record["response_text"] = text[:2000]
            (OUT / f"{name}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
            print(f"{name:42s} HTTP {status} {record['elapsed_ms']} ms")
            return status

        for symbol, sid, _sector in SECURITIES:
            for groups in GROUPS:
                label = "_".join(groups) if len(groups) == 1 else "COMBINED"
                status = call(f"{symbol}_{label}", {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": groups})
                if status in (401, 403, 429):
                    print("stopping: auth or rate-limit response")
                    stop = True
                    break
            if stop:
                break
        if not stop:
            for name, body in EDGE:
                call(name, body)
            call("edge_invalid_token", {"securityId": "2885", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "metrics": ["CO"]},
                 hdrs={**headers, "Authorization": "Bearer invalid", "access-token": "invalid"} if "Authorization" in headers else {**headers, "access-token": "invalid"})
    leaked = [p.name for p in OUT.glob("*.json") if any(s in p.read_text() for s in secrets)]
    print("secret leak check:", "FAILED " + str(leaked) if leaked else "clean")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
