import csv
import io
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, time as wall_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.accounting import LedgerError, money
from app.api.auth import get_active_session, require_csrf
from app.api.portfolio_transactions import calculate_portfolio_ledger, portfolio_valuation
from app.broker_adapters import (
    ADAPTERS,
    BrokerAdapter,
    BrokerAdapterError,
    BrokerReconnectRequired,
    DhanAdapter,
    get_broker_adapter,
)
from app.config import settings
from app.db import get_db
from app.fernet_keys import normalize_fernet_key
from app.models import (
    BrokerCredential,
    BrokerAccountMirror,
    BrokerMirror,
    BrokerOAuthState,
    DhanLedgerMirror,
    DhanLedgerDataQualityException,
    DhanTradeMirror,
    Instrument,
    InstrumentProviderIdentifier,
    MarketDataSync,
    MarketBar,
    MarketQuote,
    Portfolio,
    PortfolioTransaction,
    UserSession,
    User,
)
from app.security import hash_token, new_token
from app.provider_adapters import (
    DhanAccountRequestError,
    DhanReadOnlyAccountAdapter,
    dhan_provider_id_variants,
    dhan_provider_instrument_id,
)
from app.services.price_freshness import (
    USABLE_FRESHNESS_STATUSES,
    classify_quote,
    select_usable_quote,
    valuation_basis_for_quote,
    venue_last_completed_session,
    venue_session,
)
from app.services.price_states import (
    BASIS_LIVE,
    BASIS_SESSION_CLOSE,
    BASIS_UNAVAILABLE,
    FRESHNESS_SESSION_CLOSE,
    basis_for_freshness,
    provider_family,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brokers", tags=["read-only brokers"])
OAUTH_STATE_TTL = timedelta(minutes=10)
_dhan_master_refresh_lock = threading.Lock()
_dhan_master_miss_refresh_cache_time: float | None = None
DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS = max(
    30, int(os.getenv("DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS", "30"))
)
_dhan_snapshot_attempts: dict[UUID, float] = {}
_dhan_snapshot_attempts_lock = threading.Lock()


def _dhan_master_matches(
    master_rows: list[dict[str, str]],
    security_id: str | None,
    isin: str | None,
) -> list[dict[str, str]]:
    normalized_id = (security_id or "").strip()
    normalized_isin = (isin or "").strip().upper()
    matches = {
        (
            row.get("SECURITY_ID", "").strip(),
            row.get("EXCH_ID", "").strip().upper(),
        ): row
        for row in master_rows
        if row.get("EXCH_ID", "").strip().upper() in {"NSE", "BSE"}
        and row.get("SEGMENT", "").strip().upper() == "E"
        and row.get("INSTRUMENT", "").strip().upper() == "EQUITY"
        and row.get("SERIES", "").strip().upper() == "EQ"
        and row.get("SECURITY_ID", "").strip().isdigit()
        and (
            (normalized_id and row.get("SECURITY_ID", "").strip() == normalized_id)
            or (
                normalized_isin
                and row.get("ISIN", "").strip().upper() == normalized_isin
            )
        )
    }
    return list(matches.values())


def _dhan_master_listing_date(master_row: dict[str, str] | None) -> date | None:
    if master_row is None:
        return None
    value = (
        master_row.get("LISTING_DATE")
        or master_row.get("LIST_DATE")
        or master_row.get("LISTINGDATE")
        or ""
    ).strip()
    for date_format in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def _dhan_instrument_master(
    rows: list[BrokerMirror],
) -> tuple[list[dict[str, str]], datetime | None]:
    global _dhan_master_miss_refresh_cache_time
    cache_before = DhanAdapter._scrip_master_cache
    master_rows = DhanAdapter._scrip_master()
    cache_after = DhanAdapter._scrip_master_cache
    if cache_after is None:
        return master_rows, None
    has_unmatched_identity = any(
        not _dhan_master_matches(master_rows, row.security_id, row.isin)
        for row in rows
        if row.security_id or row.isin
    )
    if (
        has_unmatched_identity
        and cache_before is not None
        and cache_after[0] == cache_before[0]
        and _dhan_master_miss_refresh_cache_time != cache_after[0]
    ):
        with _dhan_master_refresh_lock:
            current_cache = DhanAdapter._scrip_master_cache
            if (
                current_cache is not None
                and current_cache[0] == cache_after[0]
                and _dhan_master_miss_refresh_cache_time != current_cache[0]
            ):
                DhanAdapter._scrip_master_cache = None
                DhanAdapter._equity_index_cache = None
                master_rows = DhanAdapter._scrip_master()
                cache_after = DhanAdapter._scrip_master_cache
            _dhan_master_miss_refresh_cache_time = (
                cache_after[0] if cache_after is not None else None
            )
    elif has_unmatched_identity:
        _dhan_master_miss_refresh_cache_time = cache_after[0]
    refreshed_at = _now() - timedelta(
        seconds=max(0.0, time.monotonic() - cache_after[0])
    )
    return master_rows, refreshed_at


class ConnectRequest(BaseModel):
    client_id: str | None = Field(default=None, min_length=1, max_length=80)


class MirrorRow(BaseModel):
    kind: str
    symbol: str
    exchange: str
    product: str
    security_id: str | None = None
    isin: str | None = None
    source: str
    currency: str = "INR"
    quantity: Decimal
    average_price: Decimal | None
    synced_at: datetime


class ImportRow(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    expected_manual_quantity: Decimal
    expected_broker_quantity: Decimal


class ImportRequest(BaseModel):
    portfolio_id: UUID
    confirmed: bool
    rows: list[ImportRow] = Field(min_length=1, max_length=200)


class DhanTradeCsvRequest(BaseModel):
    csv_content: str = Field(min_length=1, max_length=5_000_000)


def require_broker_access(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(status_code=403, detail={
            "code": "email_verification_required",
            "message": "Verify your email address before accessing broker connections.",
        })
    if not active_session.user.two_factor_enabled:
        raise HTTPException(status_code=403, detail={
            "code": "two_factor_setup_required",
            "message": "Set up app-based two-factor authentication before accessing broker connections.",
        })
    if not active_session.two_factor_verified:
        raise HTTPException(status_code=403, detail={
            "code": "two_factor_required",
            "message": "Verify this session with your authenticator app before accessing broker connections.",
        })
    return active_session


def _broker_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _log_dhan_sync_failure(stage: str, error: Exception, description: str) -> None:
    original = getattr(error, "orig", error)
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    table_name = getattr(diagnostic, "table_name", None)
    logger.warning(
        "Dhan sync diagnostic stage=%s error_class=%s table=%s constraint=%s description=%s",
        stage,
        type(error).__name__,
        table_name or "unknown",
        constraint_name or "unknown",
        description,
    )


def _user_id(active_session: UserSession) -> UUID:
    return active_session.user_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fernet() -> Fernet:
    key = settings.broker_encryption_key
    if key is None:
        raise _broker_error(503, "broker_setup_required", "Broker token encryption is not configured.")
    try:
        return Fernet(normalize_fernet_key(key.get_secret_value()))
    except (ValueError, TypeError) as error:
        raise _broker_error(503, "broker_setup_required", "Broker token encryption is misconfigured.") from error


def encrypt_tokens(tokens: dict[str, str]) -> str:
    return _fernet().encrypt(json.dumps(tokens, separators=(",", ":")).encode()).decode()


def decrypt_tokens(encrypted_tokens: str) -> dict[str, str]:
    try:
        payload = _fernet().decrypt(encrypted_tokens.encode())
        tokens = json.loads(payload)
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Stored broker credentials cannot be decrypted") from error
    if not isinstance(tokens, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in tokens.items()
    ):
        raise HTTPException(status_code=503, detail="Stored broker credentials are invalid")
    return tokens


def _adapter(slug: str) -> BrokerAdapter:
    try:
        adapter = get_broker_adapter(slug)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Broker not supported") from error
    if not adapter.enabled:
        raise _broker_error(
            503,
            "broker_setup_required",
            "This broker is disabled or missing required credentials and callback settings.",
        )
    return adapter


def _credential(db: Session, user_id: UUID, slug: str) -> BrokerCredential:
    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == user_id,
            BrokerCredential.broker == slug,
        )
    )
    if credential is None:
        raise HTTPException(status_code=409, detail="Broker is not connected")
    return credential


def _valid_state(
    db: Session,
    active_session: UserSession,
    slug: str,
    state: str | None,
) -> BrokerOAuthState:
    if not state:
        raise HTTPException(status_code=400, detail="Broker callback state is missing")
    state_row = db.scalar(
        select(BrokerOAuthState).where(
            BrokerOAuthState.state_hash == hash_token(state),
            BrokerOAuthState.user_id == _user_id(active_session),
            BrokerOAuthState.broker == slug,
        )
    )
    if state_row is None:
        raise HTTPException(status_code=400, detail="Broker callback state is invalid or already used")
    expires_at = state_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _now():
        db.delete(state_row)
        db.commit()
        raise HTTPException(status_code=400, detail="Broker callback state has expired")
    return state_row


def _redirect_uri(adapter: BrokerAdapter, slug: str) -> str:
    configured = getattr(adapter, "redirect_url", None)
    if not configured:
        raise HTTPException(status_code=503, detail="Broker callback URL is not configured")
    expected_path = f"/api/v1/brokers/{slug}/callback"
    parsed = urlsplit(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != expected_path
        or parsed.query
        or parsed.fragment
        or (settings.environment == "production" and parsed.scheme != "https")
    ):
        raise HTTPException(status_code=503, detail="Broker callback URL must target this broker callback endpoint")
    return configured


def _mock_return_origin(request: Request) -> str:
    candidate = request.headers.get("origin")
    if not candidate:
        candidate = str(request.base_url).rstrip("/")
    parsed = urlsplit(candidate)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    is_codespaces = bool(re.fullmatch(r"https://[a-zA-Z0-9-]+-5173\.app\.github\.dev", origin))
    local_origins = {
        settings.frontend_origin,
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    }
    if parsed.path or parsed.query or parsed.fragment or (origin not in local_origins and not is_codespaces):
        raise HTTPException(status_code=400, detail="Frontend origin is not allowed")
    return origin


def _save_credential(
    db: Session,
    user_id: UUID,
    slug: str,
    tokens: dict[str, str],
    expires_at: datetime,
) -> BrokerCredential:
    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == user_id,
            BrokerCredential.broker == slug,
        )
    )
    now = _now()
    if credential is None:
        credential = BrokerCredential(
            user_id=user_id,
            broker=slug,
            encrypted_tokens=encrypt_tokens(tokens),
            expires_at=expires_at,
            connected_at=now,
        )
        db.add(credential)
    else:
        credential.encrypted_tokens = encrypt_tokens(tokens)
        credential.expires_at = expires_at
        credential.connected_at = now
    db.commit()
    db.refresh(credential)
    return credential


def _fresh_tokens(
    db: Session, adapter: BrokerAdapter, credential: BrokerCredential
) -> dict[str, str]:
    tokens = decrypt_tokens(credential.encrypted_tokens)
    expiry = credential.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    renewal_window = timedelta(minutes=5) if adapter.slug in {"dhan", "angel_one"} else timedelta(0)
    if expiry <= _now() + renewal_window:
        try:
            tokens, expires_at = adapter.refresh_token(tokens)
        except BrokerReconnectRequired as error:
            credential.expires_at = _now()
            db.commit()
            raise _broker_error(401, "broker_authentication_required", str(error)) from error
        except BrokerAdapterError as error:
            raise _broker_error(
                502,
                "broker_provider_error",
                "Broker token refresh failed; reconnect and try again.",
            ) from error
        credential.encrypted_tokens = encrypt_tokens(tokens)
        credential.expires_at = expires_at
        db.commit()
        if adapter.slug in {"angel_one", "dhan", "yfinance"}:
            from app.api.market_data import reset_provider_circuit_breaker

            reset_provider_circuit_breaker(adapter.slug)
    return tokens


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        value = value["data"]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise BrokerAdapterError("Broker account data has an unexpected format")
    return value


def _first(record: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return default


def _normalize_rows(
    rows: Any,
    kind: str,
    synced_at: datetime,
    user_id: UUID,
    broker: str,
) -> list[BrokerMirror]:
    normalized = []
    for record in _records(rows):
        symbol = _first(record, "tradingsymbol", "trading_symbol", "tradingSymbol", "symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise BrokerAdapterError("Broker account data contains a row without a symbol")
        raw_quantity = _first(
            record,
            "quantity",
            "net_quantity",
            "netQty",
            "totalQty",
            "netqty",
            "qty",
            default="0",
        )
        raw_price = _first(
            record,
            "average_price",
            "avg_price",
            "avgPrice",
            "avgCostPrice",
            "averageprice",
            "costPrice",
        )
        try:
            quantity = Decimal(str(raw_quantity))
            average_price = Decimal(str(raw_price)) if raw_price is not None else None
            realized_profit = (
                Decimal(str(record["realizedProfit"]))
                if record.get("realizedProfit") is not None
                else None
            )
            unrealized_profit = (
                Decimal(str(record["unrealizedProfit"]))
                if record.get("unrealizedProfit") is not None
                else None
            )
        except (InvalidOperation, ValueError) as error:
            raise BrokerAdapterError("Broker account data contains invalid quantities") from error
        if any(
            value is not None and not value.is_finite()
            for value in (quantity, average_price, realized_profit, unrealized_profit)
        ):
            raise BrokerAdapterError("Broker account data contains invalid quantities")
        exchange = str(_first(record, "exchange", "exchangeSegment", "exch_seg", default="")).upper()[:32]
        product = str(_first(record, "product", "productType", "producttype", default="")).upper()[:32]
        position_type_value = _first(record, "positionType", "position_type")
        normalized.append(
            BrokerMirror(
                user_id=user_id,
                broker=broker,
                kind=kind,
                symbol=symbol.strip().upper()[:64],
                exchange=exchange,
                product=product,
                security_id=(
                    str(_first(record, "securityId", "security_id"))[:64]
                    if _first(record, "securityId", "security_id") is not None
                    else None
                ),
                isin=(
                    str(record["isin"]).strip()[:16]
                    if isinstance(record.get("isin"), str) and record["isin"].strip()
                    else None
                ),
                quantity=quantity,
                average_price=average_price,
                position_type=(
                    str(position_type_value).upper()[:16]
                    if position_type_value is not None
                    else None
                ),
                realized_profit=realized_profit,
                unrealized_profit=unrealized_profit,
                synced_at=synced_at,
            )
        )
    return normalized


def _normalize_dhan_funds(
    payload: dict[str, Any],
    user_id: UUID,
    synced_at: datetime,
) -> BrokerAccountMirror:
    available = _first(payload, "availabelBalance", "availableBalance")
    if available is None:
        raise BrokerAdapterError("Dhan fund limit response has no available balance")
    try:
        values = {
            "available_balance": Decimal(str(available)),
            "sod_limit": (
                Decimal(str(payload["sodLimit"]))
                if payload.get("sodLimit") is not None
                else None
            ),
            "collateral_amount": (
                Decimal(str(payload["collateralAmount"]))
                if payload.get("collateralAmount") is not None
                else None
            ),
            "receivable_amount": (
                Decimal(str(payload["receiveableAmount"]))
                if payload.get("receiveableAmount") is not None
                else None
            ),
            "utilized_amount": (
                Decimal(str(payload["utilizedAmount"]))
                if payload.get("utilizedAmount") is not None
                else None
            ),
            "blocked_payout_amount": (
                Decimal(str(payload["blockedPayoutAmount"]))
                if payload.get("blockedPayoutAmount") is not None
                else None
            ),
            "withdrawable_balance": (
                Decimal(str(payload["withdrawableBalance"]))
                if payload.get("withdrawableBalance") is not None
                else None
            ),
        }
    except (InvalidOperation, ValueError, TypeError) as error:
        raise BrokerAdapterError("Dhan fund limit response contains invalid amounts") from error
    if any(
        value is not None and not value.is_finite()
        for value in values.values()
    ):
        raise BrokerAdapterError("Dhan fund limit response contains invalid amounts")
    return BrokerAccountMirror(
        user_id=user_id,
        broker="dhan",
        currency="INR",
        synced_at=synced_at,
        **values,
    )


def _dhan_decimal(value: Any, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None or value == "":
        if optional:
            return None
        raise BrokerAdapterError(f"Dhan response is missing {field}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise BrokerAdapterError(f"Dhan response has invalid {field}") from error
    if not amount.is_finite():
        raise BrokerAdapterError(f"Dhan response has invalid {field}")
    return amount


def _required_dhan_decimal(value: Any, field: str) -> Decimal:
    amount = _dhan_decimal(value, field)
    assert amount is not None
    return amount


def _dhan_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BrokerAdapterError(f"Dhan response is missing {field}")
    text_value = value.strip()
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%d-%m-%Y %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%d %b %Y",
            "%d-%b-%Y",
            "%b %d, %Y",
        ):
            try:
                parsed = datetime.strptime(text_value, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise BrokerAdapterError(f"Dhan response has invalid {field}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return parsed.astimezone(timezone.utc)


def _normalize_dhan_trades(
    rows: list[dict[str, Any]],
    user_id: UUID,
    synced_at: datetime,
    source: str = "dhan",
) -> list[DhanTradeMirror]:
    normalized = []
    charge_fields = (
        "sebiTax",
        "stt",
        "brokerageCharges",
        "serviceTax",
        "exchangeTransactionCharges",
        "stampDuty",
    )
    for row in rows:
        trade_id = row.get("exchangeTradeId")
        side = str(row.get("transactionType") or "").upper()
        if trade_id is None or not str(trade_id).strip() or side not in {"BUY", "SELL"}:
            raise BrokerAdapterError("Dhan trade response is missing its trade ID or side")
        if row.get("charges") not in (None, ""):
            charges = _dhan_decimal(row.get("charges"), "charges", optional=True)
        else:
            charges_values = [
                _dhan_decimal(row.get(field), field, optional=True)
                for field in charge_fields
            ]
            charges = (
                sum((amount for amount in charges_values if amount is not None), Decimal("0"))
                if any(amount is not None for amount in charges_values)
                else None
            )
        provider_trade_id = str(trade_id)[:64]
        order_id = (
            str(row["orderId"])[:64] if row.get("orderId") is not None else None
        )
        exchange_order_id = (
            str(row["exchangeOrderId"])[:64]
            if row.get("exchangeOrderId") is not None
            else None
        )
        event_time = row.get(
            "exchangeTime", row.get("updateTime", row.get("createTime"))
        )
        identity_payload = {
            "provider_trade_id": provider_trade_id,
            "order_id": order_id,
            "exchange_order_id": exchange_order_id,
            "security_id": row.get("securityId"),
            "exchange_segment": row.get("exchangeSegment"),
            "side": side,
            "quantity": row.get("tradedQuantity"),
            "price": row.get("tradedPrice"),
            "trade_at": event_time,
        }
        identity_key = hashlib.sha256(
            json.dumps(
                identity_payload,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        normalized.append(
            DhanTradeMirror(
                user_id=user_id,
                source=source,
                trade_id=identity_key,
                provider_trade_id=provider_trade_id,
                order_id=order_id,
                exchange_order_id=exchange_order_id,
                symbol=(
                    str(row.get("tradingSymbol") or row.get("customSymbol"))[:64]
                    if row.get("tradingSymbol") or row.get("customSymbol")
                    else None
                ),
                security_id=(
                    str(row["securityId"])[:64] if row.get("securityId") is not None else None
                ),
                exchange_segment=(
                    str(row["exchangeSegment"])[:32]
                    if row.get("exchangeSegment") is not None
                    else None
                ),
                side=side,
                quantity=_required_dhan_decimal(
                    row.get("tradedQuantity"), "tradedQuantity"
                ),
                price=_required_dhan_decimal(row.get("tradedPrice"), "tradedPrice"),
                charges=charges,
                currency="INR",
                trade_at=_dhan_timestamp(event_time, "exchangeTime"),
                synced_at=synced_at,
            )
        )
    return normalized


def _normalize_dhan_ledger(
    rows: list[dict[str, Any]], user_id: UUID, synced_at: datetime
) -> list[DhanLedgerMirror]:
    normalized = []
    for row in rows:
        entry_id = row.get("vouchernumber")
        if entry_id is None or not str(entry_id).strip():
            raise BrokerAdapterError("Dhan ledger response is missing its entry ID")
        provider_entry_id = str(entry_id)[:64]
        line_identity = {
            "provider_entry_id": provider_entry_id,
            "voucherdate": row.get("voucherdate"),
            "narration": row.get("narration"),
            "voucherdesc": row.get("voucherdesc"),
            "exchange": row.get("exchange"),
            "debit": row.get("debit"),
            "credit": row.get("credit"),
            "runbal": row.get("runbal"),
        }
        line_key = hashlib.sha256(
            json.dumps(
                line_identity,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        normalized.append(
            DhanLedgerMirror(
                user_id=user_id,
                source="dhan",
                entry_id=line_key,
                provider_entry_id=provider_entry_id,
                entry_date=_dhan_timestamp(row.get("voucherdate"), "voucherdate"),
                description=(
                    str(row.get("narration") or row.get("voucherdesc"))[:1000]
                    if row.get("narration") or row.get("voucherdesc")
                    else None
                ),
                debit=_dhan_decimal(row.get("debit"), "debit", optional=True),
                credit=_dhan_decimal(row.get("credit"), "credit", optional=True),
                running_balance=_dhan_decimal(
                    row.get("runbal"), "runbal", optional=True
                ),
                currency="INR",
                synced_at=synced_at,
            )
        )
    return normalized


def _record_dhan_ledger_exceptions(
    db: Session, user_id: UUID, rows: list[dict[str, Any]], seen_at: datetime
) -> list[dict[str, Any]]:
    recorded = []
    for row in rows:
        evidence = {
            "voucherdate": row.get("voucherdate"),
            "narration": row.get("narration"),
            "voucherdesc": row.get("voucherdesc"),
            "exchange": row.get("exchange"),
            "debit": row.get("debit"),
            "credit": row.get("credit"),
        }
        fingerprint = hashlib.sha256(json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), default=str
        ).encode()).hexdigest()
        existing = db.scalar(select(DhanLedgerDataQualityException).where(
            DhanLedgerDataQualityException.user_id == user_id,
            DhanLedgerDataQualityException.fingerprint == fingerprint,
        ))
        description = str(row.get("narration") or row.get("voucherdesc") or "")[:1000] or None
        try:
            debit = _dhan_decimal(row.get("debit"), "debit", optional=True)
            credit = _dhan_decimal(row.get("credit"), "credit", optional=True)
        except BrokerAdapterError:
            debit = credit = None
        if existing is None:
            db.add(DhanLedgerDataQualityException(
                user_id=user_id,
                fingerprint=fingerprint,
                entry_date=str(row.get("voucherdate"))[:40] if row.get("voucherdate") is not None else None,
                description=description,
                debit=debit,
                credit=credit,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
                occurrence_count=1,
            ))
            recorded.append({"date": str(row.get("voucherdate") or "unknown"), "description": description, "debit": str(debit) if debit is not None else None, "credit": str(credit) if credit is not None else None})
        # Existing exceptions stay unchanged: one first-seen record is enough.
    return recorded


def _upsert_dhan_trades(
    db: Session, rows: list[DhanTradeMirror]
) -> tuple[int, int, list[dict[str, str]]]:
    inserted = 0
    updated = 0
    skipped: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        try:
            with db.begin_nested():
                was_inserted = _upsert_dhan_trade(db, row)
                db.flush()
        except IntegrityError as error:
            skipped.append({"record": str(index), "error_class": type(error).__name__})
            _log_dhan_sync_failure(
                "broker_mirror_record",
                error,
                "Dhan trade mirror record skipped; database details omitted.",
            )
            continue
        if was_inserted:
            inserted += 1
        else:
            updated += 1
    return inserted, updated, skipped


def _upsert_dhan_trade(db: Session, row: DhanTradeMirror) -> bool:
    existing = db.scalar(
        select(DhanTradeMirror).where(
            DhanTradeMirror.user_id == row.user_id,
            DhanTradeMirror.source == row.source,
            DhanTradeMirror.trade_id == row.trade_id,
        )
    )
    if existing is None and row.provider_trade_id is not None:
        existing = db.scalar(
            select(DhanTradeMirror).where(
                DhanTradeMirror.user_id == row.user_id,
                DhanTradeMirror.source == row.source,
                DhanTradeMirror.provider_trade_id == row.provider_trade_id,
            )
        )
    if existing is None and row.provider_trade_id is not None:
        existing = db.scalar(
            select(DhanTradeMirror).where(
                DhanTradeMirror.user_id == row.user_id,
                DhanTradeMirror.source == row.source,
                DhanTradeMirror.trade_id == row.provider_trade_id,
                DhanTradeMirror.provider_trade_id.is_(None),
            )
        )
    if existing is None:
        db.add(row)
        return True
    for field in (
        "trade_id", "provider_trade_id", "order_id", "exchange_order_id",
        "symbol", "security_id", "exchange_segment", "side", "quantity",
        "price", "charges", "currency", "trade_at", "synced_at",
    ):
        setattr(existing, field, getattr(row, field))
    return False


def _upsert_dhan_ledger_entry(db: Session, row: DhanLedgerMirror) -> bool:
    existing = db.scalar(
        select(DhanLedgerMirror).where(
            DhanLedgerMirror.user_id == row.user_id,
            DhanLedgerMirror.source == row.source,
            DhanLedgerMirror.entry_id == row.entry_id,
        )
    )
    if existing is None:
        existing = db.scalar(
            select(DhanLedgerMirror).where(
                DhanLedgerMirror.user_id == row.user_id,
                DhanLedgerMirror.source == row.source,
                DhanLedgerMirror.entry_id == row.provider_entry_id,
                DhanLedgerMirror.provider_entry_id.is_(None),
            )
        )
    if existing is None:
        db.add(row)
        return True
    for field in (
        "entry_id", "provider_entry_id", "entry_date", "description", "debit",
        "credit", "running_balance", "currency", "synced_at",
    ):
        setattr(existing, field, getattr(row, field))
    return False


def _upsert_broker_mirror(db: Session, row: BrokerMirror) -> None:
    existing = db.scalar(
        select(BrokerMirror).where(
            BrokerMirror.user_id == row.user_id,
            BrokerMirror.broker == row.broker,
            BrokerMirror.kind == row.kind,
            BrokerMirror.symbol == row.symbol,
            BrokerMirror.exchange == row.exchange,
            BrokerMirror.product == row.product,
        )
    )
    if existing is None:
        db.add(row)
        return
    for field in (
        "security_id", "isin", "quantity", "average_price", "position_type",
        "realized_profit", "unrealized_profit", "synced_at",
    ):
        setattr(existing, field, getattr(row, field))


def _mirror_rows(db: Session, user_id: UUID, slug: str, kind: str | None = None) -> list[BrokerMirror]:
    query = select(BrokerMirror).where(
        BrokerMirror.user_id == user_id,
        BrokerMirror.broker == slug,
    )
    if kind:
        query = query.where(BrokerMirror.kind == kind)
    return db.scalars(query.order_by(BrokerMirror.kind, BrokerMirror.symbol, BrokerMirror.exchange)).all()


def _reconciliation(
    db: Session, user_id: UUID, slug: str, portfolio_id: UUID
) -> list[dict[str, Any]]:
    portfolio = db.scalar(
        select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
    )
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    synced_credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == user_id,
            BrokerCredential.broker == slug,
        )
    )
    if synced_credential is None or synced_credential.last_synced_at is None:
        raise HTTPException(status_code=409, detail="Sync this broker before reconciling holdings")
    mirror = _mirror_rows(db, user_id, slug, "holding")
    try:
        manual_positions = calculate_portfolio_ledger(db, user_id, portfolio.id).positions
    except LedgerError as error:
        raise HTTPException(status_code=409, detail="Manual portfolio ledger is not reconcilable") from error

    broker_positions: dict[str, dict[str, Any]] = {}
    for row in mirror:
        item = broker_positions.setdefault(
            row.symbol,
            {"quantity": Decimal("0"), "weighted_cost": Decimal("0"), "priced_quantity": Decimal("0"), "exchanges": set()},
        )
        item["quantity"] += row.quantity
        item["exchanges"].add(row.exchange)
        if row.average_price is not None and row.quantity > 0:
            item["weighted_cost"] += row.quantity * row.average_price
            item["priced_quantity"] += row.quantity

    results = []
    for symbol in sorted(set(manual_positions) | set(broker_positions)):
        manual_quantity = manual_positions.get(symbol).quantity if symbol in manual_positions else Decimal("0")
        broker_item = broker_positions.get(symbol, {})
        broker_quantity = broker_item.get("quantity", Decimal("0"))
        if manual_quantity == broker_quantity:
            continue
        status = "missing" if broker_quantity == 0 else "extra" if manual_quantity == 0 else "quantity_mismatch"
        priced_quantity = broker_item.get("priced_quantity", Decimal("0"))
        average_price = (
            broker_item["weighted_cost"] / priced_quantity
            if priced_quantity > 0
            else None
        )
        results.append({
            "symbol": symbol,
            "manual_quantity": manual_quantity,
            "broker_quantity": broker_quantity,
            "difference": broker_quantity - manual_quantity,
            "status": status,
            "average_price": average_price,
            "exchanges": sorted(broker_item.get("exchanges", set())),
        })
    return results


@router.get("")
def list_brokers(
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    credentials = {
        item.broker: item
        for item in db.scalars(
            select(BrokerCredential).where(BrokerCredential.user_id == _user_id(active_session))
        ).all()
    }
    result = []
    for slug, adapter_type in ADAPTERS.items():
        credential = credentials.get(slug)
        expires_at = credential.expires_at if credential else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expired = expires_at is not None and expires_at <= _now()
        result.append({
            "slug": slug,
            "name": adapter_type.display_name,
            "enabled": adapter_type().enabled,
            "synthetic": adapter_type.is_synthetic,
            "integration_status": (
                "pending" if slug == "angel_one"
                else "synthetic" if adapter_type.is_synthetic
                else "available" if adapter_type().enabled
                else "setup_required"
            ),
            "connection_status": (
                "not_connected" if credential is None
                else "expired" if expired else "connected"
            ),
            "connected": credential is not None and not expired,
            "expires_at": expires_at,
            "last_synced_at": credential.last_synced_at if credential else None,
            "direct_login": False,
        })
    return result


@router.get("/connection-status")
def broker_connection_status(
    response: Response,
    active_session: UserSession = Depends(get_active_session),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    if not active_session.user.email_verified:
        raise HTTPException(status_code=403, detail="Verify your email address first")
    credentials = db.scalars(
        select(BrokerCredential).where(
            BrokerCredential.user_id == _user_id(active_session)
        )
    ).all()
    return {
        "connected": any(
            (credential.expires_at.replace(tzinfo=timezone.utc)
             if credential.expires_at.tzinfo is None
             else credential.expires_at) > _now()
            for credential in credentials
        )
    }


@router.post("/{slug}/connect")
async def connect_broker(
    slug: str,
    request: Request,
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    adapter = _adapter(slug)
    if slug in {"angel_one", "dhan", "yfinance"}:
        from app.api.market_data import reset_provider_circuit_breaker

        reset_provider_circuit_breaker(slug)
    _fernet()
    response.headers["Cache-Control"] = "no-store"
    try:
        raw_body = await request.body()
        body = ConnectRequest.model_validate(json.loads(raw_body) if raw_body else {})
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValidationError) as error:
        raise HTTPException(status_code=422, detail="Invalid broker connection request") from error
    credentials = {
        key: value
        for key, value in {
            "client_id": body.client_id,
        }.items()
        if value is not None
    }
    state = new_token()
    if slug == "mock_broker":
        return_origin = _mock_return_origin(request)
        redirect_uri = f"{return_origin}/api/v1/brokers/{slug}/callback"
    else:
        redirect_uri = _redirect_uri(adapter, slug)
        parsed_redirect = urlsplit(redirect_uri)
        return_origin = f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
    try:
        redirect_url = adapter.authenticate(state=state, redirect_uri=redirect_uri, credentials=credentials)
    except BrokerAdapterError as error:
        raise _broker_error(503, "broker_setup_required", str(error)) from error
    metadata = {
        **({"client_id": credentials["client_id"]} if slug == "dhan" and "client_id" in credentials else {}),
        "return_origin": return_origin,
    }
    db.add(
        BrokerOAuthState(
            user_id=_user_id(active_session),
            broker=slug,
            state_hash=hash_token(state),
            metadata_json=metadata,
            expires_at=_now() + OAUTH_STATE_TTL,
        )
    )
    db.commit()
    response.set_cookie(
        key=f"broker_state_{slug}",
        value=state,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=int(OAUTH_STATE_TTL.total_seconds()),
        path=f"/api/v1/brokers/{slug}/callback",
    )
    return {"redirect_url": redirect_url, "connected": False}


@router.get("/{slug}/callback")
def broker_callback(
    slug: str,
    request: Request,
    state: str | None = None,
    request_token: str | None = None,
    code: str | None = None,
    tokenId: str | None = None,
    error: str | None = None,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
):
    adapter = _adapter(slug)
    if error:
        raise HTTPException(status_code=400, detail="Broker authorization was not completed")
    state_cookie = request.cookies.get(f"broker_state_{slug}")
    state_row = _valid_state(db, active_session, slug, state or state_cookie)
    parameters = {
        key: value
        for key, value in {
            "request_token": request_token,
            "code": code,
            "tokenId": tokenId,
        }.items()
        if value is not None
    }
    try:
        tokens, expires_at = adapter.exchange_callback(
            parameters, state_row.metadata_json or {}
        )
        encrypted_tokens = encrypt_tokens(tokens)
    except BrokerAdapterError as error:
        raise _broker_error(
            502,
            "broker_provider_error",
            "Broker authorization failed; retry the connection.",
        ) from error

    user_id = _user_id(active_session)
    return_origin = (state_row.metadata_json or {}).get("return_origin")
    db.delete(state_row)
    credential = db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == user_id,
            BrokerCredential.broker == slug,
        )
    )
    now = _now()
    if credential is None:
        credential = BrokerCredential(
            user_id=user_id,
            broker=slug,
            encrypted_tokens=encrypted_tokens,
            expires_at=expires_at,
            connected_at=now,
        )
        db.add(credential)
    else:
        credential.encrypted_tokens = encrypted_tokens
        credential.expires_at = expires_at
        credential.connected_at = now
    try:
        db.commit()
    except IntegrityError as exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="Broker connection could not be saved") from exception
    if slug in {"angel_one", "dhan", "yfinance"}:
        from app.api.market_data import reset_provider_circuit_breaker

        reset_provider_circuit_breaker(slug)
    if not return_origin:
        redirect_uri = getattr(adapter, "redirect_url", None)
        if not redirect_uri:
            raise HTTPException(status_code=500, detail="Broker return URL is unavailable")
        parsed = urlsplit(redirect_uri)
        return_origin = f"{parsed.scheme}://{parsed.netloc}"
    response = RedirectResponse(
        url=f"{return_origin}/?broker_connected={slug}",
        status_code=303,
    )
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        key=f"broker_state_{slug}",
        path=f"/api/v1/brokers/{slug}/callback",
        secure=settings.environment == "production",
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/{slug}/sync")
def sync_broker(
    slug: str,
    response: Response,
    from_date: date | None = Query(default=None, alias="from-date"),
    to_date: date | None = Query(default=None, alias="to-date"),
    snapshot_only: bool = Query(default=False),
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    if snapshot_only and slug != "dhan":
        raise HTTPException(status_code=422, detail="Snapshot-only sync is supported for Dhan.")
    adapter = _adapter(slug)
    credential = _credential(db, _user_id(active_session), slug)
    if slug == "dhan":
        tokens = decrypt_tokens(credential.encrypted_tokens)
        expiry = credential.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= _now():
            raise _broker_error(
                401,
                "broker_authentication_required",
                "Dhan session has expired. Reconnect before syncing account data.",
            )
        dhan_reader = DhanReadOnlyAccountAdapter(
            tokens["access_token"], tokens["client_id"]
        )
    else:
        tokens = _fresh_tokens(db, adapter, credential)
        dhan_reader = None
    now = _now()
    if snapshot_only:
        user_id = _user_id(active_session)
        from app.api import market_data as market_data_api

        portfolio_rows = [
            row for row in _mirror_rows(db, user_id, "dhan")
            if row.quantity != 0
            and _broker_row_portfolio(row) in {"holding", "position"}
            and not _is_cnc_sale(row)
        ]
        security_ids = {
            row.security_id for row in portfolio_rows if row.security_id is not None
        }
        all_dhan_mappings = (
            list(db.scalars(
                select(InstrumentProviderIdentifier).where(
                    InstrumentProviderIdentifier.provider == "dhan",
                    or_(*[
                        InstrumentProviderIdentifier.provider_instrument_id.like(
                            f"{security_id}|%"
                        )
                        for security_id in security_ids
                    ]),
                )
            ))
            if security_ids
            else []
        )
        verified_mappings = [
            mapping for mapping in all_dhan_mappings if mapping.is_verified
        ]
        mapped_instruments = {
            instrument.id: instrument
            for instrument in db.scalars(
                select(Instrument).where(
                    Instrument.id.in_({
                        mapping.instrument_id for mapping in verified_mappings
                    })
                )
            )
        } if verified_mappings else {}
        session_open = any(
            (instrument := mapped_instruments.get(mapping.instrument_id)) is not None
            and any(
                row.security_id == mapping.provider_instrument_id.partition("|")[0]
                and row.symbol.strip().upper().removesuffix("-EQ")
                == mapping.provider_symbol.strip().upper().removesuffix("-EQ")
                == instrument.symbol.strip().upper().removesuffix("-EQ")
                for row in portfolio_rows
            )
            and market_data_api._market_session_state(instrument, now) == "open"
            for mapping in verified_mappings
        )
        if not session_open:
            return {
                "broker": "dhan",
                "sync_status": "skipped",
                "reason": "market_closed",
                "snapshot_min_interval_seconds": DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS,
            }
        monotonic_now = time.monotonic()
        with _dhan_snapshot_attempts_lock:
            last_attempt = _dhan_snapshot_attempts.get(user_id)
            elapsed = (
                monotonic_now - last_attempt if last_attempt is not None else None
            )
            if elapsed is not None and elapsed < DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS:
                return {
                    "broker": "dhan",
                    "sync_status": "skipped",
                    "reason": "snapshot_cadence",
                    "retry_after_seconds": max(
                        1, int(DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS - elapsed)
                    ),
                    "snapshot_min_interval_seconds": DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS,
                }
            _dhan_snapshot_attempts[user_id] = monotonic_now
    dhan_to_date = to_date or now.date()
    latest_synced_trade = db.scalar(
        select(func.max(DhanTradeMirror.trade_at)).where(
            DhanTradeMirror.user_id == _user_id(active_session),
            DhanTradeMirror.source == "dhan",
        )
    ) if slug == "dhan" else None
    dhan_from_date = from_date or (
        latest_synced_trade.date() - timedelta(days=7)
        if latest_synced_trade is not None
        else dhan_to_date - timedelta(days=365)
    )
    if dhan_from_date > dhan_to_date:
        raise HTTPException(status_code=422, detail="from-date must not be after to-date")
    endpoint_results: dict[str, dict[str, str]] = {}
    normalized: dict[str, list[BrokerMirror]] = {}
    account_snapshot: BrokerAccountMirror | None = None
    normalized_trades: list[DhanTradeMirror] | None = None
    new_trades_count = 0
    normalized_ledger: list[DhanLedgerMirror] | None = None
    new_ledger_count = 0
    persistence_skips: list[dict[str, str]] = []
    authentication_expired = False
    endpoint_calls: tuple[tuple[str, str, str], ...] = (
        ("holdings", "get_holdings", "holding"),
        ("positions", "get_positions", "position"),
    ) + ((("fundlimit", "get_fundlimit", "funds"),) if slug == "dhan" else ())
    for endpoint, method_name, kind in endpoint_calls:
        try:
            if dhan_reader is not None:
                raw_rows = getattr(dhan_reader, method_name)()
            else:
                raw_rows = getattr(adapter, method_name)(
                    tokens["access_token"], tokens
                )
            endpoint_http_status = (
                str(dhan_reader.last_http_diagnostics[-1]["http_status"])
                if dhan_reader is not None and dhan_reader.last_http_diagnostics
                else None
            )
            success_result = {"status": "succeeded"}
            if endpoint_http_status is not None:
                success_result["http_status"] = endpoint_http_status
            if kind == "funds":
                if not isinstance(raw_rows, dict):
                    raise BrokerAdapterError(
                        "Dhan fund limit response has an unexpected format"
                    )
                account_snapshot = _normalize_dhan_funds(
                    raw_rows, _user_id(active_session), now
                )
                endpoint_results[endpoint] = success_result
                continue
            try:
                normalized[kind] = _normalize_rows(
                    raw_rows,
                    kind,
                    now,
                    _user_id(active_session),
                    slug,
                )
            except BrokerAdapterError as error:
                if slug == "dhan":
                    _log_dhan_sync_failure(
                        f"{endpoint}_response_normalization",
                        error,
                        f"{endpoint.title()} response could not be normalized.",
                    )
                raise
            if endpoint == "holdings" and endpoint_http_status == "500" and not raw_rows:
                success_result["reason"] = "no_holdings_available"
            endpoint_results[endpoint] = success_result
        except BrokerReconnectRequired as error:
            credential.expires_at = now
            db.commit()
            authentication_expired = True
            endpoint_results[endpoint] = {"status": "failed", "reason": "authentication_expired"}
            if slug == "dhan":
                _log_dhan_sync_failure(
                    f"{endpoint}_authentication",
                    error,
                    "Dhan authentication was rejected by the provider.",
                )
                break
            raise _broker_error(401, "broker_authentication_required", str(error)) from error
        except DhanAccountRequestError as error:
            endpoint_results[endpoint] = {
                "status": "failed",
                "reason": error.code,
            }
            if error.status_code is not None:
                endpoint_results[endpoint]["http_status"] = str(error.status_code)
            if error.code == "provider_authentication_failed":
                credential.expires_at = now
                db.commit()
                authentication_expired = True
                break
        except BrokerAdapterError as error:
            endpoint_results[endpoint] = {"status": "failed", "reason": "provider_error"}
            if slug != "dhan":
                raise _broker_error(
                    502, "broker_provider_error", "Broker account sync failed."
                ) from error

    if dhan_reader is not None and not snapshot_only:
        trade_records: list[dict[str, Any]] = []
        try:
            for page in range(100):
                if page:
                    time.sleep(1)
                page_records = dhan_reader.get_past_trades(
                    dhan_from_date.isoformat(),
                    dhan_to_date.isoformat(),
                    page,
                )
                trade_records.extend(page_records)
                if not page_records:
                    break
            else:
                raise DhanAccountRequestError("provider_pagination_limit")
            normalized_trades = _normalize_dhan_trades(
                trade_records, _user_id(active_session), now
            )
            result = {
                "status": "succeeded",
                "record_count": str(len(normalized_trades)),
            }
            if dhan_reader.last_http_diagnostics:
                result["http_status"] = str(
                    dhan_reader.last_http_diagnostics[-1]["http_status"]
                )
            endpoint_results["trades"] = result
        except DhanAccountRequestError as error:
            result = {"status": "failed", "reason": error.code}
            if error.status_code is not None:
                result["http_status"] = str(error.status_code)
            elif dhan_reader.last_http_diagnostics:
                status = dhan_reader.last_http_diagnostics[-1]["http_status"]
                if status is not None:
                    result["http_status"] = str(status)
            endpoint_results["trades"] = result
        except BrokerAdapterError as error:
            _log_dhan_sync_failure(
                "trades_response_normalization",
                error,
                "Past trades response could not be normalized.",
            )
            endpoint_results["trades"] = {
                "status": "failed",
                "reason": "provider_error",
            }

        try:
            ledger_records = dhan_reader.get_ledger(
                dhan_from_date.isoformat(), dhan_to_date.isoformat()
            )
            unkeyed_ledger_count = sum(
                not row.get("vouchernumber") for row in ledger_records
            )
            known_ledger_exceptions = _record_dhan_ledger_exceptions(
                db,
                _user_id(active_session),
                [row for row in ledger_records if not row.get("vouchernumber")],
                now,
            )
            if unkeyed_ledger_count:
                logger.warning(
                    "Dhan ledger entries skipped because provider entry IDs are missing count=%d",
                    unkeyed_ledger_count,
                )
            normalized_ledger = []
            malformed_ledger_count = 0
            for index, row in enumerate(ledger_records):
                if not row.get("vouchernumber") or not str(row["vouchernumber"]).strip():
                    continue
                try:
                    normalized_ledger.extend(_normalize_dhan_ledger(
                        [row], _user_id(active_session), now
                    ))
                except BrokerAdapterError:
                    malformed_ledger_count += 1
                    logger.warning(
                        "Dhan ledger record skipped stage=ledger_normalization record=%d error_class=BrokerAdapterError",
                        index,
                    )
            result = {
                "status": "partial" if malformed_ledger_count else "succeeded",
                "record_count": str(len(normalized_ledger)),
                "received_count": str(len(ledger_records)),
                "malformed_count": str(malformed_ledger_count),
                "known_exception_count": str(len(known_ledger_exceptions)),
                "known_exceptions": known_ledger_exceptions,
            }
            if dhan_reader.last_http_diagnostics:
                result["http_status"] = str(
                    dhan_reader.last_http_diagnostics[-1]["http_status"]
                )
            endpoint_results["ledger"] = result
        except DhanAccountRequestError as error:
            result = {"status": "failed", "reason": error.code}
            if error.status_code is not None:
                result["http_status"] = str(error.status_code)
            elif dhan_reader.last_http_diagnostics:
                status = dhan_reader.last_http_diagnostics[-1]["http_status"]
                if status is not None:
                    result["http_status"] = str(status)
            endpoint_results["ledger"] = result
        except BrokerAdapterError as error:
            _log_dhan_sync_failure(
                "ledger_response_normalization",
                error,
                "Ledger response could not be normalized.",
            )
            endpoint_results["ledger"] = {
                "status": "failed",
                "reason": "provider_error",
            }

    failed_endpoints = [
        endpoint for endpoint, result in endpoint_results.items()
        if result["status"] == "failed"
    ]
    if failed_endpoints and not normalized and account_snapshot is None and (
        normalized_trades is None and normalized_ledger is None
    ):
        if authentication_expired:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "broker_authentication_required",
                    "message": (
                        f"Dhan authentication expired at the {failed_endpoints[0]} "
                        "endpoint. Reconnect and retry account sync."
                    ),
                    "sync_status": "failed",
                    "endpoint_results": endpoint_results,
                },
            )
        detail = {
            "code": "broker_sync_failed",
            "message": (
                f"{slug.title()} account sync failed at the "
                f"{', '.join(failed_endpoints)} endpoint."
            ),
            "sync_status": "failed",
            "endpoint_results": endpoint_results,
        }
        raise HTTPException(status_code=502, detail=detail)

    completed_at = _now()
    for rows in normalized.values():
        for row in rows:
            row.synced_at = completed_at
    if account_snapshot is not None:
        account_snapshot.synced_at = completed_at
    for row in normalized_trades or []:
        row.synced_at = completed_at
    for row in normalized_ledger or []:
        row.synced_at = completed_at

    credential = db.scalar(
        select(BrokerCredential)
        .where(
            BrokerCredential.user_id == _user_id(active_session),
            BrokerCredential.broker == slug,
        )
        .with_for_update()
    )
    if credential is None:
        raise HTTPException(status_code=409, detail="Broker was disconnected during sync")
    try:
        account_persisted = False
        # Flush the once-only quality records before opening row savepoints.
        db.flush()
        for kind, rows in normalized.items():
            for index, row in enumerate(rows):
                try:
                    with db.begin_nested():
                        _upsert_broker_mirror(db, row)
                        db.flush()
                except IntegrityError as error:
                    persistence_skips.append({"record_type": kind, "record": str(index)})
                    _log_dhan_sync_failure(
                        "broker_mirror_record", error,
                        "Broker position mirror record skipped; database details omitted.",
                    )
            # Remove only stale rows for endpoint data that was fetched successfully.
            existing_rows = db.scalars(select(BrokerMirror).where(
                BrokerMirror.user_id == _user_id(active_session),
                BrokerMirror.broker == slug,
                BrokerMirror.kind == kind,
            )).all()
            seen_keys = {(row.symbol, row.exchange, row.product) for row in rows}
            for existing_row in existing_rows:
                if (existing_row.symbol, existing_row.exchange, existing_row.product) not in seen_keys:
                    db.delete(existing_row)
        if account_snapshot is not None:
            try:
                with db.begin_nested():
                    existing_account = db.scalar(select(BrokerAccountMirror).where(
                        BrokerAccountMirror.user_id == _user_id(active_session),
                        BrokerAccountMirror.broker == slug,
                    ))
                    if existing_account is None:
                        db.add(account_snapshot)
                    else:
                        for field in (
                            "currency", "available_balance", "sod_limit",
                            "collateral_amount", "receivable_amount", "utilized_amount",
                            "blocked_payout_amount", "withdrawable_balance", "synced_at",
                        ):
                            setattr(existing_account, field, getattr(account_snapshot, field))
                    db.flush()
                account_persisted = True
            except IntegrityError as error:
                persistence_skips.append({"record_type": "account", "record": "0"})
                _log_dhan_sync_failure(
                    "broker_mirror_record", error,
                    "Broker account mirror record skipped; database details omitted.",
                )
        new_trades_count, _updated_trades_count, skipped_trades = _upsert_dhan_trades(
            db, normalized_trades or []
        )
        persistence_skips.extend({"record_type": "trade", **row} for row in skipped_trades)
        if "trades" in endpoint_results:
            endpoint_results["trades"]["new_count"] = str(new_trades_count)
        for index, row in enumerate(normalized_ledger or []):
            try:
                with db.begin_nested():
                    inserted = _upsert_dhan_ledger_entry(db, row)
                    db.flush()
                new_ledger_count += int(inserted)
            except IntegrityError as error:
                persistence_skips.append({"record_type": "ledger", "record": str(index)})
                _log_dhan_sync_failure(
                    "broker_mirror_record", error,
                    "Dhan ledger mirror record skipped; database details omitted.",
                )
        if "ledger" in endpoint_results:
            endpoint_results["ledger"]["new_count"] = str(new_ledger_count)
            endpoint_results["ledger"]["persistence_skipped_count"] = str(
                sum(item["record_type"] == "ledger" for item in persistence_skips)
            )
        if not failed_endpoints:
            credential.last_synced_at = completed_at
        db.commit()
    except Exception as error:
        if slug == "dhan":
            _log_dhan_sync_failure(
                "broker_mirror_persistence",
                error,
                "Broker mirror transaction failed; database details omitted.",
            )
        if not isinstance(error, IntegrityError):
            raise
        db.rollback()
        raise HTTPException(status_code=500, detail="Broker account sync could not be saved") from error
    return {
        "broker": slug,
        "synthetic": adapter.is_synthetic,
        "synced_at": completed_at,
        "sync_status": (
            "partial"
            if failed_endpoints
            or persistence_skips
            or any(result["status"] == "partial" for result in endpoint_results.values())
            else "successful"
        ),
        "authentication_status": "expired" if authentication_expired else "connected",
        "endpoint_results": endpoint_results,
        "holdings_count": len(normalized.get("holding", [])),
        "positions_count": len(normalized.get("position", [])),
        "cash_synced": account_persisted,
        "trades_count": new_trades_count if normalized_trades is not None else None,
        "ledger_count": new_ledger_count if normalized_ledger is not None else None,
        "persistence_skips": persistence_skips,
        "from_date": dhan_from_date.isoformat() if dhan_reader is not None else None,
        "to_date": dhan_to_date.isoformat() if dhan_reader is not None else None,
        "snapshot_only": snapshot_only,
        "snapshot_min_interval_seconds": (
            DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS if snapshot_only else None
        ),
    }


@router.post("/dhan/trades/backfill")
def backfill_dhan_trades(
    response: Response,
    from_date: date | None = Query(default=None, alias="from-date"),
    to_date: date | None = Query(default=None, alias="to-date"),
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    credential = _credential(db, _user_id(active_session), "dhan")
    expiry = credential.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= _now():
        raise _broker_error(
            401,
            "broker_authentication_required",
            "Dhan session has expired. Reconnect before backfilling trade history.",
        )
    end = to_date or _now().date()
    latest_trade_at = db.scalar(
        select(func.max(DhanTradeMirror.trade_at)).where(
            DhanTradeMirror.user_id == _user_id(active_session),
            DhanTradeMirror.source == "dhan",
        )
    )
    cursor_start = (
        latest_trade_at.date() - timedelta(days=7)
        if latest_trade_at is not None
        else date(2023, 1, 1)
    )
    effective_start = from_date or cursor_start
    if effective_start > end:
        raise HTTPException(status_code=422, detail="from-date must not be after to-date")
    tokens = decrypt_tokens(credential.encrypted_tokens)
    reader = DhanReadOnlyAccountAdapter(tokens["access_token"], tokens["client_id"])
    now = _now()
    yearly_results: list[dict[str, Any]] = []
    all_rows: list[DhanTradeMirror] = []
    page = 0
    page_count = 0
    status: int | None = None
    failure: str | None = None
    chunk_rows: list[dict[str, Any]] = []
    while True:
        if page_count:
            time.sleep(1)
        try:
            page_rows = reader.get_past_trades(
                effective_start.isoformat(), end.isoformat(), page
            )
            diagnostic = reader.last_http_diagnostics[-1]
            status = diagnostic["http_status"]
            chunk_rows.extend(page_rows)
            logger.info(
                "dhan_trade_backfill_chunk",
                extra={
                    "dhan_trade_chunk": {
                        "url": diagnostic["url"],
                        "date_range": f"{effective_start.isoformat()}..{end.isoformat()}",
                        "http_status": status,
                        "page": page,
                        "record_count": len(page_rows),
                    }
                },
            )
        except DhanAccountRequestError as error:
            diagnostic = reader.last_http_diagnostics[-1] if reader.last_http_diagnostics else {}
            status = diagnostic.get("http_status")
            failure = error.code
            logger.warning(
                "dhan_trade_backfill_chunk_failed",
                extra={
                    "dhan_trade_chunk": {
                        "url": diagnostic.get("url"),
                        "date_range": f"{effective_start.isoformat()}..{end.isoformat()}",
                        "http_status": status,
                        "page": page,
                        "reason": failure,
                    }
                },
            )
            break
        page_count += 1
        if not page_rows:
            break
        page += 1

    if not failure:
        try:
            all_rows = _normalize_dhan_trades(
                chunk_rows, _user_id(active_session), now, source="dhan"
            )
        except BrokerAdapterError as error:
            failure = "invalid_provider_response"
            _log_dhan_sync_failure(
                "trade_backfill",
                error,
                "Trade history chunk could not be normalized.",
            )

    year_counts: dict[int, int] = {}
    for row in chunk_rows:
        trade_at = row.get("exchangeTime", row.get("updateTime", row.get("createTime")))
        try:
            trade_year = _dhan_timestamp(trade_at, "trade time").year
        except BrokerAdapterError:
            continue
        year_counts[trade_year] = year_counts.get(trade_year, 0) + 1

    for cursor_year in range(effective_start.year, end.year + 1):
        chunk_start = max(effective_start, date(cursor_year, 1, 1))
        chunk_end = min(end, date(cursor_year, 12, 31))
        year_rows = [row for row in all_rows if row.trade_at.year == cursor_year]
        yearly_results.append(
            {
                "year": cursor_year,
                "from_date": chunk_start.isoformat(),
                "to_date": chunk_end.isoformat(),
                "http_status": status,
                "pages": page_count,
                "records_received": year_counts.get(cursor_year, 0),
                "status": "failed" if failure else "succeeded",
                **({"reason": failure} if failure else {}),
                "trades_imported": 0,
            }
        )

    if any(result["status"] == "failed" for result in yearly_results):
        if not all_rows:
            raise HTTPException(
                status_code=502,
                detail={"code": "dhan_trade_backfill_failed", "years": yearly_results},
            )
    existing_provider_ids = set(db.scalars(
        select(DhanTradeMirror.provider_trade_id).where(
            DhanTradeMirror.user_id == _user_id(active_session),
            DhanTradeMirror.source == "dhan",
            DhanTradeMirror.provider_trade_id.is_not(None),
        )
    ).all())
    existing_provider_ids.update(db.scalars(
        select(DhanTradeMirror.trade_id).where(
            DhanTradeMirror.user_id == _user_id(active_session),
            DhanTradeMirror.source == "dhan",
            DhanTradeMirror.provider_trade_id.is_(None),
        )
    ).all())
    inserted, updated = _upsert_dhan_trades(db, all_rows)
    inserted_by_year: dict[int, int] = {}
    # Inserted rows are identifiable by the provider trade IDs just returned by Dhan.
    inserted_provider_ids = {row.provider_trade_id for row in all_rows} - existing_provider_ids
    for row in all_rows:
        if row.provider_trade_id in inserted_provider_ids:
            inserted_by_year[row.trade_at.year] = inserted_by_year.get(row.trade_at.year, 0) + 1
    for result in yearly_results:
        result["trades_imported"] = inserted_by_year.get(result["year"], 0)
    db.commit()
    return {
        "source": "dhan",
        "from_date": effective_start,
        "to_date": end,
        "inserted": inserted,
        "updated": updated,
        "years": yearly_results,
    }


@router.post("/dhan/trades/import-csv")
def import_dhan_trade_csv(
    payload: DhanTradeCsvRequest,
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if db.scalar(
        select(BrokerCredential).where(
            BrokerCredential.user_id == _user_id(active_session),
            BrokerCredential.broker == "dhan",
        )
    ) is None:
        raise HTTPException(status_code=409, detail="Connect Dhan before importing its tradebook")
    try:
        records = list(csv.DictReader(io.StringIO(payload.csv_content)))
    except csv.Error as error:
        raise HTTPException(status_code=422, detail="Dhan CSV could not be parsed") from error
    if not records:
        raise HTTPException(status_code=422, detail="Dhan CSV contains no trade rows")

    normalized_rows: list[dict[str, Any]] = []
    for row_number, record in enumerate(records, start=2):
        normalized = {
            re.sub(r"[^a-z0-9]", "", str(key or "").casefold()): value
            for key, value in record.items()
            if key is not None
        }

        def value(*names: str) -> str | None:
            return next(
                (
                    str(normalized[name]).strip()
                    for name in names
                    if normalized.get(name) is not None
                    and str(normalized[name]).strip()
                ),
                None,
            )

        trade_id = value("exchangetradeid", "tradeid", "tradeidnumber")
        quantity = value("tradedquantity", "quantity", "qty")
        price = value("tradedprice", "price", "rate")
        side = value("transactiontype", "side", "buysell")
        trade_time = value("exchangetime", "tradetime", "tradedatetime")
        if trade_time is None:
            trade_date = value("tradedate", "exchangedate", "date")
            time_value = value("time")
            trade_time = f"{trade_date} {time_value}" if trade_date and time_value else trade_date
        if not all((trade_id, quantity, price, side, trade_time)):
            raise HTTPException(
                status_code=422,
                detail=f"Dhan CSV row {row_number} is missing trade ID, side, quantity, price, or time",
            )
        item: dict[str, Any] = {
            "exchangeTradeId": trade_id,
            "orderId": value("orderid"),
            "exchangeOrderId": value("exchangeorderid"),
            "transactionType": side.upper(),
            "exchangeSegment": value("exchangesegment", "exchange"),
            "securityId": value("securityid"),
            "tradingSymbol": value("tradingsymbol", "symbol", "scrip"),
            "customSymbol": value("customsymbol"),
            "tradedQuantity": quantity,
            "tradedPrice": price,
            "exchangeTime": trade_time,
        }
        charges = value("charges", "totalcharges", "totalchargesandtaxes")
        if charges is not None:
            item["charges"] = charges
        else:
            for key, aliases in {
                "sebiTax": ("sebitax",),
                "stt": ("stt",),
                "brokerageCharges": ("brokeragecharges", "brokerage"),
                "serviceTax": ("servicetax", "gst"),
                "exchangeTransactionCharges": ("exchangetransactioncharges",),
                "stampDuty": ("stampduty",),
            }.items():
                candidate = value(*aliases)
                if candidate is not None:
                    item[key] = candidate
        normalized_rows.append(item)

    now = _now()
    try:
        mirror_rows = _normalize_dhan_trades(
            normalized_rows, _user_id(active_session), now, source="dhan_csv"
        )
    except (BrokerAdapterError, InvalidOperation, ValueError) as error:
        raise HTTPException(status_code=422, detail="Dhan CSV contains invalid trade data") from error
    inserted, updated = _upsert_dhan_trades(db, mirror_rows)
    db.commit()
    return {
        "source": "dhan_csv",
        "records_received": len(records),
        "inserted": inserted,
        "updated": updated,
    }


@router.get("/dhan/transactions")
def get_dhan_transaction_mirror(
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    user_id = _user_id(active_session)
    trades = db.scalars(
        select(DhanTradeMirror)
        .where(DhanTradeMirror.user_id == user_id, DhanTradeMirror.source == "dhan")
        .order_by(DhanTradeMirror.trade_at.desc(), DhanTradeMirror.trade_id)
    ).all()
    ledger = db.scalars(
        select(DhanLedgerMirror)
        .where(DhanLedgerMirror.user_id == user_id, DhanLedgerMirror.source == "dhan")
        .order_by(DhanLedgerMirror.entry_date.desc(), DhanLedgerMirror.entry_id)
    ).all()
    return {
        "source": "dhan",
        "currency": "INR",
        "trades": [
            {
                "id": str(row.id),
                "trade_id": row.provider_trade_id or row.trade_id,
                "exchange_order_id": row.exchange_order_id,
                "order_id": row.order_id,
                "symbol": row.symbol,
                "security_id": row.security_id,
                "exchange_segment": row.exchange_segment,
                "side": row.side,
                "quantity": row.quantity,
                "price": row.price,
                "charges": row.charges,
                "trade_at": row.trade_at,
                "broker": "Dhan",
                "source": row.source,
                "synced_at": row.synced_at,
            }
            for row in trades
        ],
        "ledger": [
            {
                "id": str(row.id),
                "entry_id": row.provider_entry_id or row.entry_id,
                "provider_entry_id": row.provider_entry_id,
                "entry_date": row.entry_date,
                "description": row.description,
                "debit": row.debit,
                "credit": row.credit,
                "running_balance": row.running_balance,
                "currency": row.currency,
                "broker": "Dhan",
                "source": row.source,
                "synced_at": row.synced_at,
            }
            for row in ledger
        ],
    }


@router.get("/{slug}/mirror", response_model=list[MirrorRow])
def get_broker_mirror(
    slug: str,
    response: Response,
    kind: str | None = Query(default=None, pattern="^(holding|position)$"),
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    if slug not in ADAPTERS:
        raise HTTPException(status_code=404, detail="Broker not supported")
    return [
        {
            "kind": row.kind,
            "symbol": row.symbol,
            "exchange": row.exchange,
            "product": row.product,
            "security_id": row.security_id,
            "isin": row.isin,
            "source": row.broker,
            "currency": "INR" if row.broker == "dhan" else "",
            "quantity": row.quantity,
            "average_price": row.average_price,
            "synced_at": row.synced_at,
        }
        for row in _mirror_rows(db, _user_id(active_session), slug, kind)
    ]


@router.get("/dhan/portfolio")
def get_dhan_portfolio_mirror(
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    user_id = _user_id(active_session)
    now = _now()
    rows = _mirror_rows(db, user_id, "dhan")
    account = db.scalar(
        select(BrokerAccountMirror).where(
            BrokerAccountMirror.user_id == user_id,
            BrokerAccountMirror.broker == "dhan",
        )
    )
    position_rows = [
        row for row in rows
        if _broker_row_portfolio(row) == "position"
    ]
    holding_rows = [
        row for row in rows
        if _broker_row_portfolio(row) == "holding" and not _is_cnc_sale(row)
    ]
    security_ids = {
        row.security_id for row in rows if row.security_id is not None
    }
    all_dhan_mappings = (
        list(db.scalars(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.provider == "dhan",
                or_(*[
                    InstrumentProviderIdentifier.provider_instrument_id.like(
                        f"{security_id}|%"
                    )
                    for security_id in security_ids
                ]),
            )
        ))
        if security_ids
        else []
    )
    verified_mappings = [
        mapping for mapping in all_dhan_mappings if mapping.is_verified
    ]
    mapped_instruments = {
        instrument.id: instrument
        for instrument in db.scalars(
            select(Instrument).where(
                Instrument.id.in_({
                    mapping.instrument_id for mapping in verified_mappings
                })
            )
        )
    } if verified_mappings else {}
    verified_segments: dict[tuple[str, str], set[str]] = {}
    verified_instruments: dict[tuple[str, str], set[UUID]] = {}
    for mapping in verified_mappings:
        security_id, separator, segment = (
            mapping.provider_instrument_id.partition("|")
        )
        instrument = mapped_instruments.get(mapping.instrument_id)
        mapped_symbols = (
            mapping.provider_symbol.strip().upper().removesuffix("-EQ"),
            instrument.symbol.strip().upper().removesuffix("-EQ")
            if instrument is not None else "",
        )
        if separator and segment and mapped_symbols[0] == mapped_symbols[1]:
            verified_segments.setdefault(
                (security_id, mapped_symbols[0]), set()
            ).add(segment)
            verified_instruments.setdefault(
                (security_id, mapped_symbols[0]), set()
            ).add(mapping.instrument_id)

    unresolved_rows = [
        row for row in rows
        if row.security_id is not None
        and len(verified_segments.get((
            row.security_id,
            row.symbol.strip().upper().removesuffix("-EQ"),
        ), set())) != 1
    ]
    master_rows: list[dict[str, str]] = []
    master_refreshed_at: datetime | None = None
    mapping_candidates: list[dict[str, Any]] = []
    if unresolved_rows:
        try:
            master_rows, master_refreshed_at = _dhan_instrument_master(
                unresolved_rows
            )
        except BrokerAdapterError as error:
            logger.warning(
                "Dhan instrument master resolution unavailable category=%s",
                error.category,
            )
            mapping_candidates.extend({
                "symbol": row.symbol,
                "security_id": row.security_id,
                "isin": row.isin,
                "provider_instrument_id": None,
                "segment": None,
                "listing_date": None,
                "status": "master_unavailable",
                "reason": "Dhan instrument master could not be refreshed; no mapping conclusion was made.",
                "verification_required": None,
            } for row in unresolved_rows)
        else:
            for row in unresolved_rows:
                matches = _dhan_master_matches(
                    master_rows, row.security_id, row.isin
                )
                if len(matches) != 1:
                    mapping_candidates.append({
                        "symbol": row.symbol,
                        "security_id": row.security_id,
                        "isin": row.isin,
                        "provider_instrument_id": None,
                        "segment": None,
                        "listing_date": None,
                        "status": "ambiguous" if matches else "not_found",
                        "reason": (
                            "Multiple exact security ID or ISIN matches remain in the refreshed Dhan master."
                            if matches
                            else "No exact security ID or ISIN match was found in the refreshed Dhan master."
                        ),
                        "verification_required": None,
                    })
                    continue
                master_row = matches[0]
                exchange = master_row["EXCH_ID"].strip().upper()
                segment = {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}.get(exchange)
                master_security_id = master_row["SECURITY_ID"].strip()
                if segment is None:
                    mapping_candidates.append({
                        "symbol": row.symbol,
                        "security_id": row.security_id,
                        "isin": row.isin,
                        "provider_instrument_id": None,
                        "segment": None,
                        "listing_date": _dhan_master_listing_date(master_row),
                        "status": "unsupported_segment",
                        "reason": "The exact Dhan master match is not in a supported equity segment.",
                        "verification_required": None,
                    })
                    continue
                provider_instrument_id = dhan_provider_instrument_id(master_security_id, segment)
                target_instrument_ids = {
                    mapping.instrument_id
                    for mapping in all_dhan_mappings
                    if mapping.provider_instrument_id.partition("|")[0]
                    == (row.security_id or "")
                }
                candidate_mapping = next(
                    (
                        mapping for mapping in all_dhan_mappings
                        if mapping.provider_instrument_id
                        in dhan_provider_id_variants(master_security_id, segment)
                    ),
                    None,
                )
                if candidate_mapping is None and len(target_instrument_ids) == 1:
                    instrument_id = next(iter(target_instrument_ids))
                    provider_symbol = (
                        master_row.get("UNDERLYING_SYMBOL", "").strip()
                        or master_row.get("SYMBOL_NAME", "").strip()
                    )
                    candidate_mapping = InstrumentProviderIdentifier(
                        instrument_id=instrument_id,
                        provider="dhan",
                        provider_instrument_id=provider_instrument_id,
                        provider_symbol=provider_symbol,
                        is_verified=False,
                        last_seen_at=now,
                    )
                    db.add(candidate_mapping)
                    db.flush()
                    all_dhan_mappings.append(candidate_mapping)
                candidate_status = (
                    "pending_verification"
                    if candidate_mapping is not None and not candidate_mapping.is_verified
                    else "verified"
                    if candidate_mapping is not None
                    else "requires_instrument_link"
                    if not target_instrument_ids
                    else "ambiguous_local_instrument"
                )
                mapping_candidates.append({
                    "symbol": row.symbol,
                    "security_id": row.security_id,
                    "isin": row.isin,
                    "provider_instrument_id": provider_instrument_id,
                    "segment": segment,
                    "listing_date": _dhan_master_listing_date(master_row),
                    "status": candidate_status,
                    "reason": (
                        None
                        if candidate_status in {"pending_verification", "verified"}
                        else "No unique local instrument is linked by exact Dhan security ID; link the intended instrument before verification."
                    ),
                    "verification_required": (
                        "Confirm the instrument identity and exchange segment, then verify the mapping."
                        if candidate_status == "pending_verification"
                        else None
                    ),
                })
            if any(
                candidate.get("status") == "pending_verification"
                for candidate in mapping_candidates
            ):
                db.commit()
    if master_refreshed_at is None and DhanAdapter._scrip_master_cache is not None:
        master_refreshed_at = _now() - timedelta(
            seconds=max(
                0.0,
                time.monotonic() - DhanAdapter._scrip_master_cache[0],
            )
        )

    # Per-row valuation comes from the same evidence and calculation as
    # /portfolio-summary, so rows and totals cannot disagree.
    (
        valuation_quotes,
        valuation_previous_closes,
        valuation_missing,
        valuation_attempts,
    ) = _dhan_quote_evidence(db, rows, user_id, now)
    row_valuations = _calculate_broker_values(
        db,
        user_id,
        rows,
        None,
        None,
        valuation_quotes,
        valuation_previous_closes,
        valuation_missing,
        valuation_attempts,
    )["row_valuations"]
    market_session: dict[str, str] | None = None
    for _, session_instrument in valuation_quotes.values():
        session = venue_session(session_instrument, now)
        if session is not None:
            market_session = {
                "venue": "NSE"
                if session_instrument.venue.upper() in {"NSE", "XNSE"}
                else session_instrument.venue,
                "state": session.state,
                "reason": session.reason,
            }
            break

    def resolved_instrument_id(row: BrokerMirror) -> str | None:
        key = (
            row.security_id or "",
            row.symbol.strip().upper().removesuffix("-EQ"),
        )
        instrument_ids = verified_instruments.get(key, set())
        return str(next(iter(instrument_ids))) if len(instrument_ids) == 1 else None

    def live_valuation(row: BrokerMirror) -> dict[str, Any]:
        valuation = row_valuations.get(row.id)
        if valuation is not None:
            return valuation
        return {
            "status": BASIS_UNAVAILABLE,
            "basis": BASIS_UNAVAILABLE,
            "price": None,
            "provider_timestamp": None,
            "price_age_seconds": None,
            "source": None,
            "market_value": None,
            "unrealized_profit": None,
            "day_change": None,
            "prior_close": None,
            "quote_at": None,
            "stale_reason": "This row is not an open holding or position.",
            "day_change_reason": "No valued price.",
        }

    def exchange_fields(row: BrokerMirror) -> dict[str, str | None]:
        normalized_symbol = row.symbol.strip().upper().removesuffix("-EQ")
        segments = verified_segments.get(
            (row.security_id or "", normalized_symbol), set()
        )
        if len(segments) == 1:
            return {
                "exchange": next(iter(segments)),
                "exchange_raw": row.exchange,
                "exchange_resolution_reason": None,
            }
        return {
            "exchange": row.exchange,
            "exchange_raw": row.exchange,
            "exchange_resolution_reason": (
                "No unique verified Dhan segment mapping; showing broker raw value."
            ),
        }

    def serialize_position(row: BrokerMirror) -> dict[str, Any]:
        return {
            "symbol": row.symbol,
            **exchange_fields(row),
            "product": row.product,
            "security_id": row.security_id,
            "instrument_id": resolved_instrument_id(row),
            "quantity": row.quantity,
            "average_price": row.average_price,
            "position_type": row.position_type,
            "realized_profit": row.realized_profit,
            "unrealized_profit": row.unrealized_profit,
            "broker_unrealized_profit_basis": "broker_snapshot",
            "broker_snapshot_at": row.synced_at,
            "live_valuation": live_valuation(row),
            "synced_at": row.synced_at,
        }

    open_positions = [
        row for row in position_rows
        if row.quantity != 0 and (
            row.position_type is None
            or row.position_type in {"LONG", "SHORT"}
        ) and not _is_cnc_sale(row)
    ]
    closed_positions = [
        row for row in position_rows
        if row not in open_positions and not _is_cnc_sale(row)
    ]
    delivery_sales = [row for row in rows if _is_cnc_sale(row)]
    return {
        "source": "dhan",
        "currency": "INR",
        "synced_at": max(
            (
                row.synced_at for row in rows
            ),
            default=account.synced_at if account is not None else None,
        ),
        "snapshot_min_interval_seconds": DHAN_SNAPSHOT_MIN_INTERVAL_SECONDS,
        "market_session": market_session,
        "instrument_master": {
            "last_refreshed_at": (
                master_refreshed_at.isoformat()
                if master_refreshed_at is not None
                else None
            ),
            "mapping_candidates": mapping_candidates,
        },
        "holdings": [
            {
                "symbol": row.symbol,
                **exchange_fields(row),
                "product": row.product,
                "security_id": row.security_id,
                "instrument_id": resolved_instrument_id(row),
                "isin": row.isin,
                "quantity": row.quantity,
                "average_price": row.average_price,
                "live_valuation": live_valuation(row),
                "synced_at": row.synced_at,
            }
            for row in holding_rows
        ],
        "positions": [serialize_position(row) for row in open_positions],
        "closed_positions": [serialize_position(row) for row in closed_positions],
        "delivery_sales": [serialize_position(row) for row in delivery_sales],
        "funds": (
            {
                "available_balance": account.available_balance,
                "sod_limit": account.sod_limit,
                "collateral_amount": account.collateral_amount,
                "receivable_amount": account.receivable_amount,
                "utilized_amount": account.utilized_amount,
                "blocked_payout_amount": account.blocked_payout_amount,
                "withdrawable_balance": account.withdrawable_balance,
                "synced_at": account.synced_at,
            }
            if account is not None
            else None
        ),
    }


def _dhan_quote_segment(exchange: str) -> str | None:
    return {
        "NSE": "NSE_EQ",
        "XNSE": "NSE_EQ",
        "NSE_EQ": "NSE_EQ",
        "NSE_FNO": "NSE_FNO",
        "NSE_CURRENCY": "NSE_CURRENCY",
        "BSE": "BSE_EQ",
        "XBOM": "BSE_EQ",
        "BOM": "BSE_EQ",
        "BSE_EQ": "BSE_EQ",
        "BSE_FNO": "BSE_FNO",
        "BSE_CURRENCY": "BSE_CURRENCY",
        "MCX_COMM": "MCX_COMM",
    }.get(exchange.upper())


def _active_broker_rows(rows: list[BrokerMirror]) -> list[BrokerMirror]:
    return [
        row for row in rows
        if row.quantity != 0
        and not _is_cnc_sale(row)
        and (
            row.kind == "holding"
            or (
                row.kind == "position"
                and row.position_type != "CLOSED"
            )
        )
    ]


def _broker_row_portfolio(row: BrokerMirror) -> str | None:
    product = (row.product or "").strip().upper()
    if product in {"CNC", "DELIVERY"}:
        return "holding"
    if product in {"MIS", "INTRADAY"}:
        return "position"
    if row.kind == "holding":
        return "holding"
    if row.kind == "position":
        return "position"
    return None


def _is_cnc_sale(row: BrokerMirror) -> bool:
    return (
        (row.product or "").strip().upper() == "CNC"
        and row.quantity < 0
    )


def _observation_price_basis(quote: Any, instrument: Any, db: Session) -> str:
    """Basis for a quote observation, derived from the venue session, not the row type."""
    if isinstance(quote, MarketBar):
        return BASIS_SESSION_CLOSE
    now = _now()
    session = venue_session(instrument, now)
    if session is not None and session.state == "open":
        # An open-session price is "live" only when it classifies as live. An older tick is
        # "delayed" and is never labelled live. The closed-session label is unchanged.
        _, _, evidence = classify_quote(
            quote,
            instrument,
            "INR",
            now,
            last_completed_session=venue_last_completed_session(
                db, instrument, quote.source
            ),
        )
        status = str(evidence.get("quote_freshness_status", ""))
    else:
        observed_at = _utc_datetime(quote.timestamp).astimezone(ZoneInfo(instrument.timezone))
        session_close = datetime.combine(observed_at.date(), wall_time(15, 30), tzinfo=ZoneInfo(instrument.timezone))
        status = (
            "delayed"
            if quote.source in {"dhan_ws", "dhan"} and observed_at < session_close
            else FRESHNESS_SESSION_CLOSE
        )
    return valuation_basis_for_quote(status, quote.timestamp, instrument, now) or BASIS_UNAVAILABLE


def _observation(
    symbol: str,
    source: str,
    timestamp: datetime | None,
) -> dict[str, str | None]:
    return {
        "symbol": symbol,
        "source": source,
        "timestamp": timestamp.isoformat() if timestamp is not None else None,
    }


def _utc_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _price_age_seconds(quote: Any) -> float | None:
    """Seconds since the provider's own timestamp, measured on the server clock.

    None for a stored daily bar: its timestamp is a UTC-midnight session key, not a tick time.
    """
    if isinstance(quote, MarketBar):
        return None
    return max(0.0, (_now() - _utc_datetime(quote.timestamp)).total_seconds())


def _portfolio_input_reason(reasons: list[str]) -> str:
    unique_reasons = list(dict.fromkeys(reasons))
    if unique_reasons and all(
        reason.startswith("Unverified Dhan provider mapping")
        for reason in unique_reasons
    ):
        return "; ".join(unique_reasons)
    return "Missing input: " + "; ".join(unique_reasons)


def _calculate_broker_values(
    db: Session,
    user_id: UUID,
    rows: list[BrokerMirror],
    cash: Decimal | None,
    cash_synced_at: datetime | None,
    quote_by_row_id: dict[UUID, tuple[MarketQuote | MarketBar, Instrument]],
    previous_close_by_row_id: dict[UUID, MarketBar],
    missing_quote_by_row_id: dict[UUID, str],
    provider_attempts_by_row_id: dict[UUID, list[str]] | None = None,
) -> dict[str, Any]:
    active_rows = _active_broker_rows(rows)
    holdings = [
        row for row in active_rows
        if _broker_row_portfolio(row) == "holding"
    ]
    positions = [
        row for row in active_rows
        if _broker_row_portfolio(row) == "position"
    ]
    snapshot_known = bool(rows)
    broker_snapshot_at = max((row.synced_at for row in rows), default=None)
    missing_ltp = [
        missing_quote_by_row_id[row.id].removeprefix("Missing input: ")
        for row in active_rows
        if row.id not in quote_by_row_id
    ]
    quote_observations_by_row: dict[UUID, dict[str, str | None]] = {}
    for row in active_rows:
        quote_and_instrument = quote_by_row_id.get(row.id)
        if quote_and_instrument is None:
            continue
        quote, instrument = quote_and_instrument
        quote_observations_by_row[row.id] = {
            **_observation(
                instrument.symbol,
                quote.source,
                quote.timestamp,
            ),
            "quantity": str(row.quantity),
            "ltp": str(quote.close),
            "price_basis": _observation_price_basis(quote, instrument, db),
            "price_age_seconds": _price_age_seconds(quote),
            "basis_date": _utc_datetime(quote.timestamp).astimezone(
                ZoneInfo(instrument.timezone)
            ).date().isoformat(),
            "provider_timestamp": (
                _utc_datetime(quote.retrieved_at).isoformat()
                if isinstance(quote, MarketBar)
                else _utc_datetime(quote.timestamp).isoformat()
            ),
            "provider_attempts": (
                provider_attempts_by_row_id.get(row.id, [])
                if provider_attempts_by_row_id is not None
                else []
            ),
        }
    quote_observations = list(quote_observations_by_row.values())
    mirror_observations = [
        {
            **_observation(row.symbol, f"{row.broker} broker mirror", row.synced_at),
            "quantity": str(row.quantity),
            "average_cost": (
                str(row.average_price) if row.average_price is not None else None
            ),
        }
        for row in holdings
    ]
    invested_cost = (
        sum(
            (row.quantity * row.average_price for row in holdings),
            Decimal("0"),
        )
        if holdings and all(row.average_price is not None for row in holdings)
        else Decimal("0")
        if snapshot_known and not holdings
        else None
    )
    positions_exposure = (
        sum(
            (abs(row.quantity) * row.average_price for row in positions),
            Decimal("0"),
        )
        if positions and all(row.average_price is not None for row in positions)
        else Decimal("0")
        if snapshot_known and not positions
        else None
    )
    invested_cost_reason = (
        None
        if invested_cost is not None
        else (
            "Missing input: broker-mirrored average cost for one or more delivery holdings."
            if holdings else "Missing input: no delivery holdings."
        )
    )
    invested_cost_provenance = {
        "type": "derived",
        "description": "Delivery holdings only: quantity × broker-mirrored average cost.",
        "mirror_observations": mirror_observations,
        "coverage": {
            "priced": sum(row.average_price is not None for row in holdings),
            "total": len(holdings),
            "unit": "delivery holdings",
        },
        "exclusions": [
            f"{row.symbol}: broker-mirrored average cost unavailable."
            for row in holdings
            if row.average_price is None
        ],
    }
    positions_exposure_provenance = {
        "type": "derived",
        "description": (
            "Intraday and derivative positions only: absolute net quantity × "
            "broker-mirrored average cost."
        ),
        "mirror_observations": [
            {
                **_observation(
                    row.symbol, f"{row.broker} broker mirror", row.synced_at
                ),
                "quantity": str(row.quantity),
                "average_cost": (
                    str(row.average_price)
                    if row.average_price is not None else None
                ),
                "product": row.product,
            }
            for row in positions
        ],
        "coverage": {
            "priced": sum(row.average_price is not None for row in positions),
            "total": len(positions),
            "unit": "intraday and derivative positions",
        },
        "exclusions": [
            f"{row.symbol}: broker-mirrored average cost unavailable."
            for row in positions
            if row.average_price is None
        ],
    }

    all_market_value = (
        sum(
            (
                row.quantity * quote_by_row_id[row.id][0].close
                for row in active_rows
            ),
            Decimal("0"),
        )
        if active_rows and not missing_ltp
        else Decimal("0")
        if snapshot_known and not active_rows
        else None
    )
    market_value_reason = (
        _portfolio_input_reason(missing_ltp)
        if missing_ltp
        else None
        if snapshot_known and not active_rows
        else "Missing input: no open holdings or positions."
        if not active_rows
        else None
    )
    market_value_provenance = {
        "type": "derived",
        "description": (
            "Quantity × verified fresh quote; when markets are closed, the "
            "last completed-session EOD close."
        ),
        "quote_observations": quote_observations,
        "coverage": {
            "priced": len(quote_by_row_id),
            "total": len(active_rows),
            "unit": "open rows",
        },
        "exclusions": [
            missing_quote_by_row_id[row.id]
            for row in active_rows
            if row.id not in quote_by_row_id
        ],
    }

    holdings_missing_ltp = [
        missing_quote_by_row_id[row.id].removeprefix("Missing input: ")
        for row in holdings
        if row.id not in quote_by_row_id
    ]
    holdings_missing_cost = [
        row.symbol for row in holdings if row.average_price is None
    ]
    holdings_market_value = (
        sum(
            (
                row.quantity * quote_by_row_id[row.id][0].close
                for row in holdings
            ),
            Decimal("0"),
        )
        if holdings and not holdings_missing_ltp
        else Decimal("0")
        if snapshot_known and not holdings
        else None
    )
    holdings_cost = (
        sum(
            (row.quantity * row.average_price for row in holdings),
            Decimal("0"),
        )
        if holdings and not holdings_missing_cost
        else None
    )
    holdings_pnl = (
        holdings_market_value - holdings_cost
        if holdings_market_value is not None and holdings_cost is not None
        else Decimal("0")
        if snapshot_known and not holdings
        else None
    )
    missing_position_pnl = [
        row.symbol for row in positions if row.unrealized_profit is None
    ]
    position_pnl = (
        sum(
            (row.unrealized_profit for row in positions),
            Decimal("0"),
        )
        if positions and not missing_position_pnl
        else Decimal("0")
        if snapshot_known and not positions
        else None
    )
    pnl_components: list[Decimal] = []
    pnl_reasons: list[str] = []
    pnl_provenance_parts: list[str] = []
    if holdings:
        if holdings_pnl is None:
            if holdings_missing_ltp:
                pnl_reasons.extend(holdings_missing_ltp)
            if holdings_missing_cost:
                pnl_reasons.append(
                    "average cost for holdings "
                    + ", ".join(holdings_missing_cost)
                )
        else:
            pnl_components.append(holdings_pnl)
            pnl_provenance_parts.append(
                "holdings derived as market value − invested cost"
            )
    elif snapshot_known:
        pnl_components.append(Decimal("0"))
        pnl_provenance_parts.append("no delivery holdings in the broker snapshot")
    if positions:
        if position_pnl is None:
            pnl_reasons.append(
                "Dhan unrealizedProfit for positions "
                + ", ".join(missing_position_pnl)
            )
        else:
            pnl_components.append(position_pnl)
            pnl_provenance_parts.append(
                "positions use Dhan broker-supplied unrealizedProfit"
            )
    elif snapshot_known:
        pnl_components.append(Decimal("0"))
        pnl_provenance_parts.append("no open positions in the broker snapshot")
    unrealized_pnl = (
        sum(pnl_components, Decimal("0"))
        if pnl_components and not pnl_reasons
        else None
    )
    unrealized_pnl_reason = (
        _portfolio_input_reason(pnl_reasons)
        if pnl_reasons
        else "Missing input: no open holdings or positions."
        if not pnl_components
        else None
    )
    unrealized_pnl_provenance = {
        "type": "combined" if len(pnl_provenance_parts) > 1 else (
            "derived" if holdings else "broker_supplied"
        ),
        "description": " + ".join(pnl_provenance_parts),
        "quote_observations": [
            quote_observations_by_row[row.id]
            for row in holdings
            if row.id in quote_observations_by_row
        ],
        "mirror_observations": [
            {
                **_observation(row.symbol, "Dhan broker positions", row.synced_at),
                "unrealized_profit": (
                    str(row.unrealized_profit)
                    if row.unrealized_profit is not None
                    else None
                ),
            }
            for row in positions
        ],
        "coverage": {
            "priced": sum(
                row.id in quote_by_row_id and row.average_price is not None
                for row in holdings
            ) + sum(row.unrealized_profit is not None for row in positions),
            "total": len(holdings) + len(positions),
            "unit": "holding/position rows",
        },
        "exclusions": [
            *[
                missing_quote_by_row_id[row.id]
                for row in holdings
                if row.id not in quote_by_row_id
            ],
            *[
                f"{row.symbol}: broker-mirrored average cost unavailable."
                for row in holdings
                if row.average_price is None
            ],
            *[
                f"{row.symbol}: Dhan unrealizedProfit unavailable."
                for row in positions
                if row.unrealized_profit is None
            ],
        ],
    }
    holdings_unrealized_pnl_provenance = {
        "type": "derived",
        "description": (
            "Delivery holdings only: fresh verified market value minus broker-mirrored "
            "average cost."
        ),
        "quote_observations": [
            quote_observations_by_row[row.id]
            for row in holdings
            if row.id in quote_observations_by_row
        ],
        "mirror_observations": [
            {
                **_observation(row.symbol, "Dhan holdings snapshot", row.synced_at),
                "quantity": str(row.quantity),
                "average_cost": (
                    str(row.average_price) if row.average_price is not None else None
                ),
            }
            for row in holdings
        ] or (
            [{
                **_observation(
                    "Delivery holdings", "Dhan broker mirror snapshot",
                    broker_snapshot_at,
                ),
                "amount": str(holdings_pnl) if holdings_pnl is not None else None,
            }]
            if snapshot_known else []
        ),
        "coverage": {
            "priced": sum(
                row.id in quote_by_row_id and row.average_price is not None
                for row in holdings
            ),
            "total": len(holdings),
            "unit": "delivery holdings",
        },
        "exclusions": [
            *holdings_missing_ltp,
            *[
                f"{row.symbol}: broker-mirrored average cost unavailable."
                for row in holdings
                if row.average_price is None
            ],
        ],
    }
    positions_unrealized_pnl_provenance = {
        "type": "broker_supplied",
        "description": "Open intraday and derivative positions: Dhan-supplied unrealizedProfit.",
        "mirror_observations": [
            {
                **_observation(row.symbol, "Dhan positions snapshot", row.synced_at),
                "quantity": str(row.quantity),
                "unrealized_profit": (
                    str(row.unrealized_profit)
                    if row.unrealized_profit is not None else None
                ),
            }
            for row in positions
        ] or (
            [{
                **_observation(
                    "Open positions", "Dhan broker mirror snapshot",
                    broker_snapshot_at,
                ),
                "amount": str(position_pnl) if position_pnl is not None else None,
            }]
            if snapshot_known else []
        ),
        "coverage": {
            "priced": sum(row.unrealized_profit is not None for row in positions),
            "total": len(positions),
            "unit": "open positions",
        },
        "exclusions": [
            f"{row.symbol}: Dhan unrealizedProfit unavailable."
            for row in positions
            if row.unrealized_profit is None
        ],
    }

    missing_day_ltp = missing_ltp
    from app.api import market_data as market_data_api

    adjusted_previous_closes: dict[UUID, tuple[Decimal, str]] = {}
    invalid_adjustment_rows: set[UUID] = set()
    same_source_mismatch: dict[UUID, str] = {}
    for row in active_rows:
        quote_and_instrument = quote_by_row_id.get(row.id)
        previous_bar = previous_close_by_row_id.get(row.id)
        if quote_and_instrument is None or previous_bar is None:
            continue
        quote, instrument = quote_and_instrument
        if provider_family(quote.source) != provider_family(previous_bar.source):
            same_source_mismatch[row.id] = (
                f"{row.symbol}: previous close source {previous_bar.source} "
                f"differs from price source {quote.source}."
            )
            continue
        price_date = _utc_datetime(quote.timestamp).astimezone(
            ZoneInfo(instrument.timezone)
        ).date()
        adjusted_close, adjustment = market_data_api._adjust_previous_daily_close(
            db,
            user_id,
            instrument,
            previous_bar.close,
            price_date,
        )
        if adjusted_close is None:
            invalid_adjustment_rows.add(row.id)
        else:
            adjusted_previous_closes[row.id] = (adjusted_close, adjustment)
    day_change_rows = [
        row for row in active_rows
        if row.id in quote_by_row_id and row.id in adjusted_previous_closes
    ]
    missing_day_previous_close = [
        row for row in active_rows
        if row.id in quote_by_row_id and row.id not in adjusted_previous_closes
    ]
    previous_close_exclusion_reasons: dict[UUID, str] = {}
    if missing_day_previous_close:
        listing_date_by_row_id: dict[UUID, date] = {}
        try:
            master_rows, _ = _dhan_instrument_master(missing_day_previous_close)
        except BrokerAdapterError as error:
            logger.warning(
                "Dhan listing-date lookup unavailable category=%s",
                error.category,
            )
        else:
            for row in missing_day_previous_close:
                matches = _dhan_master_matches(
                    master_rows, row.security_id, row.isin
                )
                if len(matches) == 1:
                    listing_date = _dhan_master_listing_date(matches[0])
                    if listing_date is not None:
                        listing_date_by_row_id[row.id] = listing_date

        sync_by_instrument: dict[UUID, list[MarketDataSync]] = {}
        instrument_ids = {
            instrument.id
            for row in missing_day_previous_close
            if (instrument := quote_by_row_id[row.id][1]) is not None
        }
        sync_rows = db.scalars(
            select(MarketDataSync).where(
                MarketDataSync.instrument_id.in_(instrument_ids),
                MarketDataSync.interval == "1d",
            )
        ).all() if instrument_ids else []
        for sync in sync_rows:
            sync_by_instrument.setdefault(sync.instrument_id, []).append(sync)

        for row in missing_day_previous_close:
            quote, instrument = quote_by_row_id[row.id]
            timezone_info = ZoneInfo(instrument.timezone)
            quote_day = _utc_datetime(quote.timestamp).astimezone(
                timezone_info
            ).date()
            prior_reference = datetime.combine(
                quote_day, datetime.min.time(), tzinfo=timezone_info
            ).astimezone(timezone.utc)
            expected_previous_date = (
                market_data_api._expected_market_session_date(
                    instrument, prior_reference
                )
            )
            listing_date = listing_date_by_row_id.get(row.id)
            if listing_date is not None and listing_date > expected_previous_date:
                previous_close_exclusion_reasons[row.id] = (
                    f"{row.symbol}: no prior market session exists before "
                    f"listing date {listing_date.isoformat()}."
                )
                continue
            syncs = sync_by_instrument.get(instrument.id, [])
            expected_date_start = datetime.combine(
                expected_previous_date, datetime.min.time(), tzinfo=timezone_info
            ).astimezone(timezone.utc)
            expected_date_end = datetime.combine(
                expected_previous_date, datetime.max.time(), tzinfo=timezone_info
            ).astimezone(timezone.utc)
            covered_attempts = [
                sync for sync in syncs
                if sync.requested_start is not None
                and sync.requested_end is not None
                and _utc_datetime(sync.requested_start) <= expected_date_end
                and _utc_datetime(sync.requested_end) >= expected_date_start
            ]
            empty_provider_responses = [
                sync for sync in covered_attempts
                if sync.status == "success" and sync.rows_received == 0
            ]
            if empty_provider_responses:
                previous_close_exclusion_reasons[row.id] = (
                    f"{row.symbol}: provider returned no daily data for the "
                    f"previous-session date {expected_previous_date.isoformat()}."
                )
            elif not covered_attempts:
                previous_close_exclusion_reasons[row.id] = (
                    f"{row.symbol}: previous-session history for "
                    f"{expected_previous_date.isoformat()} has not been fetched."
                )
            else:
                previous_close_exclusion_reasons[row.id] = (
                    f"{row.symbol}: provider history for "
                    f"{expected_previous_date.isoformat()} did not produce a "
                    "stored real daily close."
                )
    day_change = (
        sum(
            (
                row.quantity
                * (
                    quote_by_row_id[row.id][0].close
                    - adjusted_previous_closes[row.id][0]
                )
                for row in day_change_rows
            ),
            Decimal("0"),
        )
        if day_change_rows and not missing_day_ltp
        else None
    )
    missing_previous_close = [
        row for row in active_rows
        if row.id in quote_by_row_id
        and row.id not in previous_close_by_row_id
    ]
    day_change_reason = (
        _portfolio_input_reason(missing_day_ltp)
        if missing_day_ltp
        else (
            "Partial day change: excluded "
            + "; ".join(
                f"{row.symbol}: previous-session close adjustment is unavailable."
                if row.id in invalid_adjustment_rows
                else same_source_mismatch.get(row.id)
                or previous_close_exclusion_reasons.get(
                    row.id,
                    f"{row.symbol}: stored real previous-session close unavailable.",
                )
                for row in missing_day_previous_close
            )
            + "."
            if day_change is not None
            else _portfolio_input_reason([
                f"{row.symbol}: previous-session close adjustment is unavailable."
                if row.id in invalid_adjustment_rows
                else same_source_mismatch.get(row.id)
                or previous_close_exclusion_reasons.get(
                    row.id,
                    f"{row.symbol}: stored real previous-session close unavailable.",
                )
                for row in missing_day_previous_close
            ])
        )
        if missing_day_previous_close
        else "Missing input: no stored real daily previous close for any open instrument."
        if not day_change_rows
        else None
    )
    day_change_provenance = {
        "type": "derived",
        "description": (
            "Quantity × (verified live or EOD price − official stored previous-session "
            "daily close, adjusted for recorded ex-date corporate actions; otherwise "
            "the close is explicitly unadjusted)."
        ),
        "quote_observations": [
            quote_observations_by_row[row.id]
            for row in day_change_rows
        ],
        "previous_close_observations": [
            {
                **_observation(
                    row.symbol,
                    f"stored {previous_close_by_row_id[row.id].source} 1d bar",
                    previous_close_by_row_id[row.id].timestamp,
                ),
                "quantity": str(row.quantity),
                "close": str(adjusted_previous_closes[row.id][0]),
                "raw_close": str(previous_close_by_row_id[row.id].close),
                "adjustment": adjusted_previous_closes[row.id][1],
                "basis_date": _utc_datetime(
                    previous_close_by_row_id[row.id].timestamp
                ).astimezone(
                    ZoneInfo(quote_by_row_id[row.id][1].timezone)
                ).date().isoformat(),
                "provider_timestamp": _utc_datetime(
                    previous_close_by_row_id[row.id].retrieved_at
                ).isoformat(),
            }
            for row in day_change_rows
        ],
        "exclusions": [
            previous_close_exclusion_reasons.get(
                row.id,
                f"{row.symbol}: stored real previous-session close unavailable.",
            )
            for row in missing_previous_close
        ] + [
            missing_quote_by_row_id[row.id]
            for row in active_rows
            if row.id not in quote_by_row_id
        ],
        "coverage": {
            "priced": len(day_change_rows),
            "total": len(active_rows),
            "unit": "open rows with previous close",
        },
    }

    total_value = (
        cash + all_market_value
        if cash is not None and all_market_value is not None
        else None
    )
    total_value_reason = (
        "Missing input: broker-supplied cash balance."
        if cash is None
        else market_value_reason
        if all_market_value is None
        else None
    )
    # Per-row figures from the same quote, prior close and adjustment used for the
    # totals above, so a row can never disagree with the total it contributes to.
    row_valuations: dict[UUID, dict[str, Any]] = {}
    for row in active_rows:
        priced = quote_by_row_id.get(row.id)
        if priced is None:
            row_valuations[row.id] = {
                "status": BASIS_UNAVAILABLE,
                "basis": BASIS_UNAVAILABLE,
                "price": None,
                "provider_timestamp": None,
                "price_age_seconds": None,
                "source": None,
                "market_value": None,
                "unrealized_profit": None,
                "day_change": None,
                "prior_close": None,
                "quote_at": None,
                "stale_reason": missing_quote_by_row_id.get(
                    row.id, "No verified Dhan price is available."
                ),
                "day_change_reason": "No valued price.",
            }
            continue
        quote, instrument = priced
        row_basis = _observation_price_basis(quote, instrument, db)
        adjusted = adjusted_previous_closes.get(row.id)
        row_valuations[row.id] = {
            "status": row_basis,
            "basis": row_basis,
            "price": quote.close,
            "provider_timestamp": (
                quote.retrieved_at if isinstance(quote, MarketBar) else quote.timestamp
            ),
            # A stored daily bar's timestamp is a UTC-midnight session key, not a provider
            # tick time, so it has no meaningful age.
            "price_age_seconds": _price_age_seconds(quote),
            "source": provider_family(quote.source),
            "market_value": quote.close * row.quantity,
            "unrealized_profit": (
                (quote.close - row.average_price) * row.quantity
                if row.average_price is not None
                else None
            ),
            "day_change": (
                row.quantity * (quote.close - adjusted[0])
                if adjusted is not None
                else None
            ),
            "prior_close": adjusted[0] if adjusted is not None else None,
            "quote_at": quote.timestamp,
            "stale_reason": None,
            "day_change_reason": (
                None
                if adjusted is not None
                else "Previous-session close adjustment is unavailable."
                if row.id in invalid_adjustment_rows
                else same_source_mismatch.get(row.id)
                or previous_close_exclusion_reasons.get(
                    row.id,
                    "Same-source previous-session close is unavailable.",
                )
            ),
        }
    return {
        "row_valuations": row_valuations,
        "invested_cost": invested_cost,
        "positions_exposure": positions_exposure,
        "market_value": all_market_value,
        "total_value": total_value,
        "holdings_unrealized_pnl": holdings_pnl,
        "positions_unrealized_pnl": position_pnl,
        "unrealized_pnl": unrealized_pnl,
        "day_change": day_change,
        "unavailable_reasons": {
            "invested_cost": invested_cost_reason,
            "positions_exposure": (
                None
                if positions_exposure is not None
                else (
                    "Missing input: broker-mirrored average cost for one or more "
                    "intraday or derivative positions."
                    if positions else "Missing input: no open positions."
                )
            ),
            "market_value": market_value_reason,
            "total_value": total_value_reason,
            "holdings_unrealized_pnl": (
                None
                if holdings_pnl is not None
                else _portfolio_input_reason([
                    *holdings_missing_ltp,
                    *[
                        f"{symbol}: broker-mirrored average cost unavailable."
                        for symbol in holdings_missing_cost
                    ],
                ]) if holdings else "Missing input: broker holdings snapshot unavailable."
            ),
            "positions_unrealized_pnl": (
                None
                if position_pnl is not None
                else _portfolio_input_reason([
                    f"{symbol}: Dhan unrealizedProfit unavailable."
                    for symbol in missing_position_pnl
                ]) if positions else "Missing input: broker positions snapshot unavailable."
            ),
            "unrealized_pnl": unrealized_pnl_reason,
            "day_change": day_change_reason,
        },
        "provenance": {
            "cash": {
                "type": "broker_supplied",
                "description": "Dhan-supplied available balance.",
                "mirror_observations": (
                    [{
                        **_observation(
                            "Cash balance", "Dhan funds endpoint", cash_synced_at
                        ),
                        "amount": str(cash),
                    }]
                    if cash is not None
                    else []
                ),
            },
            "invested_cost": invested_cost_provenance,
            "positions_exposure": positions_exposure_provenance,
            "market_value": market_value_provenance,
            "total_value": {
                "type": "combined",
                "description": "Dhan-supplied cash balance + derived market value.",
                "quote_observations": quote_observations,
                "coverage": {
                    "priced": len(quote_by_row_id),
                    "total": len(active_rows),
                    "unit": "open rows",
                },
                "exclusions": market_value_provenance["exclusions"],
                "mirror_observations": (
                    [{
                        **_observation(
                            "Cash balance", "Dhan funds endpoint", cash_synced_at
                        ),
                        "amount": str(cash),
                    }]
                    if cash is not None
                    else []
                ),
            },
            "holdings_unrealized_pnl": holdings_unrealized_pnl_provenance,
            "positions_unrealized_pnl": positions_unrealized_pnl_provenance,
            "unrealized_pnl": unrealized_pnl_provenance,
            "day_change": day_change_provenance,
        },
    }


def _dhan_quote_evidence(
    db: Session,
    rows: list[BrokerMirror],
    user_id: UUID,
    now: datetime,
) -> tuple[
    dict[UUID, tuple[MarketQuote | MarketBar, Instrument]],
    dict[UUID, MarketBar],
    dict[UUID, str],
    dict[UUID, list[str]],
]:
    active_rows = _active_broker_rows(rows)
    if not active_rows:
        return {}, {}, {}, {}
    row_security_segment: dict[UUID, tuple[str, str]] = {}
    missing: dict[UUID, str] = {}
    for row in active_rows:
        segment = _dhan_quote_segment(row.exchange)
        security_id = row.security_id
        if segment is None:
            try:
                master_mapping = DhanAdapter.find_verified_equity_mapping_by_identity(
                    row.symbol,
                    row.isin,
                    row.security_id,
                )
            except BrokerAdapterError:
                master_mapping = None
                reason = "Dhan instrument master lookup failed"
            else:
                reason = "no unique verified instrument-master match"
            if master_mapping is None:
                if row.security_id and row.security_id.isdigit():
                    missing[row.id] = (
                        f"Unverified Dhan provider mapping for {row.symbol} "
                        f"(security ID {row.security_id}); Dhan instrument master "
                        f"could not confirm the exchange segment "
                        f"(ISIN {row.isin or 'not supplied'}; {reason})."
                    )
                else:
                    missing[row.id] = (
                        f"Missing input: unresolved Dhan instrument-master exchange "
                        f"segment for {row.symbol} "
                        f"(ISIN {row.isin or 'not supplied'}; {reason})."
                    )
                continue
            security_id, segment = master_mapping
        if security_id is None or not security_id.isdigit():
            missing[row.id] = (
                f"Missing input: valid Dhan security ID for {row.symbol}."
            )
            continue
        row_security_segment[row.id] = (security_id, segment)

    security_ids = {
        security_id for security_id, _segment in row_security_segment.values()
    }
    mapping_filters = (
        [InstrumentProviderIdentifier.provider_instrument_id.in_(security_ids)]
        + [
            InstrumentProviderIdentifier.provider_instrument_id.like(
                f"{security_id}|%"
            )
            for security_id in security_ids
        ]
    )
    mappings = list(db.scalars(
        select(InstrumentProviderIdentifier).where(
            InstrumentProviderIdentifier.provider == "dhan",
            or_(*mapping_filters),
        )
    )) if security_ids else []
    mapping_by_security_segment = {
        (
            mapping.provider_instrument_id.partition("|")[0],
            mapping.provider_instrument_id.partition("|")[2] or None,
        ): mapping
        for mapping in mappings
    }
    instruments = {
        instrument.id: instrument
        for instrument in db.scalars(
            select(Instrument).where(
                Instrument.id.in_({
                    mapping.instrument_id for mapping in mappings
                })
            )
        )
    } if mappings else {}
    mapping_verification_changed = False
    row_instrument: dict[UUID, Instrument] = {}
    row_segment: dict[UUID, str] = {}
    for row in active_rows:
        security_segment = row_security_segment.get(row.id)
        if security_segment is None:
            continue
        security_id, segment = security_segment
        mapping = (
            mapping_by_security_segment.get((security_id, segment))
            or mapping_by_security_segment.get((security_id, None))
        )
        instrument = instruments.get(mapping.instrument_id) if mapping else None
        if mapping is None:
            missing[row.id] = (
                f"Missing input: verified Dhan instrument mapping for {row.symbol} "
                f"(security ID {security_id}, segment {segment})."
            )
            continue
        if not mapping.is_verified:
            try:
                master_mapping = DhanAdapter.find_verified_equity_mapping_by_identity(
                    row.symbol,
                    row.isin,
                    security_id,
                )
            except BrokerAdapterError:
                master_mapping = None
            instrument_segment = (
                "NSE_EQ"
                if instrument is not None
                and instrument.venue.upper() in {"NSE", "XNSE", "NSI", "NSE_EQ"}
                else "BSE_EQ"
                if instrument is not None
                and instrument.venue.upper() in {"BSE", "XBOM", "BOM", "BSE_EQ"}
                else None
            )
            instrument_identity_matches = (
                instrument is not None
                and instrument.symbol.strip().upper().removesuffix("-EQ")
                == row.symbol.strip().upper().removesuffix("-EQ")
                and mapping.provider_symbol.strip().upper().removesuffix("-EQ")
                == row.symbol.strip().upper().removesuffix("-EQ")
                and instrument_segment == segment
            )
            if (
                master_mapping == (security_id, segment)
                and instrument_identity_matches
            ):
                mapping.is_verified = True
                mapping.last_seen_at = now
                mapping_verification_changed = True
            else:
                missing[row.id] = (
                    f"Unverified Dhan provider mapping for {row.symbol} "
                    f"(security ID {security_id}, segment {segment}); "
                    "instrument master did not confirm the stored identity."
                )
                continue
        if instrument is None:
            missing[row.id] = (
                f"Missing input: local instrument for Dhan mapping {row.symbol} "
                f"(security ID {security_id}, segment {segment})."
            )
        elif instrument.is_synthetic or instrument.currency.upper() != "INR":
            missing[row.id] = (
                f"Missing input: real INR instrument mapping for {row.symbol}; "
                "synthetic and foreign-currency values are not used."
            )
        else:
            row_instrument[row.id] = instrument
            row_segment[row.id] = segment
    if mapping_verification_changed:
        db.commit()

    from app.api import market_data as market_data_api

    instrument_ids = {instrument.id for instrument in row_instrument.values()}
    instrument_by_id = {
        instrument.id: instrument for instrument in row_instrument.values()
    }
    closed_session_by_instrument = {
        instrument_id
        for instrument_id, instrument in instrument_by_id.items()
        if market_data_api._market_session_state(instrument, now) == "closed"
    }
    expected_close_date_by_instrument = {
        instrument_id: market_data_api._expected_market_session_date(
            instrument_by_id[instrument_id], now
        )
        for instrument_id in closed_session_by_instrument
    }
    cached_quotes = list(db.scalars(
        select(MarketQuote).where(MarketQuote.instrument_id.in_(instrument_ids))
    )) if instrument_ids else []
    fresh_quote_by_instrument: dict[UUID, MarketQuote] = {}
    cached_quote_failures: dict[UUID, str] = {}
    provider_attempts_by_instrument: dict[UUID, list[str]] = {}
    for instrument in row_instrument.values():
        # Cached quotes are read even when the session is closed: the last completed
        # session's quote is the valuation price. Only the live batch fetch is skipped.
        candidates = [
            quote for quote in cached_quotes
            if quote.instrument_id == instrument.id
            and quote.source in {"dhan_ws", "dhan"}
        ]
        quote, _, rejections = select_usable_quote(db, instrument, candidates, now)
        if quote is not None:
            fresh_quote_by_instrument[instrument.id] = quote
            provider_attempts_by_instrument[instrument.id] = [
                f"dhan: cached {quote.source} quote used"
            ]
            continue
        if rejections:
            cached_quote_failures[instrument.id] = "; ".join(rejections)
        provider_attempts_by_instrument[instrument.id] = [
            f"dhan: {rejection}" for rejection in rejections
        ]
        if instrument.id in closed_session_by_instrument:
            provider_attempts_by_instrument.setdefault(instrument.id, []).append(
                "live quote skipped: NSE session closed"
            )

    batch_instruments: dict[UUID, tuple[str, str]] = {}
    for row in active_rows:
        instrument = row_instrument.get(row.id)
        if (
            instrument is not None
            and instrument.id not in closed_session_by_instrument
            and instrument.id not in fresh_quote_by_instrument
        ):
            batch_instruments[instrument.id] = (
                row_segment[row.id],
                row_security_segment[row.id][0],
            )
    instruments_by_segment: dict[str, list[str]] = {}
    for segment, security_id in batch_instruments.values():
        instruments_by_segment.setdefault(segment, []).append(security_id)

    batch_failures: dict[str, str] = {}
    fetched_quotes: dict[UUID, MarketQuote] = {}
    if instruments_by_segment:
        credential = db.scalar(
            select(BrokerCredential).where(
                BrokerCredential.user_id == user_id,
                BrokerCredential.broker == "dhan",
            )
        )
        if credential is None:
            batch_failures["all"] = "Dhan access token is not connected."
        else:
            adapter = DhanAdapter()
            try:
                tokens = _fresh_tokens(db, adapter, credential)
                logger.info(
                    "broker_summary_quote_batch provider=dhan batch_call_count=1 "
                    "segment_counts=%s",
                    ",".join(
                        f"{segment}:{len(set(ids))}"
                        for segment, ids in sorted(instruments_by_segment.items())
                    ),
                )
                fetched_by_segment = adapter.get_market_quotes_batch(
                    tokens["access_token"],
                    tokens,
                    instruments_by_segment,
                )
            except HTTPException as error:
                reason = (
                    "Dhan access token could not be refreshed; reconnect Dhan."
                    if error.status_code == 401
                    else "Dhan batched quote request could not be authorized."
                )
                batch_failures["all"] = reason
            except BrokerAdapterError as error:
                batch_failures["all"] = (
                    f"Dhan batched quote request failed ({error.category})."
                )
            else:
                dhan_cache_by_instrument = {
                    quote.instrument_id: quote
                    for quote in cached_quotes
                    if quote.source == "dhan"
                }
                for instrument_id, (segment, security_id) in batch_instruments.items():
                    bar = fetched_by_segment.get(segment, {}).get(security_id)
                    if bar is None:
                        batch_failures[str(instrument_id)] = (
                            f"Dhan quote response did not include LTP for "
                            f"{instrument_by_id[instrument_id].symbol}."
                        )
                        provider_attempts_by_instrument.setdefault(
                            instrument_id, []
                        ).append("dhan: batched quote omitted the instrument")
                        continue
                    quote = dhan_cache_by_instrument.get(instrument_id)
                    if quote is None:
                        quote = MarketQuote(
                            instrument_id=instrument_id,
                            source="dhan",
                        )
                        db.add(quote)
                    quote.timestamp = bar.timestamp
                    quote.open = bar.open
                    quote.high = bar.high
                    quote.low = bar.low
                    quote.close = bar.close
                    quote.volume = bar.volume
                    quote.retrieved_at = now
                    quote.is_synthetic = False
                    fetched_quotes[instrument_id] = quote
                    provider_attempts_by_instrument.setdefault(
                        instrument_id, []
                    ).append("dhan: batched quote returned")
                if fetched_quotes:
                    db.commit()
                    for quote in fetched_quotes.values():
                        db.refresh(quote)

    # Dhan is the only valuation quote source. A row without a usable Dhan quote is
    # reported as missing; no other provider is substituted (no silent fallback).
    eod_close_instrument_ids: set[UUID] = set()
    quote_by_row_id: dict[UUID, tuple[MarketQuote | MarketBar, Instrument]] = {}
    for row in active_rows:
        instrument = row_instrument.get(row.id)
        if instrument is None:
            continue
        quote = (
            fresh_quote_by_instrument.get(instrument.id)
            or fetched_quotes.get(instrument.id)
        )
        if quote is not None:
            usable, reason, evidence = classify_quote(
                quote,
                instrument,
                "INR",
                now,
                last_completed_session=venue_last_completed_session(
                    db, instrument, quote.source
                ),
            )
            is_eod_close = instrument.id in eod_close_instrument_ids
            if (
                usable and evidence["quote_freshness_status"] in USABLE_FRESHNESS_STATUSES
            ) or is_eod_close:
                if usable and quote.source in {"dhan", "dhan_ws"}:
                    provider_attempts_by_instrument.setdefault(
                        instrument.id, []
                    ).append("dhan: quote fresh")
                quote_by_row_id[row.id] = (quote, instrument)
                continue
            missing[row.id] = (
                f"Missing input: fresh LTP for {row.symbol}; quote is "
                f"{evidence['quote_freshness_status']} ({reason or 'freshness check failed'})."
            )
        elif row.id not in missing:
            failure = batch_failures.get(
                str(instrument.id),
                batch_failures.get("all"),
            )
            cached_failure = cached_quote_failures.get(instrument.id)
            details = [
                detail for detail in (
                    cached_failure,
                    failure,
                ) if detail
            ]
            missing[row.id] = (
                f"Missing input: fresh LTP for {row.symbol}; "
                f"{'; '.join(details) or 'Dhan quote response omitted this instrument'}"
            )

    # Non-live Dhan prices are factual valuation evidence only; action-implying paths stay
    # strict. Prior-session prices retain their prior_close label, while same-session stale
    # prices remain delayed.
    for row in active_rows:
        instrument = row_instrument.get(row.id)
        if (
            instrument is None
            or row.id in quote_by_row_id
            or instrument.id in closed_session_by_instrument
        ):
            continue
        delayed_candidates = [
            candidate for candidate in cached_quotes
            if candidate.instrument_id == instrument.id
            and candidate.source in {"dhan_ws", "dhan"}
        ]
        fetched = fetched_quotes.get(instrument.id)
        if fetched is not None and all(
            candidate is not fetched for candidate in delayed_candidates
        ):
            delayed_candidates.append(fetched)
        delayed_quote, delayed_evidence, _ = select_usable_quote(
            db, instrument, delayed_candidates, now, allow_delayed=True
        )
        if delayed_quote is None:
            continue
        quote_by_row_id[row.id] = (delayed_quote, instrument)
        missing.pop(row.id, None)
        status = str(delayed_evidence.get("quote_freshness_status", "delayed"))
        provider_attempts_by_instrument.setdefault(instrument.id, []).append(
            f"dhan: {delayed_quote.source} quote is {status}; used and labelled {status}"
        )

    daily_bars = list(db.scalars(
        select(MarketBar).where(
            MarketBar.instrument_id.in_(instrument_ids),
            MarketBar.interval == "1d",
            MarketBar.is_synthetic.is_(False),
        ).order_by(MarketBar.timestamp.desc())
    )) if instrument_ids else []
    bars_by_instrument: dict[UUID, list[MarketBar]] = {}
    for bar in daily_bars:
        bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)
    eod_close_by_instrument: dict[UUID, MarketBar] = {}
    for instrument_id, expected_date in expected_close_date_by_instrument.items():
        instrument = instrument_by_id[instrument_id]
        close_candidates = [
            bar
            for bar in bars_by_instrument.get(instrument_id, [])
            if bar.source == "dhan"
            and _utc_datetime(bar.timestamp).astimezone(
                ZoneInfo(instrument.timezone)
            ).date() == expected_date
            and _utc_datetime(bar.timestamp) <= _utc_datetime(now)
            and _utc_datetime(bar.retrieved_at) <= _utc_datetime(now)
            and _utc_datetime(bar.retrieved_at).astimezone(ZoneInfo(instrument.timezone)).time() >= wall_time(15, 30)
        ]
        if close_candidates:
            eod_close_by_instrument[instrument_id] = min(
                close_candidates,
                key=lambda bar: -_utc_datetime(bar.retrieved_at).timestamp(),
            )
            provider_attempts_by_instrument.setdefault(
                instrument_id, []
            ).append(
                f"stored {eod_close_by_instrument[instrument_id].source} "
                f"daily close used for {expected_date.isoformat()}"
            )
        else:
            for row in active_rows:
                if row_instrument.get(row.id) is not None and (
                    row_instrument[row.id].id == instrument_id
                ):
                    missing[row.id] = (
                        f"Missing input: stored real daily close for last "
                        f"completed NSE session {expected_date.isoformat()} "
                        f"unavailable for {row.symbol}."
                    )

    for row in active_rows:
        instrument = row_instrument.get(row.id)
        if instrument is None or instrument.id not in closed_session_by_instrument:
            continue
        eod_close = eod_close_by_instrument.get(instrument.id)
        if eod_close is None:
            continue
        quote_by_row_id[row.id] = (eod_close, instrument)
        missing.pop(row.id, None)

    previous_close_by_instrument: dict[UUID, MarketBar] = {}
    quote_by_instrument = {
        instrument.id: quote
        for quote, instrument in quote_by_row_id.values()
    }
    for instrument_id, instrument in instrument_by_id.items():
        quote = quote_by_instrument.get(instrument_id)
        if quote is None:
            continue
        timezone_info = ZoneInfo(instrument.timezone)
        quote_day = _utc_datetime(quote.timestamp).astimezone(timezone_info).date()
        prior_session_reference = datetime.combine(
            quote_day,
            datetime.min.time(),
            tzinfo=timezone_info,
        ).astimezone(timezone.utc)
        expected_previous_date = (
            market_data_api._expected_market_session_date(
                instrument, prior_session_reference
            )
        )
        previous_bars = [
            bar for bar in bars_by_instrument.get(instrument_id, [])
            if bar.source == "dhan"
            and market_data_api._daily_session_date(bar.timestamp)
            == expected_previous_date
        ]
        if previous_bars:
            latest_day = max(
                market_data_api._daily_session_date(bar.timestamp)
                for bar in previous_bars
            )
            latest_day_bars = [
                bar for bar in previous_bars
                if market_data_api._daily_session_date(bar.timestamp) == latest_day
            ]
            previous_close_by_instrument[instrument_id] = min(
                latest_day_bars,
                key=lambda bar: -_utc_datetime(bar.retrieved_at).timestamp(),
            )

    previous_close_by_row_id = {
        row.id: previous_close_by_instrument[instrument.id]
        for row in active_rows
        if (instrument := row_instrument.get(row.id)) is not None
        and instrument.id in previous_close_by_instrument
    }
    provider_attempts_by_row_id = {
        row.id: provider_attempts_by_instrument.get(instrument.id, [])
        for row in active_rows
        if (instrument := row_instrument.get(row.id)) is not None
    }
    return (
        quote_by_row_id,
        previous_close_by_row_id,
        missing,
        provider_attempts_by_row_id,
    )


@router.get("/portfolio-summary")
def get_broker_portfolio_summary(
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
    portfolio_id: UUID | None = None,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    user_id = _user_id(active_session)
    mirror_rows = list(db.scalars(
        select(BrokerMirror).where(BrokerMirror.user_id == user_id)
    ))
    account_rows = list(db.scalars(
        select(BrokerAccountMirror).where(
            BrokerAccountMirror.user_id == user_id,
            BrokerAccountMirror.currency == "INR",
        )
    ))
    accounts_by_broker = {row.broker: row for row in account_rows}
    broker_names = sorted(
        set(accounts_by_broker)
        | {
            row.broker for row in mirror_rows
            if row.broker == "dhan" or row.broker in accounts_by_broker
        }
    )
    now = _now()
    dhan_rows = [row for row in mirror_rows if row.broker == "dhan"]
    dhan_account = accounts_by_broker.get("dhan")
    (
        dhan_quote_by_row_id,
        dhan_previous_close_by_row_id,
        dhan_missing_quotes,
        dhan_provider_attempts_by_row_id,
    ) = _dhan_quote_evidence(db, dhan_rows, user_id, now)
    dhan_values = _calculate_broker_values(
        db,
        user_id,
        dhan_rows,
        dhan_account.available_balance if dhan_account is not None else None,
        dhan_account.synced_at if dhan_account is not None else None,
        dhan_quote_by_row_id,
        dhan_previous_close_by_row_id,
        dhan_missing_quotes,
        dhan_provider_attempts_by_row_id,
    )
    brokers: list[dict[str, Any]] = []
    for broker_name in broker_names:
        account = accounts_by_broker.get(broker_name)
        rows = [row for row in mirror_rows if row.broker == broker_name]
        active_rows = _active_broker_rows(rows)
        holding_rows = [
            row for row in active_rows
            if _broker_row_portfolio(row) == "holding"
        ]
        position_rows = [
            row for row in active_rows
            if _broker_row_portfolio(row) == "position"
        ]
        currency = account.currency if account is not None else (
            "INR" if broker_name == "dhan" else None
        )
        if currency != "INR":
            continue
        has_unknown_cost = any(
            row.average_price is None for row in holding_rows
        )
        snapshot_known = bool(rows)
        invested_cost = (
            sum(
                (row.quantity * row.average_price for row in holding_rows),
                Decimal("0"),
            )
            if holding_rows and not has_unknown_cost
            else Decimal("0")
            if snapshot_known and not holding_rows
            else None
        )
        positions_exposure = (
            sum(
                (abs(row.quantity) * row.average_price for row in position_rows),
                Decimal("0"),
            )
            if position_rows and all(
                row.average_price is not None for row in position_rows
            )
            else Decimal("0")
            if snapshot_known and not position_rows
            else None
        )
        synced_at = max(
            (
                [row.synced_at for row in active_rows]
                + ([account.synced_at] if account is not None else [])
            ),
            default=None,
        )
        cash = account.available_balance if account is not None else None
        is_dhan = broker_name == "dhan"
        broker_values = dhan_values if is_dhan else {}
        unavailable_reasons = (
            broker_values["unavailable_reasons"]
            if is_dhan
            else {
                "market_value": (
                    "Missing input: fresh INR LTP from a verified Dhan instrument "
                    "mapping for this broker's holdings and open positions."
                ),
                "total_value": "Missing input: derived market value.",
                "unrealized_pnl": (
                    "Missing input: complete broker-supplied position P&L and fresh "
                    "quote-derived holdings P&L."
                ),
                "holdings_unrealized_pnl": (
                    "Missing input: fresh quote and broker-mirrored cost for delivery holdings."
                    if holding_rows else None
                ),
                "positions_unrealized_pnl": (
                    "Missing input: broker-supplied unrealized P&L for open positions."
                    if position_rows else None
                ),
                "day_change": (
                    "Missing input: fresh LTP and stored real daily previous close "
                    "for this broker's instruments."
                ),
                "invested_cost": (
                    "Missing input: broker-mirrored average cost for delivery holdings."
                    if has_unknown_cost
                    else None
                    if invested_cost is not None
                    else "Missing input: broker holdings snapshot unavailable."
                ),
                "positions_exposure": (
                    None
                    if positions_exposure is not None
                    else (
                        "Missing input: broker-mirrored average cost for one or more "
                        "intraday or derivative positions."
                        if position_rows
                        else "Missing input: broker positions snapshot unavailable."
                    )
                ),
            }
        )
        if is_dhan:
            broker_values["invested_cost"] = invested_cost
            broker_values["unavailable_reasons"]["invested_cost"] = (
                None
                if invested_cost is not None
                else (
                    "Missing input: broker-mirrored average cost for one or more "
                    "open holdings or positions."
                    if has_unknown_cost
                    else (
                        None
                        if snapshot_known
                        else "Missing input: broker holdings snapshot unavailable."
                    )
                )
            )
            broker_values["positions_exposure"] = positions_exposure
            broker_values["unavailable_reasons"]["positions_exposure"] = (
                None
                if positions_exposure is not None
                else (
                    "Missing input: broker-mirrored average cost for one or more "
                    "intraday or derivative positions."
                    if position_rows
                    else (
                        None
                        if snapshot_known
                        else "Missing input: broker positions snapshot unavailable."
                    )
                )
            )
        broker_market_value = broker_values.get("market_value")
        broker_total_value = broker_values.get("total_value")
        broker_unrealized_pnl = broker_values.get("unrealized_pnl")
        holdings_unrealized_pnl = broker_values.get("holdings_unrealized_pnl")
        positions_unrealized_pnl = broker_values.get("positions_unrealized_pnl")
        if not is_dhan and position_rows and all(
            row.unrealized_profit is not None for row in position_rows
        ):
            positions_unrealized_pnl = sum(
                (row.unrealized_profit for row in position_rows),
                Decimal("0"),
            )
        elif not is_dhan and snapshot_known and not position_rows:
            positions_unrealized_pnl = Decimal("0")
        if not is_dhan and snapshot_known and not holding_rows:
            holdings_unrealized_pnl = Decimal("0")
        if not is_dhan and snapshot_known and not active_rows:
            broker_market_value = Decimal("0")
            broker_unrealized_pnl = Decimal("0")
            broker_total_value = (
                cash + broker_market_value if cash is not None else None
            )
            unavailable_reasons["market_value"] = None
            unavailable_reasons["total_value"] = (
                None
                if broker_total_value is not None
                else "Missing input: broker-supplied cash balance."
            )
        broker_day_change = broker_values.get("day_change")
        broker_provenance = (
            broker_values.get("provenance", {})
            if is_dhan
            else {
                "cash": (
                    {
                        "type": "broker_supplied",
                        "description": "Broker-supplied available balance.",
                        "mirror_observations": (
                            [{
                                **_observation(
                                    "Cash balance",
                                    f"{broker_name} funds",
                                    account.synced_at,
                                ),
                                "amount": str(cash),
                            }]
                            if account is not None
                            else []
                        ),
                    }
                ),
                "invested_cost": {
                    "type": "derived",
                    "description": "Quantity × broker-mirrored average cost.",
                    "mirror_observations": [
                        {
                            **_observation(
                                row.symbol,
                                f"{broker_name} broker mirror",
                                row.synced_at,
                            ),
                            "quantity": str(row.quantity),
                            "average_cost": (
                                str(row.average_price)
                                if row.average_price is not None
                                else None
                            ),
                        }
                        for row in holding_rows
                    ],
                },
                "holdings_unrealized_pnl": {
                    "type": "derived",
                    "description": (
                        "Delivery holdings only. A live derived value is unavailable "
                        "from this broker summary because no verified live quote was "
                        "joined to the holdings snapshot."
                    ),
                    "mirror_observations": [
                        {
                            **_observation(
                                row.symbol, f"{broker_name} holdings snapshot", row.synced_at
                            ),
                            "quantity": str(row.quantity),
                            "average_cost": (
                                str(row.average_price)
                                if row.average_price is not None else None
                            ),
                        }
                        for row in holding_rows
                    ],
                    "exclusions": (
                        ["Verified live quote unavailable for holdings P&L."]
                        if holding_rows else []
                    ),
                },
                "positions_unrealized_pnl": {
                    "type": "broker_supplied",
                    "description": (
                        f"{broker_name} broker-supplied unrealized P&L for open positions."
                    ),
                    "mirror_observations": [
                        {
                            **_observation(
                                row.symbol, f"{broker_name} positions snapshot", row.synced_at
                            ),
                            "quantity": str(row.quantity),
                            "unrealized_profit": (
                                str(row.unrealized_profit)
                                if row.unrealized_profit is not None else None
                            ),
                        }
                        for row in position_rows
                    ],
                },
                "unrealized_pnl": {
                    "type": "combined",
                    "description": (
                        "Delivery holdings P&L is quote-derived; position P&L is "
                        "broker-supplied. Missing inputs remain unavailable."
                    ),
                    "holdings": "holdings_unrealized_pnl",
                    "positions": "positions_unrealized_pnl",
                },
            }
        )
        if not is_dhan:
            unavailable_reasons["total_value"] = (
                "Missing input: broker-supplied cash balance."
                if cash is None
                else (
                    "Missing input: derived market value."
                    if active_rows
                    else None
                )
            )
            unavailable_reasons["unrealized_pnl"] = (
                None
                if snapshot_known and not active_rows
                else unavailable_reasons["unrealized_pnl"]
            )
        if cash is None:
            unavailable_reasons["cash"] = (
                "Missing input: broker-supplied available cash balance."
            )
        brokers.append({
            "source": broker_name,
            "currency": currency,
            "synced_at": synced_at,
            "cash": str(cash) if cash is not None else None,
            "invested_cost": (
                str(invested_cost) if invested_cost is not None else None
            ),
            "positions_exposure": (
                str(positions_exposure)
                if positions_exposure is not None else None
            ),
            "holdings_unrealized_pnl": (
                str(holdings_unrealized_pnl)
                if holdings_unrealized_pnl is not None else None
            ),
            "positions_unrealized_pnl": (
                str(positions_unrealized_pnl)
                if positions_unrealized_pnl is not None else None
            ),
            "market_value": (
                str(broker_market_value) if broker_market_value is not None else None
            ),
            "total_value": (
                str(broker_total_value) if broker_total_value is not None else None
            ),
            "unrealized_pnl": (
                str(broker_unrealized_pnl)
                if broker_unrealized_pnl is not None
                else None
            ),
            "day_change": (
                str(broker_day_change) if broker_day_change is not None else None
            ),
            "unavailable_reasons": unavailable_reasons,
            "provenance": broker_provenance,
            "stream_rows": (
                [
                    {
                        "instrument_id": str(dhan_quote_by_row_id[row.id][1].id),
                        "provider_timestamp": (
                            _utc_datetime(dhan_quote_by_row_id[row.id][0].timestamp)
                            .isoformat()
                        ),
                        "valuation": dhan_values["row_valuations"].get(row.id),
                    }
                    for row in active_rows
                    if is_dhan and row.id in dhan_quote_by_row_id
                ]
                if is_dhan else []
            ),
            "holdings_count": len(holding_rows),
            "positions_count": len(position_rows),
        })

    def combined(field: str) -> Decimal | None:
        values = [broker[field] for broker in brokers]
        if not values or any(value is None for value in values):
            return None
        return sum(
            (Decimal(str(value)) for value in values),
            Decimal("0"),
        )

    source_names = [broker["source"] for broker in brokers]
    combined_synced_at = max(
        (broker["synced_at"] for broker in brokers if broker["synced_at"]),
        default=None,
    )
    result = {
        "currency": "INR",
        "source": "combined" if len(source_names) > 1 else (
            source_names[0] if source_names else None
        ),
        "sources": source_names,
        "synced_at": combined_synced_at,
        "totals": {
            "cash": str(combined("cash")) if combined("cash") is not None else None,
            "invested_cost": (
                str(combined("invested_cost"))
                if combined("invested_cost") is not None
                else None
            ),
            "positions_exposure": (
                str(combined("positions_exposure"))
                if combined("positions_exposure") is not None
                else None
            ),
            "holdings_unrealized_pnl": (
                str(combined("holdings_unrealized_pnl"))
                if combined("holdings_unrealized_pnl") is not None
                else None
            ),
            "positions_unrealized_pnl": (
                str(combined("positions_unrealized_pnl"))
                if combined("positions_unrealized_pnl") is not None
                else None
            ),
            "market_value": (
                str(combined("market_value"))
                if combined("market_value") is not None
                else None
            ),
            "total_value": (
                str(combined("total_value"))
                if combined("total_value") is not None
                else None
            ),
            "unrealized_pnl": (
                str(combined("unrealized_pnl"))
                if combined("unrealized_pnl") is not None
                else None
            ),
            "day_change": (
                str(combined("day_change"))
                if combined("day_change") is not None
                else None
            ),
            "synced_at": combined_synced_at,
        },
        "brokers": brokers,
    }
    dhan_broker = next((item for item in brokers if item["source"] == "dhan"), None)
    card_value = combined("market_value")
    dhan_value = (
        Decimal(str(dhan_broker["market_value"]))
        if dhan_broker is not None and dhan_broker["market_value"] is not None
        else None
    )
    value_difference = card_value - dhan_value if card_value is not None and dhan_value is not None else None
    result["value_comparison"] = {
        "alert": value_difference is not None and abs(value_difference) > settings.portfolio_value_alert_threshold,
        "threshold": str(settings.portfolio_value_alert_threshold),
        "app_value": str(card_value) if card_value is not None else None,
        "app_timestamp": combined_synced_at.isoformat() if combined_synced_at else None,
        "broker_value": str(dhan_value) if dhan_value is not None else None,
        "broker_timestamp": dhan_broker["synced_at"] if dhan_broker else None,
        "difference": str(value_difference) if value_difference is not None else None,
    }
    if portfolio_id is not None:
        owner = db.get(User, user_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="Portfolio owner not found")
        from app.api.market_data import market_data_provider_for_user

        app_valuation = portfolio_valuation(
            portfolio_id=portfolio_id,
            refresh=False,
            user=owner,
            provider=market_data_provider_for_user(db, user_id),
            db=db,
        )
        def difference(app_value: Decimal | None, broker_value: Any) -> str | None:
            if app_value is None or broker_value is None:
                return None
            return str(app_value - Decimal(str(broker_value)))
        result["reconciliation"] = {
            "portfolio_id": str(portfolio_id),
            "basis": "app ledger valuation compared with Dhan broker snapshot; differences are app minus Dhan",
            "app": {
                "total_value": str(app_valuation.total_value),
                "unrealized_pnl": str(app_valuation.unrealized_pnl),
                "day_change": str(app_valuation.day_change),
            },
            "broker": {
                "total_value": dhan_broker["total_value"] if dhan_broker else None,
                "unrealized_pnl": dhan_broker["unrealized_pnl"] if dhan_broker else None,
                "day_change": dhan_broker["day_change"] if dhan_broker else None,
            },
            "difference": {
                "total_value": difference(app_valuation.total_value, dhan_broker["total_value"] if dhan_broker else None),
                "unrealized_pnl": difference(app_valuation.unrealized_pnl, dhan_broker["unrealized_pnl"] if dhan_broker else None),
                "day_change": difference(app_valuation.day_change, dhan_broker["day_change"] if dhan_broker else None),
            },
        }
    return result


@router.get("/{slug}/account")
def get_broker_account(
    slug: str,
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    adapter = _adapter(slug)
    credential = _credential(db, _user_id(active_session), slug)
    tokens = _fresh_tokens(db, adapter, credential)
    try:
        return {
            "profile": adapter.get_profile(tokens["access_token"], tokens),
            "funds": adapter.get_funds(tokens["access_token"], tokens),
            "order_history": adapter.get_order_history(tokens["access_token"], tokens),
            "synthetic": adapter.is_synthetic,
        }
    except BrokerReconnectRequired as error:
        raise _broker_error(401, "broker_authentication_required", str(error)) from error
    except BrokerAdapterError as error:
        raise _broker_error(502, "broker_provider_error", "Broker account read failed.") from error


@router.get("/{slug}/reconciliation")
def reconcile_broker(
    slug: str,
    portfolio_id: UUID,
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    if slug not in ADAPTERS:
        raise HTTPException(status_code=404, detail="Broker not supported")
    rows = _reconciliation(db, _user_id(active_session), slug, portfolio_id)
    return {"broker": slug, "portfolio_id": portfolio_id, "differences": rows}


@router.post("/{slug}/reconciliation/import")
def import_broker_differences(
    slug: str,
    payload: ImportRequest,
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    if slug not in ADAPTERS:
        raise HTTPException(status_code=404, detail="Broker not supported")
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required")
    if len({row.symbol.upper() for row in payload.rows}) != len(payload.rows):
        raise HTTPException(status_code=422, detail="Each symbol may be imported only once per request")

    credential = db.scalar(
        select(BrokerCredential)
        .where(
            BrokerCredential.user_id == _user_id(active_session),
            BrokerCredential.broker == slug,
        )
        .with_for_update()
    )
    if credential is None or credential.last_synced_at is None:
        raise HTTPException(status_code=409, detail="Sync this broker before reconciling holdings")
    portfolio = db.scalar(
        select(Portfolio).where(
            Portfolio.id == payload.portfolio_id,
            Portfolio.user_id == _user_id(active_session),
        ).with_for_update()
    )
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    current = _reconciliation(db, _user_id(active_session), slug, payload.portfolio_id)
    current_by_symbol = {row["symbol"]: row for row in current}
    now = _now()
    created = []
    for selected in payload.rows:
        symbol = selected.symbol.strip().upper()
        difference = current_by_symbol.get(symbol)
        if difference is None or (
            difference["manual_quantity"] != selected.expected_manual_quantity
            or difference["broker_quantity"] != selected.expected_broker_quantity
        ):
            db.rollback()
            raise HTTPException(status_code=409, detail=f"Reconciliation changed for {symbol}; sync and review again")
        delta = difference["difference"]
        price = difference["average_price"]
        if delta == 0 or price is None or price <= 0:
            db.rollback()
            raise HTTPException(status_code=422, detail=f"{symbol} has no usable broker cost basis to import")
        transaction = PortfolioTransaction(
            portfolio_id=portfolio.id,
            transaction_type="buy" if delta > 0 else "sell",
            symbol=symbol,
            quantity=abs(delta),
            unit_price=price,
            amount=money(abs(delta) * price),
            fee=Decimal("0.00"),
            occurred_at=now,
            created_at=now,
        )
        db.add(transaction)
        db.flush()
        try:
            calculate_portfolio_ledger(db, _user_id(active_session), portfolio.id)
        except LedgerError as error:
            db.rollback()
            raise HTTPException(status_code=422, detail=f"Import would violate the manual ledger: {error}") from error
        created.append(transaction)
    db.commit()
    return {"imported_count": len(created), "transaction_ids": [str(row.id) for row in created]}


@router.delete("/{slug}", status_code=204)
def disconnect_broker(
    slug: str,
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    if slug not in ADAPTERS:
        raise HTTPException(status_code=404, detail="Broker not supported")
    db.execute(
        delete(BrokerCredential).where(
            BrokerCredential.user_id == _user_id(active_session),
            BrokerCredential.broker == slug,
        )
    )
    db.execute(
        delete(BrokerOAuthState).where(
            BrokerOAuthState.user_id == _user_id(active_session),
            BrokerOAuthState.broker == slug,
        )
    )
    db.commit()
    if slug in {"angel_one", "dhan", "yfinance"}:
        from app.api.market_data import reset_provider_circuit_breaker

        reset_provider_circuit_breaker(slug)
