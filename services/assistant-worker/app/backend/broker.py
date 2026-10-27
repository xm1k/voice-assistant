"""Durable delivery with confirmed publishes, bounded retries and dead letters."""

import asyncio
import copy
import json
import logging
import math
import os

import aio_pika
import httpx
from pamqp.commands import Basic

logger = logging.getLogger(__name__)


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
            if queue == "yandex_calendar" and destination.startswith(queue + ".retry."):
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
    if queue == "yandex_calendar" and isinstance(payload, dict) and payload.get("operation_id"):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                os.getenv("CORE_URL", "http://core:8000")
                + "/internal/yandex-calendar/operations/" + payload["operation_id"] + "/exhausted",
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
                        if queue == "yandex_calendar" and next_attempt < 5 and not blocked:
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

