# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

LABEL org.opencontainers.image.title="Golos v2 — yandex-calendar"
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
COPY --link --chown=10001:10001 backend/ /app/backend/
USER 10001:10001
CMD ["uvicorn", "backend.yandex_calendar.main:app", "--host", "0.0.0.0", "--port", "8000"]
