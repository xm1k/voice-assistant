"""Transcription worker: claims audio jobs from the transcription queue."""

import asyncio
import logging
import os

import httpx
from backend.voice.main import transcribe, trusted_audio_path

logger = logging.getLogger(__name__)


def public_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 402:
            return (
                "Провайдер распознавания речи отклонил запрос по оплате. "
                "Проверьте баланс и лимит оплаты ключа API у провайдера. Изменения не сохранены."
            )
        if status in (401, 403):
            return "Провайдер распознавания речи отклонил доступ. Проверьте API-ключ и доступ к выбранной модели. Изменения не сохранены."
    return "Не удалось распознать аудио. Проверьте файл и попробуйте ещё раз."


async def core(path, body):
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            os.getenv("CORE_URL", "http://chat-calendar-task-notification:8000") + path,
            json=body,
            headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
        )
        response.raise_for_status()
        return response.json()


async def handle_job(payload):
    job_id = payload["job_id"]
    context = await core(f"/internal/jobs/{job_id}/claim", {"kind": "transcription"})
    if context is None:
        return
    logger.info("Job claimed id=%s kind=transcription", job_id)
    try:
        text = await transcribe(context["audio_path"])
        await core(
            f"/internal/jobs/{job_id}/result", {"text": text, "claim_token": context["claim_token"]}
        )
        trusted_audio_path(context["audio_path"]).unlink(missing_ok=True)

    except Exception as exc:
        transient = isinstance(exc, (httpx.RequestError, TimeoutError)) or (
            isinstance(exc, httpx.HTTPStatusError)
            and (exc.response.status_code == 429 or exc.response.status_code >= 500)
        )
        if transient:
            await core(f"/internal/jobs/{job_id}/retry", {"claim_token": context["claim_token"]})
            raise
        logger.warning(
            "Job failed id=%s kind=transcription error=%s status=%s",
            job_id,
            type(exc).__name__,
            exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
        )
        await core(
            f"/internal/jobs/{job_id}/result",
            {"error": public_error(exc), "claim_token": context["claim_token"]},
        )
        trusted_audio_path(context["audio_path"]).unlink(missing_ok=True)


async def main():
    logger.info("Transcription worker starting queue=transcription")
    from backend.chat.broker import consume

    await consume("transcription", handle_job)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
