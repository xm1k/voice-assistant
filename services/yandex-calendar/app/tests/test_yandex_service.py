import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def service_db(monkeypatch, tmp_path):
    from backend.common.runtime import Base
    from backend.yandex_calendar import service
    from backend.yandex_calendar.models import YandexConnection

    engine = create_engine("sqlite:///" + str(tmp_path / "yandex.sqlite"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "Session", factory)
    monkeypatch.setenv("YANDEX_TOKEN_KEY", Fernet.generate_key().decode())
    with factory.begin() as db:
        db.add(
            YandexConnection(
                user_id="u",
                generation="g",
                status="connected",
                calendar_id="https://caldav.yandex.ru/calendars/u/main/",
                calendar_name="Main",
                credentials=service.encrypt({"username": "u@yandex.ru", "password": "private"}),
                calendars=[{"id": "https://caldav.yandex.ru/calendars/u/main/", "name": "Main"}],
            )
        )
    return service, factory


def operation(**updates):
    return {
        "id": "op",
        "user_id": "u",
        "generation": "g",
        "record_id": "r",
        "version": 1,
        "operation": "create",
        "scope": "series",
        "snapshot": {
            "kind": "meeting",
            "title": "Meeting",
            "start_at": "2026-10-24T10:00:00+03:00",
            "end_at": "2026-10-24T11:00:00+03:00",
            "timezone": "Europe/Moscow",
        },
        "exceptions": [],
        **updates,
    }


def test_url_guard_rejects_untrusted_origins():
    from backend.yandex_calendar.service import trusted_url

    for bad in (
        "http://caldav.yandex.ru/",
        "https://evil.example/",
        "https://caldav.yandex.ru:444/",
        "https://caldav.yandex.ru@evil.example/",
        "//evil.example/calendar",
        "https://caldav.yandex.ru/a#fragment",
    ):
        with pytest.raises(ValueError):
            trusted_url(bad)
    assert trusted_url("/calendars/u/main/") == "https://caldav.yandex.ru/calendars/u/main/"


def test_create_timeout_reconciles_semantic_marker_without_second_put(service_db, monkeypatch):
    service, factory = service_db
    remote, writes = {}, []

    def handle(request):
        if request.method == "GET":
            return (
                httpx.Response(200, text=remote["content"], headers={"ETag": '"one"'})
                if remote
                else httpx.Response(404)
            )
        assert request.headers["if-none-match"] == "*"
        remote["content"] = request.content.decode()
        writes.append(request)
        raise httpx.ReadTimeout("lost response")

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(service.RetryableYandexError):
        service.execute(operation())
    assert service.execute(operation())["status"] == "synced"
    assert service.execute(operation())["status"] == "synced"
    assert len(writes) == 1
    with factory() as db:
        from backend.yandex_calendar.models import YandexConnection

        assert "private" not in db.get(YandexConnection, "u").credentials


def test_ambiguous_write_with_external_drift_is_reconciled(service_db, monkeypatch):
    from icalendar import Calendar

    service, _ = service_db
    remote = {}
    writes = []

    def handle(request):
        if request.method == "GET":
            return (
                httpx.Response(200, text=remote["content"], headers={"ETag": '"external"'})
                if remote
                else httpx.Response(404)
            )
        calendar = Calendar.from_ical(request.content)
        writes.append(request)
        if len(writes) == 1:
            calendar.walk("VEVENT")[0].add("location", "External edit")
            remote["content"] = calendar.to_ical().decode()
            raise httpx.ReadTimeout("lost response")
        assert request.headers["if-match"] == '"external"'
        remote["content"] = calendar.to_ical().decode()
        return httpx.Response(204)

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(service.RetryableYandexError):
        service.execute(operation())
    assert service.execute(operation())["status"] == "synced"
    assert "LOCATION" not in remote["content"]


def multistatus(href, properties):
    return f"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>{href}</d:href><d:propstat><d:prop>{properties}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"""


WRITABLE = '<d:displayname>Main</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:current-user-privilege-set><d:privilege><d:write/></d:privilege></d:current-user-privilege-set><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'


@pytest.mark.parametrize("hostile", [False, True])
def test_real_caldav_discovery_transport_rejects_hostile_principal_before_request(monkeypatch, hostile):
    import niquests
    from backend.yandex_calendar import service

    seen = []

    def handle(session, method, url, **kwargs):
        seen.append(url)
        assert kwargs["allow_redirects"] is False
        assert session.trust_env is False
        assert url.startswith(service.ORIGIN + "/")
        body = kwargs["data"].decode()
        if "current-user-principal" in body:
            principal = "https://evil.example/principal/" if hostile else "/principals/u/"
            xml = multistatus(
                "/", f"<d:current-user-principal><d:href>{principal}</d:href></d:current-user-principal>"
            )
        elif "calendar-home-set" in body:
            xml = multistatus(
                "/principals/u/", "<c:calendar-home-set><d:href>/calendars/u/</d:href></c:calendar-home-set>"
            )
        else:
            xml = multistatus("/calendars/u/main/", WRITABLE)
        response = niquests.Response()
        response.status_code = 207
        response._content = xml.encode()
        response.headers = {"Content-Type": "application/xml; charset=utf-8"}
        response.url = url
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(niquests.Session, "request", handle)
    if hostile:
        with pytest.raises(service.YandexError):
            service.discover_calendars({"username": "u", "password": "private"})
        assert len(seen) == 1
    else:
        assert service.discover_calendars({"username": "u", "password": "private"}) == [
            {"id": service.ORIGIN + "/calendars/u/main/", "name": "Main"}
        ]


def test_discovery_redirect_does_not_leak_credentials(monkeypatch):
    import niquests
    from backend.yandex_calendar import service

    seen = []

    def handle(session, method, url, **kwargs):
        seen.append(url)
        assert not kwargs["allow_redirects"]
        response = niquests.Response()
        response.status_code = 302
        response.headers = {"Location": "https://evil.example/"}
        return response

    monkeypatch.setattr(niquests.Session, "request", handle)
    with pytest.raises(service.YandexError, match="untrusted_redirect"):
        service.discover_calendars({"username": "u", "password": "private"})
    assert seen == [service.ORIGIN + "/"]


def test_internal_lifecycle_encrypts_credentials_and_fences_selection(service_db, monkeypatch):
    from backend.yandex_calendar import main
    from backend.yandex_calendar.models import YandexConnection
    from fastapi.testclient import TestClient

    service, factory = service_db
    monkeypatch.setattr(main, "Session", factory)
    monkeypatch.setenv("INTERNAL_TOKEN", "internal")
    client = TestClient(main.app)
    assert client.get("/internal/status", params={"user_id": "u"}).status_code == 401
    headers = {"X-Internal-Token": "internal"}
    calendars = [{"id": service.ORIGIN + "/calendars/u/main/", "name": "Main"}]
    monkeypatch.setattr(service, "discover_calendars", lambda credentials: calendars)
    discovery = client.post(
        "/internal/discover",
        headers=headers,
        json={"user_id": "u", "username": "user", "password": "superprivate"},
    ).json()
    assert discovery["status"] == "select_calendar"
    assert "superprivate" not in str(discovery)
    collection = multistatus("/calendars/u/main/", WRITABLE)
    monkeypatch.setattr(
        service,
        "http_client",
        lambda credentials: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(207, text=collection))
        ),
    )
    assert (
        client.post(
            "/internal/connect",
            headers=headers,
            json={"user_id": "u", "generation": "g", "calendar_id": calendars[0]["id"]},
        ).status_code
        == 409
    )
    connected = client.post(
        "/internal/connect",
        headers=headers,
        json={"user_id": "u", "generation": discovery["generation"], "calendar_id": calendars[0]["id"]},
    ).json()
    assert connected["status"] == "connected"
    assert connected["generation"] == discovery["generation"]
    with factory() as db:
        assert "superprivate" not in db.get(YandexConnection, "u").credentials
    disconnected = client.post("/internal/disconnect", headers=headers, json={"user_id": "u"}).json()
    assert disconnected["generation"] != connected["generation"]
    with factory() as db:
        assert db.get(YandexConnection, "u").credentials is None


class CalendarServer:
    """A stateful protocol double enforcing real conditional request rules."""

    def __init__(self):
        self.resources = {}
        self.serial = 0
        self.requests = []
        self.timeout_after = None
        self.collection_exists = True
        self.race = False

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "REPORT":
            return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:"/>')
        if request.method == "PROPFIND":
            return (
                httpx.Response(207, text=multistatus("/calendars/u/main/", WRITABLE))
                if self.collection_exists
                else httpx.Response(404)
            )
        item = self.resources.get(path)
        if request.method == "GET":
            return (
                httpx.Response(200, text=item["content"], headers={"ETag": item["etag"]})
                if item
                else httpx.Response(404)
            )
        if self.race and item:
            item["etag"] = '"raced"'
            self.race = False
        if request.headers.get("if-none-match") == "*" and item:
            return httpx.Response(412)
        if request.headers.get("if-match") and (not item or request.headers["if-match"] != item["etag"]):
            return httpx.Response(412)
        if request.method == "PUT":
            from icalendar import Calendar

            self.serial += 1
            parsed = Calendar.from_ical(request.content)
            parsed["prodid"] = "Normalized server"
            parsed.walk("VEVENT")[0]["SEQUENCE"] = self.serial
            self.resources[path] = {"content": parsed.to_ical().decode(), "etag": f'"etag-{self.serial}"'}
            status = 204 if item else 201
        elif request.method == "DELETE":
            self.resources.pop(path, None)
            status = 204
        else:
            return httpx.Response(405)
        if self.timeout_after == request.method:
            self.timeout_after = None
            raise httpx.ReadTimeout("accepted but response lost")
        return httpx.Response(status)


@pytest.fixture
def calendar_server(service_db, monkeypatch):
    service, factory = service_db
    server = CalendarServer()
    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(server.handle))
    )
    return service, factory, server


def test_authoritative_update_delete_and_reconcile_restore_managed_resources(calendar_server):
    from icalendar import Calendar

    service, factory, server = calendar_server
    assert service.execute(operation())["status"] == "synced"
    path, item = next(iter(server.resources.items()))
    server.resources["/calendars/u/main/unrelated.ics"] = {"content": "Unrelated event", "etag": '"foreign"'}
    uid = str(Calendar.from_ical(item["content"]).walk("VEVENT")[0]["UID"])
    parsed = Calendar.from_ical(item["content"])
    parsed.walk("VEVENT")[0]["SUMMARY"] = "External title"
    item.update(content=parsed.to_ical().decode(), etag='"external"')
    desired = {**operation()["snapshot"], "title": "Local truth"}
    assert (
        service.execute(operation(id="move", version=2, operation="update", snapshot=desired))["status"]
        == "synced"
    )
    assert (
        str(Calendar.from_ical(server.resources[path]["content"]).walk("VEVENT")[0]["SUMMARY"])
        == "Local truth"
    )
    del server.resources[path]
    assert (
        service.execute(operation(id="reconcile", version=2, operation="reconcile", snapshot=desired))[
            "status"
        ]
        == "synced"
    )
    assert len(server.resources) == 2
    recreated = server.resources[path]
    assert str(Calendar.from_ical(recreated["content"]).walk("VEVENT")[0]["UID"]) == uid
    recreated["etag"] = '"another-external-change"'
    assert service.execute(operation(id="delete", version=3, operation="delete"))["status"] == "synced"
    assert list(server.resources) == ["/calendars/u/main/unrelated.ics"]
    assert service.execute(operation(id="delete-again", version=3, operation="delete"))["status"] == "synced"
    # Restoring an app-deleted resource in Yandex is also drift: a later full
    # reconciliation deletes it again using the remembered original identity.
    server.resources[path] = {"content": recreated["content"], "etag": '"restored"'}
    assert (
        service.execute(
            operation(
                id="reconcile-deleted",
                version=4,
                operation="reconcile",
                snapshot={**desired, "deleted": True},
            )
        )["status"]
        == "synced"
    )
    assert list(server.resources) == ["/calendars/u/main/unrelated.ics"]


@pytest.mark.parametrize("action", ["reconcile", "delete"])
def test_newer_reconcile_keeps_identity_of_ambiguous_create(service_db, monkeypatch, action):
    service, _ = service_db
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(404)
        if request.method == "REPORT":
            return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:"/>')
        writes.append(request)
        raise httpx.ReadTimeout("accepted create not yet visible")

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(service.RetryableYandexError):
        service.execute(operation())
    with pytest.raises(service.RetryableYandexError):
        service.execute(operation(id="new", version=2, operation=action, initial_operation_id="op"))
    assert len(writes) == 1


def test_resume_timeout_retains_new_identity_for_later_snapshot(calendar_server):
    from icalendar import Calendar

    service, _, server = calendar_server
    assert service.execute(operation())["status"] == "synced"
    old_uid = str(
        Calendar.from_ical(next(iter(server.resources.values()))["content"]).walk("VEVENT")[0]["UID"]
    )
    assert service.execute(operation(id="delete", version=2, operation="delete"))["status"] == "synced"
    server.timeout_after = "PUT"
    with pytest.raises(service.RetryableYandexError):
        service.execute(operation(id="resume", version=3, operation="update"))
    changed = {**operation()["snapshot"], "title": "Latest snapshot"}
    assert (
        service.execute(operation(id="reconcile", version=4, operation="reconcile", snapshot=changed))[
            "status"
        ]
        == "synced"
    )
    assert len(server.resources) == 1
    event = Calendar.from_ical(next(iter(server.resources.values()))["content"]).walk("VEVENT")[0]
    assert str(event["UID"]) != old_uid and str(event["SUMMARY"]) == "Latest snapshot"


def test_reconcile_never_overwrites_an_unrelated_uid_at_saved_address(calendar_server):
    from icalendar import Calendar

    service, _, server = calendar_server
    assert service.execute(operation())["status"] == "synced"
    item = next(iter(server.resources.values()))
    parsed = Calendar.from_ical(item["content"])
    parsed.walk("VEVENT")[0]["UID"] = "foreign-uid"
    item.update(content=parsed.to_ical().decode(), etag='"foreign"')
    before = len([r for r in server.requests if r.method == "PUT"])
    result = service.execute(operation(id="reconcile", operation="reconcile", version=2))
    assert result["status"] == "failed" and result["code"] == "untrusted_resource"
    assert len([r for r in server.requests if r.method == "PUT"]) == before


def test_old_operation_cannot_roll_back_newer_mapping(calendar_server):
    service, _, server = calendar_server
    assert service.execute(operation(version=3))["status"] == "synced"
    count = len(server.requests)
    assert service.execute(operation(id="late", version=2, operation="update"))["status"] == "synced"
    assert len(server.requests) == count


def test_successful_write_uses_uid_and_etag_without_content_comparison(service_db, monkeypatch):
    from backend.yandex_calendar.models import YandexMapping
    from icalendar import Calendar

    service, factory = service_db
    server = CalendarServer()

    def handle(request):
        response = server.handle(request)
        if request.method == "PUT" and response.status_code in (201, 204):
            item = server.resources[request.url.path]
            normalized = Calendar.from_ical(item["content"])
            normalized.walk("VEVENT")[0]["ORGANIZER"] = "mailto:primary-alias@yandex.ru"
            item["content"] = normalized.to_ical().decode()
        return response

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    monkeypatch.setattr(
        service.ical, "server_equivalent", lambda *args: pytest.fail("successful write compared content")
    )
    assert service.execute(operation())["status"] == "synced"
    assert service.execute(operation(id="update", version=2, operation="update"))["status"] == "synced"
    with factory() as db:
        mapping = db.get(YandexMapping, "u:g:r")
        assert mapping.etag == '"etag-2"'
        assert "primary-alias@yandex.ru" in mapping.content


@pytest.mark.parametrize("bad_response", ["wrong_uid", "missing_etag"])
def test_successful_write_requires_own_uid_and_version(service_db, monkeypatch, bad_response):
    from icalendar import Calendar

    service, _ = service_db
    server = CalendarServer()

    def handle(request):
        response = server.handle(request)
        if request.method == "GET" and response.status_code == 200:
            parsed = Calendar.from_ical(response.text)
            if bad_response == "wrong_uid":
                parsed.walk("VEVENT")[0]["UID"] = "another-event"
            return httpx.Response(
                200,
                content=parsed.to_ical(),
                headers={} if bad_response == "missing_etag" else response.headers,
            )
        return response

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    assert service.execute(operation())["status"] == "failed"


def test_full_series_and_occurrence_share_one_conditional_resource(calendar_server):
    from icalendar import Calendar

    service, _, server = calendar_server
    first = operation(snapshot={**operation()["snapshot"], "recurrence": {"frequency": "daily", "count": 3}})
    assert service.execute(first)["status"] == "synced"
    changed = operation(
        id="op2",
        version=2,
        operation="update",
        scope="occurrence",
        occurrence_at="2026-10-25T07:00:00+00:00",
        snapshot=first["snapshot"],
        exceptions=[{"occurrence_at": "2026-10-25T07:00:00+00:00", "data": {"title": "Override"}}],
    )
    assert service.execute(changed)["status"] == "synced"
    assert len(server.resources) == 1
    puts = [r for r in server.requests if r.method == "PUT"]
    assert puts[0].headers["if-none-match"] == "*"
    assert puts[1].headers["if-match"] == '"etag-1"'
    assert len(Calendar.from_ical(next(iter(server.resources.values()))["content"]).walk("VEVENT")) == 2


def test_occurrence_delete_updates_exdate_without_deleting_whole_resource(calendar_server):
    from icalendar import Calendar

    service, _, server = calendar_server
    snapshot = {**operation()["snapshot"], "recurrence": {"frequency": "daily", "count": 3}}
    assert service.execute(operation(snapshot=snapshot))["status"] == "synced"
    deleted = operation(
        id="delete-instance",
        version=2,
        operation="delete",
        scope="occurrence",
        occurrence_at="2026-10-25T07:00:00Z",
        snapshot=snapshot,
        exceptions=[{"occurrence_at": "2026-10-25T07:00:00Z", "data": {"deleted": True}}],
    )
    assert service.execute(deleted)["status"] == "synced"
    assert len(server.resources) == 1
    assert not any(request.method == "DELETE" for request in server.requests)
    master = Calendar.from_ical(next(iter(server.resources.values()))["content"]).walk("VEVENT")[0]
    assert master["EXDATE"].dts[0].dt.day == 25


def test_delete_timeout_recovery_and_resume_rotate_href_and_uid(calendar_server):
    service, factory, server = calendar_server
    from backend.yandex_calendar.models import YandexMapping

    assert service.execute(operation())["status"] == "synced"
    old_path = next(iter(server.resources))
    with factory() as db:
        old_uid = db.get(YandexMapping, "u:g:r").uid
    server.timeout_after = "DELETE"
    deletion = operation(id="delete", operation="delete", version=2)
    with pytest.raises(service.RetryableYandexError):
        service.execute(deletion)
    assert service.execute(deletion)["status"] == "synced"
    assert len([r for r in server.requests if r.method == "DELETE"]) == 1
    assert service.execute(operation(id="resume", operation="update", version=3))["status"] == "synced"
    assert next(iter(server.resources)) != old_path
    with factory() as db:
        assert db.get(YandexMapping, "u:g:r").uid != old_uid


@pytest.mark.parametrize("calendar_missing", [False, True])
def test_external_missing_resource_is_distinct_from_missing_collection(calendar_server, calendar_missing):
    service, _, server = calendar_server
    assert service.execute(operation())["status"] == "synced"
    server.resources.clear()
    server.collection_exists = not calendar_missing
    result = service.execute(operation(id="update", operation="update", version=2))
    assert result["status"] == ("disconnected" if calendar_missing else "synced")
    if calendar_missing:
        assert result["code"] == "calendar_unavailable"


def test_conditional_race_retries_against_fresh_etag(calendar_server):
    service, _, server = calendar_server
    assert service.execute(operation())["status"] == "synced"
    server.race = True
    update = operation(id="update", operation="update", version=2)
    with pytest.raises(service.RetryableYandexError):
        service.execute(update)
    assert service.execute(update)["status"] == "synced"
    assert service.execute(operation(id="later", operation="update", version=3))["status"] == "synced"


@pytest.mark.parametrize("status,expected", [(401, "reconnect_required"), (403, "calendar_unavailable")])
def test_auth_and_readonly_failures_are_visible_reconnect_states(service_db, monkeypatch, status, expected):
    service, factory = service_db
    from backend.yandex_calendar.models import YandexConnection

    monkeypatch.setattr(
        service,
        "http_client",
        lambda credentials: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status))
        ),
    )
    result = service.execute(operation())
    assert result["status"] == "disconnected"
    assert result["code"] == expected
    with factory() as db:
        assert db.get(YandexConnection, "u").status == "reconnect_required"


def test_transient_rate_limit_retries_with_delay_and_durable_intent(service_db, monkeypatch):
    service, factory = service_db
    from backend.yandex_calendar.models import YandexOperation

    def handle(request):
        with factory() as db:
            assert db.get(YandexOperation, "op").intent["content"]
        return httpx.Response(429, headers={"Retry-After": "90"})

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(service.RetryableYandexError) as raised:
        service.execute(operation())
    assert raised.value.retry_after == 90


def test_stale_generation_never_reaches_transport(service_db, monkeypatch):
    service, _ = service_db
    monkeypatch.setattr(service, "http_client", lambda credentials: pytest.fail("stale network request"))
    assert service.execute(operation(generation="old"))["status"] == "disconnected"


def test_collection_resource_size_limit_fails_before_any_remote_write(service_db, monkeypatch):
    service, factory = service_db
    from backend.yandex_calendar.models import YandexConnection

    with factory.begin() as db:
        conn = db.get(YandexConnection, "u")
        conn.calendars = [{**conn.calendars[0], "max_resource_size": 64}]
    monkeypatch.setattr(
        service, "http_client", lambda credentials: pytest.fail("oversized payload reached transport")
    )
    result = service.execute(operation())
    assert result["status"] == "failed"
    assert result["code"] == "calendar_resource_too_large"


def test_discovery_writability_requires_create_edit_delete_and_events():
    from xml.etree import ElementTree

    from backend.yandex_calendar.service import writable
    from caldav.elements import cdav, dav

    privileges = ElementTree.fromstring(
        '<d:current-user-privilege-set xmlns:d="DAV:"><d:privilege><d:write-content/></d:privilege><d:privilege><d:bind/></d:privilege></d:current-user-privilege-set>'
    )
    components = ElementTree.fromstring(
        '<c:supported-calendar-component-set xmlns:c="urn:ietf:params:xml:ns:caldav"><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
    )
    properties = {
        dav.CurrentUserPrivilegeSet.tag: privileges,
        cdav.SupportedCalendarComponentSet.tag: components,
    }
    assert not writable(properties)
    privileges.append(ElementTree.fromstring('<d:privilege xmlns:d="DAV:"><d:unbind/></d:privilege>'))
    assert writable(properties)
    components[0].attrib["name"] = "VTODO"
    assert not writable(properties)


def test_collection_validation_accepts_encoded_href_normalization():
    from backend.yandex_calendar import service

    xml = multistatus("/calendars/user%40yandex.ru/main/", WRITABLE)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(207, text=xml))) as client:
        service.validate_collection(client, "https://caldav.yandex.ru/calendars/user@yandex.ru/main/")


def test_failed_operation_can_be_manually_retried_with_same_id(service_db, monkeypatch):
    service, _ = service_db
    server = CalendarServer()
    bad = {"enabled": True}

    def handle(request):
        return httpx.Response(400) if bad["enabled"] else server.handle(request)

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    assert service.execute(operation())["status"] == "failed"
    bad["enabled"] = False
    assert service.execute(operation())["status"] == "synced"
    assert len(server.resources) == 1


@pytest.mark.parametrize("location,timeout", [(True, False), (False, False), (False, True)])
def test_server_canonical_href_adopted_by_location_or_real_uid_report(
    service_db, monkeypatch, location, timeout
):
    from urllib.parse import quote
    from xml.sax.saxutils import escape

    from backend.yandex_calendar.models import YandexMapping
    from icalendar import Calendar

    service, factory = service_db
    resource = {}
    writes = []

    def handle(request):
        if request.method == "GET":
            if request.url.path == resource.get("path"):
                return httpx.Response(200, text=resource["content"], headers={"ETag": '"canonical"'})
            return httpx.Response(404)
        if request.method == "REPORT":
            assert "UID" in request.content.decode()
            return httpx.Response(
                207,
                text=multistatus(
                    resource["path"],
                    '<d:getetag>"canonical"</d:getetag><c:calendar-data>'
                    + escape(resource["content"])
                    + "</c:calendar-data>",
                ),
            )
        writes.append(request)
        content = request.content.decode()
        resource.update(path="/calendars/u/main/server-canonical.ics", content=content)
        if timeout and len(writes) == 1:
            raise httpx.ReadTimeout("accepted canonical resource")
        return httpx.Response(201, headers={"Location": resource["path"]} if location else {})

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    if timeout:
        with pytest.raises(service.RetryableYandexError):
            service.execute(operation())
    assert service.execute(operation())["status"] == "synced"
    assert len(writes) == 1
    with factory() as db:
        mapping = db.get(YandexMapping, "u:g:r")
        assert mapping.href.endswith("/server-canonical.ics")
        uid = str(Calendar.from_ical(mapping.content).walk("VEVENT")[0]["UID"])
        assert writes[0].url.raw_path.decode().endswith(quote(uid, safe="") + ".ics")


def test_hostile_location_never_receives_credentials(service_db, monkeypatch):
    service, _ = service_db
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(201, headers={"Location": "https://evil.example/event.ics"})

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    assert service.execute(operation())["status"] == "failed"
    assert all(url.startswith(service.ORIGIN + "/") for url in seen)


@pytest.mark.parametrize("uncertain", [False, "create", "update"])
def test_yandex_normalization_without_x_markers_recovers_saved_intent(service_db, monkeypatch, uncertain):
    from icalendar import Calendar

    service, factory = service_db
    server = CalendarServer()

    def handle(request):
        response = server.handle(request)
        if request.method == "PUT" and response.status_code in (201, 204):
            item = server.resources[request.url.path]
            parsed = Calendar.from_ical(item["content"])
            for event in parsed.walk("VEVENT"):
                event.pop("X-VOICE-OPERATION", None)
                event["CLASS"] = "PRIVATE"
                event.add("organizer", "mailto:u@yandex.ru")
                event.add("categories", ["Main"])
                event.add("url", "https://calendar.yandex.ru/event?event_id=123456")
            item["content"] = parsed.to_ical().decode()
            if (uncertain == "create" and len([r for r in server.requests if r.method == "PUT"]) == 1) or (
                uncertain == "update" and len([r for r in server.requests if r.method == "PUT"]) == 2
            ):
                raise httpx.ReadTimeout("accepted normalized resource")
        return response

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    if uncertain == "create":
        with pytest.raises(service.RetryableYandexError):
            service.execute(operation())
    assert service.execute(operation())["status"] == "synced"
    changed = operation(
        id="update", version=2, operation="update", snapshot={**operation()["snapshot"], "title": "Changed"}
    )
    if uncertain == "update":
        with pytest.raises(service.RetryableYandexError):
            service.execute(changed)
    assert service.execute(changed)["status"] == "synced"
    assert len([r for r in server.requests if r.method == "PUT"]) == 2


def test_delete_uses_durable_intent_without_remote_marker_put(calendar_server):
    service, _, server = calendar_server
    assert service.execute(operation())["status"] == "synced"
    before = len([r for r in server.requests if r.method == "PUT"])
    assert service.execute(operation(id="delete", version=2, operation="delete"))["status"] == "synced"
    assert len([r for r in server.requests if r.method == "PUT"]) == before


@pytest.mark.parametrize("field", ["start_at", "end_at", "occurrence_at"])
def test_subminute_precision_fails_explicitly_without_remote_mutation(service_db, monkeypatch, field):
    service, _ = service_db
    value = operation()
    if field == "occurrence_at":
        value[field] = "2026-10-25T10:00:30+03:00"
        value["scope"] = "occurrence"
    else:
        value["snapshot"][field] = "2026-10-24T10:00:30+03:00"
    monkeypatch.setattr(
        service, "http_client", lambda credentials: pytest.fail("unsupported precision reached network")
    )
    assert service.execute(value)["code"] == "calendar_precision_unsupported"


@pytest.mark.parametrize("discovered", [False, True])
def test_ambiguous_create_never_replays_put_when_server_ignores_if_none_match(
    service_db, monkeypatch, discovered
):
    service, _ = service_db
    writes = []
    if discovered:
        monkeypatch.setattr(
            service, "lookup_uid", lambda *args: service.ORIGIN + "/calendars/u/main/hidden.ics"
        )

    def handle(request):
        if request.method == "GET":
            return httpx.Response(404)
        if request.method == "REPORT":
            return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:"/>')
        # Real Yandex can accept/overwrite despite If-None-Match:*. Once the
        # first response is ambiguous, even manual retry must be read-only.
        writes.append(request)
        raise httpx.ReadTimeout("accepted create but not yet discoverable")

    monkeypatch.setattr(
        service, "http_client", lambda credentials: httpx.Client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(service.RetryableYandexError):
        service.execute(operation())
    for _ in range(5):
        with pytest.raises(service.RetryableYandexError):
            service.execute(operation())
    assert len(writes) == 1
