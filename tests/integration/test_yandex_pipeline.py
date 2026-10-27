"""Domain/outbox → real worker → CalDAV boundary → core receipt → durable UI status."""

import asyncio
from unittest.mock import patch

import httpx
from backend.chat import main as core
from backend.chat import yandex
from backend.chat.domain import change
from backend.chat.models import Chat, Event, Outbox, YandexAccount
from backend.common.runtime import Base
from backend.yandex_calendar import service, worker
from backend.yandex_calendar.models import YandexConnection
from cryptography.fernet import Fernet
from icalendar import Calendar
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def test_create_move_delete_with_durable_status_and_duplicate_delivery(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine, tables=[table for table in Base.metadata.sorted_tables if table.name != "record_embeddings"]
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(yandex, "Session", sessions)
    monkeypatch.setattr(service, "Session", sessions)
    monkeypatch.setenv("YANDEX_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_TOKEN", "internal-test")
    remote, writes = {}, []
    calendar_url = "https://caldav.yandex.ru/calendars/test/"

    def dav(request):
        href = str(request.url)
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                text="""<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/calendars/test/</d:href><d:propstat><d:prop><d:resourcetype><c:calendar/></d:resourcetype><d:current-user-privilege-set><d:privilege><d:all/></d:privilege></d:current-user-privilege-set><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>""",
            )
        if request.method == "GET":
            if href not in remote:
                return httpx.Response(404)
            return httpx.Response(200, text=remote[href][0], headers={"ETag": remote[href][1]})
        if request.method == "REPORT":
            return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:"/>')
        if href in remote:
            assert request.headers.get("if-match") == remote[href][1], "unconditional remote mutation"
        else:
            assert request.headers.get("if-none-match") == "*", "unconditional remote create"
        writes.append(request.method)
        if request.method == "DELETE":
            del remote[href]
            return httpx.Response(204)
        assert request.method == "PUT"
        remote[href] = (request.content.decode(), '"etag-' + str(len(writes)) + '"')
        return httpx.Response(201, headers={"ETag": remote[href][1]})

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(dav))
    )
    with sessions.begin() as db:
        db.add(Chat(id="user", generation=0))
        db.add(YandexAccount(user_id="user", generation="generation", status="connected"))
        db.add(
            YandexConnection(
                user_id="user",
                generation="generation",
                status="connected",
                calendar_id=calendar_url,
                calendar_name="Test",
                credentials=service.encrypt({"username": "fake", "password": "fake"}),
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
                    select(Outbox).where(Outbox.queue == "yandex_calendar").order_by(Outbox.created_at)
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
        first = pending()
        asyncio.run(deliver(first))
        asyncio.run(deliver(first))
        assert writes == ["PUT"]
        assert (
            str(Calendar.from_ical(next(iter(remote.values()))[0]).walk("VEVENT")[0]["DESCRIPTION"])
            == "Договор"
        )
        href, (content, _) = next(iter(remote.items()))
        drifted = Calendar.from_ical(content)
        drifted.walk("VEVENT")[0]["SUMMARY"] = "External title"
        remote[href] = (drifted.to_ical().decode(), '"external"')
        assert yandex.reconcile_all("user") == {"queued": 1}
        asyncio.run(deliver(pending()))
        assert str(Calendar.from_ical(remote[href][0]).walk("VEVENT")[0]["SUMMARY"]) == "Юрист"
        remote.clear()
        assert yandex.reconcile_all("user") == {"queued": 1}
        asyncio.run(deliver(pending()))
        assert len(remote) == 1
        with sessions.begin() as db:
            change(db, "user", "update", {"title": "Перенесено"}, record["id"], 1)
        asyncio.run(deliver(pending()))
        assert (
            str(Calendar.from_ical(next(iter(remote.values()))[0]).walk("VEVENT")[0]["SUMMARY"])
            == "Перенесено"
        )
        with sessions.begin() as db:
            change(db, "user", "delete", {}, record["id"], 2)
        asyncio.run(deliver(pending()))
        assert not remote
        state = yandex.list_sync("user")["items"][0]
        assert state["status"] == "synced" and state["synced_version"] == 5
        with sessions() as db:
            latest = db.scalars(
                select(Event).where(Event.type == "yandex_calendar_sync").order_by(Event.created_at)
            ).all()[-1]
            assert latest.data["status"] == "synced" and latest.user_id == "user"
    finally:
        engine.dispose()
