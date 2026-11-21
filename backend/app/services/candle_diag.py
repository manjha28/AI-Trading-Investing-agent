"""Explain why a stock's latest daily candle is (not) stored: Dhan connection, what Dhan returns right now, what we store, and the freshness verdict.

Run from backend/:  python -m app.services.candle_diag HDFCBANK
Read-only: it calls Dhan's historical-data endpoint for the last ~10 days and prints; it writes nothing and places no order.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import BrokerCredential, Instrument, MarketBar

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.services.candle_diag SYMBOL")
        return 2
    symbol = sys.argv[1].upper()
    from app.api.market_data import _resolve_provider_candidate
    from app.services import research_pipeline as rp
    from app.services.price_freshness import official_session_close_at
    from app.services.research_scoring import expected_last_completed_session

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        inst = db.scalar(select(Instrument).where(Instrument.symbol == symbol, Instrument.is_synthetic.is_(False)).limit(1))
        if inst is None:
            print("no such instrument:", symbol)
            return 2
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        print(f"now (IST): {now.astimezone(IST):%Y-%m-%d %H:%M}   expected last completed session: {expected_last_completed_session(now)}")
        if cred is None:
            print("DHAN: no Dhan credential is stored. Connect Dhan in the app.")
            return 1
        try:
            chain = rp.dhan_only_provider(db, cred.user_id)
            print("DHAN: connected and authenticated")
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None)
            print("DHAN: NOT usable ->", (detail.get("message") if isinstance(detail, dict) else detail) or type(exc).__name__)
            print("      Dhan sessions expire (about every 24h); reconnect Dhan, then re-run.")
            return 1
        provider = chain.providers[0]
        print("\nSTORED daily bars (newest 6):")
        for b in db.scalars(select(MarketBar).where(MarketBar.instrument_id == inst.id, MarketBar.interval == "1d").order_by(MarketBar.timestamp.desc()).limit(6)):
            close_at = official_session_close_at(inst, b.timestamp.astimezone(IST).date())
            print(f"  {b.timestamp:%Y-%m-%d %H:%M} UTC  source={b.source}  close={b.close}  retrieved={b.retrieved_at:%Y-%m-%d %H:%M} UTC  "
                  f"{'complete' if b.retrieved_at >= close_at else 'PARTIAL (fetched before the session close)'}")
        print("\nDHAN returns now (last 10 days):")
        try:
            candidate, _ = _resolve_provider_candidate(db, inst, provider)
            for b in provider.get_historical_bars(candidate, "1d", now - timedelta(days=10), now, 20):
                print(f"  {b.timestamp:%Y-%m-%d %H:%M} UTC  ({b.timestamp.astimezone(IST):%Y-%m-%d %H:%M} IST)  close={b.close}")
        except Exception as exc:  # noqa: BLE001
            print("  request failed ->", getattr(exc, "code", None) or type(exc).__name__, str(exc)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
