"""Independent authentication service; initialize schema with migrate()."""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, String, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get("AUTH_DATABASE_URL")
# Missing configuration prevents database access rather than selecting a shared database.
engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
Session = sessionmaker(bind=engine, expire_on_commit=False)
PASSWORDS = PasswordHasher()
COOKIE = "golos_session"
SESSION_SECONDS = 30 * 24 * 60 * 60


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "auth_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    timezone: Mapped[str] = mapped_column(String(100))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("auth_users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def migrate():
    if engine is None:
        raise RuntimeError("AUTH_DATABASE_URL is required")
    Base.metadata.create_all(engine)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Input):
    username: str = Field(min_length=3, max_length=100, pattern=r"^\S+$")
    password: str = Field(min_length=1, max_length=1024)


class TimezoneInput(Input):
    timezone: str = Field(max_length=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid IANA timezone") from None
        return value


class Registration(Credentials, TimezoneInput):
    timezone: str = "Europe/Moscow"
    password: str = Field(min_length=8, max_length=1024)


class UserOutput(Input):
    id: str
    username: str
    timezone: str


class LogoutOutput(Input):
    ok: bool


class SessionInput(Input):
    session: str = Field(min_length=1, max_length=256)


def user_data(user):
    return {"id": user.id, "username": user.username, "timezone": user.timezone}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def lookup(db, token):
    session = db.get(AuthSession, token_hash(token or ""))
    if session is None:
        raise HTTPException(401, "Invalid session")
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired")
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(401, "Invalid session")
    return user


def authenticated(request: Request):
    with Session() as db:
        return user_data(lookup(db, request.cookies.get(COOKIE)))


def secure_cookie():
    return os.environ.get("COOKIE_SECURE", "true").lower() not in ("false", "0")


def establish(db, user, response):
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token_hash=token_hash(token),
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS),
        )
    )
    db.commit()
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=secure_cookie(),
        samesite="lax",
        max_age=SESSION_SECONDS,
        path="/",
    )
    return user_data(user)


app = FastAPI(title="Golos Auth", openapi_url="/auth/openapi.json")


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    return await call_next(request)


@app.post("/api/v1/auth/register", status_code=201, response_model=UserOutput)
def register(body: Registration, response: Response):
    with Session() as db:
        user = User(
            id=str(uuid4()),
            username=body.username,
            password_hash=PASSWORDS.hash(body.password),
            timezone=body.timezone,
        )
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Username already registered") from None
        return establish(db, user, response)


@app.post("/api/v1/auth/login", response_model=UserOutput)
def login(body: Credentials, response: Response):
    with Session() as db:
        user = db.scalar(select(User).where(User.username == body.username))
        if user is None:
            # Match the expensive verification path for unknown usernames.
            PASSWORDS.hash(body.password)
            raise HTTPException(401, "Invalid credentials")
        try:
            PASSWORDS.verify(user.password_hash, body.password)
        except VerificationError:
            raise HTTPException(401, "Invalid credentials") from None
        if PASSWORDS.check_needs_rehash(user.password_hash):
            user.password_hash = PASSWORDS.hash(body.password)
        return establish(db, user, response)


@app.post("/api/v1/auth/logout", response_model=LogoutOutput)
def logout(request: Request, response: Response):
    with Session.begin() as db:
        session = db.get(AuthSession, token_hash(request.cookies.get(COOKIE, "")))
        if session:
            db.delete(session)
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=secure_cookie(), samesite="lax")
    return {"ok": True}


@app.get("/api/v1/auth/me", response_model=UserOutput)
def me(user=Depends(authenticated)):
    return user


@app.patch("/api/v1/auth/me", response_model=UserOutput)
def update_me(body: TimezoneInput, request: Request):
    with Session.begin() as db:
        user = lookup(db, request.cookies.get(COOKIE))
        user.timezone = body.timezone
        return user_data(user)


@app.post("/internal/session", response_model=UserOutput)
def internal_session(body: SessionInput, request: Request):
    expected = os.environ.get("INTERNAL_TOKEN")
    if not expected:
        raise HTTPException(503, "Internal authentication is not configured")
    supplied = request.headers.get("x-internal-token", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "Invalid internal token")
    with Session() as db:
        return user_data(lookup(db, body.session))
