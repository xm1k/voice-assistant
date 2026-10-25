"""Fenced CalDAV lifecycle and conditional, recoverable resource mutations."""

import hashlib
import json
import os
import posixpath
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree

import caldav
import httpx
import niquests
from backend.common.runtime import Session
from caldav.elements import cdav, dav
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from . import ical
from .models import YandexConnection, YandexMapping, YandexOperation

ORIGIN = "https://caldav.yandex.ru"


class RetryableYandexError(Exception):
    def __init__(self, message="Yandex temporarily unavailable", retry_after=5):
        super().__init__(message)
        self.retry_after = retry_after


class YandexError(Exception):
    def __init__(self, code, *, reconnect=False):
        super().__init__(code)
        self.code = code
        self.reconnect = reconnect


def trusted_url(value):
    raw = str(value)
    if raw.startswith("//") or "\\" in raw or any(ord(char) < 32 for char in raw):
        raise ValueError("Untrusted CalDAV URL")
    parsed = urlsplit(urljoin(ORIGIN + "/", raw))
    if (
        parsed.scheme != "https"
        or parsed.hostname != "caldav.yandex.ru"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError("Untrusted CalDAV URL")
    return ORIGIN + (parsed.path or "/")


def resource_url(value, calendar_id, base=None):
    try:
        target = trusted_url(urljoin(base or calendar_id.rstrip("/") + "/", str(value)))
        path = posixpath.normpath(unquote(urlsplit(target).path, errors="strict"))
        collection = posixpath.normpath(unquote(urlsplit(trusted_url(calendar_id)).path, errors="strict"))
        if posixpath.dirname(path) != collection:
            raise ValueError("Resource outside selected calendar")
        return target
    except ValueError as exc:
        raise YandexError("untrusted_resource") from exc


def configured():
    try:
        Fernet(os.environ["YANDEX_TOKEN_KEY"].encode())
        return True
    except (KeyError, ValueError, TypeError):
        return False


def encrypt(credentials):
    return Fernet(os.environ["YANDEX_TOKEN_KEY"].encode()).encrypt(json.dumps(credentials).encode()).decode()


def decrypt(credentials):
    return json.loads(Fernet(os.environ["YANDEX_TOKEN_KEY"].encode()).decrypt(credentials.encode()))


def retry_delay(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 5


def http_client(credentials):
    return httpx.Client(
        auth=httpx.BasicAuth(credentials["username"], credentials["password"]),
        follow_redirects=False,
        trust_env=False,
        timeout=20,
    )


def checked_status(status, headers):
    if status in (429, 408) or status >= 500:
        raise RetryableYandexError(retry_after=retry_delay(headers.get("Retry-After", "5")))
    if status == 401:
        raise YandexError("reconnect_required", reconnect=True)
    if status == 403:
        raise YandexError("calendar_read_only", reconnect=True)
    if 300 <= status < 400:
        raise YandexError("untrusted_redirect")


def request(client, method, url, **kwargs):
    try:
        response = client.request(method, trusted_url(url), follow_redirects=False, **kwargs)
    except httpx.TransportError as exc:
        raise RetryableYandexError() from exc
    if response.status_code in (403, 413) and (
        response.status_code == 413 or b"max-resource-size" in response.content
    ):
        raise YandexError("calendar_resource_too_large")
    checked_status(response.status_code, response.headers)
    return response


class GuardedSession:
    """Guard caldav's niquests transport before any authentication or redirect hop."""

    def __init__(self, session):
        self.session = session
        self.session.trust_env = False

    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        response = self.session.request(method, trusted_url(url), **kwargs)
        checked_status(response.status_code, response.headers)
        return response

    def close(self):
        self.session.close()


def dav_client(credentials):
    client = caldav.DAVClient(
        url=ORIGIN + "/",
        username=credentials["username"],
        password=credentials["password"],
        auth_type="basic",
        timeout=20,
        enable_rfc6764=False,
        require_tls=True,
        rate_limit_handle=False,
    )
    client.session = GuardedSession(client.session)
    return client


class HTTPXDAVSession:
    """Use caldav's REPORT parser/query builder over the already guarded client."""

    def __init__(self, client):
        self.client = client

    def request(self, method, url, **kwargs):
        response = request(
            self.client, method, url, content=kwargs.get("data"), headers=kwargs.get("headers")
        )
        converted = niquests.Response()
        converted.status_code = response.status_code
        converted.headers = dict(response.headers)
        converted._content = response.content
        converted.encoding = response.encoding
        converted.url = str(response.url)
        return converted

    def close(self):
        pass


def lookup_uid(client, credentials, calendar_id, uid):
    with dav_client(credentials) as davclient:
        davclient.session.close()
        davclient.session = HTTPXDAVSession(client)
        calendar = caldav.Calendar(client=davclient, url=trusted_url(calendar_id))
        try:
            item = calendar.get_event_by_uid(uid)
        except caldav.lib.error.NotFoundError:
            return None
        except caldav.lib.error.DAVError as exc:
            raise YandexError("calendar_lookup_failed") from exc
        return resource_url(item.url, calendar_id)


def writable(properties):
    privileges = properties.get(dav.CurrentUserPrivilegeSet.tag)
    components = properties.get(cdav.SupportedCalendarComponentSet.tag)
    if privileges is None or components is None:
        return False
    rights = {element.tag.rsplit("}", 1)[-1] for element in privileges.iter()}
    event_support = any(element.attrib.get("name", "").upper() == "VEVENT" for element in components.iter())
    return event_support and (
        "all" in rights or "write" in rights or {"write-content", "bind", "unbind"}.issubset(rights)
    )


def discover_calendars(credentials):
    try:
        with dav_client(credentials) as client:
            principal = client.principal()
            result = []
            for calendar in principal.calendars():
                url = trusted_url(calendar.url)
                properties = calendar.get_properties(
                    [
                        dav.DisplayName(),
                        dav.CurrentUserPrivilegeSet(),
                        cdav.SupportedCalendarComponentSet(),
                        cdav.MaxResourceSize(),
                    ],
                    parse_props=False,
                )
                if not writable(properties):
                    continue
                display = properties.get(dav.DisplayName.tag)
                entry = {
                    "id": url,
                    "name": display.text if display is not None and display.text else "Календарь",
                }
                maximum = properties.get(cdav.MaxResourceSize.tag)
                if maximum is not None and maximum.text and maximum.text.isdigit():
                    entry["max_resource_size"] = int(maximum.text)
                result.append(entry)
            return result
    except (RetryableYandexError, YandexError):
        raise
    except ValueError as exc:
        raise YandexError("untrusted_calendar") from exc
    except caldav.lib.error.AuthorizationError as exc:
        raise YandexError("reconnect_required", reconnect=True) from exc
    except Exception as exc:
        # Never expose third-party exception text, which may contain credential-bearing URLs.
        raise RetryableYandexError() from exc


COLLECTION_QUERY = b"""<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:resourcetype/><d:current-user-privilege-set/><c:supported-calendar-component-set/></d:prop></d:propfind>"""


def validate_collection(client, calendar_id):
    response = request(
        client,
        "PROPFIND",
        calendar_id,
        content=COLLECTION_QUERY,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
    )
    if response.status_code in (404, 410):
        raise YandexError("calendar_unavailable", reconnect=True)
    response.raise_for_status()
    if response.status_code != 207:
        raise YandexError("calendar_unavailable", reconnect=True)
    root = ElementTree.fromstring(response.content)
    for item in root.findall("{DAV:}response"):
        href = item.findtext("{DAV:}href")
        if not href or unquote(trusted_url(href), errors="strict").rstrip("/") != unquote(
            trusted_url(calendar_id), errors="strict"
        ).rstrip("/"):
            continue
        props = {}
        for propstat in item.findall("{DAV:}propstat"):
            if " 200 " not in (propstat.findtext("{DAV:}status") or ""):
                continue
            container = propstat.find("{DAV:}prop")
            for prop in container if container is not None else []:
                props[prop.tag] = prop
        resource_type = props.get(dav.ResourceType.tag)
        if resource_type is None or resource_type.find("{urn:ietf:params:xml:ns:caldav}calendar") is None:
            raise YandexError("calendar_unavailable", reconnect=True)
        if not writable(props):
            raise YandexError("calendar_read_only", reconnect=True)
        return
    raise YandexError("calendar_unavailable", reconnect=True)


def execute(operation):
    result = {"generation": operation["generation"], "version": operation["version"]}
    with Session() as db:
        conn = db.scalar(
            select(YandexConnection).where(YandexConnection.user_id == operation["user_id"]).with_for_update()
        )
        if (
            not configured()
            or not conn
            or conn.status != "connected"
            or conn.generation != operation["generation"]
        ):
            return {**result, "status": "disconnected"}
        journal = db.get(YandexOperation, operation["id"])
        recovering = journal is not None
        if journal and journal.result:
            if journal.result["status"] != "failed":
                return journal.result
            journal.result = None
        key = ":".join((conn.user_id, conn.generation, operation["record_id"]))
        mapping = db.get(YandexMapping, key)
        if mapping and mapping.status in ("conflict", "missing"):
            return {**result, "status": mapping.status}
        deleting = (
            (operation["operation"] == "delete" and operation.get("scope", "series") != "occurrence")
            or operation["snapshot"].get("deleted")
            or operation["snapshot"].get("status", "active") != "active"
            or operation["snapshot"].get("kind") != "meeting"
        )
        epoch = mapping.epoch if mapping and mapping.status != "deleted" else operation["id"]
        resource_uid = (
            mapping.uid
            if mapping and mapping.status != "deleted"
            else hashlib.sha256((key + ":" + epoch).encode()).hexdigest() + "@golos"
        )
        href = (
            mapping.href
            if mapping and mapping.status != "deleted"
            else trusted_url(conn.calendar_id.rstrip("/") + "/" + quote(resource_uid, safe="") + ".ics")
        )
        try:
            if not deleting:
                ical.require_minute(operation.get("occurrence_at"))
            content = (
                ""
                if deleting
                else ical.serialize(
                    operation["snapshot"], operation.get("exceptions", []), resource_uid, operation["id"]
                )
            )
        except (ValueError, KeyError, OverflowError) as exc:
            return {
                **result,
                "status": "failed",
                "code": "calendar_precision_unsupported"
                if str(exc) == "calendar_precision_unsupported"
                else "invalid_calendar_data",
            }
        selected = next((item for item in conn.calendars if item["id"] == conn.calendar_id), {})
        maximum = selected.get("max_resource_size")
        if not deleting and maximum is not None and len(content.encode()) > maximum:
            return {**result, "status": "failed", "code": "calendar_resource_too_large"}
        if not journal:
            journal = YandexOperation(
                id=operation["id"],
                intent={
                    "href": href,
                    "epoch": epoch,
                    "content": content,
                    "action": "delete" if deleting else "put",
                    "previous_etag": mapping.etag if mapping and mapping.status != "deleted" else None,
                },
            )
            db.add(journal)
            # Durable before any remote mutation. Reacquire and reload after the commit gap.
            db.commit()
            db.expire_all()
            conn = db.scalar(
                select(YandexConnection)
                .where(YandexConnection.user_id == operation["user_id"])
                .with_for_update()
            )
            journal = db.get(YandexOperation, operation["id"])
            mapping = db.get(YandexMapping, key)
            if journal.result and journal.result["status"] != "failed":
                return journal.result
            if conn.status != "connected" or conn.generation != operation["generation"]:
                return {**result, "status": "disconnected"}
        intent = journal.intent
        href, content = intent["href"], intent["content"]
        if content:
            resource_uid = ical.uid(content)

        def finish(status, code=None):
            nonlocal mapping
            journal.result = {**result, "status": status, **({"code": code} if code else {})}
            if status in ("conflict", "missing"):
                if not mapping:
                    mapping = YandexMapping(
                        key=key,
                        user_id=conn.user_id,
                        generation=conn.generation,
                        href=href,
                        uid=resource_uid,
                        epoch=intent["epoch"],
                        content=content,
                        version=operation["version"],
                    )
                    db.add(mapping)
                mapping.status = status
            db.commit()
            return journal.result

        def synced(response_content=None, etag=None, deleted=False):
            nonlocal mapping
            if not mapping:
                mapping = YandexMapping(
                    key=key,
                    user_id=conn.user_id,
                    generation=conn.generation,
                    href=href,
                    uid=resource_uid,
                    epoch=intent["epoch"],
                )
                db.add(mapping)
            mapping.href, mapping.epoch = href, intent["epoch"]
            mapping.uid = resource_uid
            mapping.content = response_content if response_content is not None else content
            mapping.etag = etag
            mapping.status = "deleted" if deleted else "synced"
            mapping.version = operation["version"]
            return finish("synced")

        try:
            credentials = decrypt(conn.credentials)
            with http_client(credentials) as client:
                current = request(client, "GET", href)
                if (
                    current.status_code in (404, 410)
                    and recovering
                    and not deleting
                    and not intent.get("previous_etag")
                ):
                    found = lookup_uid(client, credentials, conn.calendar_id, resource_uid)
                    if found:
                        href = found
                        journal.intent = {**journal.intent, "href": href}
                        current = request(client, "GET", href)
                    if current.status_code in (404, 410) and intent.get("mutation_started"):
                        # Yandex ignores If-None-Match:*. An ambiguous first PUT
                        # can only be reconciled by reads, never replayed blindly.
                        raise RetryableYandexError("Created resource is not yet discoverable")
                if current.status_code in (404, 410):
                    if deleting and intent.get("delete_started"):
                        validate_collection(client, conn.calendar_id)
                        return synced(deleted=True)
                    if mapping and mapping.status != "deleted" and mapping.href == href:
                        validate_collection(client, conn.calendar_id)
                        return finish("missing")
                    if deleting:
                        return (
                            synced(deleted=True)
                            if mapping and mapping.status == "deleted"
                            else finish("missing")
                        )
                    current_content, etag = None, None
                else:
                    current.raise_for_status()
                    current_content, etag = current.text, current.headers.get("ETag")
                    if not etag:
                        return finish("conflict", "missing_etag")
                    if (
                        not deleting
                        and intent.get("mutation_started")
                        and ical.server_equivalent(
                            current_content,
                            content,
                            intent.get("baseline"),
                            credentials["username"],
                            conn.calendar_name,
                        )
                    ):
                        return synced(current_content, etag)
                    if (
                        etag != intent.get("previous_etag")
                        or not mapping
                        or not mapping.content
                        or not ical.equivalent(current_content, mapping.content)
                    ):
                        return finish("conflict")
                if deleting:
                    if not intent.get("delete_started"):
                        journal.intent = {**intent, "delete_started": True, "delete_etag": etag}
                        db.commit()
                        db.expire_all()
                        conn = db.scalar(
                            select(YandexConnection)
                            .where(YandexConnection.user_id == operation["user_id"])
                            .with_for_update()
                        )
                        journal = db.get(YandexOperation, operation["id"])
                        if journal.result and journal.result["status"] != "failed":
                            return journal.result
                        if conn.status != "connected" or conn.generation != operation["generation"]:
                            return finish("disconnected")
                    else:
                        etag = intent["delete_etag"]
                    response = request(client, "DELETE", href, headers={"If-Match": etag})
                    if response.status_code == 412:
                        return finish("conflict")
                    if response.status_code in (404, 410):
                        return finish("missing")
                    response.raise_for_status()
                    return synced(deleted=True)
                headers = {
                    "Content-Type": "text/calendar; charset=utf-8",
                    **({"If-Match": etag} if current_content else {"If-None-Match": "*"}),
                }
                if not intent.get("mutation_started"):
                    journal.intent = {
                        **journal.intent,
                        "mutation_started": True,
                        "baseline": mapping.content if mapping and mapping.status != "deleted" else None,
                    }
                    db.commit()
                    db.expire_all()
                    conn = db.scalar(
                        select(YandexConnection)
                        .where(YandexConnection.user_id == operation["user_id"])
                        .with_for_update()
                    )
                    journal = db.get(YandexOperation, operation["id"])
                    intent = journal.intent
                    if journal.result and journal.result["status"] != "failed":
                        return journal.result
                    if conn.status != "connected" or conn.generation != operation["generation"]:
                        return finish("disconnected")
                response = request(client, "PUT", href, content=content, headers=headers)
                if response.status_code in (409, 412):
                    return finish("conflict")
                if response.status_code in (404, 410):
                    validate_collection(client, conn.calendar_id)
                    return finish("missing")
                response.raise_for_status()
                location = response.headers.get("Location") or response.headers.get("Content-Location")
                if location:
                    href = resource_url(location, conn.calendar_id, base=href)
                    journal.intent = {**journal.intent, "href": href}
                verified = request(client, "GET", href)
                if verified.status_code in (404, 410):
                    found = lookup_uid(client, credentials, conn.calendar_id, resource_uid)
                    if not found:
                        raise RetryableYandexError("Created resource is not yet discoverable")
                    href = found
                    journal.intent = {**journal.intent, "href": href}
                    verified = request(client, "GET", href)
                    if verified.status_code in (404, 410):
                        raise RetryableYandexError("Created resource is not yet readable")
                verified.raise_for_status()
                if not ical.server_equivalent(
                    verified.text,
                    content,
                    intent.get("baseline"),
                    credentials["username"],
                    conn.calendar_name,
                ) or not verified.headers.get("ETag"):
                    return finish("conflict")
                return synced(verified.text, verified.headers["ETag"])
        except RetryableYandexError:
            db.commit()
            raise
        except YandexError as exc:
            if exc.reconnect:
                conn.status = "reconnect_required"
            return finish(
                "disconnected" if exc.reconnect else "failed",
                "calendar_unavailable" if exc.code == "calendar_read_only" else exc.code,
            )
        except InvalidToken:
            conn.status = "reconnect_required"
            return finish("disconnected", "reconnect_required")
        except (httpx.HTTPError, ValueError, KeyError, ElementTree.ParseError):
            return finish("failed", "invalid_calendar_response")
