from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User, UserSession
from app.security import hash_password, hash_token, new_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
SESSION_DAYS = 7


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    created_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _set_session_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    cookie_options = {
        "max_age": SESSION_DAYS * 24 * 60 * 60,
        "secure": settings.environment == "production",
        "samesite": "lax",
    }
    response.set_cookie(
        "session_token", session_token, httponly=True, path="/api/v1", **cookie_options
    )
    response.set_cookie("csrf_token", csrf_token, httponly=False, path="/", **cookie_options)


def _create_session(db: Session, user: User, response: Response) -> None:
    session_token = new_token()
    csrf_token = new_token()
    now = _utcnow()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(session_token),
            csrf_hash=hash_token(csrf_token),
            created_at=now,
            expires_at=now + timedelta(days=SESSION_DAYS),
        )
    )
    db.commit()
    _set_session_cookies(response, session_token, csrf_token)


def _user_response(user: User) -> UserResponse:
    return UserResponse(id=str(user.id), email=user.email, created_at=user.created_at)


@router.post("/register", response_model=UserResponse, status_code=201)
def register(credentials: Credentials, response: Response, db: Session = Depends(get_db)):
    email = str(credentials.email).casefold()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(email=email, password_hash=hash_password(credentials.password), created_at=_utcnow())
    db.add(user)
    try:
        db.flush()
        _create_session(db, user, response)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists") from error
    return _user_response(user)


@router.post("/login", response_model=UserResponse)
def login(credentials: Credentials, response: Response, db: Session = Depends(get_db)):
    email = str(credentials.email).casefold()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    _create_session(db, user, response)
    return _user_response(user)


def get_active_session(request: Request, db: Session = Depends(get_db)) -> UserSession:
    raw_token = request.cookies.get("session_token")
    if not raw_token:
        raise HTTPException(status_code=401, detail="Authentication required")
    active_session = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(raw_token),
            UserSession.revoked_at.is_(None),
        )
    )
    if active_session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    expires_at = active_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utcnow():
        raise HTTPException(status_code=401, detail="Session expired")
    return active_session


def get_current_user(active_session: UserSession = Depends(get_active_session)) -> User:
    return active_session.user


def require_csrf(
    request: Request,
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    csrf_cookie = request.cookies.get("csrf_token")
    if (
        not csrf_cookie
        or not csrf_header
        or not hmac_compare(csrf_cookie, csrf_header)
        or not hmac_compare(hash_token(csrf_cookie), active_session.csrf_hash)
    ):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return active_session


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)):
    return _user_response(user)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    active_session: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    active_session.revoked_at = _utcnow()
    db.commit()
    response.delete_cookie("session_token", path="/api/v1")
    response.delete_cookie("csrf_token", path="/")