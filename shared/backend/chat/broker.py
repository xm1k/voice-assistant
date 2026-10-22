"""Durable delivery with outbox, confirmed publishes, bounded retries and dead letters."""

import asyncio
import json
import logging
import os
from datetime import timedelta, timezone

import aio_pika
import httpx
from sqlalchemy import select

from backend.chat.domain import event, instant, records
from backend.chat.models import Chat, Job, Notification, Outbox, Record
from backend.common.runtime import Session, now
from backend.common.schemas import Query

logger = logging.getLogger(__name__)
QUEUES = ("chat", "llm_response", "transcription", "index")


async def topology(channel, name):
    await channel.declare_queue(name + ".dead", durable=True)
    await channel.declare_queue(
        name + ".retry",
        durable=True,
        arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": name, "x-message-ttl": 5000},
    )
    return await channel.declare_queue(name, durable=True)


async def publish(queue, payload):
    connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])
    try:
        channel = await connection.channel(publisher_confirms=True)
        await topology(channel, queue)
        await channel.default_exchange.publish(
            aio_pika.Message(
                json.dumps(payload).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=queue,
            mandatory=True,
        )
    finally:
        await connection.close()


async def exhausted(payload, queue):
    if (
        queue not in ("chat", "transcription", "llm_response")
        or not isinstance(payload, dict)
        or not payload.get("job_id")
    ):
        return
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            os.getenv("CORE_URL", "http://chat-calendar-task-notification:8000")
            + "/internal/jobs/"
            + payload["job_id"]
            + "/exhausted",
            json={"claim_token": payload.get("claim_token")},
            headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
        )
        response.raise_for_status()
        if queue == "transcription" and response.json().get("audio_path"):
            from backend.voice.main import trusted_audio_path

            trusted_audio_path(response.json()["audio_path"]).unlink(missing_ok=True)


async def _route_failed(queue, destination, body, attempt):
    """Publish a failed message to retry/dead using a fresh connection independent of the consumer."""
    conn = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])
    try:
        ch = await conn.channel(publisher_confirms=True)
        await topology(ch, queue)
        await ch.default_exchange.publish(
            aio_pika.Message(
                body,
                headers={"attempt": attempt},
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=destination,
            mandatory=True,
        )
    finally:
        await conn.close()


async def consume(queue, handler):
    connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])
    try:
        channel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=1)
        source = await topology(channel, queue)
        async with source.iterator() as messages:
            async for message in messages:
                payload = None
                attempt = int((message.headers or {}).get("attempt", 0))
                try:
                    payload = json.loads(message.body)
                except Exception:
                    await message.ack()
                    continue
                # Ack before handler so RabbitMQ consumer timeout never fires during slow LLM calls.
                # Retry/dead routing is handled manually below on failure.
                await message.ack()
                try:
                    await handler(payload)
                except asyncio.CancelledError:
                    try:
                        await _route_failed(queue, queue + ".retry", message.body, attempt + 1)
                    except Exception:
                        logger.warning("Failed to route cancelled message to retry, queue=%s", queue)
                    raise
                except Exception as exc:
                    blocked = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409
                    next_attempt = attempt + (0 if blocked else 1)
                    destination = queue + ".dead" if next_attempt >= 5 else queue + ".retry"
                    logger.warning(
                        "Delivery failed queue=%s attempt=%s error=%s", queue, next_attempt, type(exc).__name__
                    )
                    if next_attempt >= 5:
                        await exhausted(payload, queue)
                    try:
                        await _route_failed(queue, destination, message.body, next_attempt)
                    except Exception:
                        logger.exception("Failed to route failed message to %s, message lost", destination)
    finally:
        await connection.close()


async def publish_outbox_once():
    # PostgreSQL row locks coordinate multiple publishers. Mark only after Rabbit confirmation.
    with Session.begin() as db:
        rows = db.scalars(
            select(Outbox)
            .where(Outbox.published_at.is_(None))
            .order_by(Outbox.created_at, Outbox.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        if not rows:
            return
        connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])
        try:
            channel = await connection.channel(publisher_confirms=True)
            for row in rows:
                await topology(channel, row.queue)
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        json.dumps(row.payload).encode(),
                        content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ),
                    routing_key=row.queue,
                    mandatory=True,
                )
                row.published_at = now()
        finally:
            await connection.close()


def recover_expired():
    with Session.begin() as db:
        for job in db.scalars(select(Job).where(Job.status == "running", Job.lease_until < now())):
            db.scalar(select(Chat).where(Chat.id == job.user_id).with_for_update())
            db.refresh(job, with_for_update=True)
            if (
                job.status != "running"
                or not job.lease_until
                or job.lease_until.replace(tzinfo=timezone.utc) >= now()
            ):
                continue
            # A recovered delivery must obtain a fresh fence. Prior worker output is rejected.
            job.status, job.claim_token = "queued", None
            db.add(
                Outbox(
                    queue="chat" if job.kind == "assistant" else "transcription", payload={"job_id": job.id}
                )
            )
            event(db, job.user_id, "job", {"id": job.id, "status": job.status, "result": None, "error": None})


def reminders_once():
    with Session.begin() as db:
        user_ids = db.scalars(select(Record.user_id).where(Record.deleted.is_(False)).distinct()).all()
        current = now()
        for user_id in user_ids:
            # Shared user lock serializes concurrent schedulers and record updates.
            db.scalar(select(Chat).where(Chat.id == user_id).with_for_update())
            past = records(
                db,
                user_id,
                Query(
                    status="active",
                    period_start=(current - timedelta(days=366)).date(),
                    period_end=current.date(),
                ),
            )
            future = records(
                db,
                user_id,
                Query(
                    status="active",
                    period_start=current.date(),
                    period_end=(current + timedelta(days=366)).date(),
                ),
            )
            items = list({(x["id"], x.get("occurrence_at")): x for x in past + future}.values())
            for item in items:
                stamp = item.get("remind_at")
                if item["kind"] == "meeting" and item.get("reminder_minutes") is not None:
                    stamp = (
                        instant(item["start_at"]) - timedelta(minutes=item["reminder_minutes"])
                    ).isoformat()
                if not stamp or instant(stamp) > current:
                    continue
                occurrence = item.get("occurrence_at") or ""
                existing = db.scalar(
                    select(Notification.id).where(
                        Notification.user_id == user_id,
                        Notification.record_id == item["id"],
                        Notification.occurrence_at == occurrence,
                        Notification.due_at == stamp,
                    )
                )
                if existing:
                    continue
                from backend.common.runtime import uid

                notice = Notification(
                    id=uid(),
                    user_id=user_id,
                    record_id=item["id"],
                    occurrence_at=occurrence,
                    title=item["title"],
                    due_at=stamp,
                )
                db.add(notice)
                event(
                    db,
                    user_id,
                    "notification",
                    {
                        "id": notice.id,
                        "record_id": item["id"],
                        "occurrence_at": occurrence,
                        "title": item["title"],
                        "due_at": stamp,
                        "read": False,
                        "read_at": None,
                    },
                )
                db.flush()


async def result_handler(payload):
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            os.getenv("CORE_URL", "http://chat-calendar-task-notification:8000")
            + "/internal/jobs/"
            + payload["job_id"]
            + "/result",
            json={key: value for key, value in payload.items() if key != "job_id"},
            headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
        )
        if response.status_code == 409:
            return  # Superseded lease result is a successful discard.
        response.raise_for_status()


async def main(kind):
    if kind == "results":
        return await consume("llm_response", result_handler)
    while True:
        try:
            if kind == "publisher":
                await publish_outbox_once()
                await asyncio.to_thread(recover_expired)
            elif kind == "reminders":
                await asyncio.to_thread(reminders_once)
            else:
                raise ValueError("Unknown process")
        except Exception:
            logger.exception("Background iteration failed")
        await asyncio.sleep(1 if kind == "publisher" else 10)


if __name__ == "__main__":
    import sys

    asyncio.run(main(sys.argv[1]))
