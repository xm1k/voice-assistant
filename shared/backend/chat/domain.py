"""Domain operations. Every write is owned here and participates in caller transaction."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, rrule
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from backend.chat.models import Chat, Event, ExceptionRecord, Outbox, Record
from backend.common.errors import validation_detail
from backend.common.runtime import now, uid
from backend.common.schemas import Query, RecordData


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical(value):
    if len(value) == 10:
        return date.fromisoformat(value).isoformat()
    return instant(value).astimezone(timezone.utc).isoformat()


def recurrence_rule(data, origin):
    recurrence = data["recurrence"]
    options = {"dtstart": origin, "interval": recurrence.get("interval", 1)}
    if recurrence.get("count"):
        options["count"] = recurrence["count"]
    if recurrence.get("until"):
        options["until"] = datetime.combine(date.fromisoformat(recurrence["until"]), time.max, origin.tzinfo)
    if recurrence.get("weekdays"):
        options["byweekday"] = recurrence["weekdays"]
    return rrule({"daily": DAILY, "weekly": WEEKLY, "monthly": MONTHLY}[recurrence["frequency"]], **options)


def expand(data, start, end, exceptions, *, period_timezone=None):
    zone = ZoneInfo(data.get("timezone", "Europe/Moscow"))
    anchor_key = "start_at" if data.get("start_at") else "remind_at" if data.get("remind_at") else "due_date"
    anchor = data.get(anchor_key)
    if not anchor:
        return []
    window_zone = ZoneInfo(period_timezone) if period_timezone and anchor_key != "due_date" else zone
    lower = datetime.combine(date.fromisoformat(start), time.min, window_zone)
    upper = datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, window_zone)
    origin = (
        datetime.combine(date.fromisoformat(anchor), time.min, zone)
        if anchor_key == "due_date"
        else instant(anchor).astimezone(zone)
    )
    recurrence = data.get("recurrence")
    if not recurrence:
        finish = instant(data["end_at"]).astimezone(zone) if data.get("end_at") else origin
        return [data] if origin < upper and (finish >= lower if finish == origin else finish > lower) else []
    rule = recurrence_rule(data, origin)
    duration = (instant(data["end_at"]) - instant(data["start_at"])) if data.get("end_at") else timedelta()
    results = []
    for occurrence in rule.between(lower - duration, upper, inc=True):
        if occurrence >= upper or occurrence + duration < lower:
            continue
        stamp = (
            occurrence.date().isoformat()
            if anchor_key == "due_date"
            else occurrence.astimezone(timezone.utc).isoformat()
        )
        item = {**data, anchor_key: stamp, "occurrence_at": stamp}
        if data.get("end_at"):
            item["end_at"] = (occurrence + duration).astimezone(timezone.utc).isoformat()
        override = exceptions.get(stamp, {})
        if override.get("deleted"):
            continue
        item.update({k: v for k, v in override.items() if k != "deleted"})
        moved = (
            datetime.combine(date.fromisoformat(item[anchor_key]), time.min, zone)
            if anchor_key == "due_date"
            else instant(item[anchor_key]).astimezone(zone)
        )
        moved_end = instant(item["end_at"]).astimezone(zone) if item.get("end_at") else moved
        if moved < upper and (moved_end >= lower if moved == moved_end else moved_end > lower):
            results.append(item)
        if len(results) > 10000:
            raise HTTPException(422, "Too many occurrences; narrow the period")
    # Moved occurrences whose original date is outside this period must also appear.
    for stamp, override in exceptions.items():
        if override.get("deleted") or any(x.get("occurrence_at") == stamp for x in results):
            continue
        # Rebuild this occurrence from the current series before applying sparse overrides.
        original = (
            datetime.combine(date.fromisoformat(stamp), time.min, zone)
            if anchor_key == "due_date"
            else instant(stamp).astimezone(zone)
        )
        item = {**data, anchor_key: stamp, "occurrence_at": stamp}
        if data.get("end_at"):
            item["end_at"] = (original + duration).astimezone(timezone.utc).isoformat()
        item.update(override)
        moved = (
            datetime.combine(date.fromisoformat(item[anchor_key]), time.min, zone)
            if anchor_key == "due_date"
            else instant(item[anchor_key]).astimezone(zone)
        )
        moved_end = instant(item["end_at"]).astimezone(zone) if item.get("end_at") else moved
        if moved < upper and (moved_end >= lower if moved == moved_end else moved_end > lower):
            results.append(item)
    return results


def event(db, user_id, event_type, data):
    # All producers take this lock before assigning timestamps. Commit order per user
    # therefore matches the WebSocket high-water mark, including notification reads.
    db.scalar(select(Chat).where(Chat.id == user_id).with_for_update())
    item = Event(id=uid(), user_id=user_id, type=event_type, data=data, created_at=now())
    db.add(item)
    return item


def record_output(record):
    return {
        **record.data,
        "id": record.id,
        "version": record.version,
        "created_at": record.created_at.replace(tzinfo=record.created_at.tzinfo or timezone.utc).isoformat()
        if record.created_at
        else None,
    }


def resolve_record(db, user_id, record_id, *, version=None, occurrence_at=None):
    """Resolve an owned current record or an exact, non-deleted recurrence occurrence."""
    row = db.scalar(
        select(Record).where(Record.id == record_id, Record.user_id == user_id, Record.deleted.is_(False))
    )
    if row is None:
        raise HTTPException(404, "Запись больше недоступна")
    if version is not None and row.version != version:
        raise HTTPException(409, "Запись изменилась. Повторите запрос, чтобы получить актуальные данные.")
    data = record_output(row)
    if occurrence_at is None:
        return data
    if not data.get("recurrence"):
        raise HTTPException(404, "Повторение не найдено")
    stamp = canonical(occurrence_at)
    original_day = date.fromisoformat(stamp[:10])
    original = next(
        (
            item
            for item in expand(
                data, str(original_day - timedelta(days=1)), str(original_day + timedelta(days=1)), {}
            )
            if item.get("occurrence_at") == stamp
        ),
        None,
    )
    if original is None:
        raise HTTPException(404, "Повторение не найдено")
    exception = db.scalar(
        select(ExceptionRecord).where(
            ExceptionRecord.record_id == row.id, ExceptionRecord.occurrence_at == stamp
        )
    )
    if exception and exception.data.get("deleted"):
        raise HTTPException(404, "Повторение удалено")
    if exception:
        original.update(exception.data)
    return original


def record_key(item):
    value = item.get("due_date") or item.get("start_at") or item.get("remind_at")
    if value:
        moment = (
            datetime.combine(
                date.fromisoformat(value), time.min, ZoneInfo(item.get("timezone", "Europe/Moscow"))
            )
            if len(value) == 10
            else instant(value)
        )
        value = moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return (
        value or "0000",
        item["id"],
        canonical(item["occurrence_at"]) if item.get("occurrence_at") else "",
    )


def records(db, user_id, query: Query, timezone_name="Europe/Moscow", *, default_recurrence_window=False):
    rows = db.scalars(select(Record).where(Record.user_id == user_id, Record.deleted.is_(False))).all()
    output = []
    for row in rows:
        data = record_output(row)
        if query.kind and data["kind"] != query.kind:
            continue
        if (
            query.text
            and query.operation != "search"
            and query.text.casefold() not in (data["title"] + " " + (data.get("notes") or "")).casefold()
        ):
            continue
        overrides = {
            x.occurrence_at: x.data
            for x in db.scalars(select(ExceptionRecord).where(ExceptionRecord.record_id == row.id))
        }
        if query.period_start:
            candidates = expand(
                data, str(query.period_start), str(query.period_end), overrides, period_timezone=timezone_name
            )
        elif data.get("recurrence"):
            if query.operation not in ("nearest", "latest") and not default_recurrence_window:
                raise HTTPException(422, "Укажите период для повторяющихся записей")
            today = now().astimezone(ZoneInfo(timezone_name)).date()
            a, b = (
                (today, today + timedelta(days=366))
                if query.operation == "nearest"
                else (today - timedelta(days=366), today)
            )
            if default_recurrence_window:
                a, b = today - timedelta(days=183), today + timedelta(days=183)
            candidates = expand(data, str(a), str(b), overrides, period_timezone=timezone_name)
        else:
            candidates = [data]
        output.extend(x for x in candidates if (not query.status or x["status"] == query.status))
    output.sort(key=record_key)
    if query.operation in ("nearest", "latest"):
        current = now().astimezone(ZoneInfo(timezone_name))

        def at(item):
            value = item.get("start_at") or item.get("remind_at") or item.get("due_date")
            return (
                instant(value)
                if value and len(value) > 10
                else datetime.combine(date.fromisoformat(value), time.min, current.tzinfo)
                if value
                else None
            )

        output = [
            x
            for x in output
            if at(x) and (at(x) >= current if query.operation == "nearest" else at(x) <= current)
        ]
        return output[:1] if query.operation == "nearest" else output[-1:]
    return output


def query_records(db, user_id, query, timezone_name):
    if query.operation == "free_slots":
        today = now().astimezone(ZoneInfo(timezone_name)).date()
        a = query.period_start or today
        b = query.period_end or a + timedelta(days=6)
        meetings = records(
            db, user_id, Query(kind="meeting", status="active", period_start=a, period_end=b), timezone_name
        )
        slots = []
        for day in (a + timedelta(days=i) for i in range((b - a).days + 1)):
            if not query.period_start and day.weekday() >= 5:
                continue
            lower = datetime.combine(day, time(query.work_start), ZoneInfo(timezone_name))
            upper = datetime.combine(
                day + timedelta(days=query.work_end // 24), time(query.work_end % 24), ZoneInfo(timezone_name)
            )
            occupied = sorted(
                [
                    (instant(x["start_at"]), instant(x["end_at"]))
                    for x in meetings
                    if instant(x["start_at"]) < upper and instant(x["end_at"]) > lower
                ]
            )
            current = max(lower, now())
            for begin, finish in occupied + [(upper, upper)]:
                if begin - current >= timedelta(minutes=query.duration_minutes):
                    slots.append(
                        {
                            "start_at": current.isoformat(),
                            "end_at": (current + timedelta(minutes=query.duration_minutes)).isoformat(),
                        }
                    )
                current = max(current, finish)
        return {"items": slots[: query.limit]}
    items = records(db, user_id, query, timezone_name)
    if query.operation == "count":
        return {"items": [], "count": len(items)}
    return {"items": items[: query.limit]}


def change(
    db,
    user_id,
    operation,
    fields,
    record_id=None,
    version=None,
    scope="series",
    occurrence_at=None,
    timezone_name="Europe/Moscow",
):
    if operation == "create":
        fields = {**fields, "timezone": fields.get("timezone") or timezone_name}
        row = Record(id=uid(), user_id=user_id, version=1, data=validated(fields), deleted=False)
        db.add(row)
    else:
        row = db.scalar(
            select(Record)
            .where(Record.id == record_id, Record.user_id == user_id, Record.deleted.is_(False))
            .with_for_update()
        )
        if not row:
            raise HTTPException(404, "Record not found")
        if version is not None and row.version != version:
            raise HTTPException(409, "Запись изменилась; запросите актуальную версию")
        if scope == "occurrence":
            if not row.data.get("recurrence") or not occurrence_at:
                raise HTTPException(422, "Occurrence requires repeating record and exact date")
            stamp = canonical(occurrence_at)
            day = stamp[:10]
            original = next(
                (
                    x
                    for x in expand(
                        record_output(row),
                        str(date.fromisoformat(day) - timedelta(days=1)),
                        str(date.fromisoformat(day) + timedelta(days=1)),
                        {},
                    )
                    if x.get("occurrence_at") == stamp
                ),
                None,
            )
            if not original:
                raise HTTPException(422, "Occurrence does not belong to series")
            exception = db.scalar(
                select(ExceptionRecord).where(
                    ExceptionRecord.record_id == row.id, ExceptionRecord.occurrence_at == stamp
                )
            )
            previous = exception.data if exception else {}
            combined = {
                **{
                    k: v
                    for k, v in original.items()
                    if k not in ("id", "version", "occurrence_at", "created_at")
                },
                **{k: v for k, v in previous.items() if k != "deleted"},
                **fields,
            }
            if operation == "delete":
                update = {"deleted": True}
            else:
                normalized = validated(combined)
                overridden = (set(previous) - {"deleted"}) | set(fields)
                update = {key: normalized[key] for key in overridden}
            if exception:
                exception.data = update
            else:
                db.add(ExceptionRecord(record_id=row.id, occurrence_at=stamp, data=update))
        elif operation == "delete":
            row.deleted = True
        else:
            row.data = validated({**row.data, **fields})
        row.version += 1
    db.flush()
    from backend.chat.yandex import enqueue

    enqueue(db, row, operation, scope, canonical(occurrence_at) if occurrence_at else None)
    db.add(Outbox(queue="index", payload={"record_id": row.id, "version": row.version}))
    event(db, user_id, "records_changed", {"id": row.id, "version": row.version})
    result = record_output(row)
    if scope == "occurrence" and operation != "delete":
        result = {**original, **update, "version": row.version}
        result["occurrence_at"] = stamp
    result["conflicts"] = conflicts(db, user_id, result) if operation != "delete" else []
    return result


def validated(data):
    try:
        return RecordData.model_validate(data).model_dump(mode="json")
    except (ValidationError, ValueError, KeyError) as exc:
        raise HTTPException(422, validation_detail(exc))


def conflicts(db, user_id, item):
    if item["kind"] != "meeting" or item["status"] != "active":
        return []
    start, end = instant(item["start_at"]), instant(item["end_at"])
    zone = ZoneInfo(item["timezone"])
    candidates = records(
        db,
        user_id,
        Query(kind="meeting", status="active", period_start=start.astimezone(zone).date(),
              period_end=end.astimezone(zone).date()),
        item["timezone"],
    )
    return [
        {key: x.get(key) for key in ("id", "version", "title", "start_at", "end_at", "occurrence_at")}
        for x in candidates
        if (x["id"], x.get("occurrence_at")) != (item["id"], item.get("occurrence_at"))
        and not (x["id"] == item["id"] and instant(x["start_at"]) == start)
        and instant(x["start_at"]) < end
        and instant(x["end_at"]) > start
    ]


class RecurrenceBoundRequired(Exception):
    def __init__(self, proposed, existing):
        self.proposed, self.existing = proposed, existing
        super().__init__("Conflict check requires bounded recurrence")


def meeting_conflict_pairs(db, user_id, item):
    """Check each affected repetition, without guessing a horizon for an endless series."""
    if item["kind"] != "meeting" or item["status"] != "active":
        return []
    if not item.get("recurrence") or item.get("occurrence_at"):
        return [(item, existing) for existing in conflicts(db, user_id, item)]

    def overrides(record_id):
        return {row.occurrence_at: row.data for row in db.scalars(
            select(ExceptionRecord).where(ExceptionRecord.record_id == record_id)
        )}

    def span(data, exceptions):
        start, end = instant(data["start_at"]), instant(data["end_at"])
        recurrence = data.get("recurrence")
        if recurrence:
            if not recurrence.get("count") and not recurrence.get("until"):
                return None
            rule = recurrence_rule(data, start.astimezone(ZoneInfo(data["timezone"])))
            try:
                end = rule[-1] + (end - start)
            except IndexError:
                return (start, start)
        starts, ends = [start], [end]
        for exception in exceptions.values():
            if not exception.get("deleted"):
                if exception.get("start_at"):
                    starts.append(instant(exception["start_at"]))
                if exception.get("end_at"):
                    ends.append(instant(exception["end_at"]))
        return min(starts), max(ends)

    exceptions = overrides(item["id"])
    window = span(item, exceptions)
    windows = [window] if window else []
    if window is None:
        recurrence = item["recurrence"]
        interval = recurrence.get("interval", 1)
        # Conservative wall-clock lower bound: if even this cannot prove siblings
        # disjoint, request a finite series instead of sampling a guessed horizon.
        minimum_days = {"daily": interval, "weekly": 7 * interval, "monthly": 28 * interval}[
            recurrence["frequency"]
        ]
        weekdays = sorted(recurrence.get("weekdays") or [])
        if recurrence["frequency"] == "weekly" and len(weekdays) > 1:
            minimum_days = min([b - a for a, b in zip(weekdays, weekdays[1:])]
                               + [7 * interval + weekdays[0] - weekdays[-1]])
        duration = instant(item["end_at"]) - instant(item["start_at"])
        if duration > timedelta(days=minimum_days):
            raise RecurrenceBoundRequired(item, item)
        for stamp, exception in exceptions.items():
            if not exception.get("deleted"):
                start = instant(exception.get("start_at") or stamp)
                end = (instant(exception["end_at"]) if exception.get("end_at") else
                       instant(stamp).astimezone(ZoneInfo(item["timezone"])) + duration)
                windows.append((start, end))
        for row in db.scalars(select(Record).where(
            Record.user_id == user_id, Record.deleted.is_(False), Record.id != item["id"]
        )):
            data = record_output(row)
            if data["kind"] != "meeting" or data["status"] != "active":
                continue
            other_window = span(data, overrides(row.id))
            if other_window is None:
                raise RecurrenceBoundRequired(item, data)
            windows.append(other_window)

    pairs, seen = [], set()
    zone = ZoneInfo(item["timezone"])
    for lower, upper in windows:
        for occurrence in expand(item, str(lower.astimezone(zone).date()),
                                 str(upper.astimezone(zone).date()), exceptions):
            if occurrence["occurrence_at"] in seen:
                continue
            seen.add(occurrence["occurrence_at"])
            pairs.extend((occurrence, existing) for existing in conflicts(db, user_id, occurrence))
    return pairs
