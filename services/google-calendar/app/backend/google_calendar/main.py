import asyncio
import hashlib
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from backend.common.runtime import Session, uid
from backend.common.security import internal
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select

from . import service
from .models import GoogleConnection, GoogleOAuthState


@asynccontextmanager
async def lifespan(app):
    from .worker import run

    task = asyncio.create_task(run())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(lifespan=lifespan, dependencies=[Depends(internal)])


class User(BaseModel):
    user_id: str


class Start(User):
    session_hash: str


class Callback(Start):
    state: str
    code: str = ""
    error: str | None = None


def connection(db, user_id):
    conn = db.scalar(select(GoogleConnection).where(GoogleConnection.user_id == user_id).with_for_update())
    if not conn:
        conn = GoogleConnection(user_id=user_id, generation=uid(), status="disconnected")
        db.add(conn)
        db.flush()
    return conn


@app.get("/internal/status")
def status(user_id: str):
    with Session() as db:
        conn = db.get(GoogleConnection, user_id)
        return {
            "configured": service.configured(),
            "status": conn.status if conn else "disconnected",
            "generation": conn.generation if conn else None,
            "calendar_id": conn.calendar_id if conn else None,
        }


@app.post("/internal/oauth/start")
def start(body: Start):
    if not service.configured():
        raise HTTPException(503, "Google Calendar is not configured")
    state = secrets.token_urlsafe(32)
    with Session() as db:
        db.add(
            GoogleOAuthState(
                digest=hashlib.sha256(state.encode()).hexdigest(),
                user_id=body.user_id,
                session_hash=body.session_hash,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        db.commit()
    return {
        "url": "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(
            {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"],
                "response_type": "code",
                "scope": service.SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    }


@app.post("/internal/oauth/callback")
def callback(body: Callback):
    if not service.configured():
        raise HTTPException(503, "Google Calendar is not configured")
    with Session() as db:
        state = db.scalar(
            select(GoogleOAuthState)
            .where(GoogleOAuthState.digest == hashlib.sha256(body.state.encode()).hexdigest())
            .with_for_update()
        )
        if (
            not state
            or state.user_id != body.user_id
            or state.session_hash != body.session_hash
            or state.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
        ):
            raise HTTPException(400, "Invalid OAuth state")
        db.delete(state)
        db.commit()
        conn = connection(db, body.user_id)
        if body.error:
            db.commit()
            return {"status": conn.status, "generation": conn.generation}
        with service.client() as c:
            response = c.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": body.code,
                    "client_id": os.environ["GOOGLE_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                    "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"],
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            tokens = response.json()
            if not tokens.get("access_token") or not tokens.get("refresh_token"):
                raise HTTPException(400, "Offline Calendar access was not granted; reconnect with consent")
            if service.SCOPE not in tokens.get("scope", "").split():
                raise HTTPException(400, "Calendar permission was not granted")
            conn.tokens = service.encrypt(tokens)
            # A new app-owned calendar fences all earlier connection generations.
            response = service.request(
                c, conn, "POST", "/calendars", json={"summary": "Голос", "timeZone": "Europe/Moscow"}
            )
            response.raise_for_status()
            conn.calendar_id = response.json()["id"]
            conn.generation = uid()
            conn.status = "connected"
            db.commit()
            return {"status": conn.status, "generation": conn.generation}


@app.post("/internal/disconnect")
def disconnect(body: User):
    with Session() as db:
        conn = connection(db, body.user_id)
        conn.generation = uid()
        conn.status = "disconnected"
        conn.tokens = None
        conn.calendar_id = None
        db.execute(delete(GoogleOAuthState).where(GoogleOAuthState.user_id == body.user_id))
        db.commit()
        return {"status": "disconnected", "generation": conn.generation}
