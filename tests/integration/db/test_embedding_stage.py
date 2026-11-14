"""Chunk persistence and the embedding stage of the ingestion pipeline, end to end against
PostgreSQL with a fake extractor and fake embedding providers (no models, no network).

Vector writes need the pgvector extension. Where it is not installed the test database stores
`embedding` as `float4[]` and these tests swap in an equivalent write, so everything else (batching,
resume, refusal handling, status changes) is still exercised.
"""

from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from covenantiq.ai.embeddings.base import (
    EmbeddingAuthFailed,
    EmbeddingRejected,
    EmbeddingUnavailable,
)
from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.core.enums import ProcessingStage
from covenantiq.core.errors import PermanentTaskError
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.storage import LocalFileStorage
from covenantiq.services import ingestion_service
from covenantiq.services.ingestion_service import IngestionService
from covenantiq.services.job_service import PROCESSING, JobService
from tests.fakes import FakeExtractor, RecordingDispatcher, healthy_result
from tests.integration.factories import make_document, make_processing_job, make_user

UowFactory = Callable[[], UnitOfWork]


class Seed:
    def __init__(self, owner: UUID, document: UUID, job: UUID) -> None:
        self.owner, self.document, self.job = owner, document, job


async def seed(uow_factory: UowFactory) -> Seed:
    async with uow_factory() as uow:
        user = await uow.users.add(make_user())
        scope = OwnershipScope(user.id)
        document = await uow.documents.add(
            scope, make_document(user.id, storage_path=f"{user.id}/doc/{'a' * 64}.pdf")
        )
        job = await uow.processing_jobs.create(scope, make_processing_job(user.id, document.id))
        await uow.commit()
    return Seed(user.id, document.id, job.id)


class Scripted(HashEmbeddingProvider):
    """A hash provider that can fail or refuse on demand."""

    def __init__(self) -> None:
        super().__init__(dim=1024)
        self.fail_on_call: int | None = None
        self.error: Exception = EmbeddingUnavailable("provider is down")
        self.refuse_containing: str | None = None

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        number = len(self.calls) + 1
        if self.fail_on_call == number:
            self.calls.append(list(texts))
            raise self.error
        if self.refuse_containing and any(self.refuse_containing in t for t in texts):
            self.calls.append(list(texts))
            raise EmbeddingRejected("input refused")
        return await super().embed_documents(texts)


def build(
    uow_factory: UowFactory,
    tmp_path: Path,
    *,
    embedder: HashEmbeddingProvider | None,
    dispatcher: RecordingDispatcher | None = None,
    batch_size: int = 64,
    extractor: FakeExtractor | None = None,
) -> tuple[IngestionService, JobService]:
    jobs = JobService(uow_factory)
    service = IngestionService(
        uow_factory,
        LocalFileStorage(tmp_path),
        jobs,
        extractor or FakeExtractor(healthy_result(pages=2)),  # type: ignore[arg-type]
        embedder=embedder,
        dispatcher=dispatcher,  # type: ignore[arg-type]
        embed_batch_size=batch_size,
    )
    return service, jobs


async def rows(uow_factory: UowFactory, statement: str, **params: Any) -> list[tuple[Any, ...]]:
    async with uow_factory() as uow:
        return [tuple(r) for r in (await uow.session.execute(text(statement), params)).all()]


async def document_status(uow_factory: UowFactory, seeded: Seed) -> str:
    ((status,),) = await rows(
        uow_factory, "SELECT status FROM documents WHERE id = :id", id=seeded.document
    )
    return str(status)


async def job_state(uow_factory: UowFactory, seeded: Seed) -> tuple[str, str | None]:
    ((status, stage),) = await rows(
        uow_factory,
        "SELECT status, stage FROM document_processing_jobs WHERE id = :id",
        id=seeded.job,
    )
    return str(status), None if stage is None else str(stage)


async def chunked(
    uow_factory: UowFactory, tmp_path: Path, **kwargs: Any
) -> tuple[Seed, IngestionService, JobService]:
    seeded = await seed(uow_factory)
    service, jobs = build(uow_factory, tmp_path, **kwargs)
    result = await service.process(seeded.job, attempt=0)
    assert result["status"] == "chunked"
    return seeded, service, jobs


class TestChunkPersistence:
    async def test_clauses_and_chunks_are_stored_with_their_context(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        seeded, _, _ = await chunked(uow_factory, tmp_path, embedder=Scripted())

        clauses = await rows(
            uow_factory,
            "SELECT ordinal, clause_ref, heading, page_start, page_end, text "
            "FROM document_clauses WHERE document_id = :d ORDER BY ordinal",
            d=seeded.document,
        )
        assert [(c[1], c[2]) for c in clauses] == [("1", "1. Definitions"), ("2", "2. Payment")]
        assert [(c[3], c[4]) for c in clauses] == [(1, 1), (2, 2)]

        stored = await rows(
            uow_factory,
            "SELECT c.ordinal, c.text, c.metadata->>'heading_text', c.metadata->>'char_start', "
            "c.metadata->>'char_end', cl.text, cl.clause_ref, c.embedding IS NULL "
            "FROM document_chunks c JOIN document_clauses cl ON cl.id = c.clause_id "
            "WHERE c.document_id = :d ORDER BY c.ordinal",
            d=seeded.document,
        )
        assert [row[0] for row in stored] == list(range(len(stored)))
        for _, chunk_text, header, start, end, clause_text, ref, unembedded in stored:
            assert clause_text[int(start) : int(end)] == chunk_text  # exact substring
            assert header == f"Master Services Agreement | clause {ref}" or header.endswith(
                f"clause {ref}"
            )
            assert unembedded is True
        ((count,),) = await rows(
            uow_factory, "SELECT chunk_count FROM documents WHERE id = :d", d=seeded.document
        )
        assert count == len(stored)

    async def test_clause_types_are_carried_to_the_chunks(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        result = healthy_result(pages=1)
        seeded = await seed(uow_factory)
        service, _ = build(
            uow_factory, tmp_path, embedder=Scripted(), extractor=FakeExtractor(result)
        )
        await service.process(seeded.job, attempt=0)
        types = await rows(
            uow_factory,
            "SELECT DISTINCT unnest(clause_types) FROM document_chunks WHERE document_id = :d",
            d=seeded.document,
        )
        assert ("payment_terms",) in types

    async def test_the_job_is_handed_to_embedding_and_announced(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        dispatcher = RecordingDispatcher()
        seeded, _, _ = await chunked(
            uow_factory, tmp_path, embedder=Scripted(), dispatcher=dispatcher
        )
        assert dispatcher.embedding == [seeded.job]
        assert await job_state(uow_factory, seeded) == ("queued", "embedding")
        assert await document_status(uow_factory, seeded) == "embedding"

    async def test_a_lost_announcement_leaves_a_job_the_sweep_can_find(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        seeded, _, jobs = await chunked(
            uow_factory, tmp_path, embedder=Scripted(), dispatcher=RecordingDispatcher(fail=True)
        )
        assert await job_state(uow_factory, seeded) == ("queued", "embedding")
        stuck = await jobs.stale_queued(older_than=timedelta(seconds=0))
        assert [(j.job_id, j.stage) for j in stuck] == [(seeded.job, ProcessingStage.EMBEDDING)]

    async def test_a_freshly_handed_off_job_is_not_stale(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        _, _, jobs = await chunked(uow_factory, tmp_path, embedder=Scripted())
        assert await jobs.stale_queued(older_than=timedelta(minutes=5)) == []


class TestEmbedding:
    async def test_every_chunk_is_embedded_and_the_document_becomes_ready(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder)

        result = await service.embed(seeded.job, attempt=0)

        assert result["status"] == "ready"
        assert result["refused"] == 0
        assert await document_status(uow_factory, seeded) == "ready"
        assert await job_state(uow_factory, seeded) == ("succeeded", "embedding")
        ((total, embedded, models),) = await rows(
            uow_factory,
            "SELECT count(*), count(embedding), count(DISTINCT embedding_model) "
            "FROM document_chunks WHERE document_id = :d",
            d=seeded.document,
        )
        assert total == embedded == result["chunks"]
        assert models == 1
        ((metadata,),) = await rows(
            uow_factory, "SELECT metadata FROM documents WHERE id = :d", d=seeded.document
        )
        assert metadata["embedding_model"] == "fake-hash"

    async def test_passages_are_embedded_with_their_context_header(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder)
        await service.embed(seeded.job, attempt=0)
        sent = [t for call in embedder.calls for t in call]
        assert sent
        assert all(t.startswith("Master Services Agreement") for t in sent)
        assert any("clause 2" in t.split("\n\n")[0] for t in sent)
        stored = await rows(
            uow_factory,
            "SELECT text FROM document_chunks WHERE document_id = :d",
            d=seeded.document,
        )
        assert all(not row[0].startswith("Master Services Agreement") for row in stored)

    async def test_chunks_are_sent_in_batches_of_the_configured_size(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder, batch_size=1)
        result = await service.embed(seeded.job, attempt=0)
        assert len(embedder.calls) == result["chunks"]
        assert all(len(call) == 1 for call in embedder.calls)

    async def test_a_transient_failure_keeps_finished_batches_and_a_retry_resumes(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        embedder.fail_on_call = 2
        seeded, service, jobs = await chunked(
            uow_factory, tmp_path, embedder=embedder, batch_size=1
        )

        with pytest.raises(EmbeddingUnavailable):
            await service.embed(seeded.job, attempt=0)
        ((done,),) = await rows(
            uow_factory,
            "SELECT count(embedding) FROM document_chunks WHERE document_id = :d",
            d=seeded.document,
        )
        assert done == 1  # the first batch was committed before the failure

        # what the task wrapper does before retrying, then the retried delivery
        assert await jobs.release(PROCESSING, seeded.job)
        embedder.calls.clear()
        embedder.fail_on_call = None
        result = await service.embed(seeded.job, attempt=1)

        assert result["status"] == "ready"
        assert len(embedder.calls) == result["chunks"] - 1  # only the remainder was sent

    async def test_without_a_release_a_retry_would_find_the_job_running_and_do_nothing(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        embedder.fail_on_call = 1
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder)
        with pytest.raises(EmbeddingUnavailable):
            await service.embed(seeded.job, attempt=0)
        assert await service.embed(seeded.job, attempt=1) == {"status": "skipped"}

    async def test_a_refused_passage_is_isolated_and_the_rest_are_embedded(
        self, uow_factory: UowFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ingestion_service, "MAX_REFUSED_FRACTION", 0.5)
        embedder = Scripted()
        embedder.refuse_containing = "thirty days"
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder)

        result = await service.embed(seeded.job, attempt=0)

        assert result["status"] == "ready"
        assert result["refused"] >= 1
        assert result["chunks"] >= 1
        ((metadata,),) = await rows(
            uow_factory, "SELECT metadata FROM documents WHERE id = :d", d=seeded.document
        )
        assert metadata["chunks_without_embedding"] == result["refused"]
        flagged = await rows(
            uow_factory,
            "SELECT metadata->>'embedding_error' FROM document_chunks "
            "WHERE document_id = :d AND embedding IS NULL",
            d=seeded.document,
        )
        assert len(flagged) == result["refused"]
        assert all("refused" in row[0] for row in flagged)

    async def test_too_many_refusals_fail_the_job(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        embedder.refuse_containing = "Master Services Agreement"  # every passage
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder)

        with pytest.raises(PermanentTaskError) as exc:
            await service.embed(seeded.job, attempt=0)

        assert exc.value.job_code == "EMBEDDING_FAILED"
        assert await document_status(uow_factory, seeded) == "embedding"  # the task marks it failed

    async def test_a_rejected_key_fails_with_its_own_code(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        embedder.error = EmbeddingAuthFailed("bad key")
        embedder.fail_on_call = 1
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder)
        with pytest.raises(PermanentTaskError) as exc:
            await service.embed(seeded.job, attempt=0)
        assert exc.value.job_code == "EMBEDDING_AUTH_FAILED"

    async def test_without_a_provider_the_job_fails_clearly(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=None)
        with pytest.raises(PermanentTaskError) as exc:
            await service.embed(seeded.job, attempt=0)
        assert exc.value.job_code == "EMBEDDING_NOT_CONFIGURED"

    async def test_a_document_deleted_meanwhile_stops_embedding(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        embedder = Scripted()
        seeded, service, _ = await chunked(uow_factory, tmp_path, embedder=embedder)
        async with uow_factory() as uow:
            assert await uow.documents.soft_delete(OwnershipScope(seeded.owner), seeded.document)
            await uow.commit()
        with pytest.raises(PermanentTaskError) as exc:
            await service.embed(seeded.job, attempt=0)
        assert exc.value.job_code == "DOCUMENT_GONE"
        assert embedder.calls == []


class TestRepeatedRuns:
    async def test_processing_again_replaces_the_chunks_instead_of_duplicating_them(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        seeded, service, jobs = await chunked(uow_factory, tmp_path, embedder=Scripted())
        before = await rows(
            uow_factory,
            "SELECT count(*) FROM document_chunks WHERE document_id = :d",
            d=seeded.document,
        )
        # a replayed job starts from the top
        async with uow_factory() as uow:
            await uow.session.execute(
                text("UPDATE document_processing_jobs SET stage = NULL WHERE id = :id"),
                {"id": seeded.job},
            )
            await uow.commit()
        result = await service.process(seeded.job, attempt=1)
        after = await rows(
            uow_factory,
            "SELECT count(*) FROM document_chunks WHERE document_id = :d",
            d=seeded.document,
        )
        assert result["status"] == "chunked"
        assert after == before
        ((ordinals,),) = await rows(
            uow_factory,
            "SELECT count(DISTINCT ordinal) FROM document_chunks WHERE document_id = :d",
            d=seeded.document,
        )
        assert ordinals == after[0][0]
        assert jobs is not None
