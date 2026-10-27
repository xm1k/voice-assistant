import asyncio
from contextlib import asynccontextmanager, suppress

from backend.common.runtime import Session, uid
from backend.common.security import internal
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from . import service
from .models import YandexConnection


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
    user_id: str = Field(min_length=1, max_length=36)


class Discover(User):
    username: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=4096)


class Connect(User):
    generation: str
    calendar_id: str


def connection(db, user_id):
    value = db.scalar(select(YandexConnection).where(YandexConnection.user_id == user_id).with_for_update())
    if not value:
        value = YandexConnection(user_id=user_id, generation=uid(), status="disconnected", calendars=[])
        db.add(value)
        db.flush()
    return value


def output(conn):
    return {
        "configured": service.configured(),
        "status": conn.status if conn and service.configured() else "disconnected",
        "generation": conn.generation if conn else None,
        "calendar_id": conn.calendar_id if conn else None,
        "calendar_name": conn.calendar_name if conn else None,
    }


@app.get("/internal/status")
def status(user_id: str):
    with Session() as db:
        conn = db.get(YandexConnection, user_id)
        result = output(conn)
        if conn and conn.status == "select_calendar":
            result["calendars"] = conn.calendars
        return result


@app.post("/internal/discover")
def discover(body: Discover):
    if not service.configured():
        raise HTTPException(503, "Yandex Calendar is not configured")
    credentials = {"username": body.username, "password": body.password.get_secret_value()}
    try:
        calendars = service.discover_calendars(credentials)
    except service.RetryableYandexError as exc:
        raise HTTPException(
            503, "Yandex Calendar is temporarily unavailable", headers={"Retry-After": str(exc.retry_after)}
        ) from None
    except service.YandexError:
        raise HTTPException(
            400, "Unable to discover writable calendars; verify your Yandex app password"
        ) from None
    if not calendars:
        raise HTTPException(400, "No writable event calendars found")
    with Session() as db:
        conn = connection(db, body.user_id)
        conn.generation = uid()
        conn.credentials = service.encrypt(credentials)
        conn.calendars = calendars
        conn.status = "select_calendar"
        conn.calendar_id = None
        conn.calendar_name = None
        db.commit()
        return {"status": conn.status, "generation": conn.generation, "calendars": calendars}


@app.post("/internal/connect")
def connect(body: Connect):
    if not service.configured():
        raise HTTPException(503, "Yandex Calendar is not configured")
    with Session() as db:
        conn = connection(db, body.user_id)
        if (
            conn.generation != body.generation
            or conn.status not in ("select_calendar", "connected")
            or not conn.credentials
        ):
            raise HTTPException(409, "Calendar discovery expired; discover calendars again")
        calendar = next((item for item in conn.calendars if item["id"] == body.calendar_id), None)
        if not calendar:
            raise HTTPException(400, "Select a calendar from the current discovery")
        try:
            with service.http_client(service.decrypt(conn.credentials)) as client:
                service.validate_collection(client, calendar["id"])
        except service.RetryableYandexError as exc:
            raise HTTPException(
                503,
                "Yandex Calendar is temporarily unavailable",
                headers={"Retry-After": str(exc.retry_after)},
            ) from None
        except service.YandexError as exc:
            if exc.reconnect:
                conn.status = "reconnect_required"
                db.commit()
            raise HTTPException(400, "The selected calendar is unavailable or read-only") from None
        if conn.calendar_id and conn.calendar_id != calendar["id"]:
            conn.generation = uid()
        conn.calendar_id, conn.calendar_name = calendar["id"], calendar["name"]
        conn.status = "connected"
        db.commit()
        return output(conn)


@app.post("/internal/disconnect")
def disconnect(body: User):
    with Session() as db:
        conn = connection(db, body.user_id)
        conn.generation = uid()
        conn.status = "disconnected"
        conn.credentials = None
        conn.calendars = []
        conn.calendar_id = conn.calendar_name = None
        db.commit()
        return {"status": conn.status, "generation": conn.generation}
