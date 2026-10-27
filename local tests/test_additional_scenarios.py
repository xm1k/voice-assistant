"""Additional live LLM checks for calendar rules and non-meeting records."""
from datetime import date, time, timedelta

from conftest import now
from scenario_checks import at, cards, created, dates_sent, found_exact, parsed
from test_meeting_scenarios import (
    assert_records, control, deleted, future_day, label, meeting, updated,
)


def task(api, **fields):
    return api.seed("task", "Подготовить смету", due_date=future_day().isoformat(),
                    notes="Исходная заметка", **fields)


def reminder(api):
    return api.seed("reminder", "Позвонить нотариусу", remind_at=at(future_day(), 10),
                    notes="Исходная заметка")


def edit_task(api, text, **changes):
    original, guard = task(api), control(api)
    result = api.say(text)
    record = updated(api, result, original, **changes)
    assert_records(api, [record, guard])
    api.relogin()
    assert api.get(record["id"]) == record


def edit_reminder(api, text, **changes):
    original, guard = reminder(api), control(api)
    record = updated(api, api.say(text), original, **changes)
    assert_records(api, [record, guard])
    api.relogin()
    assert api.get(record["id"]) == record


def test_53_create_task_without_deadline(service):
    record = created(service, service.say("Добавь задачу купить конверты без срока и без напоминания"),
                     "task", "конверт")
    assert all(record.get(key) is None for key in ("due_date", "remind_at", "start_at", "end_at")), record


def test_54_rename_task(service):
    edit_task(service, "Переименуй задачу «Подготовить смету» в «Проверить смету»", title="Проверить смету")


def test_55_move_task_deadline(service):
    day = future_day(12)
    edit_task(service, f"Перенеси срок задачи «Подготовить смету» на {label(day)}", due_date=day.isoformat())


def test_56_clear_task_deadline(service):
    edit_task(service, "Убери срок у задачи «Подготовить смету», оставь её без даты", due_date=None)


def test_57_replace_task_notes(service):
    edit_task(service, "Замени заметку задачи «Подготовить смету» на «Учесть доставку»", notes="Учесть доставку")


def test_58_clear_task_notes(service):
    edit_task(service, "Очисти заметку задачи «Подготовить смету», саму задачу сохрани", notes=None)


def test_59_reopen_completed_task(service):
    original, guard = task(service, status="completed"), control(service)
    record = updated(service, service.say("Возобнови выполненную задачу «Подготовить смету», сделай активной"),
                     original, status="active")
    assert_records(service, [record, guard])


def test_60_delete_task_preserves_neighbor(service):
    original, guard = task(service), control(service)
    deleted(service, service.say("Удали задачу «Подготовить смету»"), original, [guard])


def test_61_reminder_clarifies_time(service):
    result = service.say("Напомни послезавтра проверить аптечку")
    days = dates_sent(service, 2)
    assert result["pending"] is True and not result.get("cards"), result
    assert not service.records()
    record = created(service, service.say("В 09:30 утра"), "reminder", "аптечк")
    stamp = parsed(record["remind_at"])
    assert stamp.date() in days and stamp.time() == time(9, 30), record


def test_62_move_reminder(service):
    day = future_day(12)
    edit_reminder(service, f"Перенеси напоминание «Позвонить нотариусу» на {label(day)} в 16:00",
                  remind_at=at(day, 16))


def test_63_rename_reminder(service):
    edit_reminder(service, "Переименуй напоминание «Позвонить нотариусу» в «Написать нотариусу»",
                  title="Написать нотариусу")


def test_64_cancel_reminder(service):
    edit_reminder(service, "Отмени напоминание «Позвонить нотариусу», сохрани запись со статусом отменено",
                  status="cancelled")


def test_65_delete_reminder(service):
    original, guard = reminder(service), control(service)
    deleted(service, service.say("Удали напоминание «Позвонить нотариусу»"), original, [guard])


def test_66_future_date_without_year(service):
    day = now().date() + timedelta(days=20)
    record = created(service, service.say(f"Создай встречу «Обсуждение макета» {day:%d.%m} в 14:00"),
                     "meeting", "макет")
    assert parsed(record["start_at"]) == parsed(at(day, 14)), record
    assert parsed(record["end_at"]) == parsed(at(day, 15)), record


def test_67_past_date_without_year_rolls_forward(service):
    day = now().date() - timedelta(days=2)
    if (day.month, day.day) == (2, 29):
        day -= timedelta(days=1)
    expected = date(now().year, day.month, day.day)
    if expected <= now().date():
        expected = expected.replace(year=expected.year + 1)
    record = created(service, service.say(f"Создай встречу «Обсуждение архива» {day:%d.%m} в 14:00"),
                     "meeting", "архив")
    assert parsed(record["start_at"]) == parsed(at(expected, 14)), record


def test_68_leap_day_without_year(service):
    today = now().date()
    year = today.year
    while True:
        try:
            day = date(year, 2, 29)
        except ValueError:
            year += 1
            continue
        if day > today:
            break
        year += 1
    record = created(service, service.say("Создай встречу «Високосный обзор» 29 февраля в 14:00"),
                     "meeting", "обзор")
    assert parsed(record["start_at"]) == parsed(at(day, 14)), record


def test_69_explicit_year_is_preserved(service):
    day = date(now().year + 2, 11, 23)
    record = created(service, service.say(f"Создай встречу «Стратегия развития» {label(day)} в 14:00"),
                     "meeting", "стратег")
    assert parsed(record["start_at"]) == parsed(at(day, 14)), record


def test_70_decline_conflicting_creation(service):
    day = future_day()
    existing = meeting(service, day=day)
    result = service.say(f"Создай встречу с архитектором {label(day)} с 10:30 до 11:30")
    assert result["pending"] is True and not result.get("cards"), result
    assert_records(service, [existing])
    result = service.say("Нет, не создавайте встречу с архитектором. Отменяю создание")
    assert result["pending"] is False and not result.get("cards"), result
    assert_records(service, [existing])


def test_71_adjacent_meeting_is_not_conflict(service):
    day = future_day()
    existing = meeting(service, day=day)
    result = service.say(f"Создай встречу с архитектором {label(day)} с 11:00 до 12:00")
    shown = cards(result, "created")
    assert result["pending"] is False and len(shown) == 1, result
    record = service.get(shown[0]["id"])
    assert parsed(record["start_at"]) == parsed(at(day, 11)), record
    assert parsed(record["end_at"]) == parsed(at(day, 12)), record
    assert not record["conflicts"], record
    assert_records(service, [existing, record])


def test_72_empty_schedule_is_read_only(service):
    guard = control(service)
    found_exact(service, f"Покажи все встречи {label(future_day(5))}", [])
    assert_records(service, [guard])
