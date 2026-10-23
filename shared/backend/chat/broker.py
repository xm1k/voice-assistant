"""Durable delivery with outbox, confirmed publishes, bounded retries and dead letters."""

import asyncio
import copy
import json
import logging
import math
import os
from datetime import timedelta, timezone

import aio_pika
import httpx
from pamqp.commands import Basic
from sqlalchemy import select

from backend.chat.domain import event, instant, records
from backend.chat.models import Chat, Job, Notification, Outbox, Record
from backend.common.runtime import Session, now
from backend.common.schemas import Query

logger = logging.getLogger(__name__)
QUEUES = ("chat", "llm_response", "transcription", "index", "google_calendar")


async def topology(channel, name):
    await channel.declare_queue(name + ".dead", durable=True)
    await channel.declare_queue(
        name + ".retry",
        durable=True,
        arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": name, "x-message-ttl": 5000},
    )
    return await channel.declare_queue(name, durable=True)


async def _confirmed_publish(channel, message, destination):
    confirmation = await channel.default_exchange.publish(
        message, routing_key=destination, mandatory=True, timeout=30
    )
    if not isinstance(confirmation, Basic.Ack):
        raise RuntimeError("RabbitMQ did not confirm publication")


async def _publish_message(queue, destination, message):
    connection = None
    try:
        async with asyncio.timeout(30):
            connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"], timeout=30)
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await topology(channel, queue)
            if queue == "google_calendar" and destination.startswith(queue + ".retry."):
                delay_ms = int(destination.rsplit(".", 1)[1])
                await channel.declare_queue(
                    destination, durable=True,
                    arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": queue,
                               "x-message-ttl": delay_ms, "x-expires": max(86400000, delay_ms * 2)},
                )
            await _confirmed_publish(channel, message, destination)
    finally:
        if connection is not None:
            async with asyncio.timeout(30):
                await connection.close()


async def publish(queue, payload):
    await _publish_message(
        queue,
        queue,
        aio_pika.Message(
            json.dumps(payload).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
    )


async def exhausted(payload, queue):
    if queue == "google_calendar" and isinstance(payload, dict) and payload.get("operation_id"):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                os.getenv("CORE_URL", "http://core:8000")
                + "/internal/google-calendar/operations/" + payload["operation_id"] + "/exhausted",
                headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
            )
            response.raise_for_status()
        return
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


async def _route_failed(queue, destination, message, attempt):
    routed = copy.copy(message)
    routed.headers = {**(message.headers or {}), "attempt": attempt}
    await _publish_message(queue, destination, routed)


async def consume(queue, handler):
    """Own delivery settlement and reconnection for every worker queue."""
    while True:
        connection = None
        try:
            async with asyncio.timeout(30):
                connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"], timeout=30)
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                await channel.set_qos(prefetch_count=1)
                source = await topology(channel, queue)
            async with source.iterator() as messages:
                async for message in messages:
                    attempt = int((message.headers or {}).get("attempt", 0))
                    try:
                        payload = json.loads(message.body)
                    except Exception:
                        await message.ack()
                        continue
                    try:
                        async with asyncio.timeout(600):
                            await handler(payload)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        blocked = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409
                        next_attempt = attempt + (0 if blocked else 1)
                        destination = queue + (".dead" if next_attempt >= 5 else ".retry")
                        if queue == "google_calendar" and next_attempt < 5 and not blocked:
                            delay = max(5 * 2 ** attempt, getattr(exc, "retry_after", 0) or 0)
                            destination = queue + ".retry." + str(math.ceil(delay * 1000))
                        logger.warning(
                            "Delivery failed queue=%s attempt=%s error=%s",
                            queue,
                            next_attempt,
                            type(exc).__name__,
                        )
                        try:
                            await _route_failed(queue, destination, message, next_attempt)
                            if next_attempt >= 5:
                                async with asyncio.timeout(30):
                                    await exhausted(payload, queue)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            try:
                                async with asyncio.timeout(30):
                                    await message.nack(requeue=True)
                            except Exception:
                                logger.exception("Unable to requeue delivery queue=%s", queue)
                            raise
                    # Keep ACK errors outside the handler-error branch: the durable effect may exist.
                    async with asyncio.timeout(30):
                        await message.ack()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Consumer disconnected queue=%s", queue)
        finally:
            if connection is not None:
                try:
                    async with asyncio.timeout(30):
                        await connection.close()
                except Exception:
                    logger.exception("Consumer close failed queue=%s", queue)
        await asyncio.sleep(5)


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
        connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"], timeout=30)
        try:
            async with asyncio.timeout(30):
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            for row in rows:
                async with asyncio.timeout(30):
                    await topology(channel, row.queue)
                await _confirmed_publish(
                    channel,
                    aio_pika.Message(
                        json.dumps(row.payload).encode(),
                        content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ),
                    row.queue,
                )
                row.published_at = now()
        finally:
            async with asyncio.timeout(30):
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
