"""Bounded, provider-configured transcription of uploaded audio."""

import asyncio
import os
from pathlib import Path
from uuid import UUID

import httpx

from backend.common.config import configuration

_model = None
_model_key = None
_model_lock = asyncio.Lock()


def trusted_audio_path(value):
    root = Path(os.getenv("AUDIO_DIR", "/tmp/audio")).resolve()
    path = root / value
    if Path(value).name != value or path.resolve().parent != root or path.is_symlink():
        raise ValueError("Invalid audio path")
    if path.suffix != ".audio" or str(UUID(path.stem)) != path.stem:
        raise ValueError("Invalid audio filename")
    return path


def decode_duration(path):
    import av

    seconds = 0.0
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            seconds += frame.samples / frame.sample_rate
            if seconds > 180:
                raise ValueError("Audio exceeds 180 seconds")
    if seconds == 0:
        raise ValueError("Audio contains no sound stream")


async def transcribe(value):
    global _model, _model_key
    path = trusted_audio_path(value)
    await asyncio.to_thread(decode_duration, path)
    config = configuration()["stt"]
    if config["provider"] == "local_whisper":
        async with _model_lock:
            key = (config["model"], config.get("device", "cpu"), config.get("compute_type", "int8"))
            if _model is None or key != _model_key:
                from faster_whisper import WhisperModel

                _model = await asyncio.to_thread(WhisperModel, key[0], device=key[1], compute_type=key[2])
                _model_key = key

            def execute():
                segments, _ = _model.transcribe(str(path), language=config.get("language"))
                return " ".join(segment.text.strip() for segment in segments).strip()

            return await asyncio.to_thread(execute)
    async with httpx.AsyncClient(timeout=config.get("timeout_seconds", 120)) as client:
        with path.open("rb") as source:
            response = await client.post(
                config["base_url"].rstrip("/") + "/audio/transcriptions",
                headers={"Authorization": "Bearer " + os.environ[config["api_key_env"]]},
                data={"model": config["model"], "language": config.get("language", "ru")},
                files={"file": ("audio", source, "application/octet-stream")},
            )
        response.raise_for_status()
        return response.json()["text"]
