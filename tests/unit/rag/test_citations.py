"""The citation validator's table of cases (spec 19.12). Fully offline and deterministic."""

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from covenantiq.rag.abstention import ABSTENTION_HEADLINE, UNVERIFIED_NOTICE
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import CitationConfig
from covenantiq.rag.types import ContextSource
from tests.unit.rag.builders import source

LIABILITY = (
    "6.1 Neither party excludes liability for death or personal injury caused by negligence. "
    "6.2 Subject to clause 6.1, the Supplier's total liability shall not exceed the fees paid "
    "in the twelve months before the claim. "
    "6.3 Neither party is liable for indirect or consequential loss."
)
PAYMENT = (
    "3.1 The Customer shall pay each invoice within 90 days of the date of the invoice. "
    "3.2 Late payments accrue interest at 1.5% per month."
)
TERMINATION = (
    "8.1 Either party may terminate this Agreement immediately by written notice if the other "
    "commits a material breach that is not remedied within 30 days."
)


@pytest.fixture
def sources() -> dict[str, ContextSource]:
    return {
        "C1": source("C1", LIABILITY, ref="6.2", pages=(9, 9)),
        "C2": source("C2", PAYMENT, ref="3.1", pages=(4, 5)),
        "C3": source("C3", TERMINATION, ref="8.1", pages=(11, 11)),
    }


def validate(answer: str, sources: dict[str, ContextSource], **kwargs: object):  # type: ignore[no-untyped-def]
    return CitationValidator(**kwargs).validate(answer, sources)  # type: ignore[arg-type]


class TestValidAnswers:
    def test_a_supported_cited_answer_is_grounded(self, sources: dict[str, ContextSource]) -> None:
        answer = (
            "The Supplier's total liability is capped at the fees paid "
            "in the prior twelve months [C1]."
        )
        result = validate(answer, sources)
        assert result.status == "grounded"
        assert result.text == answer
        assert [c.marker for c in result.citations] == ["C1"]
        assert (result.unsupported, result.unmarked_claims, result.invalid_markers) == (0, 0, 0)

    def test_several_sentences_each_with_their_own_source(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = (
            "Invoices must be paid within 90 days of the invoice date [C2]. "
            "Either party can terminate immediately for a material breach "
            "not remedied within 30 days [C3]."
        )
        result = validate(answer, sources)
        assert result.status == "grounded"
        assert [c.marker for c in result.citations] == ["C2", "C3"]

    def test_the_marker_after_the_full_stop_still_belongs_to_its_sentence(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = (
            "Invoices must be paid within 90 days of the invoice date. [C2] "
            "Late payments accrue interest at 1.5% per month. [C2]"
        )
        result = validate(answer, sources)
        assert result.status == "grounded"
        assert all(check.markers == ["C2"] for check in result.sentences)

    def test_a_correct_quotation_is_supported(self, sources: dict[str, ContextSource]) -> None:
        answer = 'The clause says "total liability shall not exceed the fees paid" [C1].'
        assert validate(answer, sources).status == "grounded"

    def test_a_quotation_with_a_small_typo_still_matches(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = 'It states "total liabilty shall not exceed the fees paid" [C1].'
        assert validate(answer, sources).status == "grounded"

    def test_curly_quotes_are_recognised(self, sources: dict[str, ContextSource]) -> None:
        answer = "It states “total liability shall not exceed the fees paid” [C1]."
        assert validate(answer, sources).status == "grounded"


class TestMarkerForms:
    @pytest.mark.parametrize(
        "marker_text", ["[C1, C2]", "[C1,C2]", "[C1][C2]", "[C1] [C2]", "[C2, C1]"]
    )
    def test_multi_marker_forms_are_all_understood(
        self, sources: dict[str, ContextSource], marker_text: str
    ) -> None:
        answer = (
            "Liability is capped at the fees paid, "
            f"and invoices are due within 90 days {marker_text}."
        )
        result = validate(answer, sources)
        assert sorted(c.marker for c in result.citations) == ["C1", "C2"]
        assert result.invalid_markers == 0

    def test_a_sentence_citing_two_sources_is_checked_against_both(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = (
            "The cap is the fees paid in twelve months and invoices are payable within 90 days "
            "[C1, C2]."
        )
        assert validate(answer, sources).status == "grounded"

    def test_markers_in_the_middle_of_a_sentence(self, sources: dict[str, ContextSource]) -> None:
        answer = (
            "Liability is capped at the fees paid [C1] while payment is due within 90 days [C2]."
        )
        result = validate(answer, sources)
        assert sorted(c.marker for c in result.citations) == ["C1", "C2"]


class TestUnknownMarkers:
    def test_an_unknown_marker_is_removed_counted_and_the_rest_kept(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = "Invoices are payable within 90 days [C2]. The notice period is long [C9]."
        result = validate(answer, sources)
        assert result.invalid_markers == 1
        assert "[C9]" not in result.text
        assert "[C2]" in result.text

    def test_an_unknown_marker_inside_a_list_is_dropped_from_the_list(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate("Invoices are payable within 90 days [C2, C9].", sources)
        assert result.invalid_markers == 1
        assert "[C2]" in result.text and "C9" not in result.text

    def test_an_answer_with_only_unknown_markers_is_abstained(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate("The cap is six months [C7]. Renewal is automatic [C8].", sources)
        assert result.status == "abstained"
        assert result.invalid_markers == 2

    def test_marker_text_that_is_not_a_marker_is_left_alone(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = "See [Appendix A] and [1] for 90 days payment terms [C2]."
        assert "[Appendix A]" in validate(answer, sources).text


class TestUnsupportedSentences:
    def test_a_fabricated_quotation_is_unsupported_however_well_the_rest_matches(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = (
            "The Supplier's total liability shall not exceed the fees, and "
            '"the Supplier accepts unlimited liability for data loss" [C1].'
        )
        result = validate(answer, sources)
        assert result.unsupported == 1
        assert "not in the cited source" in (result.sentences[0].reason or "")

    def test_a_sentence_unrelated_to_its_source_is_unsupported(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate("The Supplier must deliver quarterly security reports [C1].", sources)
        assert result.unsupported == 1

    def test_a_changed_number_is_unsupported_even_with_matching_words(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate(
            "The Customer shall pay each invoice within 60 days of the date of the invoice [C2].",
            sources,
        )
        assert result.unsupported == 1
        assert "60" in (result.sentences[0].reason or "")

    def test_the_overlap_threshold_is_configurable(self, sources: dict[str, ContextSource]) -> None:
        answer = "The Supplier is responsible for losses [C1]."  # a loose paraphrase
        strict = validate(answer, sources, config=CitationConfig(overlap_threshold=0.9))
        loose = validate(answer, sources, config=CitationConfig(overlap_threshold=0.0))
        assert strict.unsupported == 1 and loose.unsupported == 0

    def test_a_marker_only_sentence_has_nothing_to_verify(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate("Yes [C1].", sources)
        assert result.unsupported == 1


class TestUnmarkedClaims:
    @pytest.mark.parametrize(
        "claim",
        [
            "The notice period is 30 days.",  # a number
            "Late payments accrue interest at 1.5% per month.",
            "The fee is $5,000 per year.",  # money
            "The agreement started in March.",  # a date
            "Termination requires thirty days notice.",  # a spelled-out duration
            "The customer must keep the Confidential Information secret.",  # a defined term
        ],
    )
    def test_a_factual_sentence_without_a_marker_is_flagged(
        self, sources: dict[str, ContextSource], claim: str
    ) -> None:
        result = validate(f"Liability is capped at the fees paid [C1]. {claim}", sources)
        assert result.unmarked_claims == 1
        assert result.status in ("partially_grounded", "ungrounded")

    @pytest.mark.parametrize(
        "remark",
        [
            "That is what the contract says.",
            "I could not find anything about indemnities.",
            "Let me know if you want more detail.",
        ],
    )
    def test_a_sentence_with_nothing_checkable_is_not_flagged(
        self, sources: dict[str, ContextSource], remark: str
    ) -> None:
        result = validate(f"Liability is capped at the fees paid [C1]. {remark}", sources)
        assert result.unmarked_claims == 0
        assert result.status == "grounded"

    def test_a_defined_term_at_the_start_of_a_sentence_is_not_enough_on_its_own(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate(
            "Liability is capped at the fees paid [C1]. Termination is addressed.", sources
        )
        assert result.unmarked_claims == 0


class TestGroundingStatus:
    def good(self, n: int) -> str:
        return " ".join("Liability is capped at the fees paid [C1]." for _ in range(n))

    def test_partially_grounded_allows_up_to_30_percent_bad(
        self, sources: dict[str, ContextSource]
    ) -> None:
        # 1 unmarked claim among 4 factual sentences = 25%
        result = validate(self.good(3) + " The notice period is 30 days.", sources)
        assert result.status == "partially_grounded"
        assert result.text == (self.good(3) + " The notice period is 30 days.")  # shown as it is

    def test_more_than_30_percent_bad_is_ungrounded_with_a_visible_notice(
        self, sources: dict[str, ContextSource]
    ) -> None:
        # 2 bad among 4 factual sentences = 50%
        result = validate(
            self.good(2) + " The notice period is 30 days. The fee is $5,000.", sources
        )
        assert result.status == "ungrounded"
        assert result.text.endswith(UNVERIFIED_NOTICE)
        assert result.citations  # the valid citations are still reported

    def test_the_boundary_is_inclusive(self, sources: dict[str, ContextSource]) -> None:
        # exactly 30%: 3 bad of 10 factual
        answer = (
            self.good(7) + " The notice period is 30 days. The fee is $5,000. It began in March."
        )
        assert validate(answer, sources).status == "partially_grounded"

    def test_the_fraction_is_configurable(self, sources: dict[str, ContextSource]) -> None:
        answer = self.good(3) + " The notice period is 30 days."
        strict = validate(answer, sources, config=CitationConfig(partial_unsupported_fraction=0.1))
        assert strict.status == "ungrounded"


class TestAbstention:
    @pytest.mark.parametrize(
        "answer",
        [
            "The cap is twelve months of fees.",  # no markers at all
            "",
            "   ",
            "Yes.",
        ],
    )
    def test_no_valid_citation_means_the_answer_is_replaced(
        self, sources: dict[str, ContextSource], answer: str
    ) -> None:
        result = validate(answer, sources)
        assert result.status == "abstained"
        assert ABSTENTION_HEADLINE in result.text
        assert answer.strip() not in result.text or not answer.strip()
        assert result.citations == []

    def test_only_the_hash_of_the_discarded_answer_is_kept(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = "The cap is twelve months of fees."
        result = validate(answer, sources)
        assert result.discarded_answer_sha == hashlib.sha256(answer.encode()).hexdigest()
        assert answer not in result.text

    def test_a_valid_answer_discards_nothing(self, sources: dict[str, ContextSource]) -> None:
        assert (
            validate("Liability is capped at the fees paid [C1].", sources).discarded_answer_sha
            is None
        )

    def test_the_message_names_the_documents_searched(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = CitationValidator().validate("No idea.", sources, document_titles=["Acme MSA"])
        assert '"Acme MSA"' in result.text


class TestCitationDetails:
    def test_the_quote_is_the_source_sentence_that_matches_the_claim(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate("Late interest is 1.5% per month [C2].", sources)
        assert result.citations[0].quote.startswith("3.2 Late payments accrue interest")

    def test_each_citation_carries_where_to_find_it(
        self, sources: dict[str, ContextSource]
    ) -> None:
        result = validate("Liability is capped at the fees paid [C1].", sources)
        citation = result.citations[0]
        assert (citation.clause_ref, citation.page_start, citation.page_end) == ("6.2", 9, 9)
        assert citation.document_id == sources["C1"].document_id
        assert citation.chunk_ids == sources["C1"].chunk_ids

    def test_a_long_source_sentence_is_cut_to_the_limit_at_a_word_boundary(self) -> None:
        long_sentence = (
            "The Supplier shall provide the Services "
            + "and further obligations " * 30
            + "in full."
        )
        sources = {"C1": source("C1", long_sentence)}
        result = validate(
            "The Supplier shall provide the Services and further obligations [C1].", sources
        )
        quote = result.citations[0].quote
        assert len(quote) <= 301 and quote.endswith("…")
        assert not quote[:-1].endswith(" ")

    def test_citations_are_listed_once_each_in_marker_order(
        self, sources: dict[str, ContextSource]
    ) -> None:
        answer = (
            "Payment is due within 90 days [C2]. Liability is capped at the fees paid [C1]. "
            "Interest is 1.5% per month [C2]."
        )
        assert [c.marker for c in validate(answer, sources).citations] == ["C1", "C2"]


class TestProperties:
    @given(st.text(max_size=300))
    def test_validation_never_raises_and_always_returns_a_status(self, answer: str) -> None:
        sources = {"C1": source("C1", LIABILITY)}
        result = CitationValidator().validate(answer, sources)
        assert result.status in {"grounded", "partially_grounded", "ungrounded", "abstained"}
        assert result.text

    @given(st.text(alphabet=st.sampled_from(list("abc [C123],.")), max_size=120))
    def test_no_unknown_marker_ever_survives_in_the_text(self, answer: str) -> None:
        sources = {"C1": source("C1", LIABILITY)}
        result = CitationValidator().validate(answer, sources)
        if result.status != "abstained":
            assert "C2" not in result.text and "C3" not in result.text
