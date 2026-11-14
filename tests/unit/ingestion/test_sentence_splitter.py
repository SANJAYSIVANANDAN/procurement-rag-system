import pytest
from hypothesis import given
from hypothesis import strategies as st

from covenantiq.ingestion.chunking.sentence_splitter import split_long_span, split_sentences
from covenantiq.ingestion.chunking.tokens import estimate_tokens


def sentences(text: str) -> list[str]:
    return [text[start:end] for start, end in split_sentences(text)]


class TestSplitting:
    def test_ordinary_sentences(self) -> None:
        text = "The Supplier shall deliver. The Customer shall pay. Both parties agree."
        assert sentences(text) == [
            "The Supplier shall deliver.",
            "The Customer shall pay.",
            "Both parties agree.",
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "See Sec. 4.2 for the payment terms.",
            "The Services, i.e. the hosting services, are described in Schedule 1.",
            "Fees may change, e.g. by indexation, on notice.",
            "This is Order No. 5 under the Agreement.",
            "Acme Cloud Services Ltd. and Northwind Traders plc agree.",
            "Delivered by Widgets Inc. to the Customer.",
            "Signed by J. Smith on behalf of the Supplier.",
            "Interest accrues at 1.5% per month on overdue sums.",
            "Subject to clause 6.1, the total liability shall not exceed the fees.",
            "The notice period is thirty (30) days.",
            "Refer to Art. 28 of the GDPR for the processor obligations.",
        ],
    )
    def test_legal_abbreviations_numbers_and_initials_do_not_end_a_sentence(
        self, text: str
    ) -> None:
        assert sentences(text) == [text]

    def test_a_real_sentence_end_after_an_abbreviation_like_word_still_splits(self) -> None:
        text = "The Supplier is Acme Co. The Customer is Northwind."
        # "Co." is an abbreviation, so there is no split after it
        assert sentences(text) == [text]
        text = "Payment is due in full. The Supplier may suspend the Services."
        assert len(sentences(text)) == 2

    def test_a_number_before_the_full_stop_can_end_a_sentence(self) -> None:
        text = "The Customer shall pay within 30 days. The Supplier shall then deliver."
        assert len(sentences(text)) == 2

    def test_a_label_opening_a_line_is_not_a_sentence(self) -> None:
        assert sentences("6. Limitation of Liability") == ["6. Limitation of Liability"]
        assert sentences("12.3. Exclusions apply here.") == ["12.3. Exclusions apply here."]

    def test_question_and_exclamation_marks_end_sentences(self) -> None:
        assert len(sentences("Is that agreed? Yes. Good!")) == 3

    def test_closing_quotes_and_brackets_stay_with_their_sentence(self) -> None:
        text = 'The term is defined ("Agreement."). The next sentence follows.'
        assert sentences(text)[0] == 'The term is defined ("Agreement.").'

    def test_a_lower_case_continuation_is_not_a_boundary(self) -> None:
        text = "Payment is due. and further amounts accrue thereafter."
        assert sentences(text) == [text]

    def test_semicolons_keep_a_list_together(self) -> None:
        text = "The Supplier shall: (a) deliver the Services; (b) report monthly; and (c) invoice."
        assert sentences(text) == [text]

    def test_paragraph_breaks_are_always_boundaries(self) -> None:
        text = "1. Definitions\n\n1.1 Agreement means this document without a final stop"
        assert sentences(text) == [
            "1. Definitions",
            "1.1 Agreement means this document without a final stop",
        ]

    def test_a_numbered_paragraph_after_a_sentence_starts_a_new_one(self) -> None:
        assert len(sentences("The Supplier shall deliver. 6.2 The Customer shall pay.")) == 2

    def test_empty_and_whitespace_input(self) -> None:
        assert split_sentences("") == []
        assert split_sentences("   \n\n  ") == []


class TestSpanInvariants:
    @given(st.text(alphabet=st.sampled_from(list('abcdefgXYZ .!?;,()\n0123456789"')), max_size=300))
    def test_spans_are_ordered_disjoint_and_cover_all_non_whitespace(self, text: str) -> None:
        spans = split_sentences(text)
        previous_end = 0
        for start, end in spans:
            assert previous_end <= start < end <= len(text)
            assert text[start:end] == text[start:end].strip()
            previous_end = end
        covered = {i for start, end in spans for i in range(start, end)}
        assert {i for i, ch in enumerate(text) if not ch.isspace()} <= covered


class TestSplitLongSpan:
    def test_pieces_fit_and_cover_the_text(self) -> None:
        text = "The Supplier shall " + "provide the services as described, " * 80 + "in full."
        pieces = split_long_span(text, (0, len(text)), max_tokens=100)
        assert len(pieces) > 1
        assert all(estimate_tokens(text[s:e]) <= 100 for s, e in pieces)
        covered = {i for s, e in pieces for i in range(s, e)}
        assert {i for i, ch in enumerate(text) if not ch.isspace()} <= covered

    def test_prefers_a_semicolon_over_a_comma(self) -> None:
        text = "alpha beta gamma, delta epsilon zeta; eta theta iota, kappa lambda mu"
        pieces = split_long_span(text, (0, len(text)), max_tokens=12)  # 48 characters
        assert text[pieces[0][0] : pieces[0][1]].endswith("zeta;")

    def test_text_with_no_separators_is_hard_cut(self) -> None:
        text = "x" * 1000
        pieces = split_long_span(text, (0, len(text)), max_tokens=50)
        assert all(e - s <= 200 for s, e in pieces)
        assert sum(e - s for s, e in pieces) == 1000

    def test_a_span_that_already_fits_is_returned_whole(self) -> None:
        assert split_long_span("short text", (0, 10), max_tokens=100) == [(0, 10)]
