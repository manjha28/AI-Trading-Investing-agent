"""Keeps the Dhan session alive without anyone opening the app.

Dhan access tokens last ~24 h and can be renewed (RenewToken) only while still valid. Every cycle, a Dhan credential whose token has less than
RENEW_WITHIN left is renewed and stored (the stored expiry is clamped to the token's own expiry). A token that is already dead cannot be renewed:
it is logged once per cycle as needing a reconnect in the app. Read-only broker call; no order endpoint is involved.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.broker_adapters import BrokerAdapterError, BrokerReconnectRequired, jwt_expiry
from app.db import engine

logger = logging.getLogger(__name__)
RENEW_WITHIN = timedelta(hours=6)
CYCLE_SECONDS = 1800


def run_cycle(now: datetime | None = None) -> dict[str, int]:
    from app.api.brokers import decrypt_tokens, encrypt_tokens
    from app.broker_adapters import DhanAdapter
    from app.models import BrokerCredential
    now = now or datetime.now(timezone.utc)
    summary = {"checked": 0, "renewed": 0, "needs_reconnect": 0, "failed": 0}
    with Session(engine) as db:
        for credential in db.scalars(select(BrokerCredential).where(BrokerCredential.broker == "dhan")):
            summary["checked"] += 1
            try:
                tokens = decrypt_tokens(credential.encrypted_tokens)
                actual = jwt_expiry(tokens.get("access_token", ""))
                if actual is None:
                    continue
                if actual <= now:
                    summary["needs_reconnect"] += 1
                    credential.expires_at = actual                       # make the stored state honest: the session is over
                    db.commit()
                    logger.warning("Dhan session expired at %s UTC: reconnect Dhan in the app (an expired token cannot be renewed).", actual.strftime("%Y-%m-%d %H:%M"))
                    continue
                if actual - now > RENEW_WITHIN:
                    continue
                renewed, expires_at = DhanAdapter().refresh_token(tokens)
                credential.encrypted_tokens = encrypt_tokens(renewed)
                credential.expires_at = expires_at
                db.commit()
                summary["renewed"] += 1
                logger.info("Dhan session renewed; valid until %s UTC", expires_at.strftime("%Y-%m-%d %H:%M"))
            except BrokerReconnectRequired:
                db.rollback()
                summary["needs_reconnect"] += 1
                logger.warning("Dhan refused the renewal: reconnect Dhan in the app.")
            except (BrokerAdapterError, Exception) as exc:  # noqa: BLE001 - one credential must never stop the loop
                db.rollback()
                summary["failed"] += 1
                logger.warning("Dhan session renewal failed error_class=%s", type(exc).__name__)
    return summary


async def dhan_token_keepalive_loop() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            result = await asyncio.to_thread(run_cycle)
            if result["renewed"] or result["needs_reconnect"] or result["failed"]:
                logger.info("dhan_token_keepalive %s", result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dhan_token_keepalive_failed error_class=%s", type(exc).__name__)
        await asyncio.sleep(CYCLE_SECONDS)
