from pathlib import Path

import pytest
from pydantic import ValidationError

from covenantiq.ingestion.taxonomy import (
    DEFAULT_TAXONOMY_PATH,
    MATCH_WINDOW_CHARS,
    TaxonomyFile,
    get_taxonomy,
    load_taxonomy,
)

# The clause types the baseline contract rules (spec 20.2) look for.
RULE_CLAUSE_TYPES = {
    "limitation_of_liability",
    "exclusion_of_damages",
    "indemnification",
    "ip_indemnity",
    "termination_for_convenience",
    "termination_for_cause",
    "termination_assistance",
    "renewal",
    "payment_terms",
    "price_changes",
    "late_payment",
    "ip_ownership",
    "customer_data",
    "confidentiality",
    "data_protection",
    "breach_notification",
    "subprocessors",
    "security",
    "service_levels",
    "service_credits",
    "governing_law",
    "assignment",
    "insurance",
    "force_majeure",
    "audit_rights",
}


def test_the_shipped_taxonomy_loads_and_covers_every_rule_clause_type() -> None:
    taxonomy = get_taxonomy()
    assert DEFAULT_TAXONOMY_PATH.is_file()
    assert RULE_CLAUSE_TYPES <= set(taxonomy.types)
    assert taxonomy.version == 1


class TestClassify:
    @pytest.mark.parametrize(
        ("heading", "body", "expected"),
        [
            ("6. Limitation of Liability", "", {"limitation_of_liability"}),
            (
                "Liability",
                "The Supplier's total liability shall not exceed the fees.",
                {"limitation_of_liability"},
            ),
            (
                "8. Termination",
                "The Customer may not terminate for convenience.",
                {"termination_for_convenience"},
            ),
            ("2. Term and Renewal", "The Agreement renews automatically.", {"renewal"}),
            ("2. Initial Term", "The initial term is three years.", {"term"}),
            (
                "3. Fees and Payment",
                "Each invoice is payable within ninety days.",
                {"payment_terms"},
            ),
            (
                "10. General",
                "This Agreement is governed by the laws of England.",
                {"governing_law"},
            ),
            (
                "7. Data Protection",
                "The Supplier shall notify of a personal data breach.",
                {"data_protection", "breach_notification"},
            ),
            ("9. Confidentiality", "", {"confidentiality"}),
            ("13. Force Majeure", "", {"force_majeure"}),
        ],
    )
    def test_clause_types_are_found(self, heading: str, body: str, expected: set[str]) -> None:
        assert expected <= set(get_taxonomy().classify(heading, body))

    def test_matching_is_multi_label(self) -> None:
        found = get_taxonomy().classify(
            "Indemnity and Liability",
            "Each party shall indemnify the other. Its total liability shall not exceed the fees.",
        )
        assert {"indemnification", "limitation_of_liability"} <= set(found)

    def test_matching_ignores_case_and_extra_whitespace(self) -> None:
        assert "force_majeure" in get_taxonomy().classify("FORCE    MAJEURE", "")

    def test_phrases_match_whole_words_only(self) -> None:
        taxonomy = get_taxonomy()
        assert "service_levels" not in taxonomy.classify("Slate roofing", "")
        assert "service_levels" in taxonomy.classify("SLA", "")

    def test_only_the_start_of_the_body_is_considered(self) -> None:
        body = "x " * (MATCH_WINDOW_CHARS // 2 + 10) + "force majeure"
        assert "force_majeure" not in get_taxonomy().classify("Miscellaneous", body)
        assert "force_majeure" in get_taxonomy().classify("Miscellaneous", "force majeure " + body)

    def test_unrelated_text_matches_nothing(self) -> None:
        assert get_taxonomy().classify("Schedule 1", "Apple banana cherry.") == []

    def test_a_missing_heading_is_fine(self) -> None:
        assert "confidentiality" in get_taxonomy().classify(
            None, "Confidential Information is protected."
        )


class TestLoading:
    def write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "taxonomy.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_custom_file_loads(self, tmp_path: Path) -> None:
        taxonomy = load_taxonomy(
            self.write(tmp_path, "version: 2\ntypes:\n  demo:\n    - hello world\n")
        )
        assert taxonomy.version == 2
        assert taxonomy.classify("Hello   World", "") == ["demo"]

    def test_a_type_without_phrases_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="no phrases"):
            load_taxonomy(self.write(tmp_path, "version: 1\ntypes:\n  demo: []\n"))

    def test_an_invalid_regular_expression_fails_at_load_time(self, tmp_path: Path) -> None:
        with pytest.raises(Exception, match=r"unterminated|missing"):
            load_taxonomy(
                self.write(tmp_path, "version: 1\ntypes:\n  demo:\n    - 'broken (group'\n")
            )

    def test_unknown_top_level_keys_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            load_taxonomy(self.write(tmp_path, "version: 1\ntypes:\n  demo: [a]\nextra: 1\n"))

    def test_the_schema_model_validates_directly(self) -> None:
        with pytest.raises(ValidationError):
            TaxonomyFile.model_validate({"version": 1})
