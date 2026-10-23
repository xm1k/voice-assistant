import asyncio
from unittest.mock import patch

import httpx
import pytest
from backend.google_calendar import worker


def test_core_unavailable_after_google_success_never_reexecutes_inline():
    calls = []
    operation = {"id": "op", "generation": "generation", "version": 2}

    def core(request):
        calls.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json=operation)
        return httpx.Response(503)

    def execute(value):
        assert value == operation
        return {"status": "synced", "generation": "generation", "version": 2}

    async def run():
        network = httpx.AsyncClient(transport=httpx.MockTransport(core))
        with (
            patch.dict("os.environ", {"INTERNAL_TOKEN": "test"}),
            patch.object(worker, "execute", execute),
            patch.object(worker.httpx, "AsyncClient", return_value=network),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await worker.handle({"operation_id": "op"})

    asyncio.run(run())
    assert calls == [
        "/internal/google-calendar/operations/op",
        "/internal/google-calendar/operations/op/result",
    ]


def test_terminal_operation_is_not_sent_to_google_again():
    def core(request):
        return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})

    def execute(_):
        pytest.fail("completed operation must not reach Google")

    async def run():
        network = httpx.AsyncClient(transport=httpx.MockTransport(core))
        with (
            patch.dict("os.environ", {"INTERNAL_TOKEN": "test"}),
            patch.object(worker, "execute", execute),
            patch.object(worker.httpx, "AsyncClient", return_value=network),
        ):
            await worker.handle({"operation_id": "op"})

    asyncio.run(run())
