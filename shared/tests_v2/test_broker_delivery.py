import asyncio
import copy
import json
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aio_pika
import httpx
from backend.chat import broker
from pamqp.commands import Basic


class Delivery(aio_pika.Message):
    def __init__(self, events, changed, *, attempt=0):
        super().__init__(
            b'{"job_id":"job-1"}',
            headers={"attempt": attempt, "trace": "trace-1"},
            message_id="message-1",
            correlation_id="correlation-1",
            content_type="application/json",
            content_encoding="utf-8",
            priority=3,
            reply_to="reply",
            type="job",
            app_id="assistant",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            timestamp=datetime.now(timezone.utc),
        )
        self.events, self.changed = events, changed
        self.ack_error = self.nack_error = None

    async def ack(self):
        self.events.append("ack")
        self.changed.set()
        if self.ack_error:
            raise self.ack_error

    async def nack(self, *, requeue):
        assert requeue
        self.events.append("nack")
        self.changed.set()
        if self.nack_error:
            raise self.nack_error


class BrokerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.changed = asyncio.Event()
        self.message = Delivery(self.events, self.changed)
        self.task = None
        self.route_error = self.exhaust_error = None
        self.patches = []

    async def asyncTearDown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for patcher in reversed(self.patches):
            patcher.stop()

    def replace(self, target, attribute, value):
        patcher = patch.object(target, attribute, value)
        self.patches.append(patcher)
        return patcher.start()

    async def until(self, predicate):
        async def wait():
            while not predicate():
                self.changed.clear()
                await self.changed.wait()

        await asyncio.wait_for(wait(), 3)

    def start(self, handler, queue="chat"):
        owner = self

        class Iterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if "delivered" not in owner.events:
                    owner.events.append("delivered")
                    return owner.message
                await asyncio.Future()

        @asynccontextmanager
        async def iterator():
            yield Iterator()

        async def close():
            self.events.append("close")
            self.changed.set()

        async def backoff(_):
            self.events.append("backoff")
            self.changed.set()
            await asyncio.Future()

        async def route(queue, destination, message, attempt):
            self.events.append(("route", destination, attempt))
            self.changed.set()
            if self.route_error:
                raise self.route_error

        async def exhaust(*_):
            self.events.append("exhausted")
            self.changed.set()
            if self.exhaust_error:
                raise self.exhaust_error

        self.channel = SimpleNamespace(set_qos=AsyncMock())
        self.connection = SimpleNamespace(
            channel=AsyncMock(return_value=self.channel), close=AsyncMock(side_effect=close)
        )
        self.replace(broker.aio_pika, "connect_robust", AsyncMock(return_value=self.connection))
        self.replace(broker, "topology", AsyncMock(return_value=SimpleNamespace(iterator=iterator)))
        self.replace(broker, "_route_failed", AsyncMock(side_effect=route))
        self.replace(broker, "exhausted", AsyncMock(side_effect=exhaust))
        self.replace(broker.asyncio, "sleep", backoff)
        self.env = patch.dict("os.environ", {"RABBITMQ_URL": "amqp://unit-test"})
        self.env.start()
        self.patches.append(self.env)
        self.task = asyncio.create_task(broker.consume(queue, handler))

    async def failing_handler(self, _):
        self.events.append("handler")
        raise RuntimeError("temporary failure")

    async def test_success_ack_is_after_handler(self):
        release = asyncio.Event()

        async def handler(_):
            self.events.append("handler")
            self.changed.set()
            await release.wait()
            self.events.append("saved")

        self.start(handler)
        await self.until(lambda: "handler" in self.events)
        self.assertNotIn("ack", self.events)
        release.set()
        await self.until(lambda: "ack" in self.events)
        self.assertLess(self.events.index("saved"), self.events.index("ack"))

    async def test_retry_is_confirmed_before_ack(self):
        self.start(self.failing_handler)
        await self.until(lambda: ("route", "chat.retry", 1) in self.events)
        await self.until(lambda: "ack" in self.events)
        self.assertLess(self.events.index(("route", "chat.retry", 1)), self.events.index("ack"))

    async def test_dead_then_exhausted_then_ack(self):
        self.message.headers["attempt"] = 4
        self.start(self.failing_handler)
        await self.until(lambda: "exhausted" in self.events and ("route", "chat.dead", 5) in self.events)
        await self.until(lambda: "ack" in self.events)
        self.assertLess(self.events.index(("route", "chat.dead", 5)), self.events.index("exhausted"))
        self.assertLess(self.events.index("exhausted"), self.events.index("ack"))

    async def test_failed_retry_requeues_without_ack(self):
        self.route_error = ConnectionError("publish failed")
        self.start(self.failing_handler)
        await self.until(lambda: ("route", "chat.retry", 1) in self.events)
        self.assertNotIn("ack", self.events)
        await self.until(lambda: "nack" in self.events and "backoff" in self.events)
        self.assertIn("close", self.events)

    async def test_unavailable_core_after_dead_does_not_ack(self):
        self.message.headers["attempt"] = 4
        self.exhaust_error = ConnectionError("core unavailable")
        self.start(self.failing_handler)
        await self.until(lambda: "exhausted" in self.events)
        self.assertNotIn("ack", self.events)
        await self.until(lambda: "nack" in self.events)

    async def test_cancellation_closes_without_retry_or_ack(self):
        async def handler(_):
            self.events.append("handler")
            self.changed.set()
            await asyncio.Future()

        self.start(handler)
        await self.until(lambda: "handler" in self.events)
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.assertIn("close", self.events)
        self.assertNotIn("ack", self.events)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == "route" for e in self.events))

    async def test_lost_ack_closes_without_republishing_result(self):
        self.message.ack_error = ConnectionError("ack connection lost")

        async def handler(_):
            self.events.append("saved")

        self.start(handler)
        await self.until(lambda: "close" in self.events)
        self.assertIn("saved", self.events)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == "route" for e in self.events))

    async def test_lost_nack_still_closes_connection(self):
        self.route_error = ConnectionError("publish lost")
        self.message.nack_error = ConnectionError("channel lost")
        self.start(self.failing_handler)
        await self.until(lambda: "close" in self.events)
        self.assertNotIn("ack", self.events)

    async def test_409_does_not_increment_attempt(self):
        async def handler(_):
            raise httpx.HTTPStatusError(
                "lease held", request=httpx.Request("POST", "http://core"), response=httpx.Response(409)
            )

        self.start(handler)
        await self.until(lambda: ("route", "chat.retry", 0) in self.events)
        self.assertLess(self.events.index(("route", "chat.retry", 0)), self.events.index("ack"))

    async def test_google_retry_after_uses_durable_delay_before_ack(self):
        class QuotaError(Exception):
            retry_after = 120

        async def handler(_):
            raise QuotaError()

        self.start(handler, queue="google_calendar")
        await self.until(lambda: "ack" in self.events)
        self.assertLess(self.events.index(("route", "google_calendar.retry.120000", 1)), self.events.index("ack"))

    async def test_timeout_cancels_handler_and_routes_retry(self):
        real_timeout = asyncio.timeout
        self.replace(broker.asyncio, "timeout", lambda delay: real_timeout(0 if delay == 600 else delay))

        async def handler(_):
            try:
                await asyncio.Future()
            finally:
                self.events.append("cancelled")
                self.changed.set()

        self.start(handler)
        await self.until(lambda: ("route", "chat.retry", 1) in self.events)
        self.assertIn("cancelled", self.events)
        self.assertLess(self.events.index("cancelled"), self.events.index("ack"))


class ConfirmedPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_retry_after_declares_durable_delay_before_publication(self):
        events = []

        async def declare(name, **kwargs):
            events.append(("declare", name, kwargs))

        async def published(*args, **kwargs):
            events.append(("publish", kwargs["routing_key"]))
            return Basic.Ack(delivery_tag=1)

        channel = SimpleNamespace(declare_queue=AsyncMock(side_effect=declare),
                                  default_exchange=SimpleNamespace(publish=AsyncMock(side_effect=published)))
        connection = SimpleNamespace(channel=AsyncMock(return_value=channel), close=AsyncMock())
        with patch.dict("os.environ", {"RABBITMQ_URL": "amqp://test"}), patch.object(
            broker.aio_pika, "connect_robust", AsyncMock(return_value=connection)
        ):
            await broker._route_failed("google_calendar", "google_calendar.retry.120000", aio_pika.Message(b"{}"), 1)
        delayed = next(e for e in events if e[:2] == ("declare", "google_calendar.retry.120000"))
        assert delayed[2]["durable"] is True
        assert delayed[2]["arguments"]["x-message-ttl"] == 120000
        assert delayed[2]["arguments"]["x-dead-letter-routing-key"] == "google_calendar"
        assert events.index(delayed) < events.index(("publish", "google_calendar.retry.120000"))

    async def test_retry_preserves_message_properties(self):
        message = Delivery([], asyncio.Event(), attempt=2)
        original = copy.copy(message)
        publish = AsyncMock(return_value=Basic.Ack(delivery_tag=1))
        channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=publish))
        connection = SimpleNamespace(channel=AsyncMock(return_value=channel), close=AsyncMock())
        with (
            patch.dict("os.environ", {"RABBITMQ_URL": "amqp://unit-test"}),
            patch.object(broker.aio_pika, "connect_robust", AsyncMock(return_value=connection)),
            patch.object(broker, "topology", AsyncMock()),
        ):
            await broker._route_failed("chat", "chat.retry", message, 3)
        routed = publish.call_args.args[0]
        self.assertEqual(routed.body, original.body)
        self.assertEqual(routed.headers, {**original.headers, "attempt": 3})
        for field in (
            "content_type",
            "content_encoding",
            "message_id",
            "correlation_id",
            "priority",
            "timestamp",
            "reply_to",
            "type",
            "app_id",
            "delivery_mode",
        ):
            self.assertEqual(getattr(routed, field), getattr(original, field), field)
        self.assertEqual(message.headers["attempt"], 2)
        self.assertTrue(connection.channel.call_args.kwargs.get("on_return_raises"))

    async def test_negative_or_missing_publish_confirmation_is_failure(self):
        for confirmation in (Basic.Nack(delivery_tag=1), Basic.Reject(delivery_tag=1), None, False):
            with self.subTest(confirmation=confirmation):
                channel = SimpleNamespace(
                    default_exchange=SimpleNamespace(publish=AsyncMock(return_value=confirmation))
                )
                connection = SimpleNamespace(channel=AsyncMock(return_value=channel), close=AsyncMock())
                with (
                    patch.dict("os.environ", {"RABBITMQ_URL": "amqp://unit-test"}),
                    patch.object(broker.aio_pika, "connect_robust", AsyncMock(return_value=connection)),
                    patch.object(broker, "topology", AsyncMock()),
                ):
                    with self.assertRaises(Exception):
                        await broker.publish("chat", {"job_id": "job-1"})
                connection.close.assert_awaited()

    async def test_returned_or_timed_out_publish_fails_and_closes(self):
        for error in (aio_pika.exceptions.DeliveryError(None, None), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                channel = SimpleNamespace(
                    default_exchange=SimpleNamespace(publish=AsyncMock(side_effect=error))
                )
                connection = SimpleNamespace(channel=AsyncMock(return_value=channel), close=AsyncMock())
                with (
                    patch.dict("os.environ", {"RABBITMQ_URL": "amqp://unit-test"}),
                    patch.object(broker.aio_pika, "connect_robust", AsyncMock(return_value=connection)),
                    patch.object(broker, "topology", AsyncMock()),
                ):
                    with self.assertRaises(type(error)):
                        await broker.publish("chat", {})
                connection.close.assert_awaited_once()

    async def test_publish_sets_confirms_returns_and_bounded_io(self):
        publish = AsyncMock(return_value=Basic.Ack(delivery_tag=1))
        channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=publish))
        connection = SimpleNamespace(channel=AsyncMock(return_value=channel), close=AsyncMock())
        connect = AsyncMock(return_value=connection)
        with (
            patch.dict("os.environ", {"RABBITMQ_URL": "amqp://unit-test"}),
            patch.object(broker.aio_pika, "connect_robust", connect),
            patch.object(broker, "topology", AsyncMock()),
        ):
            await broker.publish("chat", {"job_id": "job-1"})
        self.assertEqual(
            connection.channel.call_args.kwargs, {"publisher_confirms": True, "on_return_raises": True}
        )
        self.assertEqual(connect.call_args.kwargs["timeout"], 30)
        self.assertEqual(publish.call_args.kwargs["timeout"], 30)
        self.assertTrue(publish.call_args.kwargs["mandatory"])
        self.assertEqual(json.loads(publish.call_args.args[0].body), {"job_id": "job-1"})
