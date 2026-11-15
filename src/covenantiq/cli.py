import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

import typer
from alembic import command
from pydantic import ValidationError

from covenantiq import __version__
from covenantiq.api.container import Container, build_container
from covenantiq.core.config import Settings
from covenantiq.db.migrations import alembic_config
from covenantiq.graphs.checkpointing import setup_checkpointer_sync

app = typer.Typer(help="CovenantIQ command line.", no_args_is_help=True)
dlq_app = typer.Typer(help="Inspect and replay dead-lettered tasks.", no_args_is_help=True)
app.add_typer(dlq_app, name="dlq")


def run_with_container[T](action: Callable[[Container], Awaitable[T]]) -> T:
    """Build the same container the API uses, run one async action, and clean up."""

    async def go() -> T:
        container = build_container(Settings())
        try:
            return await action(container)
        finally:
            await container.aclose()

    try:
        return asyncio.run(go())
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            typer.echo(f"{location}: {error['msg']}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def migrate() -> None:
    """Upgrade the database schema to the latest Alembic revision and create the LangGraph
    checkpoint tables."""
    command.upgrade(alembic_config(), "head")
    typer.echo("database is at head")
    setup_checkpointer_sync(Settings().checkpoint_db_url.get_secret_value())
    typer.echo("checkpoint tables are ready")


@app.command("check-config")
def check_config() -> None:
    """Load settings from the environment and report problems (secrets are never printed)."""
    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            typer.echo(f"{location}: {error['msg']}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"configuration ok (env={settings.env.value})")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="The question to search the contracts for")],
    user: Annotated[str, typer.Option(help="Email of the user whose documents to search")],
    doc: Annotated[
        list[UUID] | None, typer.Option(help="Search only this document (repeatable)")
    ] = None,
    k_final: Annotated[int, typer.Option(help="How many passages to keep")] = 8,
    rerank: Annotated[bool, typer.Option(help="Use the reranker (if one is configured)")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Print the trace as JSON")] = False,
    show_text: Annotated[int, typer.Option(help="Characters of each passage to show")] = 160,
) -> None:
    """Run a search and print what the retrieval engine did, for tuning.

    Shows the keywords, both searches' ranks, the fused and reranked order, timings, the evidence
    verdict and the context that would be sent to the model. Searches the user's ready documents
    unless --doc is given. Prints passage text, so it is a developer tool: run it where you may
    read the contracts."""
    import json

    from covenantiq.core.errors import CovenantError

    async def go(container: Container) -> None:
        from redis.asyncio import Redis

        from covenantiq.db.scope import OwnershipScope
        from covenantiq.db.uow import UnitOfWork
        from covenantiq.rag.context import build_context
        from covenantiq.rag.evidence import EvidenceAssessor
        from covenantiq.rag.factory import build_retrieval_engine
        from covenantiq.rag.query_analysis import extract_keywords
        from covenantiq.rag.types import RetrievalRequest

        settings = container.settings

        def uow_factory() -> UnitOfWork:
            return UnitOfWork(container.session_factory)

        async with uow_factory() as uow:
            found = await uow.users.get_by_email(user)
            if found is None:
                typer.echo(f"no user with the email {user}", err=True)
                raise typer.Exit(code=1)
            scope = OwnershipScope(found.id)
            if doc:
                documents = [d for d in doc]
            else:
                from covenantiq.core.enums import DocumentStatus

                rows = await uow.documents.list(scope, status=DocumentStatus.READY, limit=200)
                documents = [row.id for row in rows]
        if not documents:
            typer.echo("that user has no ready documents to search", err=True)
            raise typer.Exit(code=1)

        engine = build_retrieval_engine(
            settings, uow_factory, redis=Redis.from_url(settings.redis_url.get_secret_value())
        )
        try:
            try:
                result = await engine.retrieve(
                    RetrievalRequest(
                        scope=scope,
                        query=query,
                        document_ids=documents,
                        k_final=k_final,
                        rerank=rerank,
                    )
                )
            except CovenantError as exc:
                typer.echo(f"search failed: {exc.message}", err=True)
                raise typer.Exit(code=1) from exc
        finally:
            await engine.aclose()

        keywords = extract_keywords(query).keywords
        context = build_context(
            result,
            max_tokens=settings.context_max_tokens,
            parent_max_tokens=settings.parent_max_tokens,
        )
        verdict = EvidenceAssessor().assess(keywords, result, context)
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "trace": result.trace.to_dict(),
                        "evidence": {
                            "level": verdict.level,
                            "sufficient": verdict.sufficient,
                            "reasons": verdict.reasons,
                            "signals": verdict.signals,
                        },
                    },
                    indent=2,
                    default=str,
                )
            )
            return
        trace = result.trace
        typer.echo(f"searched {trace.document_count} document(s); model {trace.embedding_model}")
        typer.echo(f"keywords: {', '.join(trace.keywords) or '-'}")
        typer.echo(f"full-text query: {trace.fts_text or '-'}")
        typer.echo(
            f"dense {len(trace.dense)}  lexical {len(trace.lexical)}  fused {len(trace.fused)}  "
            f"exact scan {trace.exact_scan}  query vector cached {trace.query_embedding_cached}"
        )
        if trace.rerank_skipped:
            typer.echo(f"reranking skipped: {trace.rerank_skipped_reason}")
        for note in trace.notes:
            typer.echo(f"note: {note}")
        typer.echo("timings (ms): " + "  ".join(f"{k}={v}" for k, v in trace.timings_ms.items()))
        typer.echo("")
        for position, chunk in enumerate(result.chunks, start=1):
            score = (
                f"rerank={chunk.rerank_score:.3f}" if chunk.rerank_score is not None else "rerank=-"
            )
            typer.echo(
                f"{position}. {score}  rrf={chunk.rrf_score:.4f}  dense#{chunk.dense_rank or '-'}  "
                f"lexical#{chunk.lexical_rank or '-'}  clause {chunk.clause_ref or '-'}  "
                f"p.{chunk.page_start or '-'}  [{chunk.document_title}]"
            )
            if show_text:
                typer.echo("     " + " ".join(chunk.text.split())[:show_text])
        typer.echo("")
        typer.echo(
            f"evidence: {verdict.level} ({'sufficient' if verdict.sufficient else 'insufficient'})"
        )
        for reason in verdict.reasons:
            typer.echo(f"  - {reason}")
        typer.echo(
            f"context: {len(context.sources)} source(s), about {context.tokens} tokens, "
            f"{context.dropped_groups} dropped"
        )

    run_with_container(go)


@app.command("rerank-serve")
def rerank_serve(
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "0.0.0.0",  # noqa: S104
) -> None:
    """Run the reranker as its own process (the `reranker` Compose service).

    Loads the cross-encoder model once and answers `POST /v1/rerank`. The API and the workers
    call it over HTTP and never load the model themselves. Reads only CIQ_RERANK_* settings."""
    import uvicorn

    from covenantiq.ai.rerank.server import RerankServerSettings, app_from_environment

    uvicorn.run(
        app_from_environment(),
        host=host,
        port=RerankServerSettings().rerank_port,
        workers=1,  # one model in memory; scale by running more containers
        log_config=None,
    )


@app.command("check-rerank")
def check_rerank() -> None:
    """Rerank a few sentences with the configured reranker and report order and timing.

    Confirms the reranker process is reachable and ranks sensibly. Sends generic sentences."""

    async def go() -> None:
        import time

        from covenantiq.ai.rerank.base import RerankRejected, RerankUnavailable
        from covenantiq.ai.rerank.factory import build_reranker

        settings = Settings()
        reranker = build_reranker(settings)
        if reranker is None:
            typer.echo("reranking is switched off (CIQ_RERANK_PROVIDER=none)")
            return
        docs = [
            "The office kitchen is cleaned every Friday afternoon.",
            "The Supplier's aggregate liability is limited to the fees paid in the prior "
            "twelve months.",
            "Invoices are payable within thirty days of receipt.",
            "Neither party shall be liable for indirect or consequential loss.",
        ]
        query = "What is the cap on the supplier's liability?"
        try:
            started = time.monotonic()
            ranked = await reranker.rerank(query, docs, top_k=len(docs))
            elapsed_ms = round((time.monotonic() - started) * 1000)
        except (RerankUnavailable, RerankRejected) as exc:
            typer.echo(f"rerank failed: {exc.message}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            await reranker.aclose()
        typer.echo(f"provider={settings.rerank_provider} model={reranker.model} ({elapsed_ms} ms)")
        for position, (index, score) in enumerate(ranked, start=1):
            typer.echo(f"{position}. score={score:.3f}  {docs[index]}")
        if ranked[0][0] != 1:
            typer.echo("unexpected order: the liability cap should rank first", err=True)
            raise typer.Exit(code=1)
        typer.echo("rerank ok")

    try:
        asyncio.run(go())
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            typer.echo(f"{location}: {error['msg']}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("check-llm")
def check_llm() -> None:
    """Ask the configured model one tiny question and report what came back.

    A quick way to confirm a new API key and model name work before using chat or analysis. Sends
    one short, non-sensitive prompt and a one-field structured request; prints no keys."""

    async def go() -> None:
        from pydantic import BaseModel

        from covenantiq.ai.llm.base import ChatMessage
        from covenantiq.ai.llm.factory import build_llm
        from covenantiq.core.errors import CovenantError

        class Check(BaseModel):
            capital_of_france: str

        settings = Settings()
        stack = build_llm(settings)
        request = stack.routes.request(
            "classify",
            "Answer in as few words as possible.",
            [ChatMessage("user", "Capital of France?")],
        )
        try:
            text = "".join(
                [e.text async for e in stack.provider.stream(request) if e.kind == "delta"]
            )
            structured = await stack.provider.structured(request, Check)
        except CovenantError as exc:
            typer.echo(f"model call failed: {exc.message}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"provider={settings.llm_provider} model={stack.routes.model_for('classify')}")
        typer.echo(f"streamed: {text.strip()[:80]!r}")
        typer.echo(
            f"structured: {structured.value.capital_of_france!r} "
            f"(tokens in={structured.usage.input_tokens} out={structured.usage.output_tokens})"
        )
        typer.echo("model ok")

    try:
        asyncio.run(go())
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            typer.echo(f"{location}: {error['msg']}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("check-embeddings")
def check_embeddings() -> None:
    """Embed a few sentences with the configured provider and report what came back.

    A quick way to confirm a new API key, model and dimension work before ingesting documents.
    Sends three short, non-sensitive sentences to the provider; prints no keys."""

    async def go() -> None:
        from covenantiq.ai.embeddings.factory import build_embedding_provider
        from covenantiq.core.errors import CovenantError

        settings = Settings()
        provider = build_embedding_provider(settings)
        sentences = [
            "The Supplier's total liability shall not exceed the fees paid in the prior year.",
            "Liability of the vendor is capped at twelve months of fees.",
            "The office kitchen is cleaned every Friday afternoon.",
        ]
        try:
            vectors = await provider.embed_documents(sentences)
            query = await provider.embed_query("limitation of liability")
        except CovenantError as exc:
            typer.echo(f"embedding failed: {exc.message}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            await provider.aclose()

        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            norms = (sum(x * x for x in a) * sum(y * y for y in b)) ** 0.5
            return dot / norms if norms else 0.0

        related, unrelated = cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
        typer.echo(f"provider={settings.embedding_provider} model={provider.model}")
        typer.echo(f"dimension={len(vectors[0])} (expected {settings.embedding_dim})")
        typer.echo(f"query dimension={len(query)}")
        typer.echo(f"similarity: related={related:.3f} unrelated={unrelated:.3f}")
        if len(vectors[0]) != settings.embedding_dim or related <= unrelated:
            typer.echo("unexpected result: check the model and dimension", err=True)
            raise typer.Exit(code=1)
        typer.echo("embeddings ok")

    try:
        asyncio.run(go())
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            typer.echo(f"{location}: {error['msg']}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def sweep() -> None:
    """Re-publish jobs stuck in `queued` (also runs on a timer inside the API)."""
    result = run_with_container(lambda container: container.sweep_service.run_once())
    if result.skipped_by_lock:
        typer.echo("another sweeper is already running; nothing done")
    else:
        typer.echo(f"re-dispatched {result.redispatched} job(s)")


@app.command()
def reembed(
    document_id: Annotated[
        UUID | None, typer.Option(help="Re-embed only this document (default: all)")
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum documents when none is named")] = 1000,
) -> None:
    """Embed documents again, for example after changing the embedding model.

    Clears the stored vectors and queues the embedding stage. Documents are not read or
    chunked again. Set CIQ_EMBEDDING_MODEL (and the matching dimension) before running it."""
    result = run_with_container(
        lambda c: c.reembed_service.reembed(document_id=document_id, limit=limit)
    )
    typer.echo(f"queued {result.queued} document(s) for re-embedding; skipped {result.skipped}")


@dlq_app.command("list")
def dlq_list(
    queue: Annotated[str, typer.Argument(help="Work queue, e.g. document_processing")],
    limit: Annotated[int, typer.Option(help="Maximum messages to show")] = 100,
) -> None:
    """Show the dead-lettered tasks of a queue. Nothing is removed."""
    try:
        letters = run_with_container(lambda c: c.dlq_service.peek(queue, limit=limit))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if not letters:
        typer.echo(f"{queue}.dlq is empty")
        return
    for letter in letters:
        typer.echo(
            f"{letter.task}  target={letter.target_id}  task_id={letter.task_id}  "
            f"request_id={letter.request_id or '-'}  reason={letter.reason or '-'}"
        )


@dlq_app.command("replay")
def dlq_replay(
    queue: Annotated[str, typer.Argument(help="Work queue, e.g. document_processing")],
    job_id: Annotated[
        UUID | None, typer.Option(help="Replay only this job (or document) id")
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum messages to consider")] = 100,
) -> None:
    """Reset failed jobs to `queued` and publish their tasks again."""
    try:
        result = run_with_container(
            lambda c: c.dlq_service.replay(queue, job_id=job_id, limit=limit)
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"replayed {result.replayed}, skipped {result.skipped}")
