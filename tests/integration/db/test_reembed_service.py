from collections.abc import Callable
from uuid import UUID, uuid4

from sqlalchemy import text

from covenantiq.core.enums import DocumentStatus, JobStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.reembed_service import ReembedService
from tests.fakes import RecordingDispatcher
from tests.integration.factories import (
    make_chunk,
    make_clause,
    make_document,
    make_processing_job,
    make_user,
)

UowFactory = Callable[[], UnitOfWork]


async def seed_document(
    uow_factory: UowFactory, status: DocumentStatus, *, with_active_job: bool = False
) -> tuple[UUID, UUID]:
    async with uow_factory() as uow:
        user = await uow.users.add(make_user())
        scope = OwnershipScope(user.id)
        document = await uow.documents.add(scope, make_document(user.id, status=status))
        clause = make_clause(user.id, document.id, 0)
        await uow.clauses.replace_for_document(scope, document.id, [clause])
        chunks = [make_chunk(user.id, document.id, clause.id, n) for n in range(2)]
        await uow.chunks.bulk_insert(scope, chunks)
        await uow.chunks.mark_embedding_error(scope, chunks[0].id, "refused earlier")
        if with_active_job:
            await uow.processing_jobs.create(scope, make_processing_job(user.id, document.id))
        await uow.commit()
    return user.id, document.id


async def scalar(uow_factory: UowFactory, statement: str, **params: object) -> object:
    async with uow_factory() as uow:
        return (await uow.session.execute(text(statement), params)).scalar_one_or_none()


async def test_a_ready_document_goes_back_through_the_embedding_stage(
    uow_factory: UowFactory,
) -> None:
    _, document_id = await seed_document(uow_factory, DocumentStatus.READY)
    dispatcher = RecordingDispatcher()

    result = await ReembedService(uow_factory, dispatcher).reembed()  # type: ignore[arg-type]

    assert (result.queued, result.skipped) == (1, 0)
    assert (
        await scalar(uow_factory, "SELECT status FROM documents WHERE id = :d", d=document_id)
        == "embedding"
    )
    ((job_id,),) = [(j,) for j in dispatcher.embedding]
    assert (
        await scalar(
            uow_factory, "SELECT stage FROM document_processing_jobs WHERE id = :j", j=job_id
        )
        == "embedding"
    )
    assert (
        await scalar(
            uow_factory, "SELECT status FROM document_processing_jobs WHERE id = :j", j=job_id
        )
        == JobStatus.QUEUED.value
    )
    # the clauses and chunks stay; only the refusals from the earlier run are forgotten
    assert (
        await scalar(
            uow_factory,
            "SELECT count(*) FROM document_chunks WHERE document_id = :d",
            d=document_id,
        )
        == 2
    )
    assert (
        await scalar(
            uow_factory,
            "SELECT count(*) FROM document_chunks WHERE document_id = :d "
            "AND metadata ? 'embedding_error'",
            d=document_id,
        )
        == 0
    )


async def test_a_failed_document_can_be_re_embedded_too(uow_factory: UowFactory) -> None:
    await seed_document(uow_factory, DocumentStatus.FAILED)
    result = await ReembedService(uow_factory, RecordingDispatcher()).reembed()  # type: ignore[arg-type]
    assert result.queued == 1


async def test_documents_still_being_processed_are_left_alone(uow_factory: UowFactory) -> None:
    for status in (DocumentStatus.QUEUED, DocumentStatus.EXTRACTING, DocumentStatus.EMBEDDING):
        await seed_document(uow_factory, status)
    dispatcher = RecordingDispatcher()
    result = await ReembedService(uow_factory, dispatcher).reembed()  # type: ignore[arg-type]
    assert (result.queued, result.skipped) == (0, 0)
    assert dispatcher.embedding == []


async def test_a_document_with_an_active_job_is_skipped_and_left_unchanged(
    uow_factory: UowFactory,
) -> None:
    _, document_id = await seed_document(uow_factory, DocumentStatus.READY, with_active_job=True)
    dispatcher = RecordingDispatcher()

    result = await ReembedService(uow_factory, dispatcher).reembed(document_id=document_id)  # type: ignore[arg-type]

    assert (result.queued, result.skipped) == (0, 1)
    assert dispatcher.embedding == []
    # the failed attempt was rolled back as a whole
    assert (
        await scalar(uow_factory, "SELECT status FROM documents WHERE id = :d", d=document_id)
        == "ready"
    )
    assert (
        await scalar(
            uow_factory,
            "SELECT count(*) FROM document_chunks WHERE document_id = :d "
            "AND metadata ? 'embedding_error'",
            d=document_id,
        )
        == 1
    )


async def test_a_named_document_is_the_only_one_touched(uow_factory: UowFactory) -> None:
    _, wanted = await seed_document(uow_factory, DocumentStatus.READY)
    _, other = await seed_document(uow_factory, DocumentStatus.READY)
    dispatcher = RecordingDispatcher()

    result = await ReembedService(uow_factory, dispatcher).reembed(document_id=wanted)  # type: ignore[arg-type]

    assert result.queued == 1
    assert (
        await scalar(uow_factory, "SELECT status FROM documents WHERE id = :d", d=other) == "ready"
    )


async def test_unknown_and_deleted_documents_are_skipped(uow_factory: UowFactory) -> None:
    owner, deleted = await seed_document(uow_factory, DocumentStatus.READY)
    async with uow_factory() as uow:
        assert await uow.documents.soft_delete(OwnershipScope(owner), deleted)
        await uow.commit()
    service = ReembedService(uow_factory, RecordingDispatcher())  # type: ignore[arg-type]

    assert (await service.reembed(document_id=deleted)).skipped == 1
    assert (await service.reembed(document_id=uuid4())).skipped == 1
    assert (await service.reembed()).queued == 0  # deleted documents are not swept up either
