"""Assistant worker: claims LLM jobs from the chat queue and publishes decisions."""

import asyncio
import logging
import os

import httpx
from backend.llm.client import InvalidModelResponse, StaleJob, complete, run_decision
from backend.llm.prompt import system_prompt

logger = logging.getLogger(__name__)


def public_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 402:
            return (
                "Провайдер модели отклонил запрос по оплате. "
                "Проверьте баланс и лимит оплаты ключа API у провайдера. Изменения не сохранены."
            )
        if status in (401, 403):
            return "Провайдер модели отклонил доступ. Проверьте API-ключ и доступ к выбранной модели. Изменения не сохранены."
    if isinstance(exc, InvalidModelResponse):
        return str(exc)
    return "Не удалось обработать запрос. Проверьте доступность модели и её настройки, затем попробуйте ещё раз. Изменения не сохранены."


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
    from backend.chat.broker import publish

    job_id = payload["job_id"]
    context = await core(f"/internal/jobs/{job_id}/claim", {"kind": "assistant"})
    if context is None:
        return
    logger.info("Job claimed id=%s kind=assistant", job_id)
    try:
        async def query(value):
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    os.getenv("RAG_URL", "http://rag:8000") + "/query",
                    headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
                    json={"user_id": context["user_id"], "timezone": context["timezone"], "query": value},
                )
                response.raise_for_status()
                return response.json()

        messages = [{"role": "system", "content": system_prompt(context)}]
        messages.extend(
            {"role": item["role"], "content": item.get("text", item.get("content", ""))}
            for item in context["messages"]
        )
        async def validate(decision):
            return await core(f"/internal/jobs/{job_id}/validate", {
                "claim_token": context["claim_token"], "decision": decision,
            })

        decision = await run_decision(messages, complete, query, validate)

    except StaleJob:
        logger.info("Discarding stale job id=%s", job_id)
        return

    except Exception as exc:
        transient = isinstance(exc, (httpx.RequestError, TimeoutError)) or (
            isinstance(exc, httpx.HTTPStatusError)
            and (exc.response.status_code == 429 or exc.response.status_code >= 500)
        )
        if transient:
            await core(f"/internal/jobs/{job_id}/retry", {"claim_token": context["claim_token"]})
            raise
        logger.warning(
            "Job failed id=%s kind=assistant error=%s status=%s",
            job_id,
            type(exc).__name__,
            exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
        )
        await core(
            f"/internal/jobs/{job_id}/result",
            {"error": public_error(exc), "claim_token": context["claim_token"]},
        )
        return

    try:
        await publish(
            "llm_response", {"job_id": job_id, "decision": decision, "claim_token": context["claim_token"]}
        )
        logger.info("Decision published id=%s type=%s", job_id, decision["type"])
    except Exception:
        await core(f"/internal/jobs/{job_id}/retry", {"claim_token": context["claim_token"]})
        raise


async def main():
    from backend.common.config import llm_profiles
    profiles = [{"name": k, "label": v.get("label", k)} for k, v in llm_profiles().items()]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.put(
                os.getenv("CORE_URL", "http://core:8000") + "/internal/settings/providers",
                json={"profiles": profiles},
                headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
            )
            r.raise_for_status()
        logger.info("Registered %d LLM profiles with core", len(profiles))
    except Exception:
        logger.warning("Could not register LLM profiles with core")
    logger.info("Assistant worker starting queue=chat")
    from backend.chat.broker import consume

    await consume("chat", handle_job)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
