import asyncio
import hashlib
import json
import os
from datetime import date, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi import Query as Param
from pydantic import Field, ValidationError
from sqlalchemy import select

from backend.chat.domain import (
    change,
    event,
    query_records,
    record_key,
    records,
    resolve_record,
)
from backend.chat.models import Chat, Event, Job, Message, Notification, Outbox, Receipt, Setting
from backend.chat.pagination import page
from backend.chat.presentation import ACTIONS, confirmation, record_card, visible_cards
from backend.chat.responses import (
    ChatOutput,
    JobAccepted,
    JobOutput,
    MessageOutput,
    NotificationOutput,
    Page,
    RecordOutput,
)
from backend.common.runtime import Session, now, uid
from backend.common.schemas import Decision, Fields, Query, RecordData, Strict
from backend.common.security import check_origin, identity, internal, validate_session

app = FastAPI(title="Golos Domain API", version="1.0.0")
TERMINAL = ("succeeded", "failed", "cancelled")


def chat(db, user_id):
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(Chat).values(id=user_id, generation=0).on_conflict_do_nothing(index_elements=["id"]))
    item = db.scalar(select(Chat).where(Chat.id == user_id).with_for_update())
    return item


def job_output(job):
    result = job.result
    if result and "cards" in result:
        result = {**result, "cards": visible_cards(result["cards"])}
    return {"id": job.id, "status": job.status, "result": result, "error": job.error}


def message_output(item):
    return {
        "id": item.id,
        "role": item.role,
        "text": item.text,
        "created_at": item.created_at.replace(tzinfo=item.created_at.tzinfo or timezone.utc).isoformat(),
        "cards": visible_cards(item.cards or []),
    }


def add_message(db, user_id, generation, role, text, cards=None):
    item = Message(
        id=uid(),
        user_id=user_id,
        generation=generation,
        role=role,
        text=text,
        cards=cards or [],
        created_at=now(),
    )
    db.add(item)
    event(db, user_id, "message", message_output(item))
    return item


def fail_with_message(db, current, item, error):
    item.status, item.error = "failed", error
    message = add_message(db, item.user_id, current.generation, "assistant", error)
    item.result = {"message_id": message.id}


def enqueue_assistant(db, current, user, text, request_id):
    existing = db.scalar(
        select(Job).where(
            Job.user_id == user["id"],
            Job.kind == "assistant",
            Job.request_id == request_id,
        )
    )
    if existing:
        if existing.input.get("text") != text:
            raise HTTPException(409, "request_id reused with different message")
        return existing
    if not text.strip():
        raise HTTPException(422, "Empty message")
    message = add_message(db, user["id"], current.generation, "user", text)
    item = Job(
        id=uid(),
        user_id=user["id"],
        kind="assistant",
        status="queued",
        generation=current.generation,
        request_id=request_id,
        input={"text": text, "message_id": message.id, "timezone": user["timezone"]},
        created_at=now(),
    )
    db.add(item)
    db.add(Outbox(queue="chat", payload={"job_id": item.id}))
    event(db, user["id"], "job", job_output(item))
    return item


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/chat", response_model=ChatOutput)
def get_chat(user=Depends(identity)):
    with Session.begin() as db:
        item = chat(db, user["id"])
        busy = (
            db.scalar(select(Job.id).where(Job.user_id == user["id"], Job.status.not_in(TERMINAL)))
            is not None
        )
        return {"id": item.id, "pending": bool(item.pending), "busy": busy}


@app.get("/api/v1/chat/messages", response_model=Page[MessageOutput])
def get_messages(
    limit: int = Param(50, ge=1, le=100),
    cursor: str | None = None,
    direction: str = "older",
    user=Depends(identity),
):
    if direction not in ("older", "newer"):
        raise HTTPException(422, "Unknown direction")
    with Session.begin() as db:
        current = chat(db, user["id"])
        rows = db.scalars(
            select(Message)
            .where(Message.user_id == user["id"], Message.generation == current.generation)
            .order_by(Message.created_at, Message.id)
        ).all()
        items = [message_output(x) for x in rows]
        if direction == "older":
            items.reverse()

        # The anchor is immutable created_at+id; inverted lexical bytes preserve keyset order.
        def key(item):
            value = item["created_at"] + "|" + item["id"]
            return "".join(chr(255 - ord(c)) for c in value) if direction == "older" else value

        output = page(
            items,
            limit,
            cursor,
            {"user": user["id"], "generation": current.generation, "direction": direction},
            key=key,
        )
        if direction == "older":
            output["items"].reverse()
        return output


@app.delete("/api/v1/chat/messages")
def clear_messages(user=Depends(identity)):
    with Session.begin() as db:
        current = chat(db, user["id"])
        current.generation += 1
        current.pending = None
        for item in db.scalars(select(Job).where(Job.user_id == user["id"], Job.status.not_in(TERMINAL))):
            item.status = "cancelled"
            event(db, user["id"], "job", job_output(item))
        event(db, user["id"], "history_cleared", {})
    return {"ok": True}


class Send(Strict):
    text: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(min_length=1, max_length=100)


@app.post("/api/v1/chat/messages", status_code=202, response_model=JobAccepted)
def send_message(body: Send, user=Depends(identity)):
    with Session.begin() as db:
        current = chat(db, user["id"])
        item = enqueue_assistant(db, current, user, body.text, body.request_id)
        return {"job_id": item.id, "status": item.status}


@app.get("/api/v1/jobs", response_model=Page[JobOutput])
def get_jobs(
    status: str = "active",
    limit: int = Param(50, ge=1, le=100),
    cursor: str | None = None,
    user=Depends(identity),
):
    with Session() as db:
        statement = select(Job).where(Job.user_id == user["id"])
        statement = (
            statement.where(Job.status.not_in(TERMINAL))
            if status == "active"
            else statement.where(Job.status == status)
        )
        items = [job_output(x) for x in db.scalars(statement.order_by(Job.id))]
        return page(items, limit, cursor, {"user": user["id"], "status": status})


@app.get("/api/v1/jobs/{job_id}", response_model=JobOutput)
def get_job(job_id: str, user=Depends(identity)):
    with Session() as db:
        item = db.get(Job, job_id)
        if not item or item.user_id != user["id"]:
            raise HTTPException(404, "Job not found")
        return job_output(item)


def receipt(db, user_id, key, fingerprint, action):
    old = db.scalar(select(Receipt).where(Receipt.user_id == user_id, Receipt.key == key))
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
    if old:
        if old.fingerprint != digest:
            raise HTTPException(409, "Idempotency key reused for a different request")
        return old.result
    result = action()
    db.add(Receipt(user_id=user_id, key=key, fingerprint=digest, result=result))
    return result


def resource_kind(request):
    path = request.url.path.split("/")
    return {"tasks": "task", "reminders": "reminder"}.get(path[3])


@app.get("/api/v1/records", response_model=Page[RecordOutput])
@app.get("/api/v1/calendar", response_model=Page[RecordOutput])
@app.get("/api/v1/tasks", response_model=Page[RecordOutput])
@app.get("/api/v1/reminders", response_model=Page[RecordOutput])
def list_records(
    request: Request,
    period_start: date | None = None,
    period_end: date | None = None,
    kind: str | None = None,
    status: str | None = None,
    text: str | None = None,
    limit: int = Param(50, ge=1, le=100),
    cursor: str | None = None,
    user=Depends(identity),
):
    try:
        query = Query(
            kind=resource_kind(request) or kind,
            status=status,
            text=text,
            period_start=period_start,
            period_end=period_end,
        )
    except ValidationError as exc:
        raise HTTPException(422, str(exc))
    with Session() as db:
        items = records(db, user["id"], query, user["timezone"], default_recurrence_window=True)
        # JSON converts tuple anchor to list; use one sortable string.
        return page(
            items,
            limit,
            cursor,
            {"user": user["id"], "timezone": user["timezone"], **query.model_dump(mode="json")},
            key=lambda x: list(record_key(x)),
        )


@app.post("/api/v1/records", response_model=RecordOutput)
@app.post("/api/v1/calendar", response_model=RecordOutput)
@app.post("/api/v1/tasks", response_model=RecordOutput)
@app.post("/api/v1/reminders", response_model=RecordOutput)
def create_record(
    request: Request,
    body: RecordData,
    idempotency_key: str = Header(min_length=1, max_length=100),
    user=Depends(identity),
):
    forced = resource_kind(request)
    if forced and body.kind != forced:
        raise HTTPException(422, "Resource kind mismatch")
    with Session.begin() as db:
        chat(db, user["id"])  # Serialize user mutations and idempotency checks.
        fields = body.model_dump(mode="json")
        if "timezone" not in body.model_fields_set:
            fields["timezone"] = user["timezone"]
        return receipt(
            db,
            user["id"],
            idempotency_key,
            ["create", request.url.path, fields],
            lambda: change(db, user["id"], "create", fields, timezone_name=user["timezone"]),
        )


class Patch(Strict):
    fields: Fields
    scope: str | None = None
    occurrence_at: str | None = None
    version: int | None = Field(default=None, ge=1)


def check_resource(db, request, record_id, user_id):
    from backend.chat.models import Record

    row = db.get(Record, record_id)
    forced = resource_kind(request)
    if not row or row.user_id != user_id or row.deleted or (forced and row.data["kind"] != forced):
        raise HTTPException(404, "Record not found")
    return row


@app.get("/api/v1/records/{record_id}", response_model=RecordOutput)
@app.get("/api/v1/calendar/{record_id}", response_model=RecordOutput)
@app.get("/api/v1/tasks/{record_id}", response_model=RecordOutput)
@app.get("/api/v1/reminders/{record_id}", response_model=RecordOutput)
def get_record(request: Request, record_id: str, occurrence_at: str | None = None, user=Depends(identity)):
    with Session() as db:
        check_resource(db, request, record_id, user["id"])
        try:
            return resolve_record(db, user["id"], record_id, occurrence_at=occurrence_at)
        except ValueError as exc:
            raise HTTPException(422, "Некорректная дата повторения") from exc


@app.patch("/api/v1/records/{record_id}", response_model=RecordOutput)
@app.patch("/api/v1/calendar/{record_id}", response_model=RecordOutput)
@app.patch("/api/v1/tasks/{record_id}", response_model=RecordOutput)
@app.patch("/api/v1/reminders/{record_id}", response_model=RecordOutput)
def patch_record(
    request: Request,
    record_id: str,
    body: Patch,
    idempotency_key: str = Header(min_length=1, max_length=100),
    user=Depends(identity),
):
    if body.scope not in (None, "series", "occurrence"):
        raise HTTPException(422, "Unknown scope")
    with Session.begin() as db:
        chat(db, user["id"])
        check_resource(db, request, record_id, user["id"])
        if resource_kind(request) and body.fields.kind and body.fields.kind != resource_kind(request):
            raise HTTPException(422, "Resource kind mismatch")
        return receipt(
            db,
            user["id"],
            idempotency_key,
            ["patch", record_id, body.model_dump(mode="json")],
            lambda: change(
                db,
                user["id"],
                "update",
                body.fields.model_dump(mode="json", exclude_unset=True),
                record_id,
                body.version,
                body.scope or "series",
                body.occurrence_at,
                user["timezone"],
            ),
        )


@app.delete("/api/v1/records/{record_id}")
@app.delete("/api/v1/calendar/{record_id}")
@app.delete("/api/v1/tasks/{record_id}")
@app.delete("/api/v1/reminders/{record_id}")
def delete_record(
    request: Request,
    record_id: str,
    scope: str = "series",
    occurrence_at: str | None = None,
    version: int | None = None,
    idempotency_key: str = Header(min_length=1, max_length=100),
    user=Depends(identity),
):
    if scope not in ("series", "occurrence"):
        raise HTTPException(422, "Unknown scope")
    with Session.begin() as db:
        chat(db, user["id"])
        return receipt(
            db,
            user["id"],
            idempotency_key,
            ["delete", record_id, scope, occurrence_at, version],
            lambda: (
                check_resource(db, request, record_id, user["id"]),
                change(
                    db, user["id"], "delete", {}, record_id, version, scope, occurrence_at, user["timezone"]
                ),
            )[1],
        )


def notice_output(item):
    return {
        **{
            key: getattr(item, key)
            for key in ("id", "record_id", "title", "due_at", "read_at", "occurrence_at")
        },
        "read": bool(item.read_at),
    }


@app.get("/api/v1/notifications", response_model=Page[NotificationOutput])
def notifications(
    read: bool | None = None,
    limit: int = Param(50, ge=1, le=100),
    cursor: str | None = None,
    user=Depends(identity),
):
    with Session() as db:
        rows = db.scalars(
            select(Notification)
            .where(Notification.user_id == user["id"])
            .order_by(Notification.due_at, Notification.id)
        )
        items = [notice_output(x) for x in rows if read is None or bool(x.read_at) == read]
        return page(
            items,
            limit,
            cursor,
            {"user": user["id"], "read": read},
            key=lambda x: x["due_at"] + "|" + x["id"],
        )


@app.post("/api/v1/notifications/{notice_id}/read", response_model=NotificationOutput)
def read_notification(notice_id: str, user=Depends(identity)):
    with Session.begin() as db:
        chat(db, user["id"])
        row = db.get(Notification, notice_id)
        if not row or row.user_id != user["id"]:
            raise HTTPException(404, "Notification not found")
        row.read_at = row.read_at or now().isoformat()
        event(db, user["id"], "notification", notice_output(row))
        return notice_output(row)


class QueryRequest(Strict):
    user_id: str
    query: Query
    timezone: str = "Europe/Moscow"


@app.post("/internal/query", dependencies=[Depends(internal)])
def query_endpoint(body: QueryRequest):
    with Session() as db:
        return query_records(db, body.user_id, body.query, body.timezone)


@app.post("/internal/search-candidates", dependencies=[Depends(internal)])
def search_candidates(body: QueryRequest):
    with Session() as db:
        return {
            "items": records(
                db, body.user_id, body.query.model_copy(update={"operation": "search"}), body.timezone
            )
        }


class Transcription(Strict):
    user_id: str
    request_id: str = Field(min_length=1, max_length=100)
    audio_path: str
    timezone: str = "Europe/Moscow"


@app.post("/internal/transcriptions", dependencies=[Depends(internal)])
def register_audio(body: Transcription):
    from pathlib import Path

    # Accept only UUID filenames created by Voice, never arbitrary host paths.
    if (
        Path(body.audio_path).name != body.audio_path
        or not body.audio_path
        or "/" in body.audio_path
        or "\\" in body.audio_path
    ):
        raise HTTPException(422, "Invalid audio filename")
    with Session.begin() as db:
        current = chat(db, body.user_id)
        old = db.scalar(
            select(Job).where(
                Job.user_id == body.user_id, Job.kind == "transcription", Job.request_id == body.request_id
            )
        )
        if old:
            return {
                "job_id": old.id,
                "status": old.status,
                "deduplicated": True,
                "audio_path": old.input["audio_path"],
            }
        item = Job(
            id=uid(),
            user_id=body.user_id,
            kind="transcription",
            status="queued",
            request_id=body.request_id,
            generation=current.generation,
            input={"audio_path": body.audio_path, "timezone": body.timezone},
            created_at=now(),
        )
        db.add(item)
        db.add(Outbox(queue="transcription", payload={"job_id": item.id}))
        event(db, body.user_id, "job", job_output(item))
        return {"job_id": item.id, "status": item.status, "deduplicated": False}


class Claim(Strict):
    kind: str


@app.post("/internal/jobs/{job_id}/claim", dependencies=[Depends(internal)])
def claim(job_id: str, body: Claim):
    with Session.begin() as db:
        item = db.get(Job, job_id)
        if not item:
            return None
        current = chat(db, item.user_id)
        db.refresh(item)
        if item.kind != body.kind or item.status in TERMINAL:
            return None
        if item.generation != current.generation:
            item.status = "cancelled"
            event(db, item.user_id, "job", job_output(item))
            return None
        if (
            item.status == "running"
            and item.lease_until
            and item.lease_until.replace(tzinfo=timezone.utc) > now()
        ):
            raise HTTPException(409, "Job lease held")
        if item.kind == "assistant":
            prior = db.scalar(
                select(Job.id).where(
                    Job.user_id == item.user_id,
                    Job.kind == "assistant",
                    Job.generation == item.generation,
                    Job.status.not_in(TERMINAL),
                    (Job.created_at < item.created_at)
                    | ((Job.created_at == item.created_at) & (Job.id < item.id)),
                )
            )
            if prior:
                raise HTTPException(409, "Earlier message pending")
        item.status = "running"
        item.lease_until = now() + timedelta(minutes=12)
        item.claim_token = uid()
        history = db.scalars(
            select(Message)
            .where(Message.user_id == item.user_id, Message.generation == item.generation)
            .order_by(Message.created_at, Message.id)
        ).all()
        boundary = next(
            (i for i, x in enumerate(history) if x.id == item.input.get("message_id")), len(history) - 1
        )
        # Assistant replies generated for previous jobs can be later than queued user messages.
        target = history[boundary] if item.kind == "assistant" and history else None
        relevant = [
            x for i, x in enumerate(history) if (i < boundary or x.role == "assistant") and x is not target
        ]
        if target:
            relevant.append(target)
        event(db, item.user_id, "job", job_output(item))
        return {
            "job_id": item.id,
            "claim_token": item.claim_token,
            "user_id": item.user_id,
            "timezone": item.input.get("timezone", "Europe/Moscow"),
            "generation": current.generation,
            "messages": [message_output(x) for x in relevant[-30:]],
            "pending": current.pending,
            "requested_at": item.created_at.replace(
                tzinfo=item.created_at.tzinfo or timezone.utc
            ).isoformat(),
            "audio_path": item.input.get("audio_path"),
        }


class Result(Strict):
    claim_token: str | None = None
    decision: dict | None = None
    text: str | None = Field(default=None, max_length=8000)
    error: str | None = Field(default=None, max_length=2000)


class Retry(Strict):
    claim_token: str


@app.post("/internal/jobs/{job_id}/retry", dependencies=[Depends(internal)])
def retry_job(job_id: str, body: Retry):
    with Session.begin() as db:
        item = db.get(Job, job_id)
        if not item:
            raise HTTPException(404, "Job not found")
        chat(db, item.user_id)
        db.refresh(item)
        if item.status in TERMINAL:
            return job_output(item)
        if item.status != "running" or item.claim_token != body.claim_token:
            raise HTTPException(409, "Stale job lease")
        item.status, item.claim_token, item.lease_until = "queued", None, None
        event(db, item.user_id, "job", job_output(item))
        return job_output(item)


@app.post("/internal/jobs/{job_id}/result", dependencies=[Depends(internal)])
def result(job_id: str, body: Result):
    with Session.begin() as db:
        item = db.get(Job, job_id)
        if not item:
            raise HTTPException(404, "Job not found")
        current = chat(db, item.user_id)
        db.refresh(item)
        if item.status in TERMINAL:
            return job_output(item)
        if item.status != "running" or not body.claim_token or body.claim_token != item.claim_token:
            raise HTTPException(409, "Stale job lease")
        if item.generation != current.generation:
            item.status = "cancelled"
        elif body.error:
            fail_with_message(db, current, item, body.error)
        elif item.kind == "transcription":
            if not body.text or not body.text.strip():
                fail_with_message(
                    db, current, item, "Речь не найдена. Попробуйте записать сообщение ещё раз."
                )
            else:
                text = body.text.strip()
                assistant = enqueue_assistant(
                    db,
                    current,
                    {"id": item.user_id, "timezone": item.input.get("timezone", "Europe/Moscow")},
                    text,
                    uid(),
                )
                item.status, item.result = (
                    "succeeded",
                    {
                        "message_id": assistant.input["message_id"],
                        "assistant_job_id": assistant.id,
                    },
                )
        else:
            try:
                decision = Decision.model_validate(body.decision)
                if decision.type == "query":
                    raise HTTPException(422, "Unresolved model query")
                with db.begin_nested():
                    changes = []
                    cards = []
                    seen_refs = set()
                    for ref in decision.record_refs:
                        key = (ref.id, ref.occurrence_at)
                        if key not in seen_refs:
                            cards.append(
                                record_card(
                                    "found",
                                    resolve_record(
                                        db,
                                        item.user_id,
                                        ref.id,
                                        version=ref.version,
                                        occurrence_at=ref.occurrence_at,
                                    ),
                                )
                            )
                            seen_refs.add(key)
                    for command in decision.commands:
                        deleted_snapshot = (
                            resolve_record(
                                db,
                                item.user_id,
                                command.id,
                                version=command.version,
                                occurrence_at=command.occurrence_at
                                if command.scope == "occurrence"
                                else None,
                            )
                            if command.operation == "delete"
                            else None
                        )
                        fields = command.fields.model_dump(mode="json", exclude_unset=True, exclude_none=True)
                        fields.update({key: None for key in getattr(command, "clear_fields", [])})
                        changes.append(
                            change(
                                db,
                                item.user_id,
                                command.operation,
                                fields,
                                command.id,
                                command.version,
                                command.scope,
                                command.occurrence_at,
                                item.input.get("timezone", "Europe/Moscow"),
                            )
                        )
                        snapshot = changes[-1]
                        if deleted_snapshot:
                            snapshot = {**deleted_snapshot, "version": snapshot["version"], "conflicts": []}
                        cards.append(record_card(ACTIONS[command.operation], snapshot))
                    cards = visible_cards(cards)
                    current.pending = (
                        {"question": decision.question, "context": decision.context}
                        if decision.type == "clarify"
                        else None
                    )
                    # Mutation confirmations derive from committed values, not model's claimed result.
                    answer = (
                        decision.question
                        if decision.type == "clarify"
                        else (confirmation(cards) if changes else decision.answer)
                    )
                    if any(x.get("conflicts") for x in changes):
                        answer += ". Есть пересечение с другой встречей."
                    message = add_message(db, item.user_id, current.generation, "assistant", answer, cards)
                    item.status, item.result = (
                        "succeeded",
                        {
                            "answer": answer,
                            "pending": bool(current.pending),
                            "records": changes,
                            "cards": cards,
                            "message_id": message.id,
                        },
                    )
            except (ValidationError, HTTPException, ValueError) as exc:
                item.status, item.error = (
                    "failed",
                    (str(exc.detail) if isinstance(exc, HTTPException) else str(exc))[:2000],
                )
        event(db, item.user_id, "job", job_output(item))
        return job_output(item)


@app.post("/internal/jobs/{job_id}/exhausted", dependencies=[Depends(internal)])
def job_exhausted(job_id: str, body: Result):
    with Session.begin() as db:
        item = db.get(Job, job_id)
        if not item:
            return {"ok": True}
        chat(db, item.user_id)
        db.refresh(item)
        if item.status not in TERMINAL and (
            (item.status == "running" and body.claim_token != item.claim_token)
            or (body.claim_token and body.claim_token != item.claim_token)
        ):
            return {"ok": True, "superseded": True}
        if item.status not in TERMINAL:
            item.status, item.error = "failed", "Обработка не удалась после пяти попыток"
            event(db, item.user_id, "job", job_output(item))
        return {
            "ok": True,
            "audio_path": item.input.get("audio_path") if item.kind == "transcription" else None,
        }


def _get_setting(db, key, default=""):
    row = db.get(Setting, key)
    return row.value if row else default


def _set_setting(db, key, value):
    existing = db.get(Setting, key)
    if existing:
        existing.value = value
    else:
        db.add(Setting(key=key, value=value))


class SettingsInput(Strict):
    llm_provider: str


class ProvidersInput(Strict):
    profiles: list[dict]


@app.get("/api/v1/settings")
def get_settings(user=Depends(identity)):
    with Session() as db:
        providers = json.loads(_get_setting(db, "llm_providers", "[]"))
        provider = _get_setting(db, "llm_provider") or (providers[0]["name"] if providers else "")
        return {"llm_provider": provider, "llm_providers": providers}


@app.put("/api/v1/settings")
def update_settings(body: SettingsInput, user=Depends(identity)):
    with Session.begin() as db:
        providers = json.loads(_get_setting(db, "llm_providers", "[]"))
        available = [p["name"] for p in providers]
        if available and body.llm_provider not in available:
            raise HTTPException(422, "Unknown provider")
        _set_setting(db, "llm_provider", body.llm_provider)
        return {"llm_provider": body.llm_provider}


@app.get("/internal/settings", dependencies=[Depends(internal)])
def internal_get_settings():
    with Session() as db:
        providers = json.loads(_get_setting(db, "llm_providers", "[]"))
        provider = _get_setting(db, "llm_provider") or (providers[0]["name"] if providers else "")
        return {"llm_provider": provider}


@app.put("/internal/settings/providers", dependencies=[Depends(internal)])
def register_providers(body: ProvidersInput):
    with Session.begin() as db:
        _set_setting(db, "llm_providers", json.dumps(body.profiles))
        if not _get_setting(db, "llm_provider") and body.profiles:
            _set_setting(db, "llm_provider", body.profiles[0]["name"])
    return {"ok": True}


@app.get("/internal/audio/references", dependencies=[Depends(internal)])
def audio_references():
    with Session() as db:
        return {
            "items": [
                x.input["audio_path"]
                for x in db.scalars(
                    select(Job).where(Job.kind == "transcription", Job.status.not_in(TERMINAL))
                )
                if x.input.get("audio_path")
            ]
        }


@app.websocket("/api/v1/events")
async def websocket(socket: WebSocket):
    try:
        user = await asyncio.to_thread(validate_session, socket.cookies.get("golos_session"))
    except HTTPException:
        await socket.close(code=4401)
        return
    with Session() as db:
        last = db.scalar(
            select(Event)
            .where(Event.user_id == user["id"])
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        anchor = (last.created_at, last.id) if last else None
    await socket.accept()
    checked = now()
    try:
        while True:
            if (now() - checked).total_seconds() > 30:
                await asyncio.to_thread(validate_session, socket.cookies.get("golos_session"))
                checked = now()

            def batch():
                with Session() as db:
                    statement = select(Event).where(Event.user_id == user["id"])
                    if anchor:
                        statement = statement.where(
                            (Event.created_at > anchor[0])
                            | ((Event.created_at == anchor[0]) & (Event.id > anchor[1]))
                        )
                    return db.scalars(statement.order_by(Event.created_at, Event.id).limit(100)).all()

            for item in await asyncio.to_thread(batch):
                await socket.send_json({"id": item.id, "type": item.type, "data": item.data})
                anchor = (item.created_at, item.id)
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, HTTPException, RuntimeError):
        await socket.close()
