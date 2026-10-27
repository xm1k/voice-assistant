
"""Authenticated audio intake; transcription-worker owns speech recognition."""

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile
from pydantic import BaseModel

from backend.common.errors import install_validation_handler
from backend.common.security import identity


async def audio_references():
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            os.getenv("CORE_URL", "http://chat-calendar-task-notification:8000")
            + "/internal/audio/references",
            headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
        )
        response.raise_for_status()
        items = response.json()["items"]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError("Invalid audio references")
        return set(items)


async def cleanup_audio(references=audio_references):
    try:
        retained = await references()
    except Exception:
        return  # Lack of authoritative references never grants permission to delete.
    root = Path(os.getenv("AUDIO_DIR", "/tmp/audio"))
    cutoff = time.time() - 24 * 3600
    for candidate in root.glob("*.audio"):
        try:
            path = trusted_audio_path(candidate.name)
            if candidate.name not in retained and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except (ValueError, OSError):
            continue


async def cleanup_loop():
    while True:
        await cleanup_audio()
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan, docs_url="/voice/docs", openapi_url="/voice/openapi.json")
install_validation_handler(app)
MAX_BYTES = 25 * 1024 * 1024


def safe_audio_path(directory):
    return Path(directory) / (str(uuid4()) + ".audio")


def trusted_audio_path(value):
    root = Path(os.getenv("AUDIO_DIR", "/tmp/audio")).resolve()
    path = root / value
    if Path(value).name != value or path.resolve().parent != root or path.is_symlink():
        raise ValueError("Invalid audio path")
    if path.suffix != ".audio" or str(UUID(path.stem)) != path.stem:
        raise ValueError("Invalid audio filename")
    return path


class JobAccepted(BaseModel):
    job_id: str
    status: str


@app.post("/api/v1/audio", status_code=202, response_model=JobAccepted)
async def upload_audio(
    file: UploadFile, user=Depends(identity), idempotency_key: str = Header(alias="Idempotency-Key")
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(400, "Invalid Idempotency-Key")
    directory = Path(os.getenv("AUDIO_DIR", "/tmp/audio"))
    directory.mkdir(parents=True, exist_ok=True)
    path = safe_audio_path(directory)
    try:
        size = 0
        with path.open("xb") as target:
            while chunk := await file.read(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, "Audio exceeds 25 MB")
                target.write(chunk)
        if not size:
            raise HTTPException(400, "Audio is empty")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                os.getenv("CORE_URL", "http://chat-calendar-task-notification:8000")
                + "/internal/transcriptions",
                headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
                json={
                    "user_id": user["id"],
                    "request_id": idempotency_key,
                    "audio_path": path.name,
                    "timezone": user["timezone"],
                },
            )
            response.raise_for_status()
            result = response.json()
        if result.get("audio_path", path.name) != path.name or result.get("deduplicated"):
            path.unlink(missing_ok=True)
        return {"job_id": result["job_id"], "status": result["status"]}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            path.unlink(missing_ok=True)
        raise
    except (httpx.TimeoutException, httpx.NetworkError):
        # Registration may already have committed. Keep audio for the queued worker.
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
