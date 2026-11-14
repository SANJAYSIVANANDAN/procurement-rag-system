"""The search SQL against real pgvector: ordering, the filters that keep one user's text from
another, deleted and unfinished documents out, and hostile input.

Needs pgvector in the test database (CIQ_TEST_DATABASE_URL); skipped otherwise.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import pytest

from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.core.enums import DocumentStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from tests.integration.factories import make_chunk, make_clause, make_document, make_user

pytestmark = pytest.mark.usefixtures("require_pgvector")

UowFactory = Callable[[], UnitOfWork]
MODEL = "fake-hash"
embedder = HashEmbeddingProvider(dim=1024, model=MODEL)

LIABILITY = (
    "The Supplier's total liability shall not exceed the fees paid in the prior twelve months."
)
PAYMENT = "The Customer shall pay each invoice within ninety days of receipt."
TERM = "This Agreement renews automatically for successive twelve month periods."
PRIVACY = "The Supplier shall notify the Customer of a personal data breach without delay."


@dataclass
class Seeded:
    scope: OwnershipScope
    document: UUID
    chunks: dict[str, UUID]  # label -> chunk id
    clauses: dict[str, UUID]


async def seed(
    uow_factory: UowFactory,
    *,
    texts: dict[str, str] | None = None,
    status: DocumentStatus = DocumentStatus.READY,
    owner_email: str | None = None,
    model: str = MODEL,
    title: str = "Master Services Agreement",
    embed: bool = True,
) -> Seeded:
    texts = texts or {"liability": LIABILITY, "payment": PAYMENT, "term": TERM, "privacy": PRIVACY}
    async with uow_factory() as uow:
        user = await uow.users.add(make_user(owner_email))
        scope = OwnershipScope(user.id)
        document = await uow.documents.add(
            scope, make_document(user.id, status=status, title=title)
        )
        clause_rows = [
            make_clause(
                user.id,
                document.id,
                n,
                text=text,
                clause_ref=str(n + 1),
                heading=label.title(),
                heading_path=[label.title()],
            )
            for n, (label, text) in enumerate(texts.items())
        ]
        await uow.clauses.replace_for_document(scope, document.id, clause_rows)
        chunk_rows = [
            make_chunk(user.id, document.id, clause.id, n, text=text)
            for n, (clause, text) in enumerate(zip(clause_rows, texts.values(), strict=True))
        ]
        await uow.chunks.bulk_insert(scope, chunk_rows)
        if embed:
            vectors = await embedder.embed_documents(list(texts.values()))
            await uow.chunks.set_embeddings(
                scope, [(c.id, v) for c, v in zip(chunk_rows, vectors, strict=True)], model
            )
        await uow.commit()
    return Seeded(
        scope,
        document.id,
        {label: c.id for label, c in zip(texts, chunk_rows, strict=True)},
        {label: c.id for label, c in zip(texts, clause_rows, strict=True)},
    )


async def dense(
    uow_factory: UowFactory, seeded: Seeded, query: str, **kwargs: object
) -> list[UUID]:
    vector = await embedder.embed_query(query)
    async with uow_factory() as uow:
        hits = await uow.chunk_search.dense_search(
            seeded.scope,
            vector,
            document_ids=kwargs.pop("document_ids", [seeded.document]),  # type: ignore[arg-type]
            embedding_model=str(kwargs.pop("embedding_model", MODEL)),
            k=int(kwargs.pop("k", 10)),  # type: ignore[call-overload]
            **kwargs,  # type: ignore[arg-type]
        )
    return [hit.chunk_id for hit in hits]


async def lexical(
    uow_factory: UowFactory, seeded: Seeded, text: str, *, documents: list[UUID] | None = None
) -> list[UUID]:
    async with uow_factory() as uow:
        hits = await uow.chunk_search.lexical_search(
            seeded.scope, text, document_ids=documents or [seeded.document], k=10
        )
    return [hit.chunk_id for hit in hits]


class TestDense:
    async def test_the_nearest_chunk_comes_first_with_similarity_scores(
        self, uow_factory: UowFactory
    ) -> None:
        seeded = await seed(uow_factory)
        vector = await embedder.embed_query("total liability shall not exceed the fees paid")
        async with uow_factory() as uow:
            hits = await uow.chunk_search.dense_search(
                seeded.scope, vector, document_ids=[seeded.document], embedding_model=MODEL, k=4
            )
        assert hits[0].chunk_id == seeded.chunks["liability"]
        scores = [hit.score for hit in hits]
        assert scores == sorted(scores, reverse=True)
        assert 0.0 < scores[0] <= 1.0

    async def test_k_limits_the_results(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        assert len(await dense(uow_factory, seeded, "liability", k=2)) == 2

    @pytest.mark.parametrize("exact", [True, False])
    async def test_exact_and_index_scans_agree_on_a_small_set(
        self, uow_factory: UowFactory, exact: bool
    ) -> None:
        seeded = await seed(uow_factory)
        found = await dense(uow_factory, seeded, "invoice payment ninety days", exact=exact)
        assert found[0] == seeded.chunks["payment"]

    async def test_chunks_from_another_embedding_model_are_ignored(
        self, uow_factory: UowFactory
    ) -> None:
        seeded = await seed(uow_factory, model="some-other-model")
        assert await dense(uow_factory, seeded, "liability") == []
        assert await dense(uow_factory, seeded, "liability", embedding_model="some-other-model")

    async def test_chunks_without_a_vector_are_ignored(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory, embed=False)
        assert await dense(uow_factory, seeded, "liability") == []

    async def test_only_the_named_documents_are_searched(self, uow_factory: UowFactory) -> None:
        first = await seed(uow_factory)
        other_doc = await seed(uow_factory)  # a different user's document, same text
        found = await dense(uow_factory, first, "liability", document_ids=[first.document])
        assert set(found) <= set(first.chunks.values())
        assert not set(found) & set(other_doc.chunks.values())

    async def test_no_documents_means_no_results(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        assert await dense(uow_factory, seeded, "liability", document_ids=[]) == []

    async def test_settings_do_not_leak_past_the_transaction(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        await dense(uow_factory, seeded, "liability", exact=True, ef_search=7)
        async with uow_factory() as uow:
            from sqlalchemy import text

            value = (
                await uow.session.execute(text("SELECT current_setting('enable_indexscan')"))
            ).scalar()
        assert value == "on"  # SET LOCAL ended with the transaction


class TestLexical:
    async def test_terms_find_the_matching_chunk(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        assert (await lexical(uow_factory, seeded, "invoice or ninety"))[0] == seeded.chunks[
            "payment"
        ]

    async def test_stemming_matches_other_word_forms(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        assert seeded.chunks["term"] in await lexical(uow_factory, seeded, "renewal or renew")
        assert seeded.chunks["liability"] in await lexical(uow_factory, seeded, "liabilities")

    async def test_a_quoted_phrase_must_match_as_a_phrase(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        assert await lexical(uow_factory, seeded, '"personal data breach"') == [
            seeded.chunks["privacy"]
        ]
        assert await lexical(uow_factory, seeded, '"breach data personal"') == []

    async def test_or_widens_the_search(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        found = await lexical(uow_factory, seeded, "invoice or liability")
        assert {seeded.chunks["payment"], seeded.chunks["liability"]} <= set(found)

    async def test_text_that_matches_nothing_returns_nothing(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        assert await lexical(uow_factory, seeded, "zebra or giraffe") == []

    @pytest.mark.parametrize(
        "hostile",
        [
            "",
            "   ",
            "'; DROP TABLE document_chunks; --",
            "a & | ! ( ) :*",
            '" or 1=1 --',
            "((((",
            "-",
            "\\",
        ],
    )
    async def test_odd_or_hostile_input_never_raises_or_damages_anything(
        self, uow_factory: UowFactory, hostile: str
    ) -> None:
        seeded = await seed(uow_factory)
        await lexical(uow_factory, seeded, hostile)  # must not raise
        assert await lexical(uow_factory, seeded, "invoice")  # and the table is still there


class TestIsolation:
    """Spec acceptance: another user's identical chunk text is never returned; deleted documents
    are never returned."""

    async def test_another_users_identical_text_is_never_returned_by_either_search(
        self, uow_factory: UowFactory
    ) -> None:
        mine = await seed(uow_factory, owner_email="mine@example.com")
        theirs = await seed(uow_factory, owner_email="theirs@example.com")

        # even when the caller wrongly names the other user's document
        dense_found = await dense(
            uow_factory,
            mine,
            "total liability fees",
            document_ids=[mine.document, theirs.document],
            k=50,
        )
        lexical_found = await lexical(
            uow_factory, mine, "liability or invoice", documents=[mine.document, theirs.document]
        )
        for found in (dense_found, lexical_found):
            assert found
            assert not set(found) & set(theirs.chunks.values())
            assert set(found) <= set(mine.chunks.values())

    async def test_a_deleted_document_is_never_returned(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        assert await lexical(uow_factory, seeded, "liability")
        async with uow_factory() as uow:
            assert await uow.documents.soft_delete(seeded.scope, seeded.document)
            await uow.commit()
        assert await lexical(uow_factory, seeded, "liability") == []
        assert await dense(uow_factory, seeded, "liability") == []
        async with uow_factory() as uow:
            assert (
                await uow.chunk_search.get_chunks(
                    seeded.scope, list(seeded.chunks.values()), [seeded.document]
                )
                == []
            )

    @pytest.mark.parametrize(
        "status",
        [
            DocumentStatus.QUEUED,
            DocumentStatus.EXTRACTING,
            DocumentStatus.EMBEDDING,
            DocumentStatus.FAILED,
        ],
    )
    async def test_a_document_that_is_not_ready_is_never_returned(
        self, uow_factory: UowFactory, status: DocumentStatus
    ) -> None:
        seeded = await seed(uow_factory, status=status)
        assert await lexical(uow_factory, seeded, "liability") == []
        assert await dense(uow_factory, seeded, "liability") == []

    async def test_hydration_is_scoped_to_the_owner_too(self, uow_factory: UowFactory) -> None:
        mine = await seed(uow_factory)
        theirs = await seed(uow_factory)
        async with uow_factory() as uow:
            records = await uow.chunk_search.get_chunks(
                mine.scope,
                [*mine.chunks.values(), *theirs.chunks.values()],
                [mine.document, theirs.document],
            )
            clauses = await uow.chunk_search.get_clauses(
                mine.scope,
                [*mine.clauses.values(), *theirs.clauses.values()],
                [mine.document, theirs.document],
            )
        assert {r.chunk_id for r in records} == set(mine.chunks.values())
        assert {c.clause_id for c in clauses} == set(mine.clauses.values())


class TestHydration:
    async def test_chunk_records_carry_the_clause_and_document_context(
        self, uow_factory: UowFactory
    ) -> None:
        seeded = await seed(uow_factory, title="Acme MSA")
        async with uow_factory() as uow:
            (record,) = await uow.chunk_search.get_chunks(
                seeded.scope, [seeded.chunks["payment"]], [seeded.document]
            )
        assert record.text == PAYMENT
        assert record.document_title == "Acme MSA"
        assert (record.clause_ref, record.heading_path) == ("2", ["Payment"])
        assert (record.page_start, record.page_end) == (1, 1)
        assert record.clause_id == seeded.clauses["payment"]

    async def test_adjacent_chunks_are_found_by_position(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        async with uow_factory() as uow:
            records = await uow.chunk_search.get_adjacent_chunks(
                seeded.scope, [(seeded.document, 0), (seeded.document, 2)], [seeded.document]
            )
        assert sorted(r.ordinal for r in records) == [0, 2]

    async def test_clauses_come_back_whole(self, uow_factory: UowFactory) -> None:
        seeded = await seed(uow_factory)
        async with uow_factory() as uow:
            (clause,) = await uow.chunk_search.get_clauses(
                seeded.scope, [seeded.clauses["term"]], [seeded.document]
            )
        assert clause.text == TERM
        assert clause.heading == "Term"

    async def test_chunk_count_counts_embedded_chunks_of_the_model(
        self, uow_factory: UowFactory
    ) -> None:
        seeded = await seed(uow_factory)
        async with uow_factory() as uow:
            assert await uow.chunk_search.chunk_count(seeded.scope, [seeded.document], MODEL) == 4
            assert await uow.chunk_search.chunk_count(seeded.scope, [seeded.document], "other") == 0
            assert await uow.chunk_search.chunk_count(seeded.scope, [], MODEL) == 0
