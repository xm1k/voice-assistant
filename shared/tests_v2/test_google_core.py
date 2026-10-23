"""Google delivery is transactional, versioned and isolated from local writes."""

from unittest.mock import patch

import pytest
from backend.chat import google
from backend.chat import main as core
from backend.chat.domain import change
from backend.chat.models import Chat, Event, GoogleAccount, GoogleSync, GoogleSyncOperation, Outbox
from backend.common.runtime import Base
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "record_embeddings"]
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        db.add_all([Chat(id="user", generation=0), Chat(id="other", generation=0)])
    with patch.object(google, "Session", sessions):
        yield sessions
    engine.dispose()


def meeting():
    return {
        "kind": "meeting",
        "title": "Юрист",
        "start_at": "2027-01-12T10:00:00+03:00",
        "end_at": "2027-01-12T11:00:00+03:00",
        "timezone": "Europe/Moscow",
    }


def connect(db):
    db.add(GoogleAccount(user_id="user", generation="connection-1", status="connected"))
    db.flush()


def test_only_new_meetings_after_connection_are_exported(database):
    with database.begin() as db:
        old = change(db, "user", "create", meeting())
        connect(db)
        change(db, "user", "update", {"title": "Старая"}, old["id"], 1)
        change(db, "user", "create", {"kind": "task", "title": "Задача"})
        change(db, "other", "create", meeting())
        new = change(db, "user", "create", meeting())
    with database() as db:
        operations = db.scalars(select(GoogleSyncOperation)).all()
        assert len(operations) == 1
        assert operations[0].record_id == new["id"]
        messages = db.scalars(select(Outbox).where(Outbox.queue == "google_calendar")).all()
        assert [m.payload for m in messages] == [{"operation_id": operations[0].id}]


def test_rollback_discards_google_work(database):
    with database.begin() as db:
        connect(db)
    with pytest.raises(RuntimeError), database.begin() as db:
        change(db, "user", "create", meeting())
        raise RuntimeError("rollback")
    with database() as db:
        assert not db.scalars(select(GoogleSyncOperation)).all()
        assert not db.scalars(select(GoogleSync)).all()


def test_operations_keep_snapshots_and_wait_for_predecessor(database):
    with database.begin() as db:
        connect(db)
        record = change(db, "user", "create", meeting())
        change(db, "user", "update", {"title": "Перенесено"}, record["id"], 1)
    with database() as db:
        ops = db.scalars(select(GoogleSyncOperation).order_by(GoogleSyncOperation.version)).all()
        assert ops[0].payload["snapshot"]["title"] == "Юрист"
        assert ops[1].payload["snapshot"]["title"] == "Перенесено"
        assert ops[1].predecessor_id == ops[0].id
    with pytest.raises(HTTPException) as error:
        google.get_operation(ops[1].id)
    assert error.value.status_code == 409
    google.save_result(ops[0].id, google.SyncResult(status="synced", generation="connection-1", version=1))
    assert google.get_operation(ops[1].id)["version"] == 2
    assert google.list_sync("user")["items"][0]["status"] == "pending"


def test_conflict_blocks_later_google_changes_but_preserves_local_record(database):
    with database.begin() as db:
        connect(db)
        record = change(db, "user", "create", meeting())
        op = db.scalar(select(GoogleSyncOperation))
        operation_id = op.id
    google.save_result(
        operation_id,
        google.SyncResult(status="conflict", code="external_change", generation="connection-1", version=1),
    )
    with database.begin() as db:
        updated = change(db, "user", "update", {"title": "Локальное"}, record["id"], 1)
        assert updated["title"] == "Локальное"
    assert google.list_sync("user")["items"][0]["status"] == "conflict"
    with database() as db:
        assert len(db.scalars(select(GoogleSyncOperation)).all()) == 1
    assert google.list_sync("other")["items"] == []


def test_deleted_meetings_keep_visible_status_and_duplicate_callbacks_are_safe(database):
    with database.begin() as db:
        connect(db)
        record = change(db, "user", "create", meeting())
        change(db, "user", "delete", {}, record["id"], 1)
    with database() as db:
        ops = db.scalars(select(GoogleSyncOperation).order_by(GoogleSyncOperation.version)).all()
    result = google.SyncResult(status="synced", generation="connection-1", version=1)
    google.save_result(ops[0].id, result)
    google.save_result(ops[0].id, result)
    google.save_result(ops[1].id, google.SyncResult(status="missing", generation="connection-1", version=2))
    item = google.list_sync("user")["items"][0]
    assert item["title"] == "Юрист" and item["status"] == "missing"


def test_wrong_generation_and_retry_ownership_are_rejected(database):
    with database.begin() as db:
        connect(db)
        change(db, "user", "create", meeting())
        operation_id = db.scalar(select(GoogleSyncOperation)).id
    with pytest.raises(HTTPException):
        google.save_result(operation_id, google.SyncResult(status="synced", generation="wrong", version=1))
    google.save_result(
        operation_id,
        google.SyncResult(status="failed", code="delivery_exhausted", generation="connection-1", version=1),
    )
    with pytest.raises(HTTPException) as error:
        google.retry_operation(operation_id, "other")
    assert error.value.status_code == 404
    google.retry_operation(operation_id, "user")
    assert google.list_sync("user")["items"][0]["status"] == "pending"


def test_public_sync_route_requires_identity_and_is_user_scoped(database):
    import asyncio

    import httpx
    from backend.common.security import identity

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=core.app), base_url="http://test"
        ) as client:
            unauthenticated = await client.get("/api/v1/integrations/google-calendar/sync")
            assert unauthenticated.status_code == 401
            core.app.dependency_overrides[identity] = lambda: {"id": "other"}
            try:
                response = await client.get("/api/v1/integrations/google-calendar/sync")
                assert response.status_code == 200
                assert response.json() == {"items": [], "next_cursor": None}
            finally:
                core.app.dependency_overrides.clear()

    asyncio.run(run())


def test_failed_predecessor_is_retryable_even_after_new_local_changes(database):
    with database.begin() as db:
        connect(db)
        record = change(db, "user", "create", meeting())
        first_id = db.scalar(select(GoogleSyncOperation)).id
        change(db, "user", "update", {"title": "Новая версия"}, record["id"], 1)
    google.save_result(
        first_id,
        google.SyncResult(status="failed", code="delivery_exhausted", generation="connection-1", version=1),
    )
    item = google.list_sync("user")["items"][0]
    assert item["operation_id"] == first_id and item["status"] == "failed"
    google.retry_operation(first_id, "user")
    assert google.get_operation(first_id)["version"] == 1


def test_disconnect_fences_callbacks_and_never_reexports_existing_meetings(database):
    with database.begin() as db:
        connect(db)
        record = change(db, "user", "create", meeting())
        operation_id = db.scalar(select(GoogleSyncOperation)).id
        google.disconnect(db, "user", "connection-2")
        google.mirror(db, "user", {"generation": "connection-3", "status": "connected"})
    google.save_result(operation_id, google.SyncResult(status="synced", generation="connection-1", version=1))
    with database.begin() as db:
        change(db, "user", "update", {"title": "Локальное"}, record["id"], 1)
    item = google.list_sync("user")["items"][0]
    assert item["status"] == "disconnected" and item["synced_version"] == 0
    assert google.get_operation(operation_id) is None


def test_occurrence_snapshot_keeps_original_identity_and_sparse_override(database):
    with database.begin() as db:
        connect(db)
        record = change(db, "user", "create", {**meeting(), "recurrence": {"frequency": "daily", "count": 3}})
        change(
            db,
            "user",
            "update",
            {"title": "Исключение"},
            record["id"],
            1,
            "occurrence",
            "2027-01-13T07:00:00Z",
        )
    with database() as db:
        op = db.scalar(select(GoogleSyncOperation).where(GoogleSyncOperation.version == 2))
        assert op.payload["occurrence_at"] == "2027-01-13T07:00:00+00:00"
        assert op.payload["exceptions"] == [
            {"occurrence_at": "2027-01-13T07:00:00+00:00", "data": {"title": "Исключение"}}
        ]


def test_stale_status_response_cannot_undo_concurrent_disconnect(database):
    with database.begin() as db:
        connect(db)

    def remote(*args, **kwargs):
        with database.begin() as db:
            google.disconnect(db, "user", "connection-2")
        return {"configured": True, "status": "connected", "generation": "connection-1"}

    with patch.object(google, "remote", remote):
        result = google.connection_status(user={"id": "user"})
    assert result["status"] == "disconnected" and result["generation"] == "connection-2"


def test_connection_changes_notify_other_tabs_without_meetings(database):
    with database.begin() as db:
        google.mirror(db, "user", {"generation": "first", "status": "connected"})
        google.disconnect(db, "user", "second")
    with database() as db:
        events = db.scalars(
            select(Event).where(Event.type == "google_calendar_sync").order_by(Event.created_at)
        ).all()
        assert [item.data["connection"]["status"] for item in events] == ["connected", "disconnected"]
        assert all(item.user_id == "user" for item in events)


def test_dead_letter_reports_technical_failure_to_core(database, monkeypatch):
    import asyncio

    import httpx
    from backend.chat import broker

    with database.begin() as db:
        connect(db)
        change(db, "user", "create", meeting())
        operation_id = db.scalar(select(GoogleSyncOperation)).id
    monkeypatch.setenv("INTERNAL_TOKEN", "test-token")

    async def deliver():
        network = httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app))
        with patch.object(broker.httpx, "AsyncClient", return_value=network):
            await broker.exhausted({"operation_id": operation_id}, "google_calendar")

    asyncio.run(deliver())
    item = google.list_sync("user")["items"][0]
    assert item["status"] == "failed" and item["code"] == "delivery_exhausted"
    assert "повторить" in item["error"]
