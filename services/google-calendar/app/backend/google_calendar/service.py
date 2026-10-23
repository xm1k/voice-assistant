"""Google API boundary with durable operation identity and optimistic concurrency."""

import hashlib
import json
import os
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import islice
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx
from backend.chat.domain import recurrence_rule
from backend.common.runtime import Session
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from .models import GoogleConnection, GoogleMapping, GoogleOperation

API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"


class RetryableGoogleError(Exception):
    def __init__(self, message, retry_after=5):
        super().__init__(message)
        self.retry_after = retry_after


class ReconnectRequired(Exception):
    pass


def client():
    return httpx.Client(timeout=20)


def configured():
    if not all(
        os.getenv(k)
        for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GOOGLE_TOKEN_KEY")
    ):
        return False
    try:
        cipher()
        redirect = urlparse(os.environ["GOOGLE_REDIRECT_URI"])
        return bool(
            redirect.hostname
            and not redirect.fragment
            and not redirect.username
            and (
                redirect.scheme == "https"
                or (redirect.scheme == "http" and redirect.hostname in ("localhost", "127.0.0.1", "::1"))
            )
        )
    except (ValueError, TypeError):
        return False


def retry_delay(value, now=None):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(value)
            return max(0, int((target - (now or datetime.now(timezone.utc))).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 5


def cipher():
    return Fernet(os.environ["GOOGLE_TOKEN_KEY"].encode())


def encrypt(tokens):
    return cipher().encrypt(json.dumps(tokens).encode()).decode()


def decrypt(tokens):
    return json.loads(cipher().decrypt(tokens.encode()))


def event_payload(data, operation_id):
    zone = data.get("timezone", "Europe/Moscow")
    anchor = data.get("start_at") or data.get("remind_at") or data.get("due_date")
    if not anchor:
        raise ValueError("record_has_no_date")
    body = {
        "summary": data.get("title", ""),
        "description": data.get("notes") or "",
        "extendedProperties": {"private": {"voiceOperation": operation_id}},
    }
    minutes = data.get("reminder_minutes")
    body["reminders"] = {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": minutes}]
        if minutes is not None and minutes <= 40320
        else [],
    }
    if len(anchor) == 10:
        body.update(
            start={"date": anchor}, end={"date": (date.fromisoformat(anchor) + timedelta(days=1)).isoformat()}
        )
    else:
        start = datetime.fromisoformat(anchor.replace("Z", "+00:00")).astimezone(ZoneInfo(zone))
        end = data.get("end_at") or (start + timedelta(minutes=30)).isoformat()
        body.update(
            start={"dateTime": start.isoformat(), "timeZone": zone}, end={"dateTime": end, "timeZone": zone}
        )
    rec = data.get("recurrence")
    if rec:
        rule = ["FREQ=" + rec["frequency"].upper(), "INTERVAL=" + str(rec.get("interval", 1))]
        if rec.get("count") and rec.get("until"):
            origin = (
                datetime.combine(date.fromisoformat(anchor), time.min, ZoneInfo(zone))
                if len(anchor) == 10
                else start
            )
            bounded = {**data, "recurrence": {key: value for key, value in rec.items() if key != "count"}}
            count = sum(1 for _ in islice(recurrence_rule(bounded, origin), rec["count"]))
            if not count:
                raise ValueError("empty_recurrence")
            rule.append("COUNT=" + str(count))
        elif rec.get("count"):
            rule.append("COUNT=" + str(rec["count"]))
        elif rec.get("until"):
            until = datetime.combine(
                date.fromisoformat(rec["until"]), time(23, 59, 59), ZoneInfo(zone)
            ).astimezone(timezone.utc)
            rule.append("UNTIL=" + until.strftime("%Y%m%dT%H%M%SZ"))
        if rec.get("weekdays"):
            rule.append(
                "BYDAY=" + ",".join(["MO", "TU", "WE", "TH", "FR", "SA", "SU"][n] for n in rec["weekdays"])
            )
        body["recurrence"] = ["RRULE:" + ";".join(rule)]
    return body


def request(c, conn, method, path, **kwargs):
    tokens = decrypt(conn.tokens)
    headers = {"Authorization": "Bearer " + tokens["access_token"], **kwargs.pop("headers", {})}
    response = c.request(method, API + path, headers=headers, **kwargs)
    if response.status_code == 401 and tokens.get("refresh_token"):
        refreshed = c.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "refresh_token": tokens["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if refreshed.status_code in (401, 403) or (
            refreshed.status_code == 400 and refreshed.json().get("error") == "invalid_grant"
        ):
            conn.status = "reconnect_required"
            raise ReconnectRequired()
        refreshed.raise_for_status()
        tokens.update(refreshed.json())
        conn.tokens = encrypt(tokens)
        headers["Authorization"] = "Bearer " + tokens["access_token"]
        response = c.request(method, API + path, headers=headers, **kwargs)
    if retryable_response(response):
        response.raise_for_status()
    if response.status_code in (401, 403):
        conn.status = "reconnect_required"
        raise ReconnectRequired()
    return response


def retryable_response(response):
    if response.status_code == 429 or response.status_code >= 500:
        return True
    if response.status_code != 403:
        return False
    try:
        errors = response.json().get("error", {}).get("errors", [])
        return any(
            error.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded", "backendError")
            for error in errors
        )
    except (ValueError, AttributeError):
        return False


def payload_matches(current, expected):
    defaults = {
        "location": "",
        "attendees": [],
        "attachments": [],
        "conferenceData": None,
        "colorId": None,
        "visibility": "default",
        "transparency": "opaque",
        "guestsCanInviteOthers": True,
        "guestsCanModify": False,
        "guestsCanSeeOtherGuests": True,
        "anyoneCanAddSelf": False,
        "source": None,
        "focusTimeProperties": None,
        "outOfOfficeProperties": None,
        "workingLocationProperties": None,
        "birthdayProperties": None,
        "eventType": "default",
    }
    for field, default in defaults.items():
        if current.get(field, default) != expected.get(field, default):
            return False

    def external_properties(body):
        properties = deepcopy(body.get("extendedProperties", {}))
        private = properties.get("private", {})
        for marker in ("voiceOperation", "voiceDeleteOperation", "voiceDetachedBy"):
            private.pop(marker, None)
        return private, properties.get("shared", {})

    if external_properties(current) != external_properties(expected):
        return False
    if current.get("status", "confirmed") != expected.get("status", "confirmed"):
        return False
    for key in ("summary", "recurrence"):
        if current.get(key) != expected.get(key):
            return False
    if (current.get("description") or "") != (expected.get("description") or ""):
        return False

    def reminders(body):
        value = body.get("reminders", {})
        return bool(value.get("useDefault", False)), sorted(
            (item.get("method"), item.get("minutes")) for item in value.get("overrides", [])
        )

    if reminders(current) != reminders(expected):
        return False
    for boundary in ("start", "end"):
        actual = current.get(boundary, {})
        wanted = expected.get(boundary, {})
        if not wanted:
            if actual:
                return False
            continue
        if actual.get("timeZone") != wanted.get("timeZone"):
            return False
        if "date" in wanted:
            if actual.get("date") != wanted["date"]:
                return False
        elif not actual.get("dateTime") or datetime.fromisoformat(
            actual["dateTime"].replace("Z", "+00:00")
        ) != datetime.fromisoformat(wanted["dateTime"].replace("Z", "+00:00")):
            return False
    return True


def original_stamp(item):
    value = item.get("originalStartTime", {})
    stamp = value.get("dateTime") or value.get("date")
    if stamp and len(stamp) != 10:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    return stamp


def operation_payload(operation, stamp=None):
    data = dict(operation["snapshot"])
    if stamp:
        anchor = "start_at" if data.get("start_at") else "remind_at" if data.get("remind_at") else "due_date"
        if data.get("end_at"):
            duration = datetime.fromisoformat(data["end_at"].replace("Z", "+00:00")) - datetime.fromisoformat(
                data["start_at"].replace("Z", "+00:00")
            )
            data["end_at"] = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) + duration).isoformat()
        data[anchor] = stamp
        for exception in operation.get("exceptions", []):
            original = exception["occurrence_at"]
            normalized = original_stamp(
                {"originalStartTime": {"date" if len(original) == 10 else "dateTime": original}}
            )
            if normalized == stamp:
                data.update(exception["data"])
                break
    body = event_payload(data, operation["id"])
    if stamp:
        body.pop("recurrence", None)
    elif body.get("recurrence"):
        for detached in operation.get("_detached_stamps", []):
            if stamp_in_rule(data, detached):
                exclusion = (
                    "EXDATE;VALUE=DATE:" + detached.replace("-", "")
                    if len(detached) == 10
                    else "EXDATE:"
                    + datetime.fromisoformat(detached.replace("Z", "+00:00"))
                    .astimezone(timezone.utc)
                    .strftime("%Y%m%dT%H%M%SZ")
                )
                body["recurrence"].append(exclusion)
    return body


def stamp_in_rule(data, stamp):
    if not data.get("recurrence"):
        return False
    zone = ZoneInfo(data.get("timezone", "Europe/Moscow"))
    anchor = data.get("start_at") or data.get("remind_at") or data.get("due_date")
    origin = (
        datetime.combine(date.fromisoformat(anchor), time.min, zone)
        if len(anchor) == 10
        else datetime.fromisoformat(anchor.replace("Z", "+00:00")).astimezone(zone)
    )
    target = (
        datetime.combine(date.fromisoformat(stamp), time.min, zone)
        if len(stamp) == 10
        else datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(zone)
    )
    return recurrence_rule(data, origin).before(target, inc=True) == target


def inherited_matches(item, operation, stamp, journal):
    preflight = journal.intent.get("preflight", {}) if journal else {}
    old = preflight.get("instances", {}).get(stamp)
    if not old:
        return False
    if payload_matches(item, old):
        return True
    desired = operation_payload(operation, stamp)
    master = preflight.get("master", {})
    predicted = deepcopy(old)
    # Only fields inherited from the old master may acquire the new master value.
    for field in ("summary", "description", "reminders"):
        old_value = old.get(field) or ("" if field == "description" else {})
        parent_value = master.get(field) or ("" if field == "description" else {})
        if old_value == parent_value:
            predicted[field] = desired.get(field)
    sparse = next(
        (
            x["data"]
            for x in operation.get("exceptions", [])
            if original_stamp(
                {
                    "originalStartTime": {
                        "date" if len(x["occurrence_at"]) == 10 else "dateTime": x["occurrence_at"]
                    }
                }
            )
            == stamp
        ),
        {},
    )
    for field, source in (("start", "start_at"), ("end", "end_at")):
        if source not in sparse and "due_date" not in sparse and "remind_at" not in sparse:
            predicted[field] = desired[field]
    return payload_matches(item, predicted)


def detached_exception(db, c, conn, path, key, parent_id, operation, exception, known):
    stamp = original_stamp(
        {
            "originalStartTime": {
                "date" if len(exception["occurrence_at"]) == 10 else "dateTime": exception["occurrence_at"]
            }
        }
    )
    journal = db.get(GoogleOperation, operation["id"])
    if known and not known.detached:
        old = request(c, conn, "GET", path + "/" + known.event_id)
        if old.status_code not in (404, 410):
            old.raise_for_status()
            current = old.json()
            own = (
                current.get("extendedProperties", {}).get("private", {}).get("voiceDetachedBy")
                == operation["id"]
            )
            if (
                not own
                and current.get("etag") != known.etag
                and not inherited_matches(current, operation, stamp, journal)
            ):
                return "conflict"
            if not own:
                cancelled = request(
                    c,
                    conn,
                    "PATCH",
                    path + "/" + known.event_id,
                    headers={"If-Match": current["etag"]},
                    json={
                        "status": "cancelled",
                        "extendedProperties": {"private": {"voiceDetachedBy": operation["id"]}},
                    },
                )
                if cancelled.status_code == 412:
                    return "conflict"
                cancelled.raise_for_status()
        known.source_event_id = known.event_id
        known.event_id = hashlib.sha256(
            (key + ":" + stamp + ":" + parent_id + ":detached").encode()
        ).hexdigest()
        known.detached = True
        known.etag = None
    if not known:
        known = GoogleMapping(
            key=key + ":" + stamp,
            user_id=conn.user_id,
            generation=conn.generation,
            event_id=hashlib.sha256((key + ":" + stamp + ":" + parent_id + ":detached").encode()).hexdigest(),
            detached=True,
        )
        db.add(known)
    response = request(c, conn, "GET", path + "/" + known.event_id)
    if response.status_code in (404, 410):
        if known.etag:
            return "missing"
        current = None
    else:
        response.raise_for_status()
        current = response.json()
    deleting = exception["data"].get("deleted") or not operation["snapshot"].get("recurrence")
    if deleting and not current:
        known.status = "deleted"
        return "synced"
    body = (
        {"status": "cancelled", "extendedProperties": {"private": {"voiceOperation": operation["id"]}}}
        if deleting
        else operation_payload(operation, stamp)
    )
    if current:
        own = (
            current.get("extendedProperties", {}).get("private", {}).get("voiceOperation") == operation["id"]
        )
        if own:
            if (deleting and current.get("status") != "cancelled") or (
                not deleting and not payload_matches(current, body)
            ):
                return "conflict"
            known.etag = current.get("etag")
            known.status = "deleted" if deleting else "synced"
            return "synced"
        if current.get("etag") != known.etag:
            return "conflict"
        response = request(
            c,
            conn,
            "PATCH" if deleting else "PUT",
            path + "/" + known.event_id,
            json=body,
            headers={"If-Match": known.etag},
        )
    else:
        body["id"] = known.event_id
        response = request(c, conn, "POST", path, json=body)
    if response.status_code == 412:
        return "conflict"
    response.raise_for_status()
    known.etag = response.json().get("etag")
    known.status = "deleted" if deleting else "synced"
    return "synced"


def check_exceptions(
    db, c, conn, path, key, event_id, operation_id, *, capture=None, operation=None, journal=None
):
    """Series ETags do not fence edits to individual recurring instances."""
    page = None
    while True:
        params = {"showDeleted": "true", "singleEvents": "false", "maxResults": 2500}
        if page:
            params["pageToken"] = page
        response = request(c, conn, "GET", path, params=params)
        response.raise_for_status()
        for item in response.json().get("items", []):
            if item.get("recurringEventId") != event_id:
                continue
            stamp = original_stamp(item)
            known = db.get(GoogleMapping, key + ":" + stamp) if stamp else None
            own = item.get("extendedProperties", {}).get("private", {}).get("voiceOperation") == operation_id
            inherited = known and operation and inherited_matches(item, operation, stamp, journal)
            detached_source = (
                known
                and known.detached
                and known.source_event_id == item["id"]
                and item.get("status") == "cancelled"
            )
            if (
                not own
                and not inherited
                and not detached_source
                and (not known or known.etag != item.get("etag"))
            ):
                return False
            if capture is not None and known and not known.detached:
                capture[stamp] = item
        page = response.json().get("nextPageToken")
        if not page:
            return True


def apply_exceptions(db, c, conn, path, key, event_id, operation):
    """Apply sparse overrides to their original instances after a series write."""
    for exception in operation.get("exceptions", []):
        stamp = exception["occurrence_at"]
        target_stamp = original_stamp(
            {"originalStartTime": {"date" if len(stamp) == 10 else "dateTime": stamp}}
        )
        known = db.get(GoogleMapping, key + ":" + target_stamp)
        if (known and known.detached) or not stamp_in_rule(operation["snapshot"], target_stamp):
            # Nonrecurring records ignore exceptions, as domain.expand does.
            if not operation["snapshot"].get("recurrence") and not (known and known.detached):
                continue
            outcome = detached_exception(db, c, conn, path, key, event_id, operation, exception, known)
            if outcome != "synced":
                return outcome
            continue
        lookup = request(
            c,
            conn,
            "GET",
            path + "/" + event_id + "/instances",
            params={"originalStart": stamp, "showDeleted": "true"},
        )
        lookup.raise_for_status()
        target_stamp = original_stamp(
            {"originalStartTime": {"date" if len(stamp) == 10 else "dateTime": stamp}}
        )
        items = [item for item in lookup.json().get("items", []) if original_stamp(item) == target_stamp]
        if not items:
            return "missing"
        item = items[0]
        instance_key = key + ":" + target_stamp
        known = db.get(GoogleMapping, instance_key)
        own = item.get("extendedProperties", {}).get("private", {}).get("voiceOperation") == operation["id"]
        if (
            known
            and not own
            and known.etag != item.get("etag")
            and not inherited_matches(item, operation, target_stamp, db.get(GoogleOperation, operation["id"]))
        ):
            return "conflict"
        if not known:
            known = GoogleMapping(
                key=instance_key,
                user_id=conn.user_id,
                generation=conn.generation,
                event_id=item["id"],
                etag=item.get("etag"),
            )
            db.add(known)
        if own:
            override = exception["data"]
            complete = (
                item.get("status") == "cancelled"
                if override.get("deleted")
                else payload_matches(
                    item, operation_payload({**operation, "exceptions": [exception]}, target_stamp)
                )
            )
            if complete:
                known.etag = item.get("etag")
                continue
            generated = operation_payload({**operation, "exceptions": []}, target_stamp)
            if not inherited_matches(
                item, operation, target_stamp, db.get(GoogleOperation, operation["id"])
            ) and not payload_matches(item, generated):
                return "conflict"
        override = exception["data"]
        url = path + "/" + item["id"]
        headers = {"If-Match": item["etag"]}
        if override.get("deleted"):
            # Cancelled instances stay addressable and retain the operation marker.
            body = {
                "status": "cancelled",
                "extendedProperties": {"private": {"voiceOperation": operation["id"]}},
            }
            response = request(c, conn, "PATCH", url, json=body, headers=headers)
        else:
            data = dict(operation["snapshot"])
            anchor = (
                "start_at" if data.get("start_at") else "remind_at" if data.get("remind_at") else "due_date"
            )
            if data.get("end_at"):
                duration = datetime.fromisoformat(
                    data["end_at"].replace("Z", "+00:00")
                ) - datetime.fromisoformat(data["start_at"].replace("Z", "+00:00"))
                data["end_at"] = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) + duration).isoformat()
            data[anchor] = stamp
            data.update(override)
            body = event_payload(data, operation["id"])
            body.pop("recurrence", None)
            response = request(c, conn, "PATCH", url, json=body, headers=headers)
        if response.status_code == 412:
            return "conflict"
        if response.status_code in (404, 410):
            return "missing"
        response.raise_for_status()
        known.etag = response.json().get("etag")
    return "synced"


def cancel_detached(db, c, conn, path, key, parent_id, operation):
    for child in db.scalars(
        select(GoogleMapping).where(
            GoogleMapping.user_id == conn.user_id,
            GoogleMapping.generation == conn.generation,
            GoogleMapping.detached.is_(True),
        )
    ):
        if not child.key.startswith(key + ":") or child.status == "deleted":
            continue
        exception = {"occurrence_at": child.key[len(key) + 1 :], "data": {"deleted": True}}
        result = detached_exception(db, c, conn, path, key, parent_id, operation, exception, child)
        if result != "synced":
            return result
    return "synced"


def execute(operation):
    result = {"generation": operation["generation"], "version": operation["version"]}
    with Session() as db:
        conn = db.scalar(
            select(GoogleConnection).where(GoogleConnection.user_id == operation["user_id"]).with_for_update()
        )
        if not conn or conn.status != "connected" or conn.generation != operation["generation"]:
            return {**result, "status": "disconnected"}
        journal = db.get(GoogleOperation, operation["id"])
        if journal and journal.result:
            return journal.result
        key = ":".join((conn.user_id, str(conn.generation), operation["record_id"]))
        detached_stamps = [
            item.key[len(key) + 1 :]
            for item in db.scalars(
                select(GoogleMapping).where(
                    GoogleMapping.user_id == conn.user_id,
                    GoogleMapping.generation == conn.generation,
                    GoogleMapping.detached.is_(True),
                )
            )
            if item.key.startswith(key + ":")
        ]
        operation = {**operation, "_detached_stamps": detached_stamps}
        stamp = operation.get("occurrence_at") if operation.get("scope") == "occurrence" else None
        if stamp:
            stamp = original_stamp({"originalStartTime": {"date" if len(stamp) == 10 else "dateTime": stamp}})
        mapping = db.get(GoogleMapping, key + (":" + stamp if stamp else ""))
        parent = db.get(GoogleMapping, key)
        if parent and parent.status in ("conflict", "missing"):
            return {**result, "status": parent.status}
        if mapping and mapping.status in ("conflict", "missing"):
            return {**result, "status": mapping.status}
        if not journal:
            journal = GoogleOperation(id=operation["id"], intent={})
            db.add(journal)
            db.flush()
        deleting = (
            operation["operation"] == "delete"
            or operation.get("snapshot", {}).get("deleted")
            or operation.get("snapshot", {}).get("status", "active") != "active"
            or operation.get("snapshot", {}).get("kind", "meeting") != "meeting"
        )
        if stamp:
            for exception in operation.get("exceptions", []):
                value = exception["occurrence_at"]
                if original_stamp(
                    {"originalStartTime": {"date" if len(value) == 10 else "dateTime": value}}
                ) == stamp and (
                    exception["data"].get("deleted") or exception["data"].get("status", "active") != "active"
                ):
                    deleting = True
        restoring_instance = bool(
            stamp and mapping and not mapping.detached and mapping.status == "deleted" and not deleting
        )
        if mapping and mapping.status == "deleted" and (not stamp or mapping.detached):
            if deleting:
                return {**result, "status": "synced"}
            mapping.event_id = hashlib.sha256((key + operation["id"]).encode()).hexdigest()
            mapping.etag = None
            mapping.status = "syncing"

        def finish(status, code=None):
            if (
                status == "synced"
                and not code
                and any(
                    (data.get("reminder_minutes") or 0) > 40320
                    for data in [
                        operation.get("snapshot", {}),
                        *(x["data"] for x in operation.get("exceptions", [])),
                    ]
                )
            ):
                code = "reminder_local_only"
            journal.result = {**result, "status": status, **({"code": code} if code else {})}
            if mapping and status in ("conflict", "missing"):
                mapping.status = status
            db.commit()
            return journal.result

        try:
            with client() as c:
                path = "/calendars/" + quote(conn.calendar_id, safe="") + "/events"
                event_id = mapping.event_id if mapping else hashlib.sha256(key.encode()).hexdigest()
                if stamp:
                    if not parent:
                        return finish("missing")
                    master = request(c, conn, "GET", path + "/" + parent.event_id)
                    if master.status_code in (404, 410):
                        parent.status = "missing"
                        return finish("missing")
                    master.raise_for_status()
                    if master.json().get("etag") != parent.etag:
                        parent.status = "conflict"
                        return finish("conflict")
                if stamp and not mapping:
                    if not parent:
                        return finish("missing")
                    if not check_exceptions(db, c, conn, path, key, parent.event_id, operation["id"]):
                        parent.status = "conflict"
                        return finish("conflict")
                    instances = request(
                        c,
                        conn,
                        "GET",
                        path + "/" + parent.event_id + "/instances",
                        params={"originalStart": stamp, "showDeleted": "true"},
                    )
                    instances.raise_for_status()
                    items = instances.json().get("items", [])
                    matches = [x for x in items if original_stamp(x) == stamp]
                    if not matches:
                        return finish("missing")
                    item = matches[0]
                    event_id = item["id"]
                    mapping = GoogleMapping(
                        key=key + ":" + stamp,
                        user_id=conn.user_id,
                        generation=conn.generation,
                        event_id=event_id,
                        etag=item.get("etag"),
                    )
                    db.add(mapping)
                url = path + "/" + event_id
                existing = request(c, conn, "GET", url)
                if existing.status_code in (404, 410):
                    if mapping and mapping.etag:
                        calendar = request(c, conn, "GET", "/calendars/" + quote(conn.calendar_id, safe=""))
                        if calendar.status_code in (403, 404, 410):
                            conn.status = "reconnect_required"
                            return finish("disconnected", "calendar_unavailable")
                        calendar.raise_for_status()
                        if deleting and journal.intent.get("delete_started"):
                            mapping.status = "deleted"
                            return finish(
                                cancel_detached(db, c, conn, path, key, event_id, operation)
                                if not stamp
                                else "synced"
                            )
                        return finish("missing")
                    current = None
                else:
                    existing.raise_for_status()
                    current = existing.json()
                    master_owned = (
                        current.get("extendedProperties", {}).get("private", {}).get("voiceOperation")
                        == operation["id"]
                    )
                    if (
                        not stamp
                        and mapping
                        and (current.get("recurrence") or operation.get("snapshot", {}).get("recurrence"))
                    ):
                        captured = {}
                        if not check_exceptions(
                            db,
                            c,
                            conn,
                            path,
                            key,
                            event_id,
                            operation["id"],
                            capture=captured if not master_owned else None,
                            operation=operation if master_owned else None,
                            journal=journal,
                        ):
                            return finish("conflict")
                        if not master_owned and not deleting and "preflight" not in journal.intent:
                            journal.intent = {
                                **journal.intent,
                                "preflight": {"master": current, "instances": captured},
                            }
                            # Baselines must survive a crash immediately after Google accepts PUT.
                            db.commit()
                            conn = db.scalar(
                                select(GoogleConnection)
                                .where(GoogleConnection.user_id == operation["user_id"])
                                .with_for_update()
                            )
                            if conn.status != "connected" or conn.generation != operation["generation"]:
                                return finish("disconnected")
                            fresh = request(c, conn, "GET", url)
                            fresh.raise_for_status()
                            if fresh.json().get("etag") != current.get("etag") or not check_exceptions(
                                db, c, conn, path, key, event_id, operation["id"]
                            ):
                                return finish("conflict")
                    if (
                        current.get("extendedProperties", {}).get("private", {}).get("voiceOperation")
                        == operation["id"]
                    ):
                        if not deleting and not payload_matches(current, operation_payload(operation, stamp)):
                            return finish("conflict")
                        if not mapping:
                            mapping = GoogleMapping(
                                key=key, user_id=conn.user_id, generation=conn.generation, event_id=event_id
                            )
                            db.add(mapping)
                        mapping.etag = current.get("etag")
                        mapping.status = "synced"
                        return finish(
                            apply_exceptions(db, c, conn, path, key, event_id, operation)
                            if not stamp
                            else "synced"
                        )
                    own_delete = (
                        current.get("extendedProperties", {}).get("private", {}).get("voiceDeleteOperation")
                        == operation["id"]
                    )
                    if own_delete and stamp and current.get("status") == "cancelled":
                        expected = operation_payload(operation, stamp)
                        expected["status"] = "cancelled"
                        if not payload_matches(current, expected):
                            return finish("conflict")
                        mapping.etag, mapping.status = current.get("etag"), "deleted"
                        return finish("synced")
                    if own_delete:
                        delete_etag = journal.intent.get("delete_etag")
                        if (delete_etag and current.get("etag") != delete_etag) or (
                            not delete_etag
                            and not payload_matches(current, operation_payload(operation, stamp))
                        ):
                            return finish("conflict")
                    if mapping and current.get("etag") != mapping.etag and not own_delete:
                        return finish("conflict")
                    if own_delete:
                        mapping.etag = current.get("etag")
                    if not mapping and current:
                        return finish("conflict")
                headers = {"If-Match": mapping.etag} if mapping and mapping.etag else {}
                if deleting and stamp:
                    # Keep the cancelled instance's returned version for a later restoration.
                    response = request(
                        c,
                        conn,
                        "PATCH",
                        url,
                        headers=headers,
                        json={
                            "status": "cancelled",
                            "extendedProperties": {"private": {"voiceDeleteOperation": operation["id"]}},
                        },
                    )
                elif deleting:
                    if not mapping:
                        return finish("missing")
                    marker = request(
                        c,
                        conn,
                        "PATCH",
                        url,
                        json={"extendedProperties": {"private": {"voiceDeleteOperation": operation["id"]}}},
                        headers=headers,
                    )
                    if marker.status_code == 412:
                        return finish("conflict")
                    marker.raise_for_status()
                    mapping.etag = marker.json()["etag"]
                    headers = {"If-Match": mapping.etag}
                    journal.intent = {"delete_started": True, "delete_etag": mapping.etag}
                    db.commit()
                    conn = db.scalar(
                        select(GoogleConnection)
                        .where(GoogleConnection.user_id == operation["user_id"])
                        .with_for_update()
                    )
                    if conn.status != "connected" or conn.generation != operation["generation"]:
                        return finish("disconnected")
                    response = request(c, conn, "DELETE", url, headers=headers)
                else:
                    body = operation_payload(operation, stamp)
                    if restoring_instance:
                        body["status"] = "confirmed"
                        response = request(c, conn, "PATCH", url, json=body, headers=headers)
                    elif mapping and mapping.etag:
                        response = request(c, conn, "PUT", url, json=body, headers=headers)
                    else:
                        body["id"] = event_id
                        response = request(c, conn, "POST", path, json=body)
                if response.status_code == 412:
                    return finish("conflict")
                if response.status_code in (404, 410):
                    return finish("missing")
                if response.status_code in (401, 403):
                    conn.status = "reconnect_required"
                    return finish("disconnected", "reconnect_required")
                response.raise_for_status()
                if not mapping:
                    mapping = GoogleMapping(
                        key=key, user_id=conn.user_id, generation=conn.generation, event_id=event_id
                    )
                    db.add(mapping)
                mapping.etag = response.json().get("etag") if response.content else mapping.etag
                mapping.status = "deleted" if deleting else "synced"
                return finish(
                    apply_exceptions(db, c, conn, path, key, event_id, operation)
                    if not stamp and not deleting
                    else cancel_detached(db, c, conn, path, key, event_id, operation)
                    if not stamp
                    else "synced"
                )
        except (ReconnectRequired, InvalidToken):
            conn.status = "reconnect_required"
            return finish("disconnected", "reconnect_required")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            # Retain intent, but no terminal receipt: redelivery must reconcile remote state.
            db.commit()
            if isinstance(exc, httpx.TransportError):
                raise RetryableGoogleError(type(exc).__name__) from exc
            if isinstance(exc, httpx.HTTPStatusError) and retryable_response(exc.response):
                value = exc.response.headers.get("Retry-After", "5")
                raise RetryableGoogleError(type(exc).__name__, retry_delay(value)) from exc
            return {**result, "status": "failed", "code": type(exc).__name__}
