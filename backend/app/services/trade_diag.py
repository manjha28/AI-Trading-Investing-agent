"""Show how stored Dhan fills for a symbol are matched into journal trades (why a closing fill is or is not paired with its opening fill).

Run from backend/:  python -m app.services.trade_diag ONGC [--dhan FROM_DATE]     (e.g. --dhan 2026-11-10)
With --dhan it also asks Dhan's trade-history endpoint directly (a read-only GET) and prints every returned fill that mentions the symbol, so you can see
whether Dhan returns the closing fill at all. Prints the fills (with Dhan's security id and symbol text), the identity the journal groups them by, and the match result.
"""
from __future__ import annotations

import sys
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import DhanTradeMirror

IST = ZoneInfo("Asia/Kolkata")


def _probe_dhan(db: Session, needle: str, from_date: str) -> None:
    import time
    from datetime import date

    from sqlalchemy import select as sel

    from app.api.brokers import DhanReadOnlyAccountAdapter, decrypt_tokens
    from app.models import BrokerCredential
    cred = db.scalar(sel(BrokerCredential).where(BrokerCredential.broker == "dhan"))
    if cred is None:
        print("DHAN LIVE: no Dhan credential stored.")
        return
    tokens = decrypt_tokens(cred.encrypted_tokens)
    reader = DhanReadOnlyAccountAdapter(tokens["access_token"], tokens["client_id"])
    today = date.today().isoformat()
    print(f"\nDHAN LIVE trade history {from_date} .. {today}:")
    page, total, shown = 0, 0, 0
    while True:
        try:
            rows = reader.get_past_trades(from_date, today, page)
        except Exception as exc:  # noqa: BLE001
            print("  request failed ->", getattr(exc, "code", None) or type(exc).__name__)
            break
        if not rows:
            break
        total += len(rows)
        for r in rows:
            text = f"{r.get('tradingSymbol') or ''} {r.get('customSymbol') or ''}".upper()
            if needle in text:
                shown += 1
                print(f"  {r.get('exchangeTime') or r.get('updateTime')}  {r.get('transactionType'):<4} qty={r.get('tradedQuantity')} price={r.get('tradedPrice')} "
                      f"sec={r.get('securityId')} seg={r.get('exchangeSegment')} tradeId={r.get('exchangeTradeId')} trading={r.get('tradingSymbol')!r} custom={r.get('customSymbol')!r}")
        page += 1
        time.sleep(1)
    print(f"  Dhan returned {total} fills in total, {shown} mentioning {needle}.")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.services.trade_diag SYMBOL_TEXT   (e.g. ONGC)")
        return 2
    from app.api.trades import _instrument_identity, _match_dhan_fills
    needle = sys.argv[1].upper()
    live_from = sys.argv[sys.argv.index("--dhan") + 1] if "--dhan" in sys.argv[2:] and len(sys.argv) > sys.argv.index("--dhan") + 1 else None
    with Session(engine) as db:
        rows = [r for r in db.scalars(select(DhanTradeMirror).order_by(DhanTradeMirror.trade_at)) if needle in (r.symbol or "").upper()]
        by_sec = {r.security_id for r in rows if r.security_id}
        rows = [r for r in db.scalars(select(DhanTradeMirror).order_by(DhanTradeMirror.trade_at)) if needle in (r.symbol or "").upper() or r.security_id in by_sec]
        if live_from:
            _probe_dhan(db, needle, live_from)
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
