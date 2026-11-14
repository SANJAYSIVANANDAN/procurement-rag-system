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

# Only the models our pipeline uses (OCR is off): page layout and table structure. Baked in at
# build time so workers never download anything at runtime.
FROM builder AS models
RUN /app/.venv/bin/docling-tools models download layout tableformer -o /opt/docling-models

FROM builder AS test
RUN uv sync --frozen
COPY tests ./tests
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
CMD ["pytest", "tests/unit", "-q"]

FROM base AS runtime
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src ./src
COPY --chown=app:app config ./config
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --from=models --chown=app:app /opt/docling-models /opt/docling-models
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCLING_ARTIFACTS_PATH=/opt/docling-models
RUN mkdir -p /data/uploads && chown -R app:app /data
USER app
# No ENTRYPOINT or CMD: each Compose service sets its own command (uvicorn, celery, covenantiq).

# The reranker's model, baked in like the Docling models so the service never downloads anything at
# runtime (and starts offline). Pinned to a revision so a rebuild cannot silently pick up changed
# weights.
FROM builder AS rerank-models
ARG RERANK_MODEL=cross-encoder/ettin-reranker-150m-v1
ARG RERANK_MODEL_REVISION=025501c4e0f9bbeb4c5b198318e0089ff061cc14
ENV HF_HOME=/opt/hf
RUN /app/.venv/bin/python -c "import sys; from huggingface_hub import snapshot_download; snapshot_download(sys.argv[1], revision=sys.argv[2])" \
        "$RERANK_MODEL" "$RERANK_MODEL_REVISION" \
    && rm -rf /opt/hf/xet \
    # Downloading by commit hash writes no refs/main, and offline the loader resolves the default
    # revision (main) through it: point it at the pinned snapshot.
    && repo="/opt/hf/hub/models--$(echo "$RERANK_MODEL" | sed 's#/#--#g')" \
    && mkdir -p "$repo/refs" \
    && printf '%s' "$RERANK_MODEL_REVISION" > "$repo/refs/main"

# The separate reranker process (`covenantiq rerank-serve`). Same code and virtualenv as the
# backend, plus the model; nothing else in the application loads PyTorch to rerank.
FROM runtime AS reranker
COPY --from=rerank-models --chown=app:app /opt/hf /opt/hf
ENV HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1
