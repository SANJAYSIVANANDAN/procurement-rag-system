import math
import re
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from covenantiq.rag.fusion import rrf_fuse
from covenantiq.rag.query_analysis import build_fts_text, extract_keywords


def ids(n: int) -> list[UUID]:
    return [UUID(int=i + 1) for i in range(n)]


A, B, C, D, E = ids(5)


class TestRrf:
    def test_a_known_example_worked_by_hand(self) -> None:
        # dense: A B C   lexical: B D A      k = 60
        result = rrf_fuse([A, B, C], [B, D, A], k=60)
        scores = {c.chunk_id: c.rrf_score for c in result}
        assert scores[A] == pytest.approx(1 / 61 + 1 / 63)
        assert scores[B] == pytest.approx(1 / 62 + 1 / 61)
        assert scores[C] == pytest.approx(1 / 63)
        assert scores[D] == pytest.approx(1 / 62)
        assert [c.chunk_id for c in result] == [B, A, D, C]

    def test_ranks_are_recorded_one_based_and_none_when_absent(self) -> None:
        result = {c.chunk_id: c for c in rrf_fuse([A, B], [B, C])}
        assert (result[A].dense_rank, result[A].lexical_rank) == (1, None)
        assert (result[B].dense_rank, result[B].lexical_rank) == (2, 1)
        assert (result[C].dense_rank, result[C].lexical_rank) == (None, 2)

    def test_agreement_between_lists_beats_a_single_first_place(self) -> None:
        # B is second in both lists; A is first in only one
        assert rrf_fuse([A, B], [C, B])[0].chunk_id == B

    def test_only_one_list_still_works(self) -> None:
        assert [c.chunk_id for c in rrf_fuse([A, B, C], [])] == [A, B, C]
        assert [c.chunk_id for c in rrf_fuse([], [C, A])] == [C, A]
        assert rrf_fuse([], []) == []

    def test_the_limit_is_applied_after_sorting(self) -> None:
        assert [c.chunk_id for c in rrf_fuse([A, B, C, D], [], limit=2)] == [A, B]

    def test_weights_change_which_list_wins(self) -> None:
        assert rrf_fuse([A], [B], weight_dense=1.0, weight_lexical=3.0)[0].chunk_id == B
        assert rrf_fuse([A], [B], weight_dense=3.0, weight_lexical=1.0)[0].chunk_id == A

    def test_the_clause_type_boost_multiplies_and_can_reorder(self) -> None:
        types = {B: ["limitation_of_liability"], A: ["payment_terms"]}
        plain = rrf_fuse([A, B], [], clause_types=types)
        assert [c.chunk_id for c in plain] == [A, B]
        boosted = rrf_fuse(
            [A, B], [], clause_types=types, boost_types=["limitation_of_liability"], boost=1.1
        )
        assert [c.chunk_id for c in boosted] == [B, A]  # 1.1/62 > 1/61
        assert boosted[0].fused_score == pytest.approx(boosted[0].rrf_score * 1.1)
        assert boosted[0].rrf_score == pytest.approx(1 / 62)  # the stored RRF score is unboosted

    def test_the_boost_never_applies_without_a_matching_type(self) -> None:
        result = rrf_fuse([A], [], clause_types={A: ["term"]}, boost_types=["renewal"])
        assert result[0].fused_score == result[0].rrf_score

    def test_a_repeated_id_keeps_its_best_rank(self) -> None:
        result = {c.chunk_id: c for c in rrf_fuse([A, B, A], [])}
        assert result[A].dense_rank == 1
        assert len(result) == 2

    def test_ties_are_broken_deterministically(self) -> None:
        first = rrf_fuse([A], [B])
        second = rrf_fuse([A], [B])
        assert [c.chunk_id for c in first] == [c.chunk_id for c in second] == [A, B]

    @given(
        dense=st.lists(st.integers(0, 12), max_size=10, unique=True),
        lexical=st.lists(st.integers(0, 12), max_size=10, unique=True),
    )
    def test_properties(self, dense: list[int], lexical: list[int]) -> None:
        d, lx = [UUID(int=i + 1) for i in dense], [UUID(int=i + 1) for i in lexical]
        result = rrf_fuse(d, lx, limit=100)
        assert {c.chunk_id for c in result} == set(d) | set(lx)  # nothing lost, nothing invented
        scores = [c.fused_score for c in result]
        assert scores == sorted(scores, reverse=True)
        assert all(
            0 < c.rrf_score <= 2 / 61 + 1e-12 and not math.isnan(c.rrf_score) for c in result
        )


class TestKeywords:
    def test_content_words_survive_and_filler_is_dropped(self) -> None:
        found = extract_keywords("What does the contract say about limitation of liability?")
        assert found.keywords == ["limitation", "liability"]

    def test_order_is_kept_and_duplicates_are_removed(self) -> None:
        assert extract_keywords("liability cap liability Cap").keywords == ["liability", "cap"]

    def test_legal_references_and_numbers_are_kept(self) -> None:
        found = extract_keywords("Is Schedule 3 or clause 6.2 relevant to non-renewal?")
        assert "schedule" in found.keywords
        assert "3" in found.keywords
        assert "6.2" in found.keywords
        assert "non-renewal" in found.keywords

    def test_modal_verbs_are_not_search_terms(self) -> None:
        assert extract_keywords("the supplier shall indemnify").keywords == [
            "supplier",
            "indemnify",
        ]

    def test_quoted_phrases_are_kept_whole_and_not_split_into_keywords(self) -> None:
        found = extract_keywords('Where is "limitation of liability" and the cap?')
        assert found.phrases == ["limitation of liability"]
        assert found.keywords == ["cap"]
        assert found.fts_text == '"limitation of liability" or cap'

    def test_curly_quotes_work_too(self) -> None:
        found = extract_keywords("find “consequential loss” please")
        assert found.phrases == ["consequential loss"]

    def test_a_single_quoted_word_is_an_ordinary_keyword(self) -> None:
        found = extract_keywords('is "indemnity" covered')
        assert found.phrases == []
        assert "indemnity" in found.keywords

    def test_the_keyword_limit_is_respected(self) -> None:
        query = " ".join(f"term{i}" for i in range(40))
        assert len(extract_keywords(query, max_keywords=5).keywords) == 5

    @pytest.mark.parametrize("query", ["", "   ", "the of and", "?!"])
    def test_a_query_without_content_gives_no_search_text(self, query: str) -> None:
        found = extract_keywords(query)
        assert found.keywords == []
        assert found.fts_text == ""

    def test_the_search_text_joins_terms_with_or(self) -> None:
        assert build_fts_text(["cap", "fees"], []) == "cap or fees"
        assert build_fts_text([], ['"a b"'.strip('"')]) == '"a b"'

    @pytest.mark.parametrize(
        "hostile", ["'; DROP TABLE users; --", "a & b | !c", "cap -fees", '" or 1=1 --', "((("]
    )
    def test_hostile_input_cannot_leave_a_search_operator_or_stray_quote(
        self, hostile: str
    ) -> None:
        found = extract_keywords(hostile)
        without_phrases = re.sub(r'"[^"]*"', "", found.fts_text)
        assert '"' not in without_phrases  # a quote only ever opens or closes a whole phrase
        for keyword in found.keywords:
            assert not keyword.startswith("-")  # websearch treats a leading minus as NOT
            assert all(ch.isalnum() or ch in ".-/'" for ch in keyword)
        assert ";" not in found.fts_text and "&" not in found.fts_text and "|" not in found.fts_text

    @given(st.text(max_size=200))
    def test_never_raises_and_only_emits_safe_terms(self, query: str) -> None:
        found = extract_keywords(query)
        for keyword in found.keywords:
            assert keyword == keyword.lower()
            assert not keyword.startswith("-")
