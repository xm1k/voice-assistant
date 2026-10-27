from datetime import date, timedelta

from conftest import now
from scenario_checks import at, cards, created, found_exact, parsed

DATA_FIELDS = (
    "kind", "title", "notes", "status", "due_date", "start_at", "end_at", "remind_at",
    "timezone", "recurrence", "reminder_minutes",
)


def future_day(offset=7):
    return now().date() + timedelta(days=offset)


def label(day):
    return day.strftime("%d.%m.%Y")


def meeting(api, title="Обсуждение бюджета", day=None, hour=10, **fields):
    day = day or future_day()
    return api.seed("meeting", title, start_at=at(day, hour), end_at=at(day, hour + 1), **fields)


def control(api):
    return meeting(api, "Контрольная встреча по логистике", future_day(40), hour=16, notes="Не менять")


def same_fields(actual, expected, **changes):
    wanted = {**expected, **changes}
    for key in DATA_FIELDS:
        left, right = actual.get(key), wanted.get(key)
        if key in ("start_at", "end_at", "remind_at") and left and right:
            left, right = parsed(left), parsed(right)
        if key == "recurrence" and left and right:
            left = {**left, "weekdays": sorted(left["weekdays"])}
            right = {**right, "weekdays": sorted(right["weekdays"])}
        assert left == right, f"{key}: actual={left!r}, expected={right!r}; record={actual}"


def mutation(result, action, record_id):
    assert result["pending"] is False, result
    shown = result.get("cards", [])
    assert all(card["action"] in (action, "found") for card in shown), shown
    changes = [card["record"] for card in shown if card["action"] == action]
    assert len(changes) == 1 and changes[0]["id"] == record_id, changes
    return changes[0]


def updated(api, result, original, **changes):
    shown = mutation(result, "updated", original["id"])
    record = api.get(original["id"])
    assert record["id"] == original["id"] and record["version"] > original["version"], record
    assert shown["version"] == record["version"], (shown, record)
    same_fields(record, original, **changes)
    same_fields(shown, record)
    return record


def assert_records(api, expected):
    actual = api.records()
    assert {r["id"] for r in actual} == {r["id"] for r in expected}, (actual, expected)
    assert len(actual) == len(expected), (actual, expected)
    for record in expected:
        assert api.get(record["id"]) == record, record


def deleted(api, result, original, survivors):
    mutation(result, "deleted", original["id"])
    api.request("GET", f"/api/v1/records/{original['id']}", expected=404)
    assert_records(api, survivors)


def series(api, *, start=None, count=4, weekly=False):
    start = start or future_day()
    if weekly:
        start += timedelta(days=(-start.weekday()) % 7)
    recurrence = {"frequency": "weekly" if weekly else "daily", "interval": 1, "count": count,
                  "weekdays": [0] if weekly else []}
    record = meeting(api, "Дейлик проекта", start, notes="Исходная заметка", recurrence=recurrence)
    window = {"period_start": start.isoformat(),
              "period_end": (start + timedelta(days=35 if weekly else count + 2)).isoformat()}
    rows = [r for r in api.records(**window) if r["id"] == record["id"]]
    assert len(rows) == count, rows
    return record, window, rows


def occurrence_key(record):
    return parsed(record["occurrence_at"]).isoformat()


def assert_occurrences(api, original, window, before, *, removed=(), changes=None):
    changes = changes or {}
    rows = api.records(**window)
    assert all(r["id"] == original["id"] for r in rows), rows
    actual = {occurrence_key(r): r for r in rows}
    expected = {occurrence_key(r): r for r in before if occurrence_key(r) not in removed}
    assert set(actual) == set(expected) and len(rows) == len(expected), (rows, expected)
    for key, record in expected.items():
        same_fields(actual[key], record, **changes.get(key, {}))
    return rows


def edit_one(api, original, window, before, target, text, **changes):
    result = api.say(text)
    shown = mutation(result, "updated", original["id"])
    assert occurrence_key(shown) == occurrence_key(target), shown
    same_fields(shown, target, **changes)
    base = api.get(original["id"])
    assert base["version"] > original["version"], base
    same_fields(base, original)
    assert_occurrences(api, original, window, before, changes={occurrence_key(target): changes})
    return base


def test_21_delete_named_meeting(service):
    api = service
    day = future_day()
    target = meeting(api, day=day)
    neighbor = meeting(api, "Обсуждение бюджета отдела продаж", day, hour=14)
    result = api.say(f"Удали встречу «Обсуждение бюджета» {label(day)} в 10:00")
    deleted(api, result, target, [neighbor])


def test_22_delete_just_created_using_history(service):
    api = service
    guard = control(api)
    day = future_day()
    result = api.say(f"Создай встречу с юристом {label(day)} с 12:00 до 13:00")
    created = cards(result, "created")
    assert len(created) == 1, result
    target = api.get(created[0]["id"])
    deleted(api, api.say("Удали эту встречу, которую только что создали"), target, [guard])


def test_23_delete_previously_found_meeting(service):
    api = service
    day = future_day()
    target = meeting(api, "Переговоры с поставщиком", day)
    guard = control(api)
    found_exact(api, f"Найди встречу с поставщиком {label(day)}", [target])
    deleted(api, api.say("Удалите найденную встречу"), target, [guard])


def test_24_delete_past_meeting(service):
    api = service
    day = now().date() - timedelta(days=45)
    target = meeting(api, "Архивная встреча с аудитором", day)
    guard = control(api)
    deleted(api, api.say(f"Удали встречу с аудитором {label(day)} в 10:00"), target, [guard])


def test_25_delete_missing_meeting_changes_nothing(service):
    api = service
    guard = control(api)
    result = api.say(f"Удали встречу с космонавтом {label(future_day())} в 10:00")
    assert result["pending"] is False and not result.get("cards"), result
    assert_records(api, [guard])


def test_26_ambiguous_delete_waits_for_time(service):
    api = service
    day = future_day()
    first = meeting(api, "Встреча с Иваном", day, hour=10)
    second = meeting(api, "Встреча с Иваном", day, hour=14)
    result = api.say(f"Удали встречу с Иваном {label(day)}")
    assert result["pending"] is True and not result.get("cards"), result
    assert_records(api, [first, second])
    deleted(api, api.say("Ту, которая в 14:00"), second, [first])


def test_27_delete_entire_series(service):
    api = service
    original, window, before = series(api)
    guard = control(api)
    start = parsed(before[0]["start_at"]).date()
    result = api.say(f"Удали всю серию встреч «Дейлик проекта», начинающуюся {label(start)} в 10:00, "
                     "вместе со всеми повторениями, а не только одно повторение")
    deleted(api, result, original, [guard])
    assert not api.records(**window), "Deleted series still expands"


def test_28_delete_first_occurrence_only(service):
    api = service
    original, window, before = series(api)
    target = before[0]
    day = parsed(target["start_at"]).date()
    result = api.say(f"Удали только первое повторение «Дейлик проекта» {label(day)} в 10:00. "
                     "Все последующие встречи оставь")
    shown = mutation(result, "deleted", original["id"])
    assert occurrence_key(shown) == occurrence_key(target), shown
    assert_occurrences(api, original, window, before, removed={occurrence_key(target)})
    same_fields(api.get(original["id"]), original)


def test_29_create_monthly_meeting_count(service):
    api = service
    today = now().date()
    month_index = today.year * 12 + today.month
    days = [date((month_index + offset) // 12, (month_index + offset) % 12 + 1, 5)
            for offset in range(3)]
    result = api.say(f"Создай ежемесячную встречу «Обзор проекта» пятого числа каждого месяца "
                     f"с 12:00 до 13:00, начиная с {label(days[0])}. Всего три встречи")
    record = created(api, result, "meeting", "обзор")
    recurrence = record["recurrence"]
    assert recurrence == {"frequency": "monthly", "interval": 1, "weekdays": [],
                          "until": None, "count": 3}, recurrence
    same_fields(record, record, start_at=at(days[0], 12), end_at=at(days[0], 13))
    rows = api.records(period_start=days[0].isoformat(), period_end=(days[-1] + timedelta(days=35)).isoformat())
    assert len(rows) == 3 and {parsed(r["start_at"]).date() for r in rows} == set(days), rows
    for row in rows:
        assert row["id"] == record["id"], row
        day = parsed(row["start_at"]).date()
        same_fields(row, record, start_at=at(day, 12), end_at=at(day, 13))


def test_30_deleted_meeting_not_returned_in_search(service):
    api = service
    day = future_day()
    target = meeting(api, "Презентация квартального отчёта", day)
    guard = control(api)
    deleted(api, api.say(f"Удали встречу «Презентация квартального отчёта» {label(day)} в 10:00"),
            target, [guard])
    found_exact(api, f"Найди встречу «Презентация квартального отчёта» {label(day)}", [])


def test_31_meeting_history_previous_month(service):
    api = service
    end = now().date().replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    expected = [meeting(api, "Начало месяца", start), meeting(api, "Конец месяца", end)]
    meeting(api, "До периода", start - timedelta(days=1))
    meeting(api, "После периода", end + timedelta(days=1))
    api.seed("task", "Задача внутри периода", due_date=start.isoformat())
    found_exact(api, "Покажи все мои встречи за прошлый месяц", expected)


def test_32_meeting_history_previous_year(service):
    api = service
    day = date(now().year - 1, 3, 12)
    old = meeting(api, "Встреча с архитектором", day)
    meeting(api, "Встреча с архитектором", future_day())
    result = found_exact(api, f"Когда у меня была встреча с архитектором в {day.year} году?", [old])
    assert parsed(cards(result, "found")[0]["start_at"]).date() == day


def test_33_edit_old_meeting_from_search_history(service):
    api = service
    day = now().date() - timedelta(days=20)
    original = meeting(api, "Ретроспектива проекта", day, notes="Старые заметки")
    guard = control(api)
    found_exact(api, f"Найди ретроспективу проекта {label(day)}", [original])
    result = api.say("У найденной встречи замени заметку на «Итоги согласованы»")
    changed = updated(api, result, original, notes="Итоги согласованы")
    assert_records(api, [changed, guard])


def test_34_distinguish_same_meeting_different_years(service):
    api = service
    year = now().year
    first = meeting(api, "Встреча с Иваном", date(year - 2, 5, 15))
    second = meeting(api, "Встреча с Иваном", date(year - 1, 5, 15))
    found_exact(api, f"Найди встречу с Иваном 15 мая {year - 2} года", [first])
    found_exact(api, f"А теперь покажи встречу с Иваном 15 мая {year - 1} года", [second])


def test_35_history_includes_explicitly_requested_statuses(service):
    api = service
    day = now().date() - timedelta(days=30)
    expected = [meeting(api, "Завершённое обсуждение", day, status="completed"),
                meeting(api, "Отменённое обсуждение", day, hour=14, status="cancelled"),
                meeting(api, "Архивное обсуждение", day, hour=16)]
    meeting(api, "Соседний день", day + timedelta(days=1))
    found_exact(api, f"Покажи все встречи {label(day)}, включая завершённые и отменённые", expected)


def test_36_rename_meeting_preserves_other_fields(service):
    api = service
    day = future_day()
    original = meeting(api, day=day, notes="Обсудить смету")
    guard = control(api)
    changed = updated(api, api.say(f"Переименуй встречу «Обсуждение бюджета» {label(day)} в 10:00 "
                                  "в «Финансовый комитет». Остальное оставь как есть"),
                      original, title="Финансовый комитет")
    assert_records(api, [changed, guard])


def test_37_add_meeting_notes(service):
    api = service
    day = future_day()
    original = meeting(api, day=day)
    guard = control(api)
    changed = updated(api, api.say(f"Добавь к встрече «Обсуждение бюджета» {label(day)} в 10:00 "
                                  "заметку «Взять финансовый отчёт»"),
                      original, notes="Взять финансовый отчёт")
    assert_records(api, [changed, guard])


def test_38_clear_meeting_notes(service):
    api = service
    day = future_day()
    original = meeting(api, day=day, notes="Устаревшая информация")
    guard = control(api)
    changed = updated(api, api.say(f"Очисти заметку у встречи «Обсуждение бюджета» {label(day)} "
                                  "в 10:00, саму встречу сохрани"), original, notes=None)
    assert_records(api, [changed, guard])


def test_39_change_meeting_duration(service):
    api = service
    day = future_day()
    original = meeting(api, day=day)
    guard = control(api)
    changed = updated(api, api.say(f"Продли встречу «Обсуждение бюджета» {label(day)} в 10:00 "
                                  "до 11:30, начало не меняй"),
                      original, end_at=(parsed(at(day, 11)) + timedelta(minutes=30)).isoformat())
    assert_records(api, [changed, guard])


def test_40_move_meeting_to_another_date(service):
    api = service
    day, destination = future_day(), future_day(10)
    original = meeting(api, day=day, notes="Согласовать бюджет")
    guard = control(api)
    changed = updated(api, api.say(f"Перенеси встречу «Обсуждение бюджета» с {label(day)} "
                                  f"на {label(destination)}. Время 10:00–11:00 и заметки сохрани"),
                      original, start_at=at(destination, 10), end_at=at(destination, 11))
    assert_records(api, [changed, guard])


def test_41_cancel_meeting_keeps_record(service):
    api = service
    day = future_day()
    original = meeting(api, day=day)
    guard = control(api)
    changed = updated(api, api.say(f"Отмени встречу «Обсуждение бюджета» {label(day)} в 10:00. "
                                  "Поставь статус отменена, запись не удаляй"), original, status="cancelled")
    assert_records(api, [changed, guard])


def test_42_resume_cancelled_meeting(service):
    api = service
    day = future_day()
    original = meeting(api, day=day, status="cancelled")
    guard = control(api)
    changed = updated(api, api.say(f"Возобнови отменённую встречу «Обсуждение бюджета» "
                                  f"{label(day)} в 10:00: сделай её активной"), original, status="active")
    assert_records(api, [changed, guard])


def test_43_rename_entire_series(service):
    api = service
    original, window, before = series(api)
    guard = control(api)
    start = parsed(original["start_at"]).date()
    changed = updated(api, api.say(f"Переименуй всю серию «Дейлик проекта», начиная с {label(start)}, "
                                  "в «Синхронизация команды». Измени название всех повторений"),
                      original, title="Синхронизация команды")
    expected = {occurrence_key(r): {"title": "Синхронизация команды"} for r in before}
    assert_occurrences(api, original, window, before, changes=expected)
    assert api.get(guard["id"]) == guard
    assert {r["id"] for r in api.records()} == {changed["id"], guard["id"]}


def test_44_move_entire_series_time(service):
    api = service
    original, window, before = series(api)
    start = parsed(original["start_at"]).date()
    updated(api, api.say(f"У всей серии «Дейлик проекта» с {label(start)} перенеси все встречи "
                        "с 10:00–11:00 на 12:00–13:00. Даты и повторение не меняй"),
            original, start_at=at(start, 12), end_at=at(start, 13))
    rows = api.records(**window)
    assert len(rows) == len(before), rows
    by_day = {parsed(r["start_at"]).date(): r for r in rows}
    assert set(by_day) == {parsed(r["start_at"]).date() for r in before}, rows
    for old in before:
        day = parsed(old["start_at"]).date()
        same_fields(by_day[day], old, start_at=at(day, 12), end_at=at(day, 13))
    assert all(r["id"] == original["id"] for r in rows), rows


def test_45_change_notes_entire_series(service):
    api = service
    original, window, before = series(api)
    start = parsed(original["start_at"]).date()
    updated(api, api.say(f"У всей серии «Дейлик проекта» с {label(start)} замени заметку "
                        "во всех повторениях на «Подготовить статус проекта»"),
            original, notes="Подготовить статус проекта")
    expected = {occurrence_key(r): {"notes": "Подготовить статус проекта"} for r in before}
    assert_occurrences(api, original, window, before, changes=expected)


def test_46_change_weekdays_entire_series(service):
    api = service
    original, window, before = series(api, weekly=True)
    start = parsed(original["start_at"]).date()
    recurrence = {**original["recurrence"], "weekdays": [1, 3]}
    result = api.say(f"Измени всю серию «Дейлик проекта», начинающуюся {label(start)}. "
                     "Вместо понедельников встречи должны быть по вторникам и четвергам "
                     "с 10:00 до 11:00. Сохрани общее количество четыре встречи")
    # Keeping the original anchor or aligning it to the first Tuesday represents the same schedule.
    base_day = parsed(api.get(original["id"])["start_at"]).date()
    assert base_day in (start, start + timedelta(days=1)), base_day
    updated(api, result, original, recurrence=recurrence,
            start_at=at(base_day, 10), end_at=at(base_day, 11))
    rows = api.records(**window)
    days = {start + timedelta(days=n) for n in (1, 3, 8, 10)}
    assert len(rows) == 4 and {parsed(r["start_at"]).date() for r in rows} == days, rows
    assert all(r["id"] == original["id"] for r in rows), rows
    for row in rows:
        day = parsed(row["start_at"]).date()
        same_fields(row, original, start_at=at(day, 10), end_at=at(day, 11), recurrence=recurrence)


def test_47_move_only_one_occurrence(service):
    api = service
    original, window, before = series(api)
    target = before[1]
    day = parsed(target["start_at"]).date()
    edit_one(api, original, window, before, target,
             f"Перенеси только одно повторение «Дейлик проекта» {label(day)} с 10:00 на 14:00–15:00. "
             "Другие повторения оставь без изменений", start_at=at(day, 14), end_at=at(day, 15))


def test_48_rename_only_one_occurrence(service):
    api = service
    original, window, before = series(api)
    target = before[1]
    day = parsed(target["start_at"]).date()
    edit_one(api, original, window, before, target,
             f"Переименуй только встречу «Дейлик проекта» {label(day)} в 10:00 в «Обсуждение релиза». "
             "Это изменение одного повторения, название остальных встреч серии сохрани", title="Обсуждение релиза")


def test_49_notes_only_one_occurrence_persist(service):
    api = service
    original, window, before = series(api)
    target = before[2]
    day = parsed(target["start_at"]).date()
    edit_one(api, original, window, before, target,
             f"Только у повторения «Дейлик проекта» {label(day)} в 10:00 замени заметку на "
             "«Показать прототип». Заметки остальных повторений не меняй", notes="Показать прототип")
    api.relogin()
    same_fields(api.get(original["id"]), original)
    assert_occurrences(api, original, window, before,
                       changes={occurrence_key(target): {"notes": "Показать прототип"}})


def test_50_series_edit_does_not_restore_deleted_occurrence(service):
    api = service
    original, window, before = series(api)
    target = before[1]
    day = parsed(target["start_at"]).date()
    result = api.say(f"Удали только повторение «Дейлик проекта» {label(day)} в 10:00. "
                     "Остальные встречи серии сохрани")
    mutation(result, "deleted", original["id"])
    assert_occurrences(api, original, window, before, removed={occurrence_key(target)})
    current = api.get(original["id"])
    start = parsed(current["start_at"]).date()
    updated(api, api.say(f"Теперь переименуй всю оставшуюся серию «Дейлик проекта» с {label(start)} в 10:00 "
                        "в «Статус проекта». Удалённое повторение не восстанавливай"),
            current, title="Статус проекта")
    changes = {occurrence_key(r): {"title": "Статус проекта"} for r in before}
    assert_occurrences(api, original, window, before, removed={occurrence_key(target)}, changes=changes)


def test_51_conflicting_create_waits_for_consent(service):
    api = service
    day = future_day()
    existing = meeting(api, "Планирование бюджета", day, hour=10)
    result = api.say(f"Создай встречу с юристом {label(day)} с 10:30 до 11:30")
    assert result["pending"] is True and not result.get("cards"), result
    assert_records(api, [existing])
    result = api.say("Да, сохрани встречу с юристом на это время, несмотря на пересечение")
    added = cards(result, "created")
    assert result["pending"] is False and len(added) == 1, result
    assert parsed(added[0]["start_at"]) == parsed(at(day, 10)) + timedelta(minutes=30), added
    assert parsed(added[0]["end_at"]) == parsed(at(day, 11)) + timedelta(minutes=30), added
    assert_records(api, [existing, api.get(added[0]["id"])])


def test_52_conflicting_move_waits_for_new_time(service):
    api = service
    day = future_day()
    existing = meeting(api, "Планирование бюджета", day, hour=10)
    target = meeting(api, "Встреча с юристом", day, hour=14)
    result = api.say(f"Перенеси встречу с юристом {label(day)} на 10:30–11:30")
    assert result["pending"] is True and not result.get("cards"), result
    assert_records(api, [existing, target])
    result = api.say("Тогда перенеси на 12:00–13:00 в тот же день")
    moved = updated(api, result, target, start_at=at(day, 12), end_at=at(day, 13))
    assert_records(api, [existing, moved])
