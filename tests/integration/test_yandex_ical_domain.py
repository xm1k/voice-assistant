"""Exported iCalendar series expand exactly like core's domain records."""

import pytest
from icalendar import Calendar


def meeting(**updates):
    return {
        "kind": "meeting",
        "title": "Standup",
        "notes": "Notes",
        "start_at": "2026-03-27T10:00:00+01:00",
        "end_at": "2026-03-27T11:00:00+01:00",
        "timezone": "Europe/Berlin",
        "recurrence": {"frequency": "daily", "count": 10, "until": "2026-03-30"},
        **updates,
    }


@pytest.mark.parametrize(
    "recurrence",
    [
        {"frequency": "daily", "count": 8, "until": "2026-03-31"},
        {"frequency": "weekly", "weekdays": [0, 2], "count": 4},
        {"frequency": "monthly", "count": 3},
    ],
)
def test_exported_recurrence_matches_domain_expansion(recurrence):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from backend.chat.domain import expand
    from backend.yandex_calendar.ical import serialize
    from dateutil.rrule import rrulestr

    data = meeting(recurrence=recurrence)
    exceptions = {
        "2026-03-28T09:00:00+00:00": {"deleted": True},
        "2026-03-20T09:00:00+00:00": {
            "title": "Preserved",
            "start_at": "2026-03-29T12:00:00+00:00",
            "end_at": "2026-03-29T13:00:00+00:00",
        },
    }
    content = serialize(
        data, [{"occurrence_at": key, "data": value} for key, value in exceptions.items()], "uid", "op"
    )
    events = Calendar.from_ical(content).walk("VEVENT")
    master = events[0]
    zone = ZoneInfo("Europe/Berlin")
    lower, upper = datetime(2026, 3, 1, tzinfo=zone), datetime(2026, 7, 1, tzinfo=zone)
    stamps = set(
        rrulestr(master["RRULE"].to_ical().decode(), dtstart=master.decoded("DTSTART")).between(
            lower, upper, inc=True
        )
    )
    stamps.add(master.decoded("DTSTART"))
    for name in ("RDATE", "EXDATE"):
        values = master.get(name, [])
        values = values if isinstance(values, list) else [values]
        for entry in values:
            for part in entry.dts:
                if name == "RDATE":
                    stamps.add(part.dt)
                else:
                    stamps.discard(part.dt)
    overrides = {item.decoded("RECURRENCE-ID"): item for item in events[1:]}
    duration = master.decoded("DTEND") - master.decoded("DTSTART")
    actual = []
    for stamp in stamps:
        item = overrides.get(stamp)
        start = item.decoded("DTSTART") if item else stamp
        end = item.decoded("DTEND") if item else stamp + duration
        if lower <= start < upper:
            actual.append(
                (
                    start.astimezone(timezone.utc).isoformat(),
                    end.astimezone(timezone.utc).isoformat(),
                    str((item or master)["SUMMARY"]),
                )
            )
    expected = [
        (
            datetime.fromisoformat(item["start_at"]).astimezone(timezone.utc).isoformat(),
            datetime.fromisoformat(item["end_at"]).astimezone(timezone.utc).isoformat(),
            item["title"],
        )
        for item in expand(data, "2026-03-01", "2026-06-30", exceptions)
    ]
    assert sorted(actual) == sorted(expected)
