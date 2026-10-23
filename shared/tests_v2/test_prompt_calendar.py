"""Calendar facts supplied to the model use the message's local date, including today."""

import json

import pytest
from backend.common.schemas import Recurrence
from backend.llm.prompt import system_prompt


@pytest.mark.parametrize(("stamp", "expected_date", "friday"), [
    ("2026-10-23T14:56:21Z", "2026-10-23", "2026-10-23"),
    ("2026-10-23T21:30:00Z", "2026-10-24", "2026-10-30"),
    ("2026-12-31T23:30:00Z", "2027-01-01", "2027-01-01"),
])
def test_nearest_friday_includes_today_in_message_timezone(stamp, expected_date, friday):
    prompt = system_prompt({"requested_at": stamp, "timezone": "Europe/Moscow"})
    clock = json.loads(prompt.split("Время сообщения: ", 1)[1].split("\nКонфликт,", 1)[0])
    assert clock["local_date"] == expected_date
    assert clock["nearest_weekdays"]["пятница"] == friday


def test_recurrence_schema_explains_zero_based_weekdays_to_model():
    description = Recurrence.model_json_schema()["properties"]["weekdays"].get("description", "")
    assert "0=понедельник" in description
    assert "6=воскресенье" in description
    assert "[0,1,2,3,4]" in description
