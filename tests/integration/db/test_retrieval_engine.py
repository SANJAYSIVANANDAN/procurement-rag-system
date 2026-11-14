"""The retrieval engine end to end on real pgvector, with the fake embedder and reranker.

Seeds the 10-section MSA from the test fixtures as one clause and one chunk per section, then asks
questions a user would ask. Covers the spec's acceptance criteria for M9: the expected clause is in
the top 3, unanswerable questions are not sufficient, another user's identical text and deleted
documents are never returned, and retrieval falls back cleanly when the reranker fails.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import pytest

from covenantiq.ai.embeddings.base import EmbeddingUnavailable
from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.ai.rerank.base import RerankUnavailable
from covenantiq.ai.rerank.fake import LexicalOverlapReranker
from covenantiq.core.enums import DocumentStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.taxonomy import get_taxonomy
from covenantiq.rag.config import RagConfig, RetrievalConfig
from covenantiq.rag.context import build_context
from covenantiq.rag.evidence import EvidenceAssessor
from covenantiq.rag.query_analysis import extract_keywords
from covenantiq.rag.retrieval import RetrievalEngine
from covenantiq.rag.session import search_session_factory
from covenantiq.rag.types import RetrievalRequest
from tests.fakes import InMemoryCache
from tests.fixtures.documents import SECTIONS
from tests.integration.factories import make_chunk, make_clause, make_document, make_user

pytestmark = pytest.mark.usefixtures("require_pgvector")

UowFactory = Callable[[], UnitOfWork]
MODEL = "fake-hash"
embedder = HashEmbeddingProvider(dim=1024, model=MODEL)


@dataclass
class Corpus:
    scope: OwnershipScope
    document: UUID
    sections: dict[str, UUID]  # "Limitation of Liability" -> clause id
    chunks: dict[str, UUID]


async def seed(
    uow_factory: UowFactory,
    *,
    email: str | None = None,
    status: DocumentStatus = DocumentStatus.READY,
    title: str = "Acme Cloud MSA",
    long_section: str | None = None,
) -> Corpus:
    taxonomy = get_taxonomy()
    async with uow_factory() as uow:
        user = await uow.users.add(make_user(email))
        scope = OwnershipScope(user.id)
        document = await uow.documents.add(
            scope, make_document(user.id, status=status, title=title)
        )
        clauses, plan = [], []  # plan: (clause, heading, pieces, page)
        for number, section in enumerate(SECTIONS):
            heading = f"{section.number}. {section.title}"
            body = " ".join(section.clauses)
            pieces = [body]
            if long_section and section.title == long_section:
                pieces = list(section.clauses)  # several chunks, so the clause has neighbours
            clause = make_clause(
                user.id,
                document.id,
                number,
                text=body,
                clause_ref=str(section.number),
                heading=heading,
                heading_path=[heading],
                clause_types=taxonomy.classify(heading, body),
            )
            clauses.append(clause)
            plan.append((clause, section, heading, pieces))
        await uow.clauses.replace_for_document(scope, document.id, clauses)  # assigns the ids

        chunk_rows, texts = [], []
        for clause, section, heading, pieces in plan:
            for piece in pieces:
                chunk = make_chunk(user.id, document.id, clause.id, len(chunk_rows), text=piece)
                chunk.clause_types = list(clause.clause_types)
                chunk.page_start = chunk.page_end = section.page
                chunk_rows.append((section.title, chunk))
                texts.append(f"{heading} | {piece}")
        await uow.chunks.bulk_insert(scope, [c for _, c in chunk_rows])
        vectors = await embedder.embed_documents(texts)
        await uow.chunks.set_embeddings(
            scope, [(c.id, v) for (_, c), v in zip(chunk_rows, vectors, strict=True)], MODEL
        )
        await uow.commit()
    return Corpus(
        scope,
        document.id,
        {s.title: c.id for s, c in zip(SECTIONS, clauses, strict=True)},
        {title: c.id for title, c in chunk_rows},
    )


def engine(
    uow_factory: UowFactory,
    *,
    reranker: object | None = None,
    cache: InMemoryCache | None = None,
    embed: object = embedder,
    config: RagConfig | None = None,
    threshold: int = 5000,
) -> RetrievalEngine:
    return RetrievalEngine(
        search_session_factory(uow_factory),
        embed,  # type: ignore[arg-type]
        reranker,  # type: ignore[arg-type]
        config or RagConfig(),
        cache=cache,
        env="test",
        exact_scan_threshold=threshold,
    )


def ask(corpus: Corpus, query: str, **kwargs: object) -> RetrievalRequest:
    return RetrievalRequest(
        scope=corpus.scope,
        query=query,
        document_ids=[corpus.document],
        **kwargs,  # type: ignore[arg-type]
    )


def titles(result, corpus: Corpus) -> list[str]:  # type: ignore[no-untyped-def]
    names = {clause: title for title, clause in corpus.sections.items()}
    return [names[c.clause_id] for c in result.chunks]


# the question, and the section a reader would expect to answer it
QUESTIONS = [
    ("What is the cap on the supplier's total liability?", "Limitation of Liability"),
    ("How many days does the customer have to pay an invoice?", "Fees and Payment"),
    ("Does the agreement renew automatically?", "Term and Renewal"),
    ("Which law governs the agreement?", "General"),
    ("Must the supplier notify us of a personal data breach?", "Data Protection"),
    ("When can either party terminate for a material breach?", "Termination"),
    ("What availability does the supplier promise each month?", "Service Levels"),
]


class TestAnswerableQuestions:
    @pytest.mark.parametrize(("question", "expected"), QUESTIONS)
    async def test_the_expected_clause_is_in_the_top_three(
        self, uow_factory: UowFactory, question: str, expected: str
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(
            ask(corpus, question)
        )
        assert expected in titles(result, corpus)[:3]

    async def test_hybrid_beats_either_search_alone_on_an_exact_term_question(
        self, uow_factory: UowFactory
    ) -> None:
        """A question using the contract's own words is found by the lexical side even where the
        toy hash embedding is vague about it."""
        corpus = await seed(uow_factory)
        result = await engine(uow_factory).retrieve(ask(corpus, "negligence personal injury"))
        assert titles(result, corpus)[0] == "Limitation of Liability"
        top = result.chunks[0]
        assert top.lexical_rank == 1  # the exact words found it, whatever the embedding thought

    async def test_ranks_scores_and_timings_are_recorded_and_text_is_not(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(
            ask(corpus, "supplier liability cap fees")
        )
        trace = result.trace.to_dict()
        assert {"embed", "search", "fuse", "rerank", "expand", "total"} <= set(trace["timings_ms"])
        assert trace["dense"] and trace["lexical"] and trace["fused"] and trace["reranked"]
        assert trace["exact_scan"] is True  # a small set is scanned exactly
        assert "liability" in trace["keywords"]
        assert "total liability" not in str(trace)  # ids, ranks, scores: never document text

    async def test_the_chunk_carries_where_it_came_from(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(
            ask(corpus, "supplier total liability fees")
        )
        top = result.chunks[0]
        assert top.document_title == "Acme Cloud MSA"
        assert top.clause_ref == "6"
        assert top.heading_path == ["6. Limitation of Liability"]
        page = next(sec.page for sec in SECTIONS if sec.number == 6)
        assert (top.page_start, top.page_end) == (page, page)
        assert top.dense_rank is not None and top.lexical_rank is not None

    async def test_k_final_limits_the_result(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory).retrieve(ask(corpus, "supplier fees", k_final=3))
        assert len(result.chunks) == 3

    async def test_the_clause_type_boost_lifts_the_wanted_clause(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        plain = await engine(uow_factory).retrieve(ask(corpus, "supplier customer notice days"))
        boosted = await engine(uow_factory).retrieve(
            ask(
                corpus, "supplier customer notice days", clause_type_boost=["termination_for_cause"]
            )
        )
        plain_rank = titles(plain, corpus).index("Termination")
        boosted_rank = titles(boosted, corpus).index("Termination")
        assert boosted_rank <= plain_rank


class TestUnanswerableQuestions:
    @pytest.mark.parametrize(
        "question",
        [
            "What is the favourite colour of the office cat?",
            "How tall is the Eiffel Tower?",
            "Does the vendor offer a free trial of the mobile app?",
        ],
    )
    async def test_the_evidence_gate_says_insufficient(
        self, uow_factory: UowFactory, question: str
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(
            ask(corpus, question)
        )
        context = build_context(result, max_tokens=6000, parent_max_tokens=1500)
        verdict = EvidenceAssessor().assess(extract_keywords(question).keywords, result, context)
        assert verdict.sufficient is False
        assert verdict.level in ("weak", "none")

    async def test_an_answerable_question_passes_the_same_gate(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        question = "What is the cap on the supplier's total liability?"
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(
            ask(corpus, question)
        )
        context = build_context(result, max_tokens=6000, parent_max_tokens=1500)
        verdict = EvidenceAssessor().assess(extract_keywords(question).keywords, result, context)
        assert verdict.sufficient is True


class TestIsolation:
    async def test_another_users_identical_document_is_never_returned(
        self, uow_factory: UowFactory
    ) -> None:
        mine = await seed(uow_factory, email="mine@example.com")
        theirs = await seed(uow_factory, email="theirs@example.com")
        # the caller wrongly includes the other user's document id: still nothing leaks
        request = RetrievalRequest(
            scope=mine.scope,
            query="total liability fees paid",
            document_ids=[mine.document, theirs.document],
        )
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(request)
        assert result.chunks
        assert {c.document_id for c in result.chunks} == {mine.document}
        assert not {c.chunk_id for c in result.chunks} & set(theirs.chunks.values())
        assert all(p.document_id == mine.document for p in result.parents.values())

    async def test_a_deleted_document_is_never_returned(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        found = await engine(uow_factory).retrieve(ask(corpus, "total liability"))
        assert found.chunks
        async with uow_factory() as uow:
            assert await uow.documents.soft_delete(corpus.scope, corpus.document)
            await uow.commit()
        assert (await engine(uow_factory).retrieve(ask(corpus, "total liability"))).chunks == []

    async def test_a_document_still_processing_is_never_returned(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory, status=DocumentStatus.EMBEDDING)
        assert (await engine(uow_factory).retrieve(ask(corpus, "total liability"))).chunks == []

    async def test_only_the_named_documents_are_searched(self, uow_factory: UowFactory) -> None:
        first = await seed(uow_factory, email="a@example.com", title="First")
        second = await seed(uow_factory, email="b@example.com", title="Second")
        result = await engine(uow_factory).retrieve(ask(first, "total liability"))
        assert {c.document_title for c in result.chunks} == {"First"}
        assert second.document not in {c.document_id for c in result.chunks}

    async def test_no_documents_means_an_empty_result_without_searching(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        request = RetrievalRequest(scope=corpus.scope, query="liability", document_ids=[])
        result = await engine(uow_factory).retrieve(request)
        assert result.chunks == [] and result.parents == {}

    async def test_an_empty_query_is_refused(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        with pytest.raises(ValueError, match="must not be empty"):
            await engine(uow_factory).retrieve(ask(corpus, "   "))


class SlowReranker:
    model = "slow"

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        await asyncio.sleep(5)
        return [(0, 1.0)]

    async def aclose(self) -> None:
        return None


class BrokenReranker:
    model = "broken"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        raise self.error

    async def aclose(self) -> None:
        return None


class CountingReranker(LexicalOverlapReranker):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[list[str]] = []

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        self.documents.append(list(docs))
        return await super().rerank(query, docs, top_k)


class TestRerankingIsOptional:
    """Spec acceptance: retrieval falls back cleanly when the reranker times out."""

    async def baseline(self, uow_factory: UowFactory, corpus: Corpus, query: str) -> list[UUID]:
        result = await engine(uow_factory, reranker=None).retrieve(ask(corpus, query))
        return [c.chunk_id for c in result.chunks]

    async def test_a_reranker_that_times_out_leaves_the_fused_order(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        query = "supplier total liability fees"
        fast = RagConfig(retrieval=RetrievalConfig(rerank_timeout_seconds=0.05))
        result = await engine(uow_factory, reranker=SlowReranker(), config=fast).retrieve(
            ask(corpus, query)
        )
        assert result.trace.rerank_skipped is True
        assert "timed out" in (result.trace.rerank_skipped_reason or "")
        assert [c.chunk_id for c in result.chunks] == await self.baseline(
            uow_factory, corpus, query
        )
        assert all(c.rerank_score is None for c in result.chunks)

    @pytest.mark.parametrize("error", [RerankUnavailable("down"), RerankUnavailable("busy")])
    async def test_a_failing_reranker_leaves_the_fused_order(
        self, uow_factory: UowFactory, error: Exception
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=BrokenReranker(error)).retrieve(
            ask(corpus, "supplier total liability fees")
        )
        assert result.trace.rerank_skipped is True
        assert "RerankUnavailable" in (result.trace.rerank_skipped_reason or "")
        assert result.chunks

    async def test_no_reranker_configured_is_the_same_as_skipped(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=None).retrieve(ask(corpus, "liability"))
        assert result.trace.rerank_skipped is True
        assert "no reranker" in (result.trace.rerank_skipped_reason or "")

    async def test_rerank_false_skips_it_for_one_request(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        reranker = CountingReranker()
        result = await engine(uow_factory, reranker=reranker).retrieve(
            ask(corpus, "supplier liability", rerank=False)
        )
        assert result.trace.rerank_skipped is True and reranker.documents == []

    async def test_the_skipped_verdict_is_never_strong(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        question = "What is the cap on the supplier's total liability?"
        result = await engine(
            uow_factory, reranker=BrokenReranker(RerankUnavailable("x"))
        ).retrieve(ask(corpus, question))
        context = build_context(result, max_tokens=6000, parent_max_tokens=1500)
        verdict = EvidenceAssessor().assess(extract_keywords(question).keywords, result, context)
        assert verdict.level in ("moderate", "weak")

    async def test_reranking_changes_the_order_by_its_scores(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(
            ask(corpus, "personal data breach notify")
        )
        scores = [c.rerank_score for c in result.chunks]
        assert all(s is not None for s in scores)
        assert scores == sorted(scores, reverse=True)  # type: ignore[type-var]
        assert titles(result, corpus)[0] == "Data Protection"

    async def test_only_the_heading_path_and_text_are_sent_to_the_reranker(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        reranker = CountingReranker()
        await engine(uow_factory, reranker=reranker).retrieve(ask(corpus, "supplier liability"))
        (sent,) = reranker.documents
        assert all(" | " in doc for doc in sent)
        assert any(doc.startswith("6. Limitation of Liability | ") for doc in sent)


class TestCaches:
    async def test_the_query_embedding_is_cached_for_a_day(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        cache = InMemoryCache()
        first = await engine(uow_factory, cache=cache).retrieve(ask(corpus, "total liability"))
        assert first.trace.query_embedding_cached is False
        key = next(k for k in cache.data if ":qemb:" in k)
        assert cache.ttls[key] == 86400
        again = await engine(uow_factory, cache=cache).retrieve(ask(corpus, "  total   liability "))
        assert again.trace.query_embedding_cached is True  # whitespace does not change the key
        assert [c.chunk_id for c in again.chunks] == [c.chunk_id for c in first.chunks]

    async def test_rerank_scores_are_cached_for_an_hour_and_reused(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        cache, reranker = InMemoryCache(), CountingReranker()
        await engine(uow_factory, reranker=reranker, cache=cache).retrieve(
            ask(corpus, "supplier liability fees")
        )
        rerank_keys = [k for k in cache.data if ":rr:" in k]
        assert rerank_keys and all(cache.ttls[k] == 3600 for k in rerank_keys)
        second = await engine(uow_factory, reranker=reranker, cache=cache).retrieve(
            ask(corpus, "supplier liability fees")
        )
        assert len(reranker.documents) == 1  # the second call needed no reranking at all
        assert second.trace.rerank_cache_hits == len(rerank_keys)
        assert second.trace.rerank_skipped is False

    async def test_a_cache_that_fails_is_just_a_miss(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        cache = InMemoryCache()
        cache.fail = True
        result = await engine(uow_factory, reranker=LexicalOverlapReranker(), cache=cache).retrieve(
            ask(corpus, "supplier liability fees")
        )
        assert result.chunks and result.trace.rerank_skipped is False


class FailingEmbedder(HashEmbeddingProvider):
    async def embed_query(self, text: str) -> list[float]:
        raise EmbeddingUnavailable("provider is down")


class TestDegradedSearch:
    async def test_without_an_embedding_provider_keyword_search_still_answers(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, embed=FailingEmbedder(1024, MODEL)).retrieve(
            ask(corpus, "personal data breach notify")
        )
        assert titles(result, corpus)[0] == "Data Protection"
        assert result.trace.dense == []
        assert any("embedding provider" in note for note in result.trace.notes)


class TestParentExpansion:
    async def test_the_whole_clause_comes_back_as_the_parent(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory).retrieve(ask(corpus, "total liability fees"))
        top = result.chunks[0]
        parent = result.parents[top.clause_id]
        assert parent.heading_path == top.heading_path
        assert top.text in parent.text
        assert parent.token_count > 0

    async def test_expansion_can_be_skipped(self, uow_factory: UowFactory) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory).retrieve(
            ask(corpus, "total liability fees", expand_parents=False)
        )
        assert result.parents == {} and result.neighbours == {}

    async def test_a_clause_too_long_to_include_gets_its_neighbouring_chunks(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory, long_section="Limitation of Liability")
        tiny = RagConfig.model_validate({"context": {"parent_max_tokens": 5}})
        result = await engine(uow_factory, config=tiny).retrieve(
            ask(corpus, "total liability shall not exceed the fees", k_final=3)
        )
        top = next(c for c in result.chunks if c.heading_path == ["6. Limitation of Liability"])
        beside = result.neighbours[top.chunk_id]
        assert beside
        assert all(n.clause_id == top.clause_id for n in beside)  # never a chunk of another clause
        assert all(abs(n.ordinal - top.ordinal) == 1 for n in beside)

    async def test_the_context_built_from_a_result_has_numbered_sources(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, reranker=LexicalOverlapReranker()).retrieve(
            ask(corpus, "supplier total liability fees")
        )
        context = build_context(result, max_tokens=6000, parent_max_tokens=1500)
        assert context.sources[0].marker == "C1"
        assert 'document="Acme Cloud MSA"' in context.text
        assert 'clause="6"' in context.text


class TestModelMismatch:
    async def test_chunks_embedded_with_another_model_are_not_searched_and_the_trace_says_why(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        other = HashEmbeddingProvider(dim=1024, model="a-newer-model")
        result = await engine(uow_factory, embed=other).retrieve(
            ask(corpus, "personal data breach")
        )
        assert result.trace.dense == []
        assert any("reembed" in note and "a-newer-model" in note for note in result.trace.notes)
        assert titles(result, corpus)[0] == "Data Protection"  # keyword search still answers


class TestRerankScoresAreFetchedInOneRoundTrip:
    async def test_all_candidates_are_looked_up_with_a_single_call(
        self, uow_factory: UowFactory
    ) -> None:
        corpus = await seed(uow_factory)
        cache = InMemoryCache()
        await engine(uow_factory, reranker=LexicalOverlapReranker(), cache=cache).retrieve(
            ask(corpus, "supplier liability fees")
        )
        assert cache.lookups == 2  # one for the query vector, one for every rerank score


class TestExactScan:
    @pytest.mark.parametrize(("threshold", "exact"), [(5000, True), (1, False)])
    async def test_a_small_candidate_set_is_scanned_exactly(
        self, uow_factory: UowFactory, threshold: int, exact: bool
    ) -> None:
        corpus = await seed(uow_factory)
        result = await engine(uow_factory, threshold=threshold).retrieve(
            ask(corpus, "supplier liability")
        )
        assert result.trace.exact_scan is exact
        assert result.chunks
