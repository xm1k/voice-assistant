from pathlib import Path

import backend.google_calendar
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

backend.google_calendar.__path__.append(
    str(Path(__file__).resolve().parents[2] / "services/google-calendar/app/backend/google_calendar")
)


def test_event_payload_preserves_local_recurrence():
    from backend.google_calendar.service import event_payload

    body = event_payload(
        {
            "title": "Meeting",
            "start_at": "2026-03-23T10:00:00+01:00",
            "end_at": "2026-03-23T11:00:00+01:00",
            "timezone": "Europe/Berlin",
            "recurrence": {"frequency": "weekly", "weekdays": [0, 2], "count": 5},
        },
        "op",
    )
    assert body["start"]["timeZone"] == "Europe/Berlin"
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;INTERVAL=1;COUNT=5;BYDAY=MO,WE"]
    assert body["extendedProperties"]["private"]["voiceOperation"] == "op"


def test_payload_notes_and_reminder():
    from backend.google_calendar.service import event_payload

    result = event_payload(
        {"title": "Call", "notes": "Agenda", "due_date": "2026-10-23", "reminder_minutes": 15}, "op"
    )
    assert result["description"] == "Agenda"
    assert result["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]}


def test_occurrence_payload_uses_original_instance_and_sparse_override():
    from backend.google_calendar.service import operation_payload

    operation = op(
        snapshot={
            "title": "Meeting",
            "start_at": "2026-10-23T10:00:00+00:00",
            "end_at": "2026-10-23T11:00:00+00:00",
            "recurrence": {"frequency": "daily"},
        },
        scope="occurrence",
        occurrence_at="2026-10-24T10:00:00+00:00",
        exceptions=[{"occurrence_at": "2026-10-24T10:00:00+00:00", "data": {"title": "Changed"}}],
    )
    body = operation_payload(operation, operation["occurrence_at"])
    assert body["summary"] == "Changed"
    assert body["start"]["dateTime"].startswith("2026-10-24")
    assert body["end"]["dateTime"].startswith("2026-10-24")
    assert "recurrence" not in body


def test_recurring_identity_follows_local_clock_across_dst():
    from backend.google_calendar.service import operation_payload, stamp_in_rule

    data = {
        "kind": "meeting",
        "title": "Berlin",
        "timezone": "Europe/Berlin",
        "start_at": "2026-10-23T10:00:00+02:00",
        "end_at": "2026-10-23T11:00:00+02:00",
        "recurrence": {"frequency": "daily", "count": 5},
    }
    assert stamp_in_rule(data, "2026-10-26T09:00:00+00:00") is True
    assert stamp_in_rule(data, "2026-10-26T08:00:00+00:00") is False
    payload = operation_payload(op(snapshot=data), "2026-10-26T09:00:00+00:00")
    assert payload["start"] == {"dateTime": "2026-10-26T10:00:00+01:00", "timeZone": "Europe/Berlin"}
    assert payload["end"]["dateTime"] == "2026-10-26T10:00:00+00:00"


@pytest.fixture
def google_service(monkeypatch, tmp_path):
    from backend.common.runtime import Base
    from backend.google_calendar import service
    from backend.google_calendar.models import GoogleConnection
    from cryptography.fernet import Fernet

    engine = create_engine("sqlite:///" + str(tmp_path / "google.sqlite"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "Session", factory)
    monkeypatch.setenv("GOOGLE_TOKEN_KEY", Fernet.generate_key().decode())
    with factory.begin() as db:
        db.add(
            GoogleConnection(
                user_id="u",
                generation="gen1",
                status="connected",
                calendar_id="cal",
                tokens=service.encrypt({"access_token": "secret"}),
            )
        )
    return service, factory


def op(**updates):
    return {
        "id": "op1",
        "user_id": "u",
        "generation": "gen1",
        "record_id": "r",
        "version": 1,
        "operation": "create",
        "scope": "series",
        "snapshot": {"title": "Meeting", "due_date": "2026-10-23"},
        **updates,
    }


def test_unreadable_token_requests_reconnection_instead_of_retrying_google(google_service, monkeypatch):
    from backend.google_calendar.models import GoogleConnection
    from cryptography.fernet import Fernet

    service, factory = google_service
    monkeypatch.setenv("GOOGLE_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        service,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("invalid local credentials must not reach Google")
            )
        ),
    )
    result = service.execute(op())
    assert result["status"] == "disconnected" and result["code"] == "reconnect_required"
    with factory() as db:
        assert db.get(GoogleConnection, "u").status == "reconnect_required"


def test_cancel_then_restore_instance_keeps_returned_google_etag(google_service, monkeypatch):
    import json

    from backend.google_calendar.models import GoogleMapping

    service, factory = google_service
    stamp = "2026-10-24T10:00:00+00:00"
    snapshot = {
        "title": "Meeting",
        "timezone": "UTC",
        "start_at": "2026-10-23T10:00:00+00:00",
        "end_at": "2026-10-23T11:00:00+00:00",
        "recurrence": {"frequency": "daily"},
    }
    instance = {
        **service.operation_payload(op(id="previous", snapshot=snapshot), stamp),
        "id": "child",
        "etag": "old",
    }
    with factory.begin() as db:
        db.add(
            GoogleMapping(
                key="u:gen1:r", user_id="u", generation="gen1", event_id="parent", etag="parent-etag"
            )
        )
        db.add(
            GoogleMapping(
                key="u:gen1:r:" + stamp, user_id="u", generation="gen1", event_id="child", etag="old"
            )
        )

    def api(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": "parent", "etag": "parent-etag"}
                if request.url.path.endswith("/parent")
                else instance,
            )
        assert request.method == "PATCH", "cancelled recurring instances must retain their new ETag"
        assert request.headers["if-match"] == instance["etag"]
        instance.update(json.loads(request.content))
        instance["etag"] = instance["etag"] + "-changed"
        return httpx.Response(200, json=instance)

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(api)))
    deleted = op(
        operation="delete",
        scope="occurrence",
        occurrence_at=stamp,
        snapshot=snapshot,
        exceptions=[{"occurrence_at": stamp, "data": {"deleted": True}}],
    )
    assert service.execute(deleted)["status"] == "synced"
    assert instance["status"] == "cancelled"
    restored = op(
        id="restore",
        operation="update",
        version=2,
        scope="occurrence",
        occurrence_at=stamp,
        snapshot=snapshot,
        exceptions=[{"occurrence_at": stamp, "data": {"title": "Restored"}}],
    )
    assert service.execute(restored)["status"] == "synced"
    assert instance["status"] == "confirmed" and instance["id"] == "child"


def test_create_timeout_reconciles_marker(google_service, monkeypatch):
    service, factory = google_service
    remote = {}
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=remote) if remote else httpx.Response(404)
        body = __import__("json").loads(request.content)
        remote.update(body, etag="etag1")
        writes.append(body)
        raise httpx.ReadTimeout("lost response")

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(service.RetryableGoogleError):
        service.execute(op())
    assert service.execute(op())["status"] == "synced"
    assert service.execute(op())["status"] == "synced"
    assert len(writes) == 1
    with factory() as db:
        from backend.google_calendar.models import GoogleConnection

        assert "secret" not in db.get(GoogleConnection, "u").tokens


def test_external_edit_blocks_updates(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(GoogleMapping(key="u:gen1:r", user_id="u", generation="gen1", event_id="event", etag="old"))
    methods = []

    def handle(request):
        methods.append(request.method)
        return httpx.Response(200, json={"id": "event", "etag": "external"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    assert service.execute(op(operation="update"))["status"] == "conflict"
    assert service.execute(op(id="op2", operation="update"))["status"] == "conflict"
    assert methods == ["GET"]


def test_first_delete_missing_is_external(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(GoogleMapping(key="u:gen1:r", user_id="u", generation="gen1", event_id="event", etag="old"))
    monkeypatch.setattr(
        service,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda r: (
                    httpx.Response(200, json={"id": "cal"})
                    if r.url.path.endswith("/calendars/cal")
                    else httpx.Response(404)
                )
            )
        ),
    )
    assert service.execute(op(operation="delete"))["status"] == "missing"


def test_stale_generation_never_calls_google(google_service, monkeypatch):
    service, _ = google_service
    monkeypatch.setattr(service, "client", lambda: pytest.fail("network for stale generation"))
    assert service.execute(op(generation=0))["status"] == "disconnected"


def test_series_detects_external_exception(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(GoogleMapping(key="u:gen1:r", user_id="u", generation="gen1", event_id="event", etag="old"))

    def handle(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "external",
                            "recurringEventId": "event",
                            "originalStartTime": {"date": "2026-10-24"},
                            "etag": "external",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"id": "event", "etag": "old"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    assert (
        service.execute(
            op(
                operation="update",
                snapshot={"title": "Meeting", "due_date": "2026-10-23", "recurrence": {"frequency": "daily"}},
            )
        )["status"]
        == "conflict"
    )


@pytest.mark.parametrize("refresh_token", ["refresh", None])
def test_oauth_state_is_bound_single_use_and_tokens_encrypted(google_service, monkeypatch, refresh_token):
    from urllib.parse import parse_qs, urlparse

    from backend.google_calendar import main
    from backend.google_calendar.models import GoogleConnection
    from fastapi.testclient import TestClient

    service, factory = google_service
    monkeypatch.setattr(main, "Session", factory)
    for key, value in {
        "INTERNAL_TOKEN": "internal",
        "GOOGLE_CLIENT_ID": "client",
        "GOOGLE_CLIENT_SECRET": "secret",
        "GOOGLE_REDIRECT_URI": "http://localhost/callback",
    }.items():
        monkeypatch.setenv(key, value)

    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "private", "refresh_token": refresh_token, "scope": service.SCOPE}
            )
        return httpx.Response(200, json={"id": "new-calendar"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    client = TestClient(main.app)
    assert client.get("/internal/status", params={"user_id": "u"}).status_code == 401
    headers = {"X-Internal-Token": "internal"}
    url = client.post(
        "/internal/oauth/start", headers=headers, json={"user_id": "u", "session_hash": "session"}
    ).json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    data = {"user_id": "u", "session_hash": "wrong", "state": state, "code": "code"}
    assert client.post("/internal/oauth/callback", headers=headers, json=data).status_code == 400
    data["session_hash"] = "session"
    result = client.post("/internal/oauth/callback", headers=headers, json=data)
    if not refresh_token:
        assert result.status_code == 400
        with factory() as db:
            assert db.get(GoogleConnection, "u").calendar_id == "cal"
        return
    assert result.status_code == 200
    assert result.json()["status"] == "connected"
    assert client.post("/internal/oauth/callback", headers=headers, json=data).status_code == 400
    with factory() as db:
        conn = db.get(GoogleConnection, "u")
        assert conn.calendar_id == "new-calendar"
        assert "private" not in conn.tokens and "refresh" not in conn.tokens
    disconnected = client.post("/internal/disconnect", headers=headers, json={"user_id": "u"}).json()
    assert disconnected["generation"] != result.json()["generation"]


def test_delete_timeout_is_reconciled_without_second_delete(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(GoogleMapping(key="u:gen1:r", user_id="u", generation="gen1", event_id="event", etag="old"))
    remote = {"exists": True, "deletes": 0}

    def handle(request):
        if request.method == "GET":
            if request.url.path.endswith("/calendars/cal"):
                return httpx.Response(200, json={"id": "cal"})
            return (
                httpx.Response(200, json={"id": "event", "etag": "old"})
                if remote["exists"]
                else httpx.Response(404)
            )
        if request.method == "PATCH":
            assert request.headers["if-match"] == "old"
            return httpx.Response(200, json={"id": "event", "etag": "marked"})
        assert request.headers["if-match"] == "marked"
        remote.update(exists=False, deletes=remote["deletes"] + 1)
        raise httpx.ReadTimeout("lost deletion response")

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(service.RetryableGoogleError):
        service.execute(op(operation="delete"))
    assert service.execute(op(operation="delete"))["status"] == "synced"
    assert remote["deletes"] == 1


def test_resume_uses_new_google_id_after_owned_deletion(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(
            GoogleMapping(
                key="u:gen1:r",
                user_id="u",
                generation="gen1",
                event_id="old-event",
                etag="old",
                status="deleted",
            )
        )
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(404)
        body = __import__("json").loads(request.content)
        writes.append(body)
        return httpx.Response(200, json={**body, "etag": "new"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    assert service.execute(op(id="resume", operation="update"))["status"] == "synced"
    assert writes[0]["id"] != "old-event"
    with factory() as db:
        assert db.get(GoogleMapping, "u:gen1:r").status == "synced"


def test_marker_does_not_hide_later_manual_edit(google_service, monkeypatch):
    service, _ = google_service
    body = service.operation_payload(op())
    body.update(summary="Someone changed this", etag="external")
    monkeypatch.setattr(
        service,
        "client",
        lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))),
    )
    assert service.execute(op())["status"] == "conflict"


def test_missing_calendar_requires_reconnect(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(GoogleMapping(key="u:gen1:r", user_id="u", generation="gen1", event_id="event", etag="old"))
    monkeypatch.setattr(
        service, "client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    )
    result = service.execute(op(operation="update"))
    assert result["status"] == "disconnected"
    assert result["code"] == "calendar_unavailable"


def test_count_and_until_use_effective_bounded_count():
    from backend.google_calendar.service import event_payload

    payload = event_payload(
        {
            "title": "Daily",
            "start_at": "2026-03-27T10:00:00+01:00",
            "timezone": "Europe/Berlin",
            "recurrence": {"frequency": "daily", "count": 10, "until": "2026-03-30"},
        },
        "op",
    )
    assert payload["recurrence"] == ["RRULE:FREQ=DAILY;INTERVAL=1;COUNT=4"]
    with pytest.raises(ValueError, match="empty_recurrence"):
        event_payload(
            {
                "title": "Daily",
                "due_date": "2026-03-27",
                "recurrence": {"frequency": "daily", "count": 10, "until": "2026-03-26"},
            },
            "op",
        )


def test_payload_recovery_normalizes_empty_fields_but_detects_timezone_and_status():
    from backend.google_calendar.service import event_payload, payload_matches

    expected = event_payload(
        {"title": "Daily", "start_at": "2026-03-27T10:00:00+01:00", "timezone": "Europe/Berlin"}, "op"
    )
    current = __import__("copy").deepcopy(expected)
    current.pop("description")
    current["reminders"].pop("overrides")
    assert payload_matches(current, expected)
    current["start"]["timeZone"] = "Europe/Paris"
    assert not payload_matches(current, expected)
    current["start"]["timeZone"] = "Europe/Berlin"
    current["status"] = "cancelled"
    assert not payload_matches(current, expected)


def test_get_denied_requires_reconnect(google_service, monkeypatch):
    service, _ = google_service
    monkeypatch.setattr(
        service, "client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    )
    assert service.execute(op()) == {
        "status": "disconnected",
        "code": "reconnect_required",
        "generation": "gen1",
        "version": 1,
    }


def test_config_requires_valid_key_and_safe_redirect(google_service, monkeypatch):
    service, _ = google_service
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://example.com/callback")
    assert service.configured()
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://example.com/callback")
    assert not service.configured()
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost/callback")
    monkeypatch.setenv("GOOGLE_TOKEN_KEY", "invalid")
    assert not service.configured()


def test_retry_after_parses_http_date():
    from datetime import datetime, timezone

    from backend.google_calendar.service import retry_delay

    now = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
    assert retry_delay("Fri, 23 Oct 2026 10:01:00 GMT", now) == 60
    assert retry_delay("90", now) == 90
    assert retry_delay("nonsense", now) == 5


def test_rate_limit_403_retries_without_disconnect(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleConnection

    monkeypatch.setattr(
        service,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    403,
                    json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}},
                    headers={"Retry-After": "30"},
                )
            )
        ),
    )
    with pytest.raises(service.RetryableGoogleError) as raised:
        service.execute(op())
    assert raised.value.retry_after == 30
    with factory() as db:
        assert db.get(GoogleConnection, "u").status == "connected"


def test_occurrence_fences_external_parent_edit(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(
            GoogleMapping(
                key="u:gen1:r", user_id="u", generation="gen1", event_id="parent", etag="parent-old"
            )
        )
        db.add(
            GoogleMapping(
                key="u:gen1:r:2026-10-24",
                user_id="u",
                generation="gen1",
                event_id="instance",
                etag="instance-old",
            )
        )
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "parent", "etag": "external"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    assert (
        service.execute(op(operation="update", scope="occurrence", occurrence_at="2026-10-24"))["status"]
        == "conflict"
    )
    assert all(r.method == "GET" for r in requests)
    with factory() as db:
        assert db.get(GoogleMapping, "u:gen1:r").status == "conflict"


def test_cancelled_occurrence_resume_preserves_instance_identity(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(
            GoogleMapping(
                key="u:gen1:r", user_id="u", generation="gen1", event_id="parent", etag="parent-etag"
            )
        )
        db.add(
            GoogleMapping(
                key="u:gen1:r:2026-10-24",
                user_id="u",
                generation="gen1",
                event_id="instance",
                etag="instance-old",
                status="deleted",
            )
        )
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": "parent", "etag": "parent-etag"}
                if request.url.path.endswith("/parent")
                else {"id": "instance", "etag": "instance-old", "status": "cancelled"},
            )
        writes.append(request)
        return httpx.Response(200, json={"id": "instance", "etag": "restored"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    assert (
        service.execute(op(operation="update", scope="occurrence", occurrence_at="2026-10-24"))["status"]
        == "synced"
    )
    assert writes[0].method == "PATCH"
    assert writes[0].url.path.endswith("/instance")
    assert __import__("json").loads(writes[0].content)["status"] == "confirmed"


def test_unsupported_google_reminder_exports_with_local_warning(google_service, monkeypatch):
    service, _ = google_service
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(404)
        body = __import__("json").loads(request.content)
        writes.append(body)
        return httpx.Response(200, json={**body, "etag": "new"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    result = service.execute(
        op(snapshot={"title": "Meeting", "due_date": "2026-10-23", "reminder_minutes": 40321})
    )
    assert result["status"] == "synced"
    assert result["code"] == "reminder_local_only"
    assert writes[0]["reminders"]["overrides"] == []


@pytest.mark.parametrize("external", [False, True, "location"])
def test_inherited_etag_change_uses_durable_preflight(google_service, external):
    service, factory = google_service
    from backend.google_calendar.models import GoogleConnection, GoogleMapping, GoogleOperation

    operation = op(
        snapshot={"title": "New parent", "due_date": "2026-10-23", "recurrence": {"frequency": "daily"}},
        exceptions=[{"occurrence_at": "2026-10-24", "data": {"notes": "override"}}],
    )
    old_operation = {**operation, "snapshot": {**operation["snapshot"], "title": "Old parent"}}
    old_master = service.operation_payload(old_operation)
    baseline = {
        **service.operation_payload(old_operation, "2026-10-24"),
        "id": "instance",
        "etag": "old",
        "originalStartTime": {"date": "2026-10-24"},
    }
    inherited = {
        **service.operation_payload(operation, "2026-10-24"),
        "id": "instance",
        "etag": "inherited",
        "originalStartTime": {"date": "2026-10-24"},
    }
    inherited["extendedProperties"] = {}
    if external == "location":
        inherited["location"] = "Externally added"
    elif external:
        inherited["description"] = "External edit"
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"items": [inherited]})
        writes.append(request)
        return httpx.Response(200, json={"etag": "written"})

    with factory.begin() as db:
        db.add(
            GoogleMapping(
                key="u:gen1:r:2026-10-24", user_id="u", generation="gen1", event_id="instance", etag="old"
            )
        )
        db.add(
            GoogleOperation(
                id=operation["id"],
                intent={"preflight": {"master": old_master, "instances": {"2026-10-24": baseline}}},
            )
        )
    with factory.begin() as db, httpx.Client(transport=httpx.MockTransport(handle)) as c:
        status = service.apply_exceptions(
            db, c, db.get(GoogleConnection, "u"), "/calendars/cal/events", "u:gen1:r", "parent", operation
        )
    assert status == ("conflict" if external else "synced")
    assert bool(writes) == (not external)


def test_out_of_rule_exception_becomes_stable_detached_event(google_service):
    service, factory = google_service
    from backend.google_calendar.models import GoogleConnection, GoogleMapping

    operation = op(
        snapshot={"title": "Moved series", "due_date": "2026-11-01", "recurrence": {"frequency": "daily"}},
        exceptions=[{"occurrence_at": "2026-10-24", "data": {"notes": "keep me"}}],
    )
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(404)
        writes.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"etag": "new"})

    with factory.begin() as db, httpx.Client(transport=httpx.MockTransport(handle)) as c:
        assert (
            service.apply_exceptions(
                db, c, db.get(GoogleConnection, "u"), "/calendars/cal/events", "u:gen1:r", "parent", operation
            )
            == "synced"
        )
    assert writes[0]["start"] == {"date": "2026-10-24"}
    assert writes[0]["description"] == "keep me"
    assert "recurrence" not in writes[0]
    with factory() as db:
        assert db.get(GoogleMapping, "u:gen1:r:2026-10-24").detached


def test_series_timeout_recovers_inherited_etags_from_committed_baseline(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping, GoogleOperation

    operation = op(
        snapshot={"title": "New parent", "due_date": "2026-10-23", "recurrence": {"frequency": "daily"}},
        exceptions=[{"occurrence_at": "2026-10-24", "data": {"notes": "override"}}],
    )
    old = {**operation, "id": "previous", "snapshot": {**operation["snapshot"], "title": "Old parent"}}
    remote = {
        "parent": {**service.operation_payload(old), "id": "parent", "etag": "parent-old"},
        "instance": {
            **service.operation_payload(old, "2026-10-24"),
            "id": "instance",
            "etag": "instance-old",
            "recurringEventId": "parent",
            "originalStartTime": {"date": "2026-10-24"},
        },
    }
    writes = []
    with factory.begin() as db:
        db.add(
            GoogleMapping(
                key="u:gen1:r", user_id="u", generation="gen1", event_id="parent", etag="parent-old"
            )
        )
        db.add(
            GoogleMapping(
                key="u:gen1:r:2026-10-24",
                user_id="u",
                generation="gen1",
                event_id="instance",
                etag="instance-old",
            )
        )

    def handle(request):
        if request.method == "GET":
            if request.url.path.endswith("/events") or request.url.path.endswith("/instances"):
                return httpx.Response(200, json={"items": [remote["instance"]]})
            return httpx.Response(200, json=remote["parent"])
        writes.append(request)
        if request.method == "PUT":
            with factory() as db:
                assert (
                    db.get(GoogleOperation, operation["id"]).intent["preflight"]["instances"]["2026-10-24"][
                        "etag"
                    ]
                    == "instance-old"
                )
            remote["parent"] = {**service.operation_payload(operation), "id": "parent", "etag": "parent-new"}
            remote["instance"].update(summary="New parent", etag="instance-inherited")
            raise httpx.ReadTimeout("lost series response")
        return httpx.Response(200, json={"etag": "instance-written"})

    monkeypatch.setattr(service, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(service.RetryableGoogleError):
        service.execute(operation)
    assert service.execute(operation)["status"] == "synced"
    assert [request.method for request in writes] == ["PUT", "PATCH"]


def test_detached_exception_is_excluded_if_rule_later_covers_original_stamp():
    from backend.google_calendar.service import operation_payload

    operation = op(
        snapshot={"title": "Meeting", "due_date": "2026-10-23", "recurrence": {"frequency": "daily"}},
        _detached_stamps=["2026-10-24"],
    )
    assert "EXDATE;VALUE=DATE:20261024" in operation_payload(operation)["recurrence"]


def test_disconnect_invalidates_pending_oauth_states(google_service, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from backend.google_calendar import main
    from backend.google_calendar.models import GoogleOAuthState
    from fastapi.testclient import TestClient

    _, factory = google_service
    monkeypatch.setattr(main, "Session", factory)
    monkeypatch.setenv("INTERNAL_TOKEN", "internal")
    with factory.begin() as db:
        db.add(
            GoogleOAuthState(
                digest="pending",
                user_id="u",
                session_hash="s",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
    response = TestClient(main.app).post(
        "/internal/disconnect", headers={"X-Internal-Token": "internal"}, json={"user_id": "u"}
    )
    assert response.status_code == 200
    with factory() as db:
        assert db.get(GoogleOAuthState, "pending") is None


def test_blocked_parent_wins_over_synced_child(google_service, monkeypatch):
    service, factory = google_service
    from backend.google_calendar.models import GoogleMapping

    with factory.begin() as db:
        db.add(
            GoogleMapping(
                key="u:gen1:r",
                user_id="u",
                generation="gen1",
                event_id="parent",
                etag="old",
                status="conflict",
            )
        )
        db.add(
            GoogleMapping(
                key="u:gen1:r:2026-10-24",
                user_id="u",
                generation="gen1",
                event_id="child",
                etag="old",
                status="synced",
            )
        )
    monkeypatch.setattr(service, "client", lambda: pytest.fail("blocked series reached Google"))
    assert service.execute(op(scope="occurrence", occurrence_at="2026-10-24"))["status"] == "conflict"


def test_detached_timeout_recovery_and_recurrence_removal(google_service):
    service, factory = google_service
    from backend.google_calendar.models import GoogleConnection, GoogleMapping

    operation = op(
        snapshot={"title": "Moved", "due_date": "2026-11-01", "recurrence": {"frequency": "daily"}},
        exceptions=[{"occurrence_at": "2026-10-24", "data": {"notes": "keep"}}],
    )
    remote = {}
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=remote) if remote else httpx.Response(404)
        body = __import__("json").loads(request.content)
        writes.append(request.method)
        remote.update(body, etag="created" if request.method == "POST" else "cancelled")
        if request.method == "POST":
            raise httpx.ReadTimeout("accepted detached create")
        return httpx.Response(200, json=remote)

    with factory() as db, httpx.Client(transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(httpx.ReadTimeout):
            service.apply_exceptions(
                db, c, db.get(GoogleConnection, "u"), "/calendars/cal/events", "u:gen1:r", "parent", operation
            )
        db.commit()
    with factory.begin() as db, httpx.Client(transport=httpx.MockTransport(handle)) as c:
        assert (
            service.apply_exceptions(
                db, c, db.get(GoogleConnection, "u"), "/calendars/cal/events", "u:gen1:r", "parent", operation
            )
            == "synced"
        )
    removed = {
        **operation,
        "id": "remove-recurrence",
        "snapshot": {**operation["snapshot"], "recurrence": None},
    }
    with factory.begin() as db, httpx.Client(transport=httpx.MockTransport(handle)) as c:
        assert (
            service.apply_exceptions(
                db, c, db.get(GoogleConnection, "u"), "/calendars/cal/events", "u:gen1:r", "parent", removed
            )
            == "synced"
        )
    assert writes == ["POST", "PATCH"]
    assert remote["status"] == "cancelled"
    with factory() as db:
        assert db.get(GoogleMapping, "u:gen1:r:2026-10-24").status == "deleted"


def test_new_series_inherited_operation_marker_does_not_skip_sparse_override(google_service):
    service, factory = google_service
    from backend.google_calendar.models import GoogleConnection

    operation = op(
        snapshot={"title": "New series", "due_date": "2026-10-23", "recurrence": {"frequency": "daily"}},
        exceptions=[{"occurrence_at": "2026-10-24", "data": {"notes": "override"}}],
    )
    generated = {
        **service.operation_payload({**operation, "exceptions": []}, "2026-10-24"),
        "id": "instance",
        "etag": "generated",
        "originalStartTime": {"date": "2026-10-24"},
    }
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"items": [generated]})
        writes.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"etag": "overridden"})

    with factory.begin() as db, httpx.Client(transport=httpx.MockTransport(handle)) as c:
        assert (
            service.apply_exceptions(
                db, c, db.get(GoogleConnection, "u"), "/calendars/cal/events", "u:gen1:r", "parent", operation
            )
            == "synced"
        )
    assert writes[0]["description"] == "override"


def test_recovery_marker_does_not_hide_manual_location(google_service, monkeypatch):
    service, _ = google_service
    remote = {**service.operation_payload(op()), "location": "External room", "etag": "external"}
    monkeypatch.setattr(
        service,
        "client",
        lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=remote))),
    )
    assert service.execute(op())["status"] == "conflict"
