"""A delivery completes only after core acknowledges the durable Google result."""

import asyncio
import os

import httpx
from backend.chat.broker import consume

from .service import execute


async def handle(payload):
    operation_id = payload["operation_id"]
    async with httpx.AsyncClient(
        timeout=45, headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]}
    ) as client:
        url = (
            os.getenv("CORE_URL", "http://core:8000") + "/internal/google-calendar/operations/" + operation_id
        )
        response = await client.get(url)
        response.raise_for_status()
        operation = response.json()
        if operation is None:
            return
        if operation.get("blocked"):
            result = {
                "status": operation["blocked"],
                "generation": operation["generation"],
                "version": operation["version"],
            }
        else:
            result = await asyncio.to_thread(execute, operation)
        response = await client.post(url + "/result", json=result)
        response.raise_for_status()


async def run():
    await consume("google_calendar", handle)
