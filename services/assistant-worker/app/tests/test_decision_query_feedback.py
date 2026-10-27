import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from backend.llm.client import run_decision


@pytest.mark.parametrize("error", [
    "Укажите период для повторяющихся записей",
    {"errors": [{"loc": ["period_end"], "message": "Укажите конец периода"}]},
])
def test_query_error_is_corrected_before_accepting_results(error):
    histories = []
    decisions = iter([
        {"type": "query", "query": {"kind": "meeting"}},
        {"type": "query", "query": {"kind": "meeting", "period_start": "2026-11-03",
                                     "period_end": "2026-11-03"}},
        {"type": "ready", "answer": "Найдена серия", "record_refs": [{"id": "series", "version": 2}]},
    ])

    async def complete(history):
        histories.append(list(history))
        return next(decisions)

    query = AsyncMock(side_effect=[{"items": [], "error": error},
                                   {"items": [{"id": "series", "version": 2}]}])
    validate = AsyncMock(return_value=None)
    result = asyncio.run(run_decision([], complete, query, validate))
    feedback = histories[1][-1]["content"]
    assert "query_failed" in feedback
    assert json.dumps(error, ensure_ascii=False) in feedback
    assert "не означает" in feedback
    assert "Query result" not in feedback
    assert query.await_count == 2
    validate.assert_awaited_once()
    assert result["type"] == "ready"


def test_successful_empty_query_is_not_an_error():
    histories = []

    async def complete(history):
        histories.append(list(history))
        return ({"type": "query", "query": {"kind": "meeting"}} if len(histories) == 1
                else {"type": "ready", "answer": "Встреч нет"})

    result = asyncio.run(run_decision([], complete, AsyncMock(return_value={"items": []}),
                                      AsyncMock(return_value=None)))
    assert "Query result" in histories[1][-1]["content"]
    assert "query_failed" not in histories[1][-1]["content"]
    assert result["answer"] == "Встреч нет"


def test_loop_logs_job_attempt_query_and_validation(caplog):
    decisions = iter([
        {"type": "query", "query": {"kind": "meeting", "text": "Private title"}},
        {"type": "ready", "answer": "Встреч нет"},
    ])

    async def complete(history):
        return next(decisions)

    with caplog.at_level(logging.INFO, logger="backend.llm.client"):
        asyncio.run(run_decision([], complete, AsyncMock(return_value={"items": []}),
                                 AsyncMock(return_value=None), job_id="job-123"))
    assert "job=job-123 attempt=1" in caplog.text
    assert "query_result" in caplog.text
    assert "validation_result" in caplog.text
    assert "Private title" not in caplog.text


def test_error_result_cannot_authorize_record_references():
    decisions = iter([
        {"type": "query", "query": {"kind": "meeting"}},
        {"type": "ready", "answer": "Найдено", "record_refs": [{"id": "untrusted", "version": 1}]},
        {"type": "ready", "answer": "Поиск не выполнен"},
    ])

    async def complete(history):
        return next(decisions)

    validate = AsyncMock(return_value=None)
    asyncio.run(run_decision([], complete, AsyncMock(return_value={
        "items": [{"id": "untrusted", "version": 1}], "error": "Invalid query",
    }), validate))
    assert validate.await_count == 1
    assert validate.call_args.args[0]["answer"] == "Поиск не выполнен"
