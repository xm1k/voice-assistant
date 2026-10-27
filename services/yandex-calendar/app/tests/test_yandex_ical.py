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


def test_rrule_dst_sparse_override_and_preserved_outside_rule():
    from backend.yandex_calendar.ical import serialize

    content = serialize(
        meeting(),
        [
            {"occurrence_at": "2026-03-28T09:00:00+00:00", "data": {"title": "Changed"}},
            {"occurrence_at": "2026-03-29T08:00:00+00:00", "data": {"deleted": True}},
            {"occurrence_at": "2026-03-20T09:00:00+00:00", "data": {"notes": "Preserved"}},
        ],
        "uid",
        "operation",
    )
    calendar = Calendar.from_ical(content)
    events = calendar.walk("VEVENT")
    assert len(events) == 3
    master = events[0]
    assert master["rrule"]["COUNT"] == [4]
    assert "UNTIL" not in master["rrule"]
    assert master.decoded("dtstart").tzinfo.key == "Europe/Berlin"
    assert calendar.walk("VTIMEZONE")
    changed = next(item for item in events[1:] if item.decoded("recurrence-id").day == 28)
    assert changed.decoded("recurrence-id").hour == 10
    assert str(changed["summary"]) == "Changed"
    assert master["rdate"].dts[0].dt.day == 20
    assert master["exdate"].dts[0].dt.day == 29


def test_semantic_compare_accepts_normalization_but_rejects_external_fields():
    from backend.yandex_calendar.ical import equivalent, serialize

    content = serialize(meeting(recurrence=None), [], "uid", "operation")
    normalized = Calendar.from_ical(content)
    normalized["prodid"] = "Server"
    normalized.walk("VEVENT")[0].add("sequence", 4)
    assert equivalent(content, normalized.to_ical())
    normalized.walk("VEVENT")[0].add("location", "Externally changed")
    assert not equivalent(content, normalized.to_ical())


def test_nonrecurring_ignores_old_exceptions_and_serialization_is_stable():
    from backend.yandex_calendar.ical import serialize

    content = serialize(
        meeting(recurrence=None),
        [{"occurrence_at": "2020-01-01T09:00:00Z", "data": {"title": "Old"}}],
        "uid",
        "operation",
    )
    assert len(Calendar.from_ical(content).walk("VEVENT")) == 1
    assert content == serialize(meeting(recurrence=None), [], "uid", "operation")


def test_unbounded_timezone_covers_last_year_and_is_cached():
    from datetime import date, datetime, timedelta

    from backend.yandex_calendar.ical import timezone_component

    timezone_component.cache_clear()
    component = timezone_component("Europe/Berlin", date(2026, 1, 1), date(9999, 12, 31))
    parsed = Calendar.from_ical(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + component + b"END:VCALENDAR\r\n")
    zone = parsed.walk("VTIMEZONE")[0].to_tz(lookup_tzid=False)
    for year in (2026, 9999):
        assert datetime(year, 1, 15, tzinfo=zone).utcoffset() == timedelta(hours=1)
        assert datetime(year, 7, 15, tzinfo=zone).utcoffset() == timedelta(hours=2)
    assert timezone_component("Europe/Berlin", date(2026, 1, 1), date(9999, 12, 31)) == component
    assert timezone_component.cache_info().hits == 1


def test_finite_monthly_horizon_and_override_zone_are_exported():
    from backend.yandex_calendar.ical import serialize

    content = serialize(
        meeting(recurrence={"frequency": "monthly", "count": 24}),
        [{"occurrence_at": "2026-03-28T09:00:00Z", "data": {"timezone": "America/New_York"}}],
        "uid",
        "op",
    )
    zones = Calendar.from_ical(content).walk("VTIMEZONE")
    assert {str(item["tzid"]) for item in zones} == {"Europe/Berlin", "America/New_York"}
    assert all("2029" in str(item["comment"]) for item in zones)


def test_semantic_normalization_combines_exdate_lines_and_defaults():
    from backend.yandex_calendar.ical import equivalent, serialize

    content = serialize(
        meeting(recurrence={"frequency": "daily", "count": 4}),
        [
            {"occurrence_at": "2026-03-28T09:00:00Z", "data": {"deleted": True}},
            {"occurrence_at": "2026-03-29T08:00:00Z", "data": {"deleted": True}},
        ],
        "uid",
        "operation",
    )
    normalized = Calendar.from_ical(content)
    event = normalized.walk("VEVENT")[0]
    values = [part.dt for value in event["EXDATE"] for part in value.dts]
    del event["EXDATE"]
    event.add("EXDATE", values)
    del event["STATUS"]
    event.add("TRANSP", "OPAQUE")
    normalized.add("CALSCALE", "GREGORIAN")
    assert equivalent(content, normalized.to_ical())


def test_observed_yandex_timezone_rewrite_and_organizer_display_metadata():
    from backend.yandex_calendar.ical import serialize, server_equivalent
    from icalendar import Timezone

    desired = serialize(meeting(recurrence=None, notes=None), [], "uid", "op")
    current = Calendar.from_ical(desired)
    current.subcomponents = [item for item in current.subcomponents if item.name != "VTIMEZONE"]
    from pathlib import Path

    timezone = Timezone.from_ical((Path(__file__).parent / "fixtures/yandex-berlin.ics").read_bytes())
    current.add_component(timezone)
    current.add("method", "PUBLISH")
    event = current.walk("VEVENT")[0]
    event.pop("DESCRIPTION")
    event.add("organizer", "mailto:account@yandex.ru", parameters={"CN": "Display Name"})
    event.add("url", "https://calendar.yandex.ru/event?event_id=1234")
    event.add("categories", ["Selected"])
    assert server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected")
    baseline = current.to_ical()
    timezone.add("comment", "Changed definition")
    assert not server_equivalent(current.to_ical(), desired, baseline, "account@yandex.ru", "Selected")
    del timezone["COMMENT"]
    timezone.subcomponents[0]["TZOFFSETTO"] = __import__("datetime").timedelta(hours=3)
    assert not server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected")


def test_yandex_omits_only_owned_display_alarm_description_and_default_interval():
    from backend.yandex_calendar.ical import serialize, server_equivalent

    desired = serialize(
        meeting(recurrence={"frequency": "daily", "interval": 1, "count": 3}, reminder_minutes=15),
        [],
        "uid",
        "op",
    )
    current = Calendar.from_ical(desired)
    current.walk("VEVENT")[0]["RRULE"].pop("INTERVAL")
    current.walk("VALARM")[0].pop("DESCRIPTION")
    assert server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected")
    current.walk("VALARM")[0]["ACTION"] = "EMAIL"
    assert not server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected")


@pytest.mark.parametrize("until,accepted", [("20261028T194300Z", True), ("20261029T194300Z", False)])
def test_finite_count_rewritten_to_until_requires_exact_complete_sequence(until, accepted):
    from backend.yandex_calendar.ical import serialize, server_equivalent

    desired = serialize(
        meeting(
            start_at="2026-10-26T19:43:00+01:00",
            end_at="2026-10-26T20:43:00+01:00",
            recurrence={"frequency": "daily", "count": 3},
        ),
        [],
        "uid",
        "op",
    )
    current = Calendar.from_ical(desired)
    master = current.walk("VEVENT")[0]
    master.pop("RRULE")
    from icalendar import vRecur

    master.add("rrule", vRecur.from_ical("FREQ=DAILY;UNTIL=" + until + ";INTERVAL=1"))
    assert server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected") is accepted


def test_finite_rule_is_never_equated_to_unbounded_rule():
    from backend.yandex_calendar.ical import serialize, server_equivalent

    desired = serialize(meeting(recurrence={"frequency": "daily", "count": 3}), [], "uid", "op")
    current = Calendar.from_ical(desired)
    current.walk("VEVENT")[0]["RRULE"].pop("COUNT")
    assert not server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected")


def test_new_exception_owns_distinct_yandex_url_then_baseline_fences_it():
    from backend.yandex_calendar.ical import serialize, server_equivalent

    data = meeting(recurrence={"frequency": "daily", "count": 3})
    baseline = Calendar.from_ical(serialize(data, [], "uid", "op1"))
    baseline.walk("VEVENT")[0].add("url", "https://calendar.yandex.ru/event?event_id=1234")
    desired = serialize(
        data, [{"occurrence_at": "2026-03-28T09:00:00Z", "data": {"title": "Override"}}], "uid", "op2"
    )
    current = Calendar.from_ical(desired)
    events = current.walk("VEVENT")
    events[0].add("url", "https://calendar.yandex.ru/event?event_id=1234")
    events[1].add("url", "https://calendar.yandex.ru/event?event_id=5678")
    assert server_equivalent(current.to_ical(), desired, baseline.to_ical(), "account@yandex.ru", "Selected")
    saved = current.to_ical()
    events[1]["URL"] = "https://calendar.yandex.ru/event?event_id=9999"
    assert not server_equivalent(current.to_ical(), desired, saved, "account@yandex.ru", "Selected")


def test_future_events_keep_historical_timezone_baseline_for_yandex():
    from backend.yandex_calendar.ical import serialize

    content = serialize(
        meeting(start_at="2027-03-27T10:00:00+01:00", end_at="2027-03-27T11:00:00+01:00", recurrence=None),
        [],
        "uid",
        "op",
    )
    zone = Calendar.from_ical(content).walk("VTIMEZONE")[0]
    assert min(item.decoded("DTSTART").year for item in zone.subcomponents) <= 1970


def test_legal_until_beyond_ten_thousand_occurrences_compares_without_cap():
    from backend.yandex_calendar.ical import serialize, server_equivalent
    from icalendar import vRecur

    desired = serialize(meeting(recurrence={"frequency": "daily", "until": "2056-03-27"}), [], "uid", "op")
    current = Calendar.from_ical(desired)
    master = current.walk("VEVENT")[0]
    master["RRULE"] = vRecur.from_ical("FREQ=DAILY;UNTIL=20560327T120000Z")
    assert server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected")
    master["RRULE"] = vRecur.from_ical("FREQ=DAILY;UNTIL=20560328T120000Z")
    assert not server_equivalent(current.to_ical(), desired, None, "account@yandex.ru", "Selected")
