"""Opt-in real Yandex proof; touches only this run's marked resources."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import pytest
from backend.yandex_calendar import service
from backend.yandex_calendar.models import YandexConnection, YandexMapping, YandexOperation
from cryptography.fernet import Fernet
from icalendar import Calendar
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("YANDEX_TEST_CREDENTIALS_FILE"), reason="Set a private YANDEX_TEST_CREDENTIALS_FILE"
    ),
]


def _credentials():
    path = Path(os.environ["YANDEX_TEST_CREDENTIALS_FILE"])
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError("Credentials require an owned regular file with mode0600")
        if Path(__file__).resolve().parents[2] in path.resolve().parents:
            raise ValueError("Credentials must be outside the repository")
        config = json.load(source)
    if not config.get("calendar_id"):
        raise ValueError("Explicit test calendar selection required")
    return config


def _exercise(tmp_path, monkeypatch):
    config = _credentials()
    credentials = {"username": config["username"], "password": config["password"]}
    original_request = service.request

    def traced_request(client, method, url, **kwargs):
        response = original_request(client, method, url, **kwargs)
        information = {"method": method, "http_status": response.status_code}
        if response.status_code >= 400 and response.status_code != 404:
            try:
                error = ElementTree.fromstring(response.content)
                information["error_tags"] = [item.tag for item in error.iter()]
                description = error.findtext("{DAV:}responsedescription")
                if description:
                    information["description"] = description.replace(config["password"], "<redacted>")[:300]
            except ElementTree.ParseError:
                information["content_type"] = response.headers.get("Content-Type")
        print(json.dumps(information))
        return response

    monkeypatch.setattr(service, "request", traced_request)
    calendars = service.discover_calendars(credentials)
    selected = next((item for item in calendars if item["id"] == config["calendar_id"]), None)
    if not selected or (config.get("calendar_name") and selected["name"] != config["calendar_name"]):
        raise ValueError("Selected test calendar not found")
    with service.http_client(credentials) as client:
        service.validate_collection(client, selected["id"])
    engine = create_engine("sqlite:///" + str(tmp_path / "yandex-live.sqlite"))
    for model in (YandexConnection, YandexMapping, YandexOperation):
        model.__table__.create(engine, checkfirst=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "Session", sessions)
    monkeypatch.setenv("YANDEX_TOKEN_KEY", Fernet.generate_key().decode())
    user_id, generation, record_id = str(uuid4()), str(uuid4()), str(uuid4())
    marker = "[Голос test " + uuid4().hex + "]"
    with sessions.begin() as db:
        db.add(
            YandexConnection(
                user_id=user_id,
                generation=generation,
                status="connected",
                credentials=service.encrypt(credentials),
                calendars=calendars,
                calendar_id=selected["id"],
                calendar_name=selected["name"],
            )
        )
    zone = ZoneInfo("Europe/Berlin")
    # Future Saturday before Berlin's spring transition: the three instances
    # exercise both winter and summer offsets without firing a reminder now.
    march_end = datetime(datetime.now(timezone.utc).year + 1, 3, 31, 10, tzinfo=zone)
    sunday = march_end - timedelta(days=(march_end.weekday() + 1) % 7)
    start = sunday - timedelta(days=1)
    data = {
        "kind": "meeting",
        "title": marker,
        "notes": "Temporary integration proof; no invitations.",
        "status": "active",
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=1)).isoformat(),
        "timezone": zone.key,
        "reminder_minutes": None,
        "recurrence": None,
    }

    def operation(version, action="update", **extra):
        return {
            "id": str(uuid4()),
            "user_id": user_id,
            "generation": generation,
            "record_id": record_id,
            "version": version,
            "operation": action,
            "scope": "series",
            "snapshot": dict(data),
            "exceptions": [],
            **extra,
        }

    def synced(stage, value):
        result = service.execute(value)
        print(json.dumps({"stage": stage, "status": result["status"], "code": result.get("code")}))
        assert result["status"] == "synced"

    def own_resource(client):
        with sessions() as db:
            mapping = db.get(YandexMapping, ":".join((user_id, generation, record_id)))
            href, uid = mapping.href, mapping.uid
        response = service.request(client, "GET", href)
        response.raise_for_status()
        calendar = Calendar.from_ical(response.text)
        events = calendar.walk("VEVENT")
        assert events and all(
            str(event.get("UID")) == uid and str(event.get("SUMMARY", "")).startswith(marker)
            for event in events
        )
        return href, response, calendar

    cleanup_failed = False
    try:
        first = operation(1, "create")
        synced("create", first)
        synced("duplicate create", first)
        data["title"] = marker + " moved"
        data["notes"] = ""
        data["reminder_minutes"] = 15
        data["start_at"], data["end_at"] = (
            (start + timedelta(minutes=15)).isoformat(),
            (start + timedelta(minutes=75)).isoformat(),
        )
        synced("move", operation(2))
        data["recurrence"] = {"frequency": "daily", "interval": 1, "count": 3}
        synced("recurrence", operation(3))
        original = (start + timedelta(days=1, minutes=15)).astimezone(timezone.utc).isoformat()
        override = {"occurrence_at": original, "data": {"title": marker + " occurrence"}}
        synced("occurrence", operation(4, scope="occurrence", occurrence_at=original, exceptions=[override]))
        cancelled = (start + timedelta(days=2, minutes=15)).astimezone(timezone.utc).isoformat()
        synced(
            "cancel occurrence",
            operation(
                5,
                "delete",
                scope="occurrence",
                occurrence_at=cancelled,
                exceptions=[override, {"occurrence_at": cancelled, "data": {"deleted": True}}],
            ),
        )
        deletion = operation(6, "delete", snapshot={**data, "deleted": True})
        synced("delete", deletion)
        synced("duplicate delete", deletion)

        record_id = str(uuid4())
        data["title"] = marker + " conflict proof"
        synced("conflict setup", operation(1, "create"))
        with service.http_client(credentials) as client:
            href, response, calendar = own_resource(client)
            calendar.walk("VEVENT")[0]["DESCRIPTION"] = "Own test resource edited outside integration."
            external = service.request(
                client,
                "PUT",
                href,
                content=calendar.to_ical(),
                headers={"If-Match": response.headers["ETag"], "Content-Type": "text/calendar"},
            )
            external.raise_for_status()
            _, changed, _ = own_resource(client)
            assert changed.headers["ETag"] != response.headers["ETag"]
            stale = service.request(
                client,
                "PUT",
                href,
                content=response.content,
                headers={"If-Match": response.headers["ETag"], "Content-Type": "text/calendar"},
            )
            assert stale.status_code == 412
            duplicate = service.request(
                client,
                "PUT",
                href,
                content=changed.content,
                headers={"If-None-Match": "*", "Content-Type": "text/calendar"},
            )
            # Yandex currently ignores wildcard If-None-Match. Probe only this
            # run's resource with its unchanged current body, never user data.
            assert duplicate.status_code in (200, 201, 204, 412)
            print(json.dumps({"if_none_match_enforced": duplicate.status_code == 412}))
            _, verified, _ = own_resource(client)
            assert str(Calendar.from_ical(verified.text).walk("VEVENT")[0]["DESCRIPTION"]) == (
                "Own test resource edited outside integration."
            )
        data["title"] = marker + " assistant edit after conflict"
        result = service.execute(operation(2))
        print(json.dumps({"stage": "external conflict", "status": result["status"]}))
        assert result["status"] == "conflict"

        record_id = str(uuid4())
        data["title"] = marker + " missing proof"
        synced("missing setup", operation(1, "create"))
        with service.http_client(credentials) as client:
            href, response, _ = own_resource(client)
            deleted = service.request(client, "DELETE", href, headers={"If-Match": response.headers["ETag"]})
            assert deleted.status_code in (200, 204)
        result = service.execute(operation(2))
        print(json.dumps({"stage": "external deletion", "status": result["status"]}))
        assert result["status"] == "missing"
    finally:
        with sessions() as db:
            journals = list(db.scalars(select(YandexOperation)))
            targets = {item.intent["href"] for item in journals if item.intent.get("href")}
            uids = {
                service.ical.uid(item.intent["content"]) for item in journals if item.intent.get("content")
            }
            uids.update(item.uid for item in db.scalars(select(YandexMapping)))
        try:
            with service.http_client(credentials) as client:
                for uid in uids:
                    found = service.lookup_uid(client, credentials, selected["id"], uid)
                    if found:
                        targets.add(found)
                for href in targets:
                    response = service.request(client, "GET", href)
                    if response.status_code in (404, 410):
                        continue
                    response.raise_for_status()
                    events = Calendar.from_ical(response.text).walk("VEVENT")
                    if (
                        not events
                        or any(str(event.get("UID")) not in uids for event in events)
                        or not all(str(event.get("SUMMARY", "")).startswith(marker) for event in events)
                        or not response.headers.get("ETag")
                    ):
                        cleanup_failed = True
                        continue
                    deleted = service.request(
                        client, "DELETE", href, headers={"If-Match": response.headers["ETag"]}
                    )
                    if deleted.status_code not in (200, 204, 404, 410):
                        cleanup_failed = True
        finally:
            engine.dispose()
        if cleanup_failed:
            raise AssertionError("Live test cleanup could not safely remove its own marked resources")


def test_live_yandex_create_move_recurrence_delete(tmp_path, monkeypatch):
    # Avoid printing credentials through third-party exception tracebacks.
    failure = None
    try:
        _exercise(tmp_path, monkeypatch)
    except Exception as exc:
        failure = "Live Yandex check failed: " + type(exc).__name__
        code = getattr(exc, "code", None)
        if code in {"reconnect_required", "calendar_unavailable", "calendar_read_only", "untrusted_redirect"}:
            failure += " (" + code + ")"
    if failure:
        pytest.fail(failure, pytrace=False)
