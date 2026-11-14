from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from covenantiq.ingestion.chunking.chunker import ChunkSettings, chunk_clause
from covenantiq.ingestion.chunking.clause_builder import (
    ClauseDraft,
    build_clauses,
    choose_heading_depth,
    group_blocks,
)
from covenantiq.ingestion.chunking.document import chunk_document, context_header
from covenantiq.ingestion.chunking.tokens import estimate_tokens
from covenantiq.ingestion.extraction.types import ExtractedBlock
from covenantiq.ingestion.taxonomy import get_taxonomy


def heading(
    text: str, level: int = 1, ref: str | None = None, page: int | None = 1
) -> ExtractedBlock:
    return ExtractedBlock("heading", text, page, level, (text,), ref)


def para(
    text: str, page: int | None = 1, path: tuple[str, ...] = (), **extra: object
) -> ExtractedBlock:
    return ExtractedBlock("paragraph", text, page, None, path, None, **extra)  # type: ignore[arg-type]


def table(text: str, page: int | None = 1) -> ExtractedBlock:
    return ExtractedBlock("table", text, page, None, (), None)


SENTENCE = "The Supplier shall provide the Services in accordance with this Agreement. "


def long_text(sentences: int) -> str:
    return (SENTENCE * sentences).strip()


def clause_of(*blocks: ExtractedBlock, ordinal: int = 0) -> ClauseDraft:
    return ClauseDraft(
        ordinal=ordinal, clause_ref="1", heading="H", heading_path=("H",), blocks=list(blocks)
    )


class TestClauseBuilder:
    def test_headings_up_to_the_depth_start_clauses(self) -> None:
        blocks = [
            heading("1. Services", 1, "1"),
            para("Intro."),
            heading("1.1 Scope", 2, "1.1"),
            para("Scope text."),
            heading("2. Fees", 1, "2"),
            para("Fee text."),
        ]
        assert [len(g) for g in group_blocks(blocks, 1)] == [4, 2]
        assert [len(g) for g in group_blocks(blocks, 2)] == [2, 2, 2]

    def test_the_document_title_at_level_zero_never_starts_a_clause(self) -> None:
        blocks = [heading("Master Agreement", 0), heading("1. A", 1, "1"), para("x")]
        groups = group_blocks(blocks, 3)
        assert [[b.text for b in g] for g in groups] == [["Master Agreement"], ["1. A", "x"]]
        assert [
            len(g) for g in group_blocks([heading("T", 0), para("p"), heading("1. A", 1)], 3)
        ] == [2, 1]

    def test_content_before_the_first_heading_forms_a_clause_without_a_ref(self) -> None:
        clauses = build_clauses(
            [para("Preamble text.")],
            heading_depth=1,
            parent_max_tokens=1500,
            taxonomy=get_taxonomy(),
        )
        assert len(clauses) == 1
        assert clauses[0].clause_ref is None
        assert clauses[0].heading is None

    def test_depth_is_the_deepest_level_with_a_substantial_median(self) -> None:
        big = para(long_text(12))  # ~ 250 tokens
        blocks = [
            heading("1. A", 1, "1"), big, heading("1.1 B", 2, "1.1"), big,
            heading("2. C", 1, "2"), big, heading("2.1 D", 2, "2.1"), big,
        ]  # fmt: skip
        assert choose_heading_depth(blocks) == 3
        tiny = [
            heading("1. A", 1),
            para("x"),
            heading("1.1 B", 2),
            para("y"),
            heading("2. C", 1),
            para("z"),
        ]
        assert choose_heading_depth(tiny) == 1

    def test_configured_depth_wins(self) -> None:
        assert choose_heading_depth([heading("A", 1), para("x")], configured=2) == 2

    def test_an_oversized_clause_is_split_at_block_boundaries_with_a_suffixed_ref(self) -> None:
        blocks = [heading("5. Big", 1, "5")] + [para(long_text(10)) for _ in range(6)]
        clauses = build_clauses(
            blocks, heading_depth=1, parent_max_tokens=500, taxonomy=get_taxonomy()
        )
        assert len(clauses) > 1
        assert clauses[0].clause_ref == "5"
        assert clauses[1].clause_ref == "5#2"
        assert [c.ordinal for c in clauses] == list(range(len(clauses)))
        assert all(c.heading == "5. Big" for c in clauses)

    def test_clause_types_come_from_the_heading_and_start_of_the_body(self) -> None:
        clauses = build_clauses(
            [heading("6. Limitation of Liability", 1, "6"), para("Total liability is capped.")],
            heading_depth=1,
            parent_max_tokens=1500,
            taxonomy=get_taxonomy(),
        )
        assert "limitation_of_liability" in clauses[0].clause_types

    def test_block_spans_locate_each_block_in_the_clause_text(self) -> None:
        clause = clause_of(para("first"), para("second"))
        assert clause.text == "first\n\nsecond"
        assert [(s, e, b.text) for s, e, b in clause.spans] == [(0, 5, "first"), (7, 13, "second")]

    def test_pages_are_the_min_and_max_of_the_blocks_and_none_without_pages(self) -> None:
        assert (
            clause_of(para("a", page=3), para("b", page=5)).page_start,
            clause_of(para("a", page=3), para("b", page=5)).page_end,
        ) == (3, 5)
        assert clause_of(para("a", page=None)).page_start is None


class TestChunker:
    def test_a_short_clause_is_a_single_chunk(self) -> None:
        chunks = chunk_clause(clause_of(para("Short text.")))
        assert len(chunks) == 1
        assert chunks[0].text == "Short text."

    def test_empty_clause_produces_no_chunks(self) -> None:
        assert chunk_clause(clause_of(para("   "))) == []

    def test_a_long_clause_is_split_near_the_target_and_below_the_max(self) -> None:
        chunks = chunk_clause(clause_of(para(long_text(80))))
        assert len(chunks) > 1
        assert all(c.token_count <= 500 for c in chunks)
        assert all(c.token_count >= 100 for c in chunks[:-1])

    def test_chunks_are_exact_substrings_of_the_clause_text(self) -> None:
        clause = clause_of(para(long_text(60)), para(long_text(30)))
        for chunk in chunk_clause(clause):
            assert clause.text[chunk.char_start : chunk.char_end] == chunk.text

    def test_consecutive_chunks_overlap_by_a_sentence(self) -> None:
        chunks = chunk_clause(clause_of(para(long_text(80))))
        assert chunks[1].char_start < chunks[0].char_end
        assert chunks[1].char_start > chunks[0].char_start

    def test_no_overlap_when_the_last_sentence_is_too_long_to_repeat(self) -> None:
        cfg = ChunkSettings(target_tokens=100, max_tokens=200, overlap_tokens=5)
        chunks = chunk_clause(clause_of(para(long_text(30))), cfg)
        assert all(b.char_start >= a.char_end for a, b in pairwise(chunks))

    def test_a_single_run_on_sentence_is_split_below_the_maximum(self) -> None:
        run_on = "the Supplier shall do all of the things, " * 120
        chunks = chunk_clause(clause_of(para("The Supplier " + run_on + "and then stop.")))
        assert len(chunks) > 1
        assert all(c.token_count <= 500 for c in chunks)

    def test_a_small_table_is_one_chunk_and_flagged(self) -> None:
        grid = "| Fee | Amount |\n| --- | --- |\n| Setup | 100 |\n| Run | 200 |"
        chunks = chunk_clause(clause_of(para(long_text(6)), table(grid)))
        table_chunks = [c for c in chunks if c.has_table]
        assert len(table_chunks) == 1
        assert table_chunks[0].text == grid

    def test_a_large_table_is_split_into_row_groups_repeating_the_header(self) -> None:
        header = "| Item | Description |\n| --- | --- |\n"
        rows = "".join(f"| {i} | {'word ' * 30}|\n" for i in range(60))
        chunks = chunk_clause(clause_of(table(header + rows)))
        assert len(chunks) > 1
        assert all(c.text.startswith("| Item | Description |") for c in chunks)
        assert chunks[0].header_repeated is False
        assert all(c.header_repeated for c in chunks[1:])
        assert all(c.token_count <= 500 for c in chunks)
        assert all(c.has_table for c in chunks)

    def test_a_table_is_never_merged_with_the_surrounding_text(self) -> None:
        grid = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        chunks = chunk_clause(clause_of(para(long_text(10)), table(grid), para(long_text(10))))
        assert sum(1 for c in chunks if c.has_table) == 1
        assert all("| A | B |" not in c.text for c in chunks if not c.has_table)

    def test_pages_and_injection_metadata_come_from_the_overlapping_blocks(self) -> None:
        clean = para(long_text(30), page=2)
        flagged = para(
            long_text(30), page=3, injection_score=0.9, injection_flags=("ignore_previous",)
        )
        chunks = chunk_clause(clause_of(clean, flagged))
        assert chunks[0].page_start == 2
        assert chunks[0].injection_score == 0.0
        assert chunks[-1].page_end == 3
        assert chunks[-1].injection_score == 0.9
        assert chunks[-1].injection_flags == ("ignore_previous",)

    def test_chunks_without_pages_have_none(self) -> None:
        chunks = chunk_clause(clause_of(para(long_text(30), page=None)))
        assert all(c.page_start is None and c.page_end is None for c in chunks)


class TestChunkDocument:
    def blocks(self) -> list[ExtractedBlock]:
        return [
            heading("Master Services Agreement", 0, None),
            heading("1. Services", 1, "1"),
            para(long_text(8), path=("1. Services",)),
            heading("2. Limitation of Liability", 1, "2"),
            para(long_text(40), path=("2. Limitation of Liability",)),
        ]

    def test_ordinals_are_document_wide_and_headers_are_set(self) -> None:
        result = chunk_document(self.blocks(), title="MSA", taxonomy=get_taxonomy())
        assert [c.ordinal for c in result.chunks] == list(range(len(result.chunks)))
        assert all(c.header.startswith("MSA") for c in result.chunks)
        assert any("clause 2" in c.header for c in result.chunks)

    def test_every_chunk_points_at_an_existing_clause(self) -> None:
        result = chunk_document(self.blocks(), title="MSA", taxonomy=get_taxonomy())
        ordinals = {c.ordinal for c in result.clauses}
        assert {c.clause_ordinal for c in result.chunks} <= ordinals

    def test_context_header_omits_missing_parts(self) -> None:
        bare = ClauseDraft(
            ordinal=0, clause_ref=None, heading=None, heading_path=(), blocks=[para("x")]
        )
        assert context_header("MSA", bare) == "MSA"
        full = ClauseDraft(
            ordinal=0,
            clause_ref="4.1",
            heading="Fees",
            heading_path=("4. Terms", "4.1 Fees"),
            blocks=[para("x")],
        )
        assert context_header("MSA", full) == "MSA | 4. Terms > 4.1 Fees | clause 4.1"

    def test_the_stored_text_does_not_contain_the_header(self) -> None:
        result = chunk_document(self.blocks(), title="MSA", taxonomy=get_taxonomy())
        assert all(not c.text.startswith("MSA |") for c in result.chunks)

    def test_a_document_without_headings_still_chunks(self) -> None:
        result = chunk_document([para(long_text(40))], title="Plain", taxonomy=get_taxonomy())
        assert len(result.clauses) == 1
        assert len(result.chunks) > 1


@settings(max_examples=60, deadline=None)
@given(
    sentence_count=st.integers(min_value=1, max_value=120),
    target=st.integers(min_value=40, max_value=400),
    block_count=st.integers(min_value=1, max_value=4),
)
def test_chunks_cover_the_clause_and_respect_the_limit(
    sentence_count: int, target: int, block_count: int
) -> None:
    cfg = ChunkSettings(target_tokens=target, max_tokens=target + 100, overlap_tokens=30)
    blocks = [para(long_text(sentence_count)) for _ in range(block_count)]
    clause = clause_of(*blocks)
    chunks = chunk_clause(clause, cfg)
    assert chunks
    assert all(clause.text[c.char_start : c.char_end] == c.text for c in chunks)
    assert all(estimate_tokens(c.text) <= cfg.max_tokens for c in chunks)
    covered = {i for c in chunks for i in range(c.char_start, c.char_end)}
    assert {i for i, ch in enumerate(clause.text) if not ch.isspace()} <= covered
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts)
