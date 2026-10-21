from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.email_verification import EmailDeliveryError, send_verification_email
from app.models import EmailVerificationToken, User, UserSession
from app.security import hash_password, hash_token, new_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
SESSION_DAYS = 7
EMAIL_VERIFICATION_HOURS = 24
VERIFICATION_RESEND_SECONDS = 60
REGISTRATION_MESSAGE = "If registration can be completed, check your email for next steps."


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    created_at: datetime
    email_verified: bool
    verification_email_sent: bool | None = None


class RegistrationResponse(BaseModel):
    message: str


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128)


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
    return UserResponse(
        id=str(user.id),
        email=user.email,
        created_at=user.created_at,
        email_verified=user.email_verified,
    )


def _new_verification_token(db: Session, user: User) -> str:
    token = new_token()
    now = _utcnow()
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=hash_token(token),
            created_at=now,
            expires_at=now + timedelta(hours=EMAIL_VERIFICATION_HOURS),
        )
    )
    return token


def _send_verification_email(user: User, token: str) -> bool:
    try:
        send_verification_email(user.email, token)
    except EmailDeliveryError:
        return False
    return True


@router.post("/register", response_model=RegistrationResponse, status_code=202)
def register(credentials: Credentials, db: Session = Depends(get_db)):
    email = str(credentials.email).casefold()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        return RegistrationResponse(message=REGISTRATION_MESSAGE)

    user = User(email=email, password_hash=hash_password(credentials.password), created_at=_utcnow())
    db.add(user)
    try:
        db.flush()
        verification_token = _new_verification_token(db, user)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RegistrationResponse(message=REGISTRATION_MESSAGE)
    _send_verification_email(user, verification_token)
    return RegistrationResponse(message=REGISTRATION_MESSAGE)


@router.post("/login", response_model=UserResponse)
def login(credentials: Credentials, response: Response, db: Session = Depends(get_db)):
    email = str(credentials.email).casefold()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    _create_session(db, user, response)
    return _user_response(user)


@router.post("/verify-email", response_model=UserResponse)
def verify_email(
    payload: VerifyEmailRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    token_row = db.scalar(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == hash_token(payload.token))
        .with_for_update()
    )
    if token_row is None or token_row.consumed_at is not None:
        raise HTTPException(status_code=400, detail="Verification link is invalid or already used")

    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utcnow():
        raise HTTPException(status_code=410, detail="Verification link has expired")

    user = db.get(User, token_row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Verification link is invalid or already used")
    now = _utcnow()
    user.email_verified = True
    token_row.consumed_at = now
    db.commit()
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


@router.post("/verification/resend", status_code=202)
def resend_verification(
    response: Response,
    active_session: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    user = active_session.user
    result = {"message": "If your account needs verification, a new email will be sent shortly."}
    if user.email_verified:
        return result

    db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    now = _utcnow()
    latest = db.scalar(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user.id)
        .order_by(desc(EmailVerificationToken.created_at))
        .limit(1)
    )
    if latest is not None:
        created_at = latest.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at + timedelta(seconds=VERIFICATION_RESEND_SECONDS) > now:
            raise HTTPException(
                status_code=429,
                detail="Please wait before requesting another verification email",
            )

    db.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    token = _new_verification_token(db, user)
    db.commit()
    if not _send_verification_email(user, token):
        raise HTTPException(status_code=503, detail="Verification email could not be sent; try again later")
    return result