"""assistant-worker and core agree on meeting-conflict feedback; no model or network needed."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from backend.chat import main as core
from backend.chat.domain import change
from backend.chat.models import Chat, Job, Record
from backend.common.runtime import Base, now
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.responses import JSONResponse


@pytest.fixture
def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        table for table in Base.metadata.sorted_tables if table.name != "record_embeddings"
    ])
    sessions = sessionmaker(engine, expire_on_commit=False)
    with patch.object(core, "Session", sessions):
        yield sessions
    engine.dispose()


def command(start="2027-01-12T10:30:00+03:00", end="2027-01-12T11:30:00+03:00", **values):
    return {"operation": "create", "fields": {
        "kind": "meeting", "title": "Юрист", "start_at": start, "end_at": end,
        "timezone": "Europe/Moscow",
    }, **values}


def ready(*commands, **values):
    return {"type": "ready", "commands": list(commands), **values}


def seed(database, *, occupied=True):
    with database.begin() as db:
        db.add(Chat(id="user", generation=0))
        db.flush()
        if occupied:
            change(db, "user", "create", {**command()["fields"], "title": "Бюджет",
                   "start_at": "2027-01-12T10:00:00+03:00", "end_at": "2027-01-12T11:00:00+03:00"})
        db.add(job("first"))


def job(name):
    return Job(id=name, user_id="user", kind="assistant", status="running", generation=0,
               request_id=name, input={"timezone": "Europe/Moscow", "text": "Создай встречу"},
               claim_token=name, lease_until=now() + timedelta(minutes=5))


def check(name, decision):
    response = core.validate_job_decision(name, core.DecisionCheck(claim_token=name, decision=decision))
    return json.loads(response.body)["detail"]["errors"][0] if isinstance(response, JSONResponse) else response


def finish(name, decision):
    return core.result(name, core.Result(claim_token=name, decision=decision))


def rows(database, model):
    with database() as db:
        return db.scalars(select(model)).all()


def ask(database, decision):
    feedback = check("first", decision)
    assert feedback["valid"] is False and feedback["code"] == "meeting_conflict", feedback
    question = {"type": "clarify", "question": "Есть пересечение с бюджетом. Оставить время?",
                "context": "Создать встречу с юристом в 10:30–11:30"}
    assert check("first", question) == {"valid": True}
    assert finish("first", question)["result"]["pending"] is True
    with database.begin() as db:
        pending = db.get(Chat, "user").pending["meeting_conflict"]
        db.add(job("second"))
    return feedback, pending


def test_worker_uses_core_feedback_to_request_question(database):
    from backend.workers import assistant

    seed(database)
    initial = ready(command())
    question = {"type": "clarify", "question": "Есть пересечение. Оставить время?",
                "context": "Создать встречу с юристом"}
    completions = AsyncMock(side_effect=[initial, question])
    publish = AsyncMock()

    async def core_api(path, body):
        if path.endswith("/claim"):
            return {"user_id": "user", "timezone": "Europe/Moscow", "claim_token": "first",
                    "messages": [{"role": "user", "text": "Создай встречу"}]}
        assert path.endswith("/validate"), path
        return check("first", body["decision"])

    with patch.object(assistant, "core", core_api), patch.object(assistant, "complete", completions), \
         patch("backend.broker.publish", publish):
        asyncio.run(assistant.handle_job({"job_id": "first"}))
    assert completions.await_count == 2
    feedback = completions.call_args_list[1].args[0][-1]["content"]
    assert "meeting_conflict" in feedback and "Бюджет" in feedback
    published = publish.call_args.args[1]["decision"]
    assert published["type"] == "clarify" and not published["commands"]
    assert len(rows(database, Record)) == 1


@pytest.mark.parametrize("operation", ["create", "move", "large_batch"])
def test_conflict_flow_over_http_when_worker_discards_validation_body(database, operation):
    from backend.llm.client import run_decision

    seed(database)
    initial = ready(command())
    if operation == "large_batch":
        initial = ready(command(fields={**command()["fields"], "notes": "x" * 5000}),
                        command(fields={**command()["fields"], "title": "Вторая встреча", "notes": "y" * 5000}))
    if operation == "move":
        with database.begin() as db:
            target = change(db, "user", "create", command(start="2027-01-12T14:00:00+03:00",
                                                          end="2027-01-12T15:00:00+03:00")["fields"])
        initial = ready({"operation": "update", "id": target["id"], "version": target["version"],
                         "fields": {"start_at": command()["fields"]["start_at"],
                                    "end_at": command()["fields"]["end_at"]}})
    question = {"type": "clarify", "question": "Есть пересечение с бюджетом. Оставить время?",
                "context": "Создать или перенести встречу с юристом"}
    completions = AsyncMock(side_effect=[initial, question])
    core.app.dependency_overrides[core.internal] = lambda: None

    async def run(name, completion):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app), base_url="http://core") as client:
            async def validate(decision):
                response = await client.post(f"/internal/jobs/{name}/validate", json={
                    "claim_token": name, "decision": decision,
                })
                response.raise_for_status()
                # Older workers ignore successful response bodies.

            return await run_decision([], completion, AsyncMock(side_effect=AssertionError("Unexpected query")), validate)

    try:
        decision = asyncio.run(run("first", completions))
        assert decision["type"] == "clarify" and not decision["commands"]
        feedback = completions.call_args_list[1].args[0][-1]["content"]
        assert "meeting_conflict" in feedback and "Бюджет" in feedback and "clarify" in feedback
        assert len(rows(database, Record)) == (2 if operation == "move" else 1)
        finish("first", decision)
        with database.begin() as db:
            pending = db.get(Chat, "user").pending["meeting_conflict"]
            db.add(job("second"))
        followup = {**initial, "conflict_confirmation": pending["token"]}
        if operation == "move":
            followup = ready({**initial["commands"][0], "fields": {
                "start_at": "2027-01-12T12:00:00+03:00", "end_at": "2027-01-12T13:00:00+03:00",
            }})
        accepted = asyncio.run(run("second", AsyncMock(return_value=followup)))
        assert finish("second", accepted)["status"] == "succeeded"
        assert len(rows(database, Record)) == (3 if operation == "large_batch" else 2)
    finally:
        core.app.dependency_overrides.pop(core.internal, None)
