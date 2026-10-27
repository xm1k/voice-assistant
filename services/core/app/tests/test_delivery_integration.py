"""Opt-in real PostgreSQL/RabbitMQ proofs; never use a production vhost/database."""

import asyncio
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import aio_pika
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.chat import main as core
from backend.chat.models import Chat, Event, Job, Message, Outbox, Record
from backend.common.runtime import Base, now
from backend.workers import index as rag

PG = os.getenv("TEST_POSTGRES_URL")
RMQ = os.getenv("TEST_RABBITMQ_URL")
pytestmark = pytest.mark.skipif(not PG, reason="set TEST_POSTGRES_URL to an isolated local test database")


@pytest.fixture
def database():
    engine = create_engine(PG)
    schema = "delivery_" + uuid4().hex
    with engine.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    scoped = create_engine(PG, connect_args={"options": f"-csearch_path={schema},public"})
    Base.metadata.create_all(
        scoped, tables=[table for table in Base.metadata.sorted_tables if table.name != "record_embeddings"]
    )
    sessions = sessionmaker(scoped, expire_on_commit=False)
    with patch.object(core, "Session", sessions), patch.object(rag, "Session", sessions):
        yield sessions, schema
    scoped.dispose()
    with engine.begin() as db:
        db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    engine.dispose()


def seed(sessions, kind="assistant", generation=0):
    user, job, token = str(uuid4()), str(uuid4()), str(uuid4())
    with sessions.begin() as db:
        db.add(Chat(id=user, generation=generation))
        db.add(
            Job(
                id=job,
                user_id=user,
                kind=kind,
                status="running",
                generation=0,
                request_id=str(uuid4()),
                input={},
                claim_token=token,
                lease_until=now() + timedelta(seconds=720),
            )
        )
    return user, job, token


def answer(token):
    return core.Result(
        claim_token=token,
        decision={
            "type": "ready",
            "commands": [{"operation": "create", "fields": {"kind": "task", "title": "Exactly once"}}],
        },
    )


def counts(sessions):
    with sessions() as db:
        return tuple(len(db.scalars(select(model)).all()) for model in (Record, Message, Outbox, Event))


def test_duplicate_and_concurrent_results(database):
    sessions, _ = database
    _, job, token = seed(sessions)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: core.result(job, answer(token)), range(2)))
    assert results[0] == results[1]
    before = counts(sessions)
    core.result(job, answer(token))
    assert counts(sessions) == before
    assert before[0:3] == (1, 1, 1)


def test_duplicate_transcription_creates_one_assistant(database):
    sessions, _ = database
    _, job, token = seed(sessions, "transcription")
    body = core.Result(claim_token=token, text="Купить билеты")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: core.result(job, body), range(2)))
    assert results[0] == results[1]
    with sessions() as db:
        assert len(db.scalars(select(Job).where(Job.kind == "assistant")).all()) == 1
        assert len(db.scalars(select(Outbox)).all()) == 1


def test_stale_lease_and_generation_have_no_effect(database):
    sessions, _ = database
    _, job, token = seed(sessions)
    with pytest.raises(HTTPException) as error:
        core.result(job, answer("old-token"))
    assert error.value.status_code == 409
    assert counts(sessions) == (0, 0, 0, 0)
    _, job, token = seed(sessions, generation=1)
    assert core.result(job, answer(token))["status"] == "cancelled"
    assert counts(sessions)[:3] == (0, 0, 0)


@pytest.mark.skipif(not RMQ, reason="set TEST_RABBITMQ_URL to an isolated test vhost")
@pytest.mark.parametrize("stage", ["before", "during", "committed"])
def test_process_death_redelivers_without_duplicate_effect(database, tmp_path, stage):
    sessions, schema = database
    _, job, token = seed(sessions)
    marker = tmp_path / "stage"
    # Run the actual core consumer in a separate killable process.
    script = """
import asyncio, os, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from backend.chat.broker import consume
from backend.chat import broker
from backend.chat import main as core
if sys.argv[3] == "before":
    import time
    original_loads = broker.json.loads
    def held_decode(value):
        Path(sys.argv[2]).touch()
        time.sleep(60)
        return original_loads(value)
    broker.json.loads = held_decode
async def handler(payload):
    if sys.argv[3] == "during":
        Path(sys.argv[2]).touch()
        await asyncio.Future()
    core.result(payload["job_id"], core.Result.model_validate(payload["body"]))
    Path(sys.argv[2]).touch()
    await asyncio.Future()
async def main():
    await consume("chat", handler)
asyncio.run(main())
"""
    env = {
        **os.environ,
        "RABBITMQ_URL": RMQ,
        "DOMAIN_DATABASE_URL": PG,
        "PGOPTIONS": f"-csearch_path={schema},public",
    }

    async def publish():
        from backend.chat.broker import publish

        with patch.dict(os.environ, {"RABBITMQ_URL": RMQ}):
            await publish("chat", {"job_id": job, "body": answer(token).model_dump()})

    asyncio.run(publish())
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), str(marker), stage],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), (
            proc.stderr.read().decode() if proc.poll() is not None else "consumer did not reach stage"
        )
    finally:
        proc.kill()
        proc.wait(timeout=5)
        proc.stderr.close()

    async def redeliver():
        connection = await aio_pika.connect(RMQ, timeout=10)
        try:
            channel = await connection.channel()
            queue = await channel.get_queue("chat")
            async with asyncio.timeout(15):
                async with queue.iterator() as messages:
                    async for message in messages:
                        assert message.redelivered
                        core.result(job, answer(token))
                        await message.ack()
                        break
            assert (await queue.declare()).message_count == 0
        finally:
            await connection.close()

    asyncio.run(redeliver())
    assert counts(sessions)[:3] == (1, 1, 1)


def test_outbox_not_marked_until_confirmed(database):
    from types import SimpleNamespace

    from pamqp.commands import Basic

    from backend.chat import broker

    sessions, _ = database
    with sessions.begin() as db:
        db.add(Outbox(queue="index", payload={"record_id": "test", "version": 1}))

    async def run(confirmation):
        channel = SimpleNamespace(
            default_exchange=SimpleNamespace(publish=AsyncMock(return_value=confirmation))
        )
        connection = SimpleNamespace(channel=AsyncMock(return_value=channel), close=AsyncMock())
        with (
            patch.object(broker, "Session", sessions),
            patch.dict(os.environ, {"RABBITMQ_URL": "amqp://test"}),
            patch.object(broker.aio_pika, "connect_robust", AsyncMock(return_value=connection)),
            patch.object(broker, "topology", AsyncMock()),
        ):
            await broker.publish_outbox_once()

    with pytest.raises(RuntimeError):
        asyncio.run(run(Basic.Nack(delivery_tag=1)))
    with sessions() as db:
        assert db.scalar(select(Outbox)).published_at is None
    asyncio.run(run(Basic.Ack(delivery_tag=1)))
    with sessions() as db:
        assert db.scalar(select(Outbox)).published_at is not None
