# syntax=docker/dockerfile:1
# Build context: repo root
FROM ghcr.io/astral-sh/uv:0.9.7 AS uv
FROM docker.io/python:3.12.12-slim-bookworm AS dependencies
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY shared/pyproject.toml shared/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM docker.io/python:3.12.12-slim-bookworm AS runtime
WORKDIR /app
LABEL org.opencontainers.image.title="Golos v2 — yandex-calendar"
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
COPY --link --from=dependencies /app/.venv /app/.venv
COPY --link --chown=10001:10001 shared/backend/ /app/backend/
COPY --link --chown=10001:10001 services/yandex-calendar/app/backend/ /app/backend/
USER 10001:10001
CMD ["uvicorn", "backend.yandex_calendar.main:app", "--host", "0.0.0.0", "--port", "8000"]
