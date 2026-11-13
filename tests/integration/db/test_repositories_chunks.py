from collections.abc import Callable

import pytest
from sqlalchemy import text

from covenantiq.db.models import Document
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from tests.integration.factories import make_chunk, make_clause, make_document, make_user

UowFactory = Callable[[], UnitOfWork]


async def seed(uow_factory: UowFactory) -> tuple[OwnershipScope, OwnershipScope, Document]:
    async with uow_factory() as uow:
        alice = await uow.users.add(make_user())
        bob = await uow.users.add(make_user())
        document = await uow.documents.add(OwnershipScope(alice.id), make_document(alice.id))
        await uow.commit()
    return OwnershipScope(alice.id), OwnershipScope(bob.id), document


async def test_replace_clauses_cascades_to_chunks(uow_factory: UowFactory) -> None:
    alice, _, document = await seed(uow_factory)
    async with uow_factory() as uow:
        clause = make_clause(alice.owner_id, document.id, 0)
        await uow.clauses.replace_for_document(alice, document.id, [clause])
        await uow.chunks.bulk_insert(alice, [make_chunk(alice.owner_id, document.id, clause.id, 0)])
        await uow.commit()

    async with uow_factory() as uow:
        replacement = make_clause(alice.owner_id, document.id, 0, text="New text.")
        await uow.clauses.replace_for_document(alice, document.id, [replacement])
        await uow.commit()
        assert await uow.chunks.pending_embedding_batch(alice, document.id, 10) == []
        listed = await uow.clauses.list_for_document(alice, document.id)
        assert [c.text for c in listed] == ["New text."]


async def test_clause_type_filter_and_isolation(uow_factory: UowFactory) -> None:
    alice, bob, document = await seed(uow_factory)
    async with uow_factory() as uow:
        await uow.clauses.replace_for_document(
            alice,
            document.id,
            [
                make_clause(alice.owner_id, document.id, 0, clause_types=["liability"]),
                make_clause(
                    alice.owner_id, document.id, 1, clause_types=["termination", "renewal"]
                ),
                make_clause(alice.owner_id, document.id, 2),
            ],
        )
        await uow.commit()

    async with uow_factory() as uow:
        doc_id = document.id
        assert [c.ordinal for c in await uow.clauses.list_for_document(alice, doc_id)] == [0, 1, 2]
        by_type = await uow.clauses.list_for_document(alice, doc_id, ["renewal", "payment"])
        assert [c.ordinal for c in by_type] == [1]
        assert await uow.clauses.list_for_document(bob, doc_id) == []
        ids = [c.id for c in await uow.clauses.list_for_document(alice, doc_id)]
        assert len(await uow.clauses.get_many(alice, ids)) == 3
        assert await uow.clauses.get_many(bob, ids) == []


async def test_search_vector_is_generated_from_text_and_heading(uow_factory: UowFactory) -> None:
    alice, _, document = await seed(uow_factory)
    async with uow_factory() as uow:
        clause = make_clause(alice.owner_id, document.id, 0)
        await uow.clauses.replace_for_document(alice, document.id, [clause])
        chunk = make_chunk(
            alice.owner_id, document.id, clause.id, 0, text="Liability is capped at fees paid."
        )
        chunk.metadata_ = {"heading_text": "Limitation of Liability"}
        await uow.chunks.bulk_insert(alice, [chunk])
        await uow.commit()

    async with uow_factory() as uow:
        matches = await uow.session.execute(
            text(
                "SELECT count(*) FROM document_chunks WHERE tsv @@ plainto_tsquery('english', :q)"
            ),
            {"q": "capped fees"},
        )
        assert matches.scalar_one() == 1
        heading = await uow.session.execute(
            text(
                "SELECT count(*) FROM document_chunks "
                "WHERE tsv @@ plainto_tsquery('english', 'limitation')"
            )
        )
        assert heading.scalar_one() == 1


async def test_delete_chunks_for_document_is_scoped(uow_factory: UowFactory) -> None:
    alice, bob, document = await seed(uow_factory)
    async with uow_factory() as uow:
        clause = make_clause(alice.owner_id, document.id, 0)
        await uow.clauses.replace_for_document(alice, document.id, [clause])
        await uow.chunks.bulk_insert(
            alice,
            [make_chunk(alice.owner_id, document.id, clause.id, n) for n in range(3)],
        )
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.chunks.delete_for_document(bob, document.id) == 0
        assert await uow.chunks.delete_for_document(alice, document.id) == 3
        await uow.commit()


@pytest.mark.usefixtures("require_pgvector")
async def test_embeddings_are_written_and_leave_the_pending_batch(
    uow_factory: UowFactory,
) -> None:
    alice, _bob, document = await seed(uow_factory)
    async with uow_factory() as uow:
        clause = make_clause(alice.owner_id, document.id, 0)
        await uow.clauses.replace_for_document(alice, document.id, [clause])
        chunks = [make_chunk(alice.owner_id, document.id, clause.id, n) for n in range(3)]
        await uow.chunks.bulk_insert(alice, chunks)
        await uow.commit()

    async with uow_factory() as uow:
        pending = await uow.chunks.pending_embedding_batch(alice, document.id, 2)
        assert [c.ordinal for c in pending] == [0, 1]
        await uow.chunks.set_embeddings(
            alice, [(c.id, [0.1] * 1024) for c in pending], "voyage-law-2"
        )
        await uow.commit()

    async with uow_factory() as uow:
        remaining = await uow.chunks.pending_embedding_batch(alice, document.id, 10)
        assert [c.ordinal for c in remaining] == [2]
