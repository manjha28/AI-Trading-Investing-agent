import csv
import io
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.accounting import LedgerError, money
from app.api.auth import get_active_session, require_csrf
from app.api.portfolio_transactions import calculate_portfolio_ledger
from app.broker_adapters import (
    ADAPTERS,
    BrokerAdapter,
    BrokerAdapterError,
    BrokerReconnectRequired,
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
    DhanTradeMirror,
    Portfolio,
    PortfolioTransaction,
    UserSession,
)
from app.security import hash_token, new_token
from app.provider_adapters import (
    DhanAccountRequestError,
    DhanReadOnlyAccountAdapter,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brokers", tags=["read-only brokers"])
OAUTH_STATE_TTL = timedelta(minutes=10)


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
    logger.warning(
        "Dhan sync diagnostic stage=%s error_class=%s description=%s",
        stage,
        type(error).__name__,
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
        except (InvalidOperation, ValueError) as error:
            raise BrokerAdapterError("Broker account data contains invalid quantities") from error
        if not quantity.is_finite() or (average_price is not None and not average_price.is_finite()):
            raise BrokerAdapterError("Broker account data contains invalid quantities")
        exchange = str(_first(record, "exchange", "exchangeSegment", "exch_seg", default="")).upper()[:32]
        product = str(_first(record, "product", "productType", "producttype", default="")).upper()[:32]
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


def _upsert_dhan_trades(
    db: Session, rows: list[DhanTradeMirror]
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    for row in rows:
        existing = db.scalar(
            select(DhanTradeMirror).where(
                DhanTradeMirror.user_id == row.user_id,
                DhanTradeMirror.source == row.source,
                DhanTradeMirror.trade_id == row.trade_id,
            )
        )
        if existing is None:
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
            inserted += 1
            continue
        for field in (
            "trade_id",
            "provider_trade_id",
            "order_id",
            "exchange_order_id",
            "symbol",
            "security_id",
            "exchange_segment",
            "side",
            "quantity",
            "price",
            "charges",
            "currency",
            "trade_at",
            "synced_at",
        ):
            setattr(existing, field, getattr(row, field))
        updated += 1
    return inserted, updated


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
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
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
    dhan_to_date = to_date or now.date()
    dhan_from_date = from_date or (dhan_to_date - timedelta(days=365))
    if dhan_from_date > dhan_to_date:
        raise HTTPException(status_code=422, detail="from-date must not be after to-date")
    endpoint_results: dict[str, dict[str, str]] = {}
    normalized: dict[str, list[BrokerMirror]] = {}
    account_snapshot: BrokerAccountMirror | None = None
    normalized_trades: list[DhanTradeMirror] | None = None
    normalized_ledger: list[DhanLedgerMirror] | None = None
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

    if dhan_reader is not None:
        trade_records: list[dict[str, Any]] = []
        try:
            for page in range(100):
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
            if unkeyed_ledger_count:
                logger.warning(
                    "Dhan ledger entries skipped because provider entry IDs are missing count=%d",
                    unkeyed_ledger_count,
                )
            normalized_ledger = _normalize_dhan_ledger(
                [
                    row for row in ledger_records
                    if row.get("vouchernumber") is not None
                    and str(row["vouchernumber"]).strip()
                ],
                _user_id(active_session),
                now,
            )
            result = {
                "status": "partial" if unkeyed_ledger_count else "succeeded",
                "record_count": str(len(normalized_ledger)),
                "received_count": str(len(ledger_records)),
                "skipped_count": str(unkeyed_ledger_count),
            }
            if unkeyed_ledger_count:
                result["reason"] = "missing_provider_entry_id"
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
        successful_kinds = set(normalized)
        mirror_delete = delete(BrokerMirror).where(
            BrokerMirror.user_id == _user_id(active_session),
            BrokerMirror.broker == slug,
        )
        if failed_endpoints:
            mirror_delete = mirror_delete.where(
                BrokerMirror.kind.in_(successful_kinds)
            )
        db.execute(mirror_delete)
        db.add_all([
            row for rows in normalized.values() for row in rows
        ])
        if account_snapshot is not None:
            existing_account = db.scalar(
                select(BrokerAccountMirror).where(
                    BrokerAccountMirror.user_id == _user_id(active_session),
                    BrokerAccountMirror.broker == slug,
                )
            )
            if existing_account is None:
                db.add(account_snapshot)
            else:
                for field in (
                    "currency",
                    "available_balance",
                    "sod_limit",
                    "collateral_amount",
                    "receivable_amount",
                    "utilized_amount",
                    "blocked_payout_amount",
                    "withdrawable_balance",
                    "synced_at",
                ):
                    setattr(existing_account, field, getattr(account_snapshot, field))
        for row in normalized_trades or []:
            existing_trade = db.scalar(
                select(DhanTradeMirror).where(
                    DhanTradeMirror.user_id == _user_id(active_session),
                    DhanTradeMirror.source == "dhan",
                    DhanTradeMirror.trade_id == row.trade_id,
                )
            )
            if existing_trade is None:
                db.add(row)
            else:
                for field in (
                    "provider_trade_id",
                    "order_id",
                    "exchange_order_id",
                    "symbol",
                    "security_id",
                    "exchange_segment",
                    "side",
                    "quantity",
                    "price",
                    "charges",
                    "currency",
                    "trade_at",
                    "synced_at",
                ):
                    setattr(existing_trade, field, getattr(row, field))
        for row in normalized_ledger or []:
            existing_entry = db.scalar(
                select(DhanLedgerMirror).where(
                    DhanLedgerMirror.user_id == _user_id(active_session),
                    DhanLedgerMirror.source == "dhan",
                    DhanLedgerMirror.entry_id == row.entry_id,
                )
            )
            if existing_entry is None:
                existing_entry = db.scalar(
                    select(DhanLedgerMirror).where(
                        DhanLedgerMirror.user_id == _user_id(active_session),
                        DhanLedgerMirror.source == "dhan",
                        DhanLedgerMirror.entry_id == row.provider_entry_id,
                        DhanLedgerMirror.provider_entry_id.is_(None),
                    )
                )
            if existing_entry is None:
                db.add(row)
            else:
                for field in (
                    "entry_id",
                    "provider_entry_id",
                    "entry_date",
                    "description",
                    "debit",
                    "credit",
                    "running_balance",
                    "currency",
                    "synced_at",
                ):
                    setattr(existing_entry, field, getattr(row, field))
        if not failed_endpoints:
            credential.last_synced_at = now
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
        raise HTTPException(status_code=502, detail="Broker account sync could not be saved") from error
    return {
        "broker": slug,
        "synthetic": adapter.is_synthetic,
        "synced_at": now,
        "sync_status": (
            "partial"
            if failed_endpoints
            or any(result["status"] == "partial" for result in endpoint_results.values())
            else "successful"
        ),
        "authentication_status": "expired" if authentication_expired else "connected",
        "endpoint_results": endpoint_results,
        "holdings_count": len(normalized.get("holding", [])),
        "positions_count": len(normalized.get("position", [])),
        "cash_synced": account_snapshot is not None,
        "trades_count": len(normalized_trades) if normalized_trades is not None else None,
        "ledger_count": len(normalized_ledger) if normalized_ledger is not None else None,
        "from_date": dhan_from_date.isoformat() if dhan_reader is not None else None,
        "to_date": dhan_to_date.isoformat() if dhan_reader is not None else None,
    }


@router.post("/dhan/trades/backfill")
def backfill_dhan_trades(
    response: Response,
    from_date: date = Query(default=date(2023, 1, 1), alias="from-date"),
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
    if from_date > end:
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
                from_date.isoformat(), end.isoformat(), page
            )
            diagnostic = reader.last_http_diagnostics[-1]
            status = diagnostic["http_status"]
            chunk_rows.extend(page_rows)
            logger.info(
                "dhan_trade_backfill_chunk",
                extra={
                    "dhan_trade_chunk": {
                        "url": diagnostic["url"],
                        "date_range": f"{from_date.isoformat()}..{end.isoformat()}",
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
                        "date_range": f"{from_date.isoformat()}..{end.isoformat()}",
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

    for cursor_year in range(from_date.year, end.year + 1):
        chunk_start = max(from_date, date(cursor_year, 1, 1))
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
                "trades_imported": len(year_rows),
            }
        )

    if any(result["status"] == "failed" for result in yearly_results):
        if not all_rows:
            raise HTTPException(
                status_code=502,
                detail={"code": "dhan_trade_backfill_failed", "years": yearly_results},
            )
    inserted, updated = _upsert_dhan_trades(db, all_rows)
    db.commit()
    return {
        "source": "dhan",
        "from_date": from_date,
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
    rows = _mirror_rows(db, user_id, "dhan")
    account = db.scalar(
        select(BrokerAccountMirror).where(
            BrokerAccountMirror.user_id == user_id,
            BrokerAccountMirror.broker == "dhan",
        )
    )
    return {
        "source": "dhan",
        "currency": "INR",
        "synced_at": max(
            (
                row.synced_at for row in rows
            ),
            default=account.synced_at if account is not None else None,
        ),
        "holdings": [
            {
                "symbol": row.symbol,
                "exchange": row.exchange,
                "security_id": row.security_id,
                "isin": row.isin,
                "quantity": row.quantity,
                "average_price": row.average_price,
                "synced_at": row.synced_at,
            }
            for row in rows
            if row.kind == "holding"
        ],
        "positions": [
            {
                "symbol": row.symbol,
                "exchange": row.exchange,
                "product": row.product,
                "security_id": row.security_id,
                "quantity": row.quantity,
                "average_price": row.average_price,
                "synced_at": row.synced_at,
            }
            for row in rows
            if row.kind == "position"
        ],
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


@router.get("/portfolio-summary")
def get_broker_portfolio_summary(
    response: Response,
    active_session: UserSession = Depends(require_broker_access),
    db: Session = Depends(get_db),
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
    brokers: list[dict[str, Any]] = []
    for broker_name in broker_names:
        account = accounts_by_broker.get(broker_name)
        rows = [row for row in mirror_rows if row.broker == broker_name]
        currency = account.currency if account is not None else (
            "INR" if broker_name == "dhan" else None
        )
        if currency != "INR":
            continue
        has_unknown_cost = any(
            row.average_price is None for row in rows
        )
        invested_cost = (
            sum(
                (row.quantity * row.average_price for row in rows),
                Decimal("0"),
            )
            if not has_unknown_cost
            else None
        )
        synced_at = max(
            (
                [row.synced_at for row in rows]
                + ([account.synced_at] if account is not None else [])
            ),
            default=None,
        )
        cash = account.available_balance if account is not None else None
        brokers.append({
            "source": broker_name,
            "currency": currency,
            "synced_at": synced_at,
            "cash": str(cash) if cash is not None else None,
            "invested_cost": (
                str(invested_cost) if invested_cost is not None else None
            ),
            "market_value": None,
            "total_value": None,
            "unrealized_pnl": None,
            "day_change": None,
            "holdings_count": sum(row.kind == "holding" for row in rows),
            "positions_count": sum(row.kind == "position" for row in rows),
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
    return {
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
