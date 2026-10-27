# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.7 AS uv
FROM docker.io/python:3.12.12-slim-bookworm AS dependencies
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM docker.io/python:3.12.12-slim-bookworm AS runtime
WORKDIR /app
LABEL org.opencontainers.image.title="Golos v2 — assistant-worker"
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/models/huggingface
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir /audio /models && chown 10001:10001 /audio /models
COPY --link --from=dependencies /app/.venv /app/.venv
COPY --link --chown=10001:10001 backend/ /app/backend/
COPY --link --chown=10001:10001 config/ /app/config/
USER 10001:10001
CMD ["python", "-m", "backend.workers.assistant"]
