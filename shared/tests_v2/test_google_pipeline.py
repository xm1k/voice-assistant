"""Exercise real domain → worker → Google boundary → core callback → durable UI state."""

import asyncio
import json
from unittest.mock import patch

import httpx
from backend.chat import google
from backend.chat import main as core
from backend.chat.domain import change
from backend.chat.models import Chat, Event, GoogleAccount, Outbox
from backend.common.runtime import Base
from backend.google_calendar import service, worker
from backend.google_calendar.models import GoogleConnection
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def test_create_move_delete_with_durable_status_and_duplicate_delivery(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "record_embeddings"]
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(google, "Session", sessions)
    monkeypatch.setattr(service, "Session", sessions)
    monkeypatch.setenv("GOOGLE_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_TOKEN", "internal-test")
    remote, writes = {}, []

    def google_api(request):
        event_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and "/events/" not in request.url.path:
            return httpx.Response(200, json={"id": "calendar"})
        if request.method == "GET":
            return httpx.Response(200, json=remote[event_id]) if event_id in remote else httpx.Response(404)
        body = json.loads(request.content) if request.content else None
        if request.method == "POST":
            event_id = body["id"]
            assert event_id not in remote, "duplicate external create"
            remote[event_id] = body
        else:
            assert request.headers["if-match"] == remote[event_id]["etag"], "unconditional external write"
            if request.method == "DELETE":
                del remote[event_id]
                writes.append("DELETE")
                return httpx.Response(204)
            if request.method == "PATCH":
                remote[event_id].update(body)
            else:
                remote[event_id] = {**body, "id": event_id}
        writes.append(request.method)
        remote[event_id]["etag"] = '"etag-' + str(len(writes)) + '"'
        return httpx.Response(200, json=remote[event_id])

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(google_api)))
    with sessions.begin() as db:
        db.add(Chat(id="user", generation=0))
        db.add(GoogleAccount(user_id="user", generation="generation", status="connected"))
        db.add(
            GoogleConnection(
                user_id="user",
                generation="generation",
                status="connected",
                calendar_id="calendar",
                tokens=service.encrypt({"access_token": "fake-token"}),
            )
        )

    async def deliver(operation_id):
        network = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=core.app), headers={"X-Internal-Token": "internal-test"}
        )
        with patch.object(worker.httpx, "AsyncClient", return_value=network):
            await worker.handle({"operation_id": operation_id})

    def pending():
        with sessions() as db:
            return (
                db.scalars(
                    select(Outbox).where(Outbox.queue == "google_calendar").order_by(Outbox.created_at)
                )
                .all()[-1]
                .payload["operation_id"]
            )

    try:
        with sessions.begin() as db:
            record = change(
                db,
                "user",
                "create",
                {
                    "kind": "meeting",
                    "title": "Юрист",
                    "notes": "Договор",
                    "start_at": "2027-01-12T10:00:00+03:00",
                    "end_at": "2027-01-12T11:00:00+03:00",
                },
            )
        operation_id = pending()
        asyncio.run(deliver(operation_id))
        asyncio.run(deliver(operation_id))
        assert writes == ["POST"]
        assert next(iter(remote.values()))["description"] == "Договор"
        assert google.list_sync("user")["items"][0]["synced_version"] == 1
        with sessions.begin() as db:
            change(
                db,
                "user",
                "update",
                {"title": "Перенесено", "start_at": "2027-01-12T10:30:00+03:00"},
                record["id"],
                1,
            )
        asyncio.run(deliver(pending()))
        assert next(iter(remote.values()))["summary"] == "Перенесено"
        with sessions.begin() as db:
            change(db, "user", "delete", {}, record["id"], 2)
        asyncio.run(deliver(pending()))
        assert not remote
        state = google.list_sync("user")["items"][0]
        assert state["status"] == "synced" and state["synced_version"] == 3
        with sessions() as db:
            latest = db.scalars(
                select(Event).where(Event.type == "google_calendar_sync").order_by(Event.created_at)
            ).all()[-1]
            assert latest.data["status"] == "synced" and latest.user_id == "user"
    finally:
        engine.dispose()
