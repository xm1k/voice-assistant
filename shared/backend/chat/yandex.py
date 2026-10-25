"""Core-owned Yandex operation lifecycle. No Yandex credentials or API calls here."""

import os
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, SecretStr
from sqlalchemy import select

from backend.chat.models import ExceptionRecord, Outbox, YandexAccount, YandexSync, YandexSyncOperation
from backend.common.runtime import Session, uid
from backend.common.schemas import Strict
from backend.common.security import identity, internal

ERRORS = {
    "external_change": "Событие изменено в Яндекс Календаре. Синхронизация этой встречи остановлена.",
    "missing": "Событие не найдено в Яндекс Календаре. Возможно, оно удалено вручную.",
    "reconnect_required": "Подключите Яндекс Календарь заново: пароль приложения не действует.",
    "calendar_unavailable": "Нет доступа к выбранному календарю Яндекса. Проверьте подключение.",
    "delivery_exhausted": "Не удалось завершить синхронизацию с Яндекс Календарём. Можно повторить отправку.",
    "disconnected": "Отправка в Яндекс Календарь остановлена: подключение отключено.",
    "failed": "Не удалось синхронизировать встречу с Яндекс Календарём.",
    "invalid_calendar_data": "Данные встречи не удалось преобразовать в формат Яндекс Календаря.",
    "calendar_precision_unsupported": "Яндекс Календарь поддерживает время с точностью до минуты. Встреча сохранена локально, но не отправлена в Яндекс.",
    "calendar_resource_too_large": "Серия встреч превышает допустимый размер события в Яндекс Календаре.",
    "untrusted_resource": "Яндекс вернул недопустимый адрес события. Отправка остановлена.",
    "untrusted_redirect": "Яндекс вернул недопустимое перенаправление. Отправка остановлена.",
}
BLOCKED = {"conflict", "missing", "disconnected"}
NO_REMOTE_WRITE = {"invalid_calendar_data", "calendar_precision_unsupported", "calendar_resource_too_large"}


def lock_user(db, user_id):
    # Reuse core's upsert-and-lock: a new user may not have opened the chat yet.
    from backend.chat.main import chat

    return chat(db, user_id)


def sync_output(row):
    return {
        "record_id": row.record_id,
        "title": row.title,
        "version": row.version,
        "synced_version": row.synced_version,
        "operation_id": row.operation_id,
        "status": row.status,
        "code": row.code,
        "error": ERRORS.get(row.code, ERRORS["failed"]) if row.code else None,
    }


def emit(db, row):
    from backend.chat.domain import event

    event(db, row.user_id, "yandex_calendar_sync", sync_output(row))


def enqueue(db, record, operation, scope, occurrence_at):
    account = db.get(YandexAccount, record.user_id)
    if not account or account.status != "connected":
        return
    row = db.get(YandexSync, record.id)
    if row is None:
        if operation != "create" or record.data["kind"] != "meeting":
            return
        row = YandexSync(
            record_id=record.id,
            user_id=record.user_id,
            generation=account.generation,
            title=record.data["title"],
            version=record.version,
            synced_version=0,
            operation_id=uid(),
            status="pending",
        )
        db.add(row)
        predecessor = None
    else:
        row.title, row.version = record.data["title"], record.version
        if row.generation != account.generation or row.status in BLOCKED:
            emit(db, row)
            return
        predecessor = row.operation_id
    operation_id = uid()
    payload = {
        "id": operation_id,
        "user_id": record.user_id,
        "generation": account.generation,
        "record_id": record.id,
        "version": record.version,
        "operation": operation,
        "scope": scope,
        "occurrence_at": occurrence_at,
        "snapshot": {**record.data, "deleted": record.deleted},
        "exceptions": [
            {"occurrence_at": e.occurrence_at, "data": e.data}
            for e in db.scalars(select(ExceptionRecord).where(ExceptionRecord.record_id == record.id))
        ],
        "predecessor_id": predecessor,
    }
    db.add(
        YandexSyncOperation(
            id=operation_id,
            user_id=record.user_id,
            record_id=record.id,
            generation=account.generation,
            version=record.version,
            predecessor_id=predecessor,
            payload=payload,
            status="pending",
        )
    )
    row.operation_id, row.status, row.code = operation_id, "pending", None
    db.add(Outbox(queue="yandex_calendar", payload={"operation_id": operation_id}))
    emit(db, row)


def list_sync(user_id, after=None, limit=100):
    with Session() as db:
        query = select(YandexSync).where(YandexSync.user_id == user_id)
        if after:
            query = query.where(YandexSync.record_id > after)
        rows = db.scalars(query.order_by(YandexSync.record_id).limit(limit + 1)).all()
        items = []
        failed = db.scalars(
            select(YandexSyncOperation)
            .where(YandexSyncOperation.user_id == user_id, YandexSyncOperation.status == "failed")
            .order_by(YandexSyncOperation.version)
        ).all()
        for row in rows[:limit]:
            item = sync_output(row)
            first_failed = next(
                (
                    op
                    for op in failed
                    if op.record_id == row.record_id
                    and op.generation == row.generation
                    and op.version > row.synced_version
                    and (op.code not in NO_REMOTE_WRITE or op.id == row.operation_id)
                ),
                None,
            )
            if first_failed and row.status not in BLOCKED:
                item.update(
                    operation_id=first_failed.id,
                    status="failed",
                    code=first_failed.code,
                    error=ERRORS.get(first_failed.code, ERRORS["failed"]),
                )
            items.append(item)
        return {"items": items, "next_cursor": rows[limit - 1].record_id if len(rows) > limit else None}


def get_operation(operation_id):
    with Session() as db:
        op = db.get(YandexSyncOperation, operation_id)
        if op is None:
            raise HTTPException(404, "Unknown Yandex operation")
        account = db.get(YandexAccount, op.user_id)
        if (
            op.status != "pending"
            or not account
            or account.generation != op.generation
            or account.status != "connected"
        ):
            return None
        previous = db.get(YandexSyncOperation, op.predecessor_id) if op.predecessor_id else None
        # These errors are established before any CalDAV mutation. A corrected snapshot
        # can proceed; uncertain external effects and technical failures still fence it.
        while previous and previous.status == "failed" and previous.code in NO_REMOTE_WRITE:
            previous = (
                db.get(YandexSyncOperation, previous.predecessor_id) if previous.predecessor_id else None
            )
        if previous and previous.status in ("pending", "failed"):
            raise HTTPException(409, "Predecessor must finish first")
        if previous and previous.status != "synced":
            return {**op.payload, "blocked": previous.status}
        return op.payload


class SyncResult(Strict):
    status: Literal["synced", "conflict", "missing", "failed", "disconnected"]
    generation: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)
    code: str | None = Field(default=None, max_length=50)


def save_result(operation_id, result):
    with Session.begin() as db:
        op = db.get(YandexSyncOperation, operation_id)
        if op is None:
            raise HTTPException(404, "Unknown Yandex operation")
        lock_user(db, op.user_id)
        db.refresh(op, with_for_update=True)
        if result.generation != op.generation or result.version != op.version:
            raise HTTPException(409, "Yandex operation fence mismatch")
        if op.status != "pending":
            return {"accepted": True}
        account, row = db.get(YandexAccount, op.user_id), db.get(YandexSync, op.record_id)
        if not account or account.generation != op.generation or account.status == "disconnected":
            op.status, op.code = "disconnected", "disconnected"
            return {"accepted": True}
        op.status = result.status
        op.code = (
            result.code
            if result.code in ERRORS
            else {
                "conflict": "external_change",
                "missing": "missing",
                "failed": "failed",
                "disconnected": "disconnected",
            }.get(result.status)
        )
        if op.code == "reconnect_required":
            account.status = "reconnect_required"
        if result.status == "synced":
            row.synced_version = max(row.synced_version, op.version)
        # A blocked predecessor blocks the whole series, even if newer local writes exist.
        if row.operation_id == op.id or result.status in BLOCKED:
            row.status, row.code = op.status, op.code
        emit(db, row)
        return {"accepted": True}


def disconnect(db, user_id, generation):
    from backend.chat.domain import event

    account = db.get(YandexAccount, user_id)
    if account:
        account.status, account.generation = "disconnected", generation
    for row in db.scalars(select(YandexSync).where(YandexSync.user_id == user_id)):
        row.status, row.code = "disconnected", "disconnected"
        emit(db, row)
    for op in db.scalars(
        select(YandexSyncOperation).where(
            YandexSyncOperation.user_id == user_id, YandexSyncOperation.status == "pending"
        )
    ):
        op.status, op.code = "disconnected", "disconnected"
    event(
        db,
        user_id,
        "yandex_calendar_sync",
        {"connection": {"status": "disconnected", "generation": generation}},
    )


def retry_operation(operation_id, user_id):
    with Session.begin() as db:
        lock_user(db, user_id)
        op = db.get(YandexSyncOperation, operation_id)
        if not op or op.user_id != user_id:
            raise HTTPException(404, "Операция не найдена")
        row, account = db.get(YandexSync, op.record_id), db.get(YandexAccount, user_id)
        if (
            op.status != "failed"
            or op.code not in {"delivery_exhausted", "failed"}
            or row.status in BLOCKED
            or not account
            or account.status != "connected"
            or account.generation != op.generation
        ):
            raise HTTPException(409, "Эту операцию нельзя повторить")
        op.status, op.code = "pending", None
        row.status, row.code = "pending", None
        db.add(Outbox(queue="yandex_calendar", payload={"operation_id": op.id}))
        emit(db, row)
        return sync_output(row)


router = APIRouter()
BASE = "/api/v1/integrations/yandex-calendar"


def remote(method, path, **kwargs):
    try:
        with httpx.Client(timeout=45) as client:
            response = client.request(
                method,
                os.getenv("YANDEX_CALENDAR_URL", "http://yandex-calendar:8000") + path,
                headers={"X-Internal-Token": os.getenv("INTERNAL_TOKEN", "")},
                **kwargs,
            )
            if response.status_code in (400, 401, 403, 409, 422):
                raise HTTPException(
                    response.status_code,
                    "Не удалось подключить календарь. Проверьте логин, пароль приложения и выбранный календарь.",
                )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        raise HTTPException(503, "Сервис Яндекс Календаря временно недоступен") from None


def same_origin(request):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        raise HTTPException(403, "Недопустимый источник запроса")


def secure_credentials(request):
    same_origin(request)
    if (
        request.url.scheme != "https"
        and request.headers.get("x-forwarded-proto") != "https"
        and request.url.hostname not in ("localhost", "127.0.0.1", "::1")
    ):
        raise HTTPException(403, "Для передачи пароля приложения требуется HTTPS")


def mirror(db, user_id, state):
    from backend.chat.domain import event

    account = db.get(YandexAccount, user_id)
    generation = state.get("generation")
    if not generation:
        return
    changed = not account or (account.generation, account.status) != (generation, state["status"])
    if account and account.generation != generation:
        disconnect(db, user_id, generation)
    if not account:
        account = YandexAccount(user_id=user_id, generation=generation, status=state["status"])
        db.add(account)
    account.generation, account.status = generation, state["status"]
    if changed:
        event(
            db,
            user_id,
            "yandex_calendar_sync",
            {"connection": {"status": account.status, "generation": account.generation}},
        )


@router.get(BASE)
def connection_status(user=Depends(identity)):
    try:
        with Session() as db:
            account = db.get(YandexAccount, user["id"])
            observed = (account.generation, account.status) if account else None
        state = remote("GET", "/internal/status", params={"user_id": user["id"]})
        with Session.begin() as db:
            lock_user(db, user["id"])
            account = db.get(YandexAccount, user["id"])
            current = (account.generation, account.status) if account else None
            if current == observed:
                mirror(db, user["id"], state)
            elif account:
                state = {**state, "generation": account.generation, "status": account.status}
            return {
                key: state.get(key)
                for key in ("configured", "status", "generation", "calendar_id", "calendar_name")
            }
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        return {"configured": False, "status": "unavailable", "generation": None}


class Credentials(Strict):
    username: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)


class CalendarSelection(Strict):
    generation: str = Field(min_length=1, max_length=36)
    calendar_id: str = Field(min_length=1, max_length=2048)


@router.post(BASE + "/discover")
def discover(body: Credentials, request: Request, user=Depends(identity)):
    secure_credentials(request)
    with Session.begin() as db:
        lock_user(db, user["id"])
        outcome = remote(
            "POST",
            "/internal/discover",
            json={
                "user_id": user["id"],
                "username": body.username,
                "password": body.password.get_secret_value(),
            },
        )
        mirror(db, user["id"], outcome)
        return outcome


@router.post(BASE + "/connect")
def connect_account(body: CalendarSelection, request: Request, user=Depends(identity)):
    same_origin(request)
    with Session.begin() as db:
        lock_user(db, user["id"])
        outcome = remote("POST", "/internal/connect", json={"user_id": user["id"], **body.model_dump()})
        mirror(db, user["id"], outcome)
        return outcome


@router.delete(BASE)
def disconnect_account(request: Request, user=Depends(identity)):
    same_origin(request)
    with Session.begin() as db:
        lock_user(db, user["id"])
        outcome = remote("POST", "/internal/disconnect", json={"user_id": user["id"]})
        disconnect(db, user["id"], outcome["generation"])
        return outcome


@router.get(BASE + "/sync")
def sync_status(
    cursor: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=100, ge=1, le=100),
    user=Depends(identity),
):
    return list_sync(user["id"], cursor, limit)


@router.post(BASE + "/operations/{operation_id}/retry")
def retry(operation_id: str, request: Request, user=Depends(identity)):
    same_origin(request)
    return retry_operation(operation_id, user["id"])


@router.get("/internal/yandex-calendar/operations/{operation_id}", dependencies=[Depends(internal)])
def operation_details(operation_id: str):
    return get_operation(operation_id)


@router.post("/internal/yandex-calendar/operations/{operation_id}/result", dependencies=[Depends(internal)])
def operation_result(operation_id: str, body: SyncResult):
    return save_result(operation_id, body)


@router.post(
    "/internal/yandex-calendar/operations/{operation_id}/exhausted", dependencies=[Depends(internal)]
)
def operation_exhausted(operation_id: str):
    with Session() as db:
        op = db.get(YandexSyncOperation, operation_id)
        if op is None:
            raise HTTPException(404, "Unknown Yandex operation")
        body = SyncResult(
            status="failed", generation=op.generation, version=op.version, code="delivery_exhausted"
        )
    return save_result(operation_id, body)
