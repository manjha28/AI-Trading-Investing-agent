"""Show how stored Dhan fills for a symbol are matched into journal trades (why a closing fill is or is not paired with its opening fill).

Run from backend/:  python -m app.services.trade_diag ONGC
Read-only. Prints the fills (with Dhan's security id and symbol text), the identity the journal groups them by, and the match result.
"""
from __future__ import annotations

import sys
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import DhanTradeMirror

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.services.trade_diag SYMBOL_TEXT   (e.g. ONGC)")
        return 2
    from app.api.trades import _instrument_identity, _match_dhan_fills
    needle = sys.argv[1].upper()
    with Session(engine) as db:
        rows = [r for r in db.scalars(select(DhanTradeMirror).order_by(DhanTradeMirror.trade_at)) if needle in (r.symbol or "").upper()]
        by_sec = {r.security_id for r in rows if r.security_id}
        rows = [r for r in db.scalars(select(DhanTradeMirror).order_by(DhanTradeMirror.trade_at)) if needle in (r.symbol or "").upper() or r.security_id in by_sec]
        if not rows:
            print("no stored fills contain", needle, "- Dhan has not returned them (run Journal > load history and read its message).")
            return 1
        print(f"{len(rows)} stored fill(s) mentioning {needle} (or sharing a security id with one):")
        for r in rows:
            print(f"  {r.trade_at.astimezone(IST):%Y-%m-%d %H:%M} IST  {r.side:<4} qty={r.quantity} price={r.price}  seg={r.exchange_segment} sec={r.security_id} source={r.source}  symbol={r.symbol!r}")
            print(f"      identity = {_instrument_identity(r)}")
        result = _match_dhan_fills(rows)
        print("\nMATCHES:")
        for m in result["matches"]:
            print(f"  {m['side']} qty={m['quantity']} entry={m['entry_time']:%Y-%m-%d %H:%M} exit={m['exit_time']:%Y-%m-%d %H:%M} net={m['net_pnl']}")
        print("open fills:", len(result["open_fill_ids"]), "| expired:", len(result["expired_fill_ids"]), "| unmatched:", len(result["unmatched_fill_ids"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
