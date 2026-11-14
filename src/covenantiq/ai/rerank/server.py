"""The reranker process: a small HTTP service that holds the cross-encoder model.

It exists so that PyTorch, the model and its CPU threads are never inside the API or a worker.
Reranking is CPU-heavy (about 14 query/passage pairs per second on a desktop CPU for the 150M
model), and in-process it would stall the event loop and compete with extraction. As its own
container it can be given its own CPU and memory limit, restarted without touching anything else,
and scaled on its own.

It is started with `covenantiq rerank-serve` and reads only its own few settings; it has no
database, broker or API keys, and it never logs the text it scores.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Protocol

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from covenantiq.core.config import ETTIN_RERANK_MODEL
from covenantiq.core.logging import get_logger

log = get_logger(__name__)

MAX_QUERY_CHARS = 2000
MAX_DOCUMENT_CHARS = 8000
# Requests in flight or queued (the one being scored plus those waiting behind it). Beyond this
# the server answers 503 at once, so a caller with a 3 second budget falls back instead of
# queueing past it.
MAX_PENDING = 3


class RerankServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CIQ_", extra="ignore")

    rerank_model: str = ETTIN_RERANK_MODEL
    rerank_threads: Annotated[int, Field(gt=0)] = 4
    rerank_max_tokens: Annotated[int, Field(gt=0, le=8000)] = 512
    rerank_batch_size: Annotated[int, Field(gt=0)] = 4
    rerank_max_documents: Annotated[int, Field(gt=0)] = 100
    rerank_port: Annotated[int, Field(gt=0, lt=65536)] = 8100


class DeadlineExceeded(Exception):
    """The caller's time budget ran out before every document was scored."""


class Scorer(Protocol):
    name: str

    def score(
        self, query: str, documents: Sequence[str], deadline: float | None = None
    ) -> list[float]:
        """Raw relevance scores, one per document, in order. Blocking. Raises
        `DeadlineExceeded` if `deadline` (a `time.monotonic()` value) passes first."""
        ...


def score_in_batches(
    predict: Callable[[list[tuple[str, str]]], Sequence[float]],
    query: str,
    documents: Sequence[str],
    *,
    batch_size: int,
    deadline: float | None,
    clock: Callable[[], float] = time.monotonic,
) -> list[float]:
    """Score in small batches, checking the caller's deadline between them.

    Reranking is slow on a CPU, so a request that cannot finish inside its caller's budget must stop
    early: otherwise the caller gives up after 3 seconds while the model carries on for 20 and
    everything queued behind it waits for work nobody will read. A batch is not started if the
    previous batch's duration says it would end after the deadline. Documents are scored shortest
    first, which wastes the least padding, and the scores are returned in the original order."""
    order = sorted(range(len(documents)), key=lambda index: len(documents[index]))
    scores = [0.0] * len(documents)
    last_batch_seconds = 0.0
    for start in range(0, len(order), batch_size):
        if deadline is not None and clock() + last_batch_seconds >= deadline:
            raise DeadlineExceeded
        chunk = order[start : start + batch_size]
        batch_started = clock()
        for index, value in zip(
            chunk, predict([(query, documents[index]) for index in chunk]), strict=True
        ):
            scores[index] = float(value)
        last_batch_seconds = clock() - batch_started
    return scores


class CrossEncoderScorer:
    """The real model, through sentence-transformers. Loading takes a few seconds and a few
    hundred MB; do it once, at start-up."""

    def __init__(self, model_name: str, *, threads: int, max_tokens: int, batch_size: int) -> None:
        # imported here so that nothing else in the application pulls in PyTorch
        import torch
        from sentence_transformers import CrossEncoder

        torch.set_num_threads(threads)
        self.name = model_name
        self._batch_size = batch_size
        self._model = CrossEncoder(model_name, max_length=max_tokens, device="cpu")

    def score(
        self, query: str, documents: Sequence[str], deadline: float | None = None
    ) -> list[float]:
        return score_in_batches(
            lambda pairs: self._model.predict(
                pairs, batch_size=self._batch_size, show_progress_bar=False
            ),
            query,
            documents,
            batch_size=self._batch_size,
            deadline=deadline,
        )


class RerankRequest(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)]
    documents: list[Annotated[str, Field(max_length=MAX_DOCUMENT_CHARS)]]
    top_k: Annotated[int, Field(gt=0)] | None = None
    # How long the caller will wait, in milliseconds from now. The server stops scoring when it
    # runs out (waiting for the model counts) and answers 504, freeing the CPU.
    deadline_ms: Annotated[int, Field(gt=0, le=600_000)] | None = None


class RerankHit(BaseModel):
    index: int
    score: float


class RerankResponse(BaseModel):
    model: str
    results: list[RerankHit]
    elapsed_ms: int


def create_app(
    load_scorer: Callable[[], Scorer], *, max_documents: int = 100, max_pending: int = MAX_PENDING
) -> FastAPI:
    state: dict[str, Scorer] = {}
    inference = asyncio.Lock()  # one scoring job at a time: the model uses every thread it has
    pending = 0

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        started = time.monotonic()
        state["scorer"] = await asyncio.to_thread(load_scorer)
        log.info(
            "rerank.model_loaded",
            model=state["scorer"].name,
            seconds=round(time.monotonic() - started, 1),
        )
        yield

    app = FastAPI(title="CovenantIQ reranker", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, str]:
        if "scorer" not in state:
            response.status_code = 503
            return {"status": "loading"}
        return {"status": "ok", "model": state["scorer"].name}

    @app.post("/v1/rerank", response_model=RerankResponse)
    async def rerank(request: RerankRequest) -> RerankResponse:
        nonlocal pending
        arrived = time.monotonic()
        deadline = arrived + request.deadline_ms / 1000 if request.deadline_ms else None
        scorer = state["scorer"]
        if not request.documents:
            raise HTTPException(status_code=422, detail="documents must not be empty")
        if len(request.documents) > max_documents:
            raise HTTPException(
                status_code=413, detail=f"at most {max_documents} documents per request"
            )
        if pending >= max_pending:
            raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "1"})

        pending += 1
        try:
            async with inference:
                started = time.monotonic()
                try:
                    scores = await asyncio.to_thread(
                        scorer.score, request.query, request.documents, deadline
                    )
                except DeadlineExceeded:
                    log.warning(
                        "rerank.deadline_exceeded",
                        documents=len(request.documents),
                        waited_ms=round((started - arrived) * 1000),
                        scored_ms=round((time.monotonic() - started) * 1000),
                    )
                    raise HTTPException(status_code=504, detail="deadline exceeded") from None
                elapsed_ms = round((time.monotonic() - started) * 1000)
        finally:
            pending -= 1

        ranked = sorted(enumerate(scores), key=lambda pair: (-pair[1], pair[0]))
        top_k = request.top_k or len(ranked)
        log.info("rerank.scored", documents=len(request.documents), elapsed_ms=elapsed_ms)
        return RerankResponse(
            model=scorer.name,
            results=[RerankHit(index=index, score=score) for index, score in ranked[:top_k]],
            elapsed_ms=elapsed_ms,
        )

    return app


def app_from_environment() -> FastAPI:
    """The app `covenantiq rerank-serve` runs, configured from `CIQ_RERANK_*` variables."""
    settings = RerankServerSettings()
    return create_app(
        lambda: CrossEncoderScorer(
            settings.rerank_model,
            threads=settings.rerank_threads,
            max_tokens=settings.rerank_max_tokens,
            batch_size=settings.rerank_batch_size,
        ),
        max_documents=settings.rerank_max_documents,
    )
