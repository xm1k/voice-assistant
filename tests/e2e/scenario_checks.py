from datetime import date, datetime, time, timedelta

from conftest import TZ


def at(day, hour):
    return datetime.combine(day, time(hour), TZ).isoformat()


def parsed(value):
    stamp = datetime.fromisoformat(value)
    assert stamp.utcoffset() is not None, f"Timezone missing: {value}"
    return stamp.astimezone(TZ)


def dates_sent(api, offset=0):
    return {stamp.date() + timedelta(days=offset) for stamp in (api.sent_before, api.sent_after)}


def weekday(day, target):
    return day + timedelta(days=(target - day.weekday()) % 7)


def cards(result, action):
    found = result.get("cards", [])
    assert all(card["action"] == action for card in found), f"Unexpected actions: {found}"
    return [card["record"] for card in found]


def created(api, result, kind, word):
    assert result["pending"] is False, result
    shown = cards(result, "created")
    assert len(shown) == 1, shown
    saved = api.get(shown[0]["id"])
    assert saved["kind"] == kind, saved
    assert word.casefold() in saved["title"].casefold(), saved
    assert saved["timezone"] == "Europe/Moscow", saved
    assert saved["status"] == "active", saved
    assert saved["created_at"] and parsed(saved["created_at"]).tzinfo, saved
    assert saved["version"] >= 1, saved
    assert shown[0]["version"] == saved["version"], (shown, saved)
    assert {r["id"] for r in api.records()} == {saved["id"]}, "Unexpected extra records"
    return saved


def meeting_at(record, days, hour):
    start, end = parsed(record["start_at"]), parsed(record["end_at"])
    assert start.date() in days, (start, days)
    assert start.time() == time(hour), start
    assert end - start == timedelta(hours=1), (start, end)
    assert record.get("due_date") is None, record


def found_exact(api, text, expected):
    before = api.records()
    result = api.say(text)
    assert result["pending"] is False, result
    actual = cards(result, "found")
    assert {r["id"] for r in actual} == {r["id"] for r in expected}, (actual, expected)
    assert len(actual) == len(expected), (actual, expected)
    assert api.records() == before, "Read-only query changed records"
    return result


def found_period(api, text, universe, *, kind=None, offset=0, next_week=False):
    before = api.records()
    result = api.say(text)
    assert result["pending"] is False, result
    actual = cards(result, "found")
    alternatives = []
    for day in dates_sent(api):
        start = day + timedelta(days=7 - day.weekday()) if next_week else day + timedelta(days=offset)
        end = start + timedelta(days=6) if next_week else start
        expected = []
        for record in universe:
            value = record.get("due_date") or record.get("start_at") or record.get("remind_at")
            record_day = date.fromisoformat(value) if len(value) == 10 else parsed(value).date()
            if (start <= record_day <= end and record["status"] == "active"
                    and (kind is None or record["kind"] == kind)):
                expected.append(record["id"])
        alternatives.append(set(expected))
    actual_ids = {record["id"] for record in actual}
    assert actual_ids in alternatives and len(actual_ids) == len(actual), (actual, alternatives)
    assert api.records() == before, "Read-only query changed records"
    return result


def seed_day(api, day, prefix="Сегодня"):
    return [
        api.seed("task", prefix + " отчёт", due_date=day.isoformat()),
        api.seed("meeting", prefix + " команда", start_at=at(day, 15), end_at=at(day, 16)),
        api.seed("reminder", prefix + " звонок", remind_at=at(day, 10)),
    ]


