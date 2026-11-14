from uuid import UUID

import pytest

from covenantiq.rag.config import EvidenceConfig
from covenantiq.rag.context import build_context
from covenantiq.rag.evidence import EvidenceAssessor, stems
from covenantiq.rag.types import EvidenceVerdict
from tests.unit.rag.builders import chunk, result

DEFAULT_DOC = UUID(int=1000)
TEXT = "The Supplier's total liability shall not exceed the fees paid."


def assess(
    scores: list[float | None],
    *,
    keywords: list[str] | None = None,
    skipped: bool = False,
    ranks: tuple[int | None, int | None] = (1, 1),
    text: str = TEXT,
    required: set[UUID] | None = None,
    docs: list[UUID] | None = None,
    config: EvidenceConfig | None = None,
) -> EvidenceVerdict:
    chunks = [
        chunk(
            text,
            rerank=score,
            dense=ranks[0] if i == 0 else None,
            lexical=ranks[1] if i == 0 else None,
            document=docs[i] if docs else DEFAULT_DOC,
        )
        for i, score in enumerate(scores)
    ]
    res = result(chunks, skipped=skipped)
    context = build_context(res, max_tokens=6000, parent_max_tokens=1500)
    return EvidenceAssessor(config).assess(
        keywords if keywords is not None else ["liability", "fees"],
        res,
        context,
        required_documents=required or (),
    )


class TestWithRerankScores:
    @pytest.mark.parametrize(
        ("scores", "level", "sufficient"),
        [
            ([0.90, 0.60, 0.10], "strong", True),  # top >= 0.55 and two chunks >= 0.35
            ([0.75], "strong", True),  # one chunk >= 0.7 is enough breadth
            ([0.60], "moderate", True),  # a strong score that nothing else supports
            ([0.60, 0.20], "moderate", True),
            ([0.45, 0.40], "moderate", True),  # 0.35 to 0.55
            ([0.34], "weak", False),
            ([0.05, 0.01], "weak", False),
        ],
    )
    def test_the_spec_table(self, scores: list[float], level: str, sufficient: bool) -> None:
        verdict = assess(scores)
        assert (verdict.level, verdict.sufficient) == (level, sufficient)

    def test_thresholds_come_from_the_config(self) -> None:
        strict = EvidenceConfig(rerank_strong=0.9, rerank_moderate=0.8, rerank_single_strong=0.95)
        assert assess([0.85, 0.82], config=strict).level == "moderate"
        assert assess([0.60], config=strict).level == "weak"

    def test_the_boundaries_are_inclusive(self) -> None:
        assert assess([0.55, 0.35]).level == "strong"
        assert assess([0.35]).level == "moderate"

    def test_the_verdict_says_why(self) -> None:
        verdict = assess([0.20])
        assert any("0.20" in reason for reason in verdict.reasons)
        assert verdict.signals["top_score"] == 0.20

    def test_no_chunks_is_none(self) -> None:
        res = result([])
        context = build_context(res, max_tokens=100, parent_max_tokens=10)
        verdict = EvidenceAssessor().assess(["x"], res, context)
        assert (verdict.level, verdict.sufficient) == ("none", False)


class TestRerankSkipped:
    def test_agreement_of_both_searches_gives_moderate_and_never_strong(self) -> None:
        verdict = assess([None], skipped=True, ranks=(2, 5))
        assert (verdict.level, verdict.sufficient) == ("moderate", True)
        assert any("skipped" in r for r in verdict.reasons)

    @pytest.mark.parametrize("ranks", [(1, None), (None, 1), (11, 1), (1, 11), (None, None)])
    def test_a_result_in_only_one_search_or_ranked_low_is_weak(
        self, ranks: tuple[int | None, int | None]
    ) -> None:
        verdict = assess([None], skipped=True, ranks=ranks)
        assert (verdict.level, verdict.sufficient) == ("weak", False)

    def test_the_agreement_window_is_configurable(self) -> None:
        loose = EvidenceConfig(rrf_agreement_rank=20)
        assert assess([None], skipped=True, ranks=(15, 15), config=loose).level == "moderate"


class TestKeywordCoverage:
    def test_missing_key_terms_downgrade_the_level(self) -> None:
        text = "The Customer shall pay each invoice within ninety days."
        verdict = assess([0.9, 0.9], keywords=["liability", "indemnity", "cap", "fees"], text=text)
        assert verdict.level == "moderate"  # strong, minus one
        assert verdict.signals["keyword_coverage"] == 0.0
        assert any("key terms" in r for r in verdict.reasons)

    def test_enough_coverage_leaves_it_alone(self) -> None:
        keywords = ["liability", "fees", "unrelated", "other"]  # half are present
        assert assess([0.9, 0.9], keywords=keywords).level == "strong"

    def test_stemming_matches_word_forms(self) -> None:
        text = "Invoices are payable within thirty days."
        keywords = ["invoice", "payables", "day"]  # other forms of words in the text
        assert assess([0.9, 0.9], keywords=keywords, text=text).level == "strong"

    def test_no_keywords_means_no_coverage_check(self) -> None:
        verdict = assess([0.9, 0.9], keywords=[])
        assert verdict.level == "strong" and verdict.signals["keyword_coverage"] is None

    def test_a_weak_result_cannot_fall_below_weak_from_coverage(self) -> None:
        assert assess([0.1], keywords=["zebra"]).level == "weak"


class TestDocumentCoverage:
    A, B = UUID(int=1), UUID(int=2)

    def test_every_compared_document_must_contribute(self) -> None:
        verdict = assess([0.9, 0.9], docs=[self.A, self.A], required={self.A, self.B})
        assert verdict.level == "moderate"
        assert any("compared documents" in r for r in verdict.reasons)

    def test_a_document_with_only_a_weak_chunk_does_not_count(self) -> None:
        verdict = assess([0.9, 0.1], docs=[self.A, self.B], required={self.A, self.B})
        assert verdict.level == "moderate"

    def test_all_documents_covered_keeps_the_level(self) -> None:
        verdict = assess([0.9, 0.9], docs=[self.A, self.B], required={self.A, self.B})
        assert verdict.level == "strong"


def test_stems_drop_stop_words_and_reduce_word_forms() -> None:
    assert stems("The Suppliers are liable for terminating") == stems("supplier liable terminate")
    assert stems("of the and") == set()
