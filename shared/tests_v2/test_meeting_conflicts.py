"""Core owns conflict detection, rollback and consent; no model or network needed."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from backend.chat import main as core
from backend.chat.domain import change
from backend.chat.models import Chat, Event, Job, Outbox, Record
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


def test_conflict_returns_details_and_rolls_back_all_effects(database):
    seed(database)
    before = {model: len(rows(database, model)) for model in (Record, Event, Outbox)}
    feedback = check("first", ready(command()))
    assert feedback["valid"] is False and feedback["code"] == "meeting_conflict", feedback
    conflict = feedback["conflicts"][0]
    assert conflict["existing"]["title"] == "Бюджет"
    assert conflict["existing"]["start_at"] == "2027-01-12T10:00:00+03:00"
    assert conflict["proposed"]["start_at"] == "2027-01-12T10:30:00+03:00"
    assert {model: len(rows(database, model)) for model in before} == before
    assert check("first", ready(command()))["valid"] is False


def test_consent_is_only_available_after_question_and_binds_exact_action(database):
    seed(database)
    decision = ready(command())
    _, pending = ask(database, decision)
    assert len(rows(database, Record)) == 1
    approved = {**decision, "conflict_confirmation": pending["token"]}
    assert check("second", approved) == {"valid": True}
    assert len(rows(database, Record)) == 1  # Preview never commits records.
    assert finish("second", approved)["status"] == "succeeded"
    assert len(rows(database, Record)) == 2
    assert rows(database, Chat)[0].pending is None


@pytest.mark.parametrize("change_action", [False, True])
def test_missing_or_reused_consent_cannot_bypass_conflicts(database, change_action):
    seed(database)
    _, pending = ask(database, ready(command()))
    decision = ready(command())
    if change_action:
        decision = ready(command(start="2027-01-12T10:15:00+03:00"),
                         conflict_confirmation=pending["token"])
    assert check("second", decision)["valid"] is False
    assert len(rows(database, Record)) == 1


def test_changed_conflicting_meeting_requires_new_consent(database):
    seed(database)
    _, pending = ask(database, ready(command()))
    with database.begin() as db:
        record = db.scalar(select(Record))
        change(db, "user", "update", {"end_at": "2027-01-12T11:15:00+03:00"}, record.id, record.version)
    feedback = check("second", ready(command(), conflict_confirmation=pending["token"]))
    assert feedback["valid"] is False
    assert len(rows(database, Record)) == 1


def test_no_conflict_applies_and_touching_boundaries_are_allowed(database):
    seed(database)
    decision = ready(command(start="2027-01-12T11:00:00+03:00"))
    assert check("first", decision) == {"valid": True}
    assert len(rows(database, Record)) == 1
    assert finish("first", decision)["status"] == "succeeded"
    assert len(rows(database, Record)) == 2


def test_batch_checks_final_state_and_rolls_back_tasks_too(database):
    seed(database, occupied=False)
    decision = ready({"operation": "create", "fields": {"kind": "task", "title": "Отчёт"}},
                     command(), command(start="2027-01-12T11:00:00+03:00"))
    assert check("first", decision)["valid"] is False
    assert not rows(database, Record)
    assert not rows(database, Outbox)


def test_conflict_appearing_after_preview_requeues_instead_of_saving(database):
    seed(database, occupied=False)
    decision = ready(command())
    assert check("first", decision) == {"valid": True}
    with database.begin() as db:
        change(db, "user", "create", {**command()["fields"], "title": "Срочная встреча"})
    result = finish("first", decision)
    assert result["status"] == "queued", result
    assert len(rows(database, Record)) == 1
    queued = [outbox for outbox in rows(database, Outbox) if outbox.queue == "chat"]
    assert len(queued) == 1 and queued[0].payload == {"job_id": "first"}


def test_message_queued_before_question_cannot_confirm(database):
    seed(database)
    with database.begin() as db:
        db.add(job("queued-before-question"))
    _, pending = ask(database, ready(command()))
    assert check("queued-before-question", ready(command(), conflict_confirmation=pending["token"]))["valid"] is False


def test_move_checks_new_interval_but_does_not_conflict_with_itself(database):
    seed(database, occupied=False)
    with database.begin() as db:
        target = change(db, "user", "create", command()["fields"])
    move = {"operation": "update", "id": target["id"], "version": target["version"],
            "fields": {"start_at": "2027-01-12T10:45:00+03:00"}}
    assert check("first", ready(move)) == {"valid": True}
    with database.begin() as db:
        change(db, "user", "create", command()["fields"])
    assert check("first", ready(move))["valid"] is False
    assert rows(database, Record)[0].version == 1


def test_batch_can_resolve_a_conflict_by_deleting_other_meeting(database):
    seed(database)
    occupied = rows(database, Record)[0]
    decision = ready(command(), {"operation": "delete", "id": occupied.id, "version": occupied.version})
    assert check("first", decision) == {"valid": True}
    result = finish("first", decision)
    assert result["status"] == "succeeded"
    assert not result["result"]["records"][0]["conflicts"]
    assert len([row for row in rows(database, Record) if not row.deleted]) == 1


def test_finite_series_checks_later_occurrences(database):
    seed(database)
    decision = ready(command(fields={**command()["fields"], "start_at": "2027-01-11T10:30:00+03:00",
                                    "end_at": "2027-01-11T11:30:00+03:00",
                                    "recurrence": {"frequency": "daily", "count": 3}}))
    assert check("first", decision)["valid"] is False
    assert len(rows(database, Record)) == 1


def test_endless_series_checks_future_finite_meetings_without_an_arbitrary_horizon(database):
    seed(database, occupied=False)
    with database.begin() as db:
        change(db, "user", "create", command(start="2037-01-12T10:30:00+03:00",
                                             end="2037-01-12T11:30:00+03:00")["fields"])
    decision = ready(command(fields={**command()["fields"], "recurrence": {"frequency": "daily"}}))
    assert check("first", decision)["valid"] is False
    assert len(rows(database, Record)) == 1


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
         patch("backend.chat.broker.publish", publish):
        asyncio.run(assistant.handle_job({"job_id": "first"}))
    assert completions.await_count == 2
    feedback = completions.call_args_list[1].args[0][-1]["content"]
    assert "meeting_conflict" in feedback and "Бюджет" in feedback
    published = publish.call_args.args[1]["decision"]
    assert published["type"] == "clarify" and not published["commands"]
    assert len(rows(database, Record)) == 1


def test_conflicting_batch_consent_survives_new_generated_ids(database):
    seed(database, occupied=False)
    decision = ready(command(), command(start="2027-01-12T11:00:00+03:00"))
    _, pending = ask(database, decision)
    approved = {**decision, "conflict_confirmation": pending["token"]}
    assert check("second", approved) == {"valid": True}
    assert finish("second", approved)["status"] == "succeeded"
    assert len(rows(database, Record)) == 2


def test_changing_notes_does_not_require_consent_for_existing_overlap(database):
    seed(database)
    with database.begin() as db:
        target = change(db, "user", "create", command()["fields"])
    decision = ready({"operation": "update", "id": target["id"], "version": target["version"],
                      "fields": {"notes": "Взять документы"}})
    assert check("first", decision) == {"valid": True}
    assert finish("first", decision)["status"] == "succeeded"


def test_occurrence_move_keeps_other_repetitions_unchanged(database):
    seed(database)
    with database.begin() as db:
        target = change(db, "user", "create", {**command()["fields"],
                        "start_at": "2027-01-11T14:00:00+03:00", "end_at": "2027-01-11T15:00:00+03:00",
                        "recurrence": {"frequency": "daily", "count": 3}})
    decision = ready({"operation": "update", "id": target["id"], "version": target["version"],
                      "scope": "occurrence", "occurrence_at": "2027-01-12T11:00:00+00:00",
                      "fields": {"start_at": "2027-01-12T10:30:00+03:00",
                                 "end_at": "2027-01-12T11:30:00+03:00"}})
    _, pending = ask(database, decision)
    approved = {**decision, "conflict_confirmation": pending["token"]}
    assert check("second", approved) == {"valid": True}
    assert finish("second", approved)["status"] == "succeeded"
    with database() as db:
        series = db.get(Record, target["id"])
        assert series.data["start_at"] == "2027-01-11T14:00:00+03:00"


def test_overlapping_siblings_in_finite_series_need_consent(database):
    seed(database, occupied=False)
    decision = ready(command(fields={**command()["fields"], "end_at": "2027-01-13T11:30:00+03:00",
                                    "recurrence": {"frequency": "daily", "count": 3}}))
    assert check("first", decision)["valid"] is False
    assert not rows(database, Record)


def test_two_endless_series_return_structured_request_for_bounds(database):
    seed(database, occupied=False)
    with database.begin() as db:
        change(db, "user", "create", {**command()["fields"], "recurrence": {"frequency": "daily"}})
    decision = ready(command(fields={**command()["fields"], "recurrence": {"frequency": "daily"}}))
    feedback = check("first", decision)
    assert feedback["valid"] is False and feedback["requires_recurrence_bound"] is True
    assert check("first", ready(command(start="2027-01-12T14:00:00+03:00",
                                        end="2027-01-12T15:00:00+03:00")))["valid"] is False
    question = {"type": "clarify", "question": "До какой даты повторять новую встречу?"}
    assert finish("first", question)["result"]["pending"] is True
    assert len(rows(database, Record)) == 1


def test_endless_series_with_possible_sibling_overlap_requires_bounds(database):
    seed(database, occupied=False)
    decision = ready(command(fields={**command()["fields"], "end_at": "2027-01-13T11:30:00+03:00",
                                    "recurrence": {"frequency": "daily"}}))
    feedback = check("first", decision)
    assert feedback["valid"] is False and feedback["requires_recurrence_bound"] is True
    assert not rows(database, Record)


def test_endless_series_checks_moved_exceptions_far_in_the_future(database):
    seed(database, occupied=False)
    with database.begin() as db:
        target = change(db, "user", "create", {**command()["fields"], "recurrence": {"frequency": "daily"}})
        target = change(db, "user", "update", {"start_at": "2037-01-12T11:00:00+03:00",
                        "end_at": "2037-01-12T12:00:00+03:00"}, target["id"], target["version"],
                        scope="occurrence", occurrence_at="2037-01-11T07:30:00+00:00")
    decision = ready({"operation": "update", "id": target["id"], "version": target["version"],
                      "fields": {"status": "active"}})
    assert check("first", decision)["valid"] is False


def test_http_conflict_rejects_decision_and_commits_required_question(database):
    seed(database)
    core.app.dependency_overrides[core.internal] = lambda: None
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app), base_url="http://core") as client:
            return await client.post("/internal/jobs/first/validate", json={
                "claim_token": "first", "decision": ready(command()),
            })

    try:
        response = asyncio.run(request())
    finally:
        core.app.dependency_overrides.pop(core.internal, None)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "meeting_conflict"
    assert detail["errors"][0]["code"] == "meeting_conflict"
    assert detail["errors"][0]["conflicts"][0]["existing"]["title"] == "Бюджет"
    assert rows(database, Job)[0].input["meeting_conflict"]["conflicts"]
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
