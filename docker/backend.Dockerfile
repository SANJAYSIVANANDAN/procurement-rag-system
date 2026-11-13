# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app
WORKDIR /app

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Docling model download stage is added in Milestone 7, when the dependency arrives.

FROM builder AS test
RUN uv sync --frozen
COPY tests ./tests
COPY config ./config
CMD ["pytest", "tests/unit", "-q"]

FROM base AS runtime
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src ./src
COPY --chown=app:app config ./config
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data/uploads && chown -R app:app /data
USER app
# No ENTRYPOINT or CMD: each Compose service sets its own command (uvicorn, celery, covenantiq).
