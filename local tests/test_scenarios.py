from datetime import date, time, timedelta
from uuid import uuid4

from conftest import now
from scenario_checks import (
    at,
    cards,
    created,
    dates_sent,
    found_exact,
    found_period,
    meeting_at,
    parsed,
    seed_day,
    weekday,
)


def test_01_reminder_tomorrow_persists_after_login(service):
    api = service
    record = created(api, api.say("Напомни мне завтра в 10 утра позвонить Ивану"), "reminder", "Иван")
    stamp = parsed(record["remind_at"])
    assert stamp.date() in dates_sent(api, 1) and stamp.time() == time(10), stamp
    api.relogin()
    assert api.get(record["id"]) == record


def test_02_task_friday(service):
    api = service
    record = created(api, api.say("В пятницу нужно отправить отчет"), "task", "отч")
    assert date.fromisoformat(record["due_date"]) in {weekday(d, 4) for d in dates_sent(api)}, record
    assert not record.get("start_at") and not record.get("end_at") and not record.get("remind_at"), record


def test_03_task_end_of_week(service):
    api = service
    record = created(api, api.say("Поставь задачу купить билеты до конца недели"), "task", "билет")
    assert date.fromisoformat(record["due_date"]) in {weekday(d, 6) for d in dates_sent(api)}, record
    assert not record.get("start_at") and not record.get("end_at"), record


def test_04_meeting_tomorrow(service):
    api = service
    record = created(api, api.say("Добавь встречу с командой завтра в 15:00"), "meeting", "команд")
    meeting_at(record, dates_sent(api, 1), 15)


def test_05_meeting_thursday(service):
    api = service
    result = api.say("В четверг в 11 у меня встреча с дизайнером")
    days = {weekday(d, 3) for d in dates_sent(api)}
    stamps = (api.sent_before, api.sent_after)
    if all(stamp.weekday() == 3 and stamp.time() >= time(11) for stamp in stamps):
        assert result["pending"] is True, "Past Thursday time must trigger date clarification"
    # The documented contract asks for a date if today's named weekday/time has passed.
    if result["pending"]:
        assert any(stamp.weekday() == 3 and stamp.time() >= time(11)
                   for stamp in (api.sent_before, api.sent_after)), result
        assert not api.records() and not result.get("cards"), result
        chosen = weekday(api.sent_after.date(), 3) + timedelta(days=7)
        result = api.say("В следующий четверг, " + chosen.strftime("%d.%m.%Y"))
        days = {chosen}
    record = created(api, result, "meeting", "дизайнер")
    meeting_at(record, days, 11)


def test_06_reminder_two_hours(service):
    api = service
    record = created(api, api.say("Напомни через два часа проверить духовку"), "reminder", "духов")
    stamp = parsed(record["remind_at"])
    # Natural language has minute precision; requested_at is assigned server-side during POST.
    assert api.sent_before - timedelta(minutes=1) + timedelta(hours=2) <= stamp, stamp
    assert stamp <= api.sent_after + timedelta(hours=2, minutes=1), stamp


def test_07_today_paraphrases_all_kinds(service):
    api = service
    day = now().date()
    universe = seed_day(api, day) + seed_day(api, day + timedelta(days=1), "Завтра")
    for text in ("Что у меня сегодня?", "Какие планы на сегодня?"):
        found_period(api, text, universe)


def test_08_today_meetings_only(service):
    api = service
    day = now().date()
    universe = seed_day(api, day) + seed_day(api, day + timedelta(days=1), "Завтра")
    found_period(api, "Покажи мои встречи на сегодня", universe, kind="meeting")


def test_09_tasks_tomorrow(service):
    api = service
    day = now().date()
    universe = (seed_day(api, day) + seed_day(api, day + timedelta(days=1), "Завтра")
                + seed_day(api, day + timedelta(days=2), "Послезавтра"))
    found_period(api, "Какие задачи у меня на завтра?", universe, kind="task", offset=1)


def test_10_next_week_boundaries(service):
    api = service
    today = now().date()
    monday = today + timedelta(days=7 - today.weekday())
    sunday = monday + timedelta(days=6)
    api.seed("task", "Отчёт понедельник", due_date=monday.isoformat())
    api.seed("meeting", "Воскресная встреча", start_at=at(sunday, 12), end_at=at(sunday, 13))
    api.seed("reminder", "Среда звонок", remind_at=at(monday + timedelta(days=2), 10))
    api.seed("task", "До периода", due_date=(monday - timedelta(days=1)).isoformat())
    api.seed("task", "После периода", due_date=(sunday + timedelta(days=1)).isoformat())
    api.seed("task", "Отменённая задача", due_date=monday.isoformat(), status="cancelled")
    # Populate both candidate weeks if a Sunday/Monday rollover happens during setup.
    api.seed("task", "Следующий понедельник", due_date=(monday + timedelta(days=7)).isoformat())
    api.seed("meeting", "Следующее воскресенье", start_at=at(sunday + timedelta(days=7), 12),
             end_at=at(sunday + timedelta(days=7), 13))
    found_period(api, "Что запланировано на следующей неделе?", api.records(), next_week=True)


def test_11_search_presentation_paraphrase(service):
    api = service
    old = (now().date() - timedelta(days=60)).isoformat()
    expected = api.seed("task", "Подготовить слайды для выступления перед инвесторами", due_date=old)
    api.seed("task", "Заказать билеты на поезд", due_date=old)
    found_exact(api, "Найди задачу про презентацию для инвесторов", [expected])


def test_12_search_old_meeting(service):
    api = service
    day = now().date() - timedelta(days=90)
    expected = api.seed("meeting", "Обсуждение бюджета с Иваном", start_at=at(day, 11), end_at=at(day, 12))
    api.seed("meeting", "Обсуждение бюджета с Анной", start_at=at(day, 14), end_at=at(day, 15))
    result = found_exact(api, "Когда у меня была встреча с Иваном?", [expected])
    shown = cards(result, "found")[0]
    assert parsed(shown["start_at"]) == parsed(expected["start_at"]), shown


def test_13_search_contract_semantic(service):
    api = service
    old = (now().date() - timedelta(days=120)).isoformat()
    expected = api.seed("task", "Согласовать условия соглашения с поставщиком", due_date=old)
    api.seed("task", "Заказать канцелярию в офис", due_date=old)
    found_exact(api, "Я когда-то ставил задачу по договору с поставщиком, найди её", [expected])


def test_14_clarification_preserves_date(service):
    api = service
    result = api.say("Поставь встречу с Иваном послезавтра")
    days = dates_sent(api, 2)
    assert result["pending"] is True and not result.get("cards"), result
    assert "?" in result["answer"], result
    assert not api.records()
    record = created(api, api.say("В 14:00"), "meeting", "Иван")
    meeting_at(record, days, 14)


def test_15_task_timed_requires_consent(service):
    api = service
    result = api.say("Поставь задачу завтра в 10:00 купить лекарства")
    days = dates_sent(api, 1)
    assert result["pending"] is True and not result.get("cards"), result
    assert "напомин" in result["answer"].casefold(), result
    assert not api.records()
    record = created(api, api.say("Да, сделайте напоминание"), "reminder", "лекарств")
    stamp = parsed(record["remind_at"])
    assert stamp.date() in days and stamp.time() == time(10), record


def test_16_weekday_daily_meeting(service):
    api = service
    result = api.say("Создай регулярную встречу дейлик в 10 по будням")
    expected_days = set()
    for stamp in (api.sent_before, api.sent_after):
        candidate = stamp.date()
        if stamp.time() >= time(10):
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        expected_days.add(candidate)
    if result["pending"]:
        assert not api.records() and not result.get("cards"), result
        first_day = max(expected_days)
        result = api.say(f"Начните {first_day:%d.%m.%Y} в 10:00. Повторяйте по будням "
                         "без даты окончания. Каждая встреча длится час.")
        expected_days = {first_day}
    record = created(api, result, "meeting", "дейлик")
    recurrence = record["recurrence"]
    assert recurrence["frequency"] == "weekly" and recurrence["interval"] == 1, recurrence
    assert sorted(recurrence["weekdays"]) == [0, 1, 2, 3, 4], recurrence
    assert recurrence.get("until") is None and recurrence.get("count") is None, recurrence
    first = parsed(record["start_at"])
    meeting_at(record, expected_days, 10)
    end = first.date() + timedelta(days=13)
    expanded = api.records(period_start=first.date().isoformat(), period_end=end.isoformat())
    expected = {first.date() + timedelta(days=n) for n in range(14)
                if (first.date() + timedelta(days=n)).weekday() < 5}
    assert {parsed(r["start_at"]).date() for r in expanded} == expected, expanded
    assert len(expanded) == len(expected) and all(r["id"] == record["id"] for r in expanded), expanded


def test_17_move_discussed_meeting(service):
    api = service
    original = created(api, api.say("Добавь встречу с командой завтра в 15:00"), "meeting", "команд")
    result = api.say("Перенеси эту встречу на 17:00, сохрани длительность один час")
    shown = cards(result, "updated")
    assert len(shown) == 1 and shown[0]["id"] == original["id"], shown
    updated = api.get(original["id"])
    assert updated["version"] > original["version"], updated
    meeting_at(updated, {parsed(original["start_at"]).date()}, 17)
    assert updated["title"] == original["title"], updated
    assert len(api.records()) == 1


def test_18_delete_single_occurrence(service):
    api = service
    start = now().date() + timedelta(days=2)
    original = api.seed("meeting", "Дейлик проекта", start_at=at(start, 10), end_at=at(start, 11),
                        recurrence={"frequency": "daily", "interval": 1, "count": 3})
    end = start + timedelta(days=2)
    query = {"period_start": start.isoformat(), "period_end": end.isoformat()}
    before = api.records(**query)
    assert len(before) == 3, before
    target = start + timedelta(days=1)
    result = api.say(f"Удали только одно повторение встречи «Дейлик проекта» {target:%d.%m.%Y} "
                     "в 10:00. Остальную серию сохрани.")
    deleted = cards(result, "deleted")
    assert len(deleted) == 1 and deleted[0]["id"] == original["id"], deleted
    assert parsed(deleted[0]["start_at"]).date() == target, deleted
    after = api.records(**query)
    assert len(after) == 2 and {parsed(r["start_at"]).date() for r in after} == {start, end}, after
    assert all(r["id"] == original["id"] for r in after), after


def test_19_complete_task(service):
    api = service
    original = api.seed("task", "Отправить отчёт бухгалтеру", due_date=now().date().isoformat())
    api.seed("task", "Купить бумагу", due_date=now().date().isoformat())
    before = {r["id"]: r for r in api.records()}
    result = api.say("Я отправил отчёт бухгалтеру, отметь эту задачу выполненной")
    shown = cards(result, "updated")
    assert len(shown) == 1 and shown[0]["id"] == original["id"], shown
    updated = api.get(original["id"])
    assert updated["status"] == "completed" and updated["version"] > original["version"], updated
    after = {r["id"]: r for r in api.records()}
    assert set(after) == set(before), after
    assert all(after[key] == value for key, value in before.items() if key != original["id"])


def test_20_chat_request_idempotency(service):
    api = service
    request_id = uuid4().hex
    text = "Поставь задачу купить билеты до конца недели"
    first = api.submit(text, request_id)
    second = api.submit(text, request_id)
    assert first == second, (first, second)
    result = api.wait(first)
    record = created(api, result, "task", "билет")
    third = api.submit(text, request_id)
    assert third == first, (first, third)
    api.wait(third)
    assert [r["id"] for r in api.records()] == [record["id"]]
    jobs = api.pages("/api/v1/jobs", status="succeeded")
    assert [job["id"] for job in jobs] == [first], jobs
    messages = api.pages("/api/v1/chat/messages")
    assert len([m for m in messages if m["role"] == "user"]) == 1, messages
    assert len([m for m in messages if m["role"] == "assistant"]) == 1, messages
