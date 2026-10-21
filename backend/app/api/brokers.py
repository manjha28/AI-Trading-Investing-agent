import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app.accounting import LedgerError, calculate_ledger, money
from app.api.auth import get_active_session, require_csrf
from app.broker_adapters import (
    ADAPTERS,
    BrokerAdapter,
    BrokerAdapterError,
    BrokerReconnectRequired,
    get_broker_adapter,
)
from app.config import settings
from app.db import get_db
from app.models import (
    BrokerCredential,
    BrokerMirror,
    BrokerOAuthState,
    Portfolio,
    PortfolioTransaction,
    UserSession,
)
from app.security import hash_token, new_token

router = APIRouter(prefix="/brokers", tags=["read-only brokers"])
OAUTH_STATE_TTL = timedelta(minutes=10)


class ConnectRequest(BaseModel):
    client_id: str | None = Field(default=None, min_length=1, max_length=80)


class MirrorRow(BaseModel):
    kind: str
    symbol: str
    exchange: str
    product: str
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


def _user_id(active_session: UserSession) -> UUID:
    return active_session.user_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fernet() -> Fernet:
    key = settings.broker_encryption_key
    if key is None:
        raise _broker_error(503, "broker_setup_required", "Broker token encryption is not configured.")
    try:
        return Fernet(key.get_secret_value().encode())
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
                quantity=quantity,
                average_price=average_price,
                synced_at=synced_at,
            )
        )
    return normalized


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
    transactions = db.scalars(
        select(PortfolioTransaction).where(PortfolioTransaction.portfolio_id == portfolio.id)
    ).all()
    try:
        manual_positions = calculate_ledger(transactions).positions
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
    active_session: UserSession = Depends(require_broker_access),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    adapter = _adapter(slug)
    credential = _credential(db, _user_id(active_session), slug)
    tokens = _fresh_tokens(db, adapter, credential)
    now = _now()
    try:
        holdings = _normalize_rows(
            adapter.get_holdings(tokens["access_token"], tokens),
            "holding",
            now,
            _user_id(active_session),
            slug,
        )
        positions = _normalize_rows(
            adapter.get_positions(tokens["access_token"], tokens),
            "position",
            now,
            _user_id(active_session),
            slug,
        )
    except BrokerReconnectRequired as error:
        raise _broker_error(401, "broker_authentication_required", str(error)) from error
    except BrokerAdapterError as error:
        raise _broker_error(502, "broker_provider_error", "Broker account sync failed.") from error

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
    db.execute(
        delete(BrokerMirror).where(
            BrokerMirror.user_id == _user_id(active_session),
            BrokerMirror.broker == slug,
        )
    )
    db.add_all(holdings + positions)
    credential.last_synced_at = now
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=502, detail="Broker account sync could not be saved") from error
    return {
        "broker": slug,
        "synthetic": adapter.is_synthetic,
        "synced_at": now,
        "holdings_count": len(holdings),
        "positions_count": len(positions),
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
    return _mirror_rows(db, _user_id(active_session), slug, kind)


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
            calculate_ledger(
                db.scalars(
                    select(PortfolioTransaction).where(
                        PortfolioTransaction.portfolio_id == portfolio.id
                    )
                ).all()
            )
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
