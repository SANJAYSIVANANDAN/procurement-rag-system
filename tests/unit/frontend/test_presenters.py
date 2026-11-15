"""Presentation logic: markers, grouping, exports, the comparison matrix. No Streamlit."""

import csv
import io
from typing import ClassVar

from frontend import presenters as p


def finding(
    rule: str, severity: str = "high", status: str = "problematic", **extra: object
) -> dict:
    return {
        "id": f"id-{rule}",
        "rule_id": rule,
        "risk_category": extra.pop("category", "liability"),
        "clause_status": status,
        "severity": severity,
        "title": f"Title {rule}",
        "explanation": "Because.",
        "business_impact": None,
        "recommended_action": None,
        "clause_ref": "12.2",
        "page_number": 14,
        "confidence": 0.8,
        "evidence": [
            {
                "quote": 'He said "no"',
                "document_title": "MSA",
                "page_start": 14,
                "page_end": 14,
                "clause_ref": "12.2",
            }
        ],
        "details": extra.pop("details", {}),
        **extra,
    }


class TestMarkers:
    CITATIONS: ClassVar[list[dict]] = [{"marker": 1}, {"marker": 3}]

    def test_markers_become_superscript_badges_linked_to_their_citation(self) -> None:
        out = p.markers_to_html("Capped at fees [C1], carve-outs apply [C1, C3].", self.CITATIONS)
        assert out.count("<sup") == 2
        assert 'href="#cit-1"' in out and 'href="#cit-3"' in out
        assert "[C1" not in out

    def test_a_marker_without_a_citation_is_a_plain_number(self) -> None:
        out = p.markers_to_html("Claim [C2].", self.CITATIONS)
        assert "<sup" in out and "#cit-2" not in out

    def test_nothing_in_the_text_can_become_markup(self) -> None:
        out = p.markers_to_html("<script>alert(1)</script> & <b>bold</b> [C1]", self.CITATIONS)
        assert "<script>" not in out and "<b>" not in out
        assert "&lt;script&gt;" in out and "&amp;" in out

    def test_text_without_markers_is_unchanged_apart_from_escaping(self) -> None:
        assert p.markers_to_html("Plain **markdown** text.") == "Plain **markdown** text."


class TestFindings:
    FINDINGS: ClassVar[list[dict]] = [
        finding("LIAB-001", "high", category="liability"),
        finding("PAY-001", "medium", "ambiguous", category="payment"),
        finding("LIAB-002", "critical", "absent", category="liability"),
        finding(
            "OBL-001",
            "info",
            "acceptable",
            category="obligations",
            details={
                "kind": "obligation",
                "party": "vendor",
                "action": "send notice",
                "deadline_text": "within 30 days",
            },
        ),
    ]

    def test_obligations_are_listed_apart_from_rated_findings(self) -> None:
        rated, obligations = p.split_findings(self.FINDINGS)
        assert [f["rule_id"] for f in rated] == ["LIAB-001", "PAY-001", "LIAB-002"]
        assert p.obligation_rows(obligations) == [
            {
                "Party": "Vendor",
                "Obligation": "send notice",
                "Deadline": "within 30 days",
                "Trigger": "",
                "Clause": "12.2",
                "Page": 14,
            }
        ]

    def test_grouping_puts_the_worst_severity_first_then_categories_alphabetically(self) -> None:
        rated, _ = p.split_findings(self.FINDINGS)
        grouped = p.group_findings(rated)
        assert [s for s, _ in grouped] == ["critical", "high", "medium"]
        assert grouped[0][1][0][0] == "liability"

    def test_filters_combine(self) -> None:
        rated, _ = p.split_findings(self.FINDINGS)
        assert [f["rule_id"] for f in p.filter_findings(rated, statuses=["absent"])] == ["LIAB-002"]
        assert [
            f["rule_id"]
            for f in p.filter_findings(rated, severities=["high", "medium"], statuses=["ambiguous"])
        ] == ["PAY-001"]
        assert len(p.filter_findings(rated)) == 3

    def test_severity_counts_include_empty_levels(self) -> None:
        rated, _ = p.split_findings(self.FINDINGS)
        assert p.severity_counts(rated) == {
            "critical": 1,
            "high": 1,
            "medium": 1,
            "low": 0,
            "info": 0,
        }

    def test_csv_export_quotes_what_needs_quoting(self) -> None:
        rated, _ = p.split_findings(self.FINDINGS)
        rows = list(csv.DictReader(io.StringIO(p.findings_csv(rated))))
        assert len(rows) == 3 and rows[0]["rule"] == "LIAB-001"
        assert rows[0]["evidence"] == 'He said "no"'

    def test_markdown_export_has_the_findings_obligations_and_disclaimer(self) -> None:
        report = {
            "findings": self.FINDINGS,
            "analyzed_at": "2026-11-15T10:00:00Z",
            "ruleset_version": "v1",
            "disclaimer": "Not advice.",
        }
        text = p.findings_markdown("Acme MSA", report)
        assert text.startswith("# Contract risk analysis: Acme MSA")
        assert "## Critical" in text and "### Liability" in text and "Title LIAB-002" in text
        assert "## Obligations and deadlines" in text and "send notice" in text
        assert text.rstrip().endswith("Not advice.")


class TestComparison:
    COMPARISON: ClassVar[dict] = {
        "result": {
            "vendors": [{"id": "a", "name": "Acme"}, {"id": "g", "name": "Globex"}],
            "ranking": ["a", "g"],
            "ranking_suppressed_reason": None,
            "dimensions": [
                {
                    "dimension": "legal",
                    "comparable": True,
                    "leader": "a",
                    "cells": {
                        "a": {"score": 20.0, "severity": "low"},
                        "g": {"score": 55.0, "severity": "medium"},
                    },
                },
                {
                    "dimension": "data_security",
                    "comparable": False,
                    "reason": "Globex: coverage 0.30",
                    "leader": None,
                    "cells": {
                        "a": {"score": 30.0, "severity": "low"},
                        "g": {"score": 70.0, "severity": "high"},
                    },
                },
            ],
            "signals": [
                {
                    "signal_id": "OPS-SLA",
                    "label": "Uptime SLA",
                    "comparable": False,
                    "reason": "Missing in Globex — cannot compare",
                    "cells": {
                        "a": {"status": "favorable"},
                        "g": {"status": "insufficient_evidence"},
                    },
                },
                {
                    "signal_id": "FIN-RENEW",
                    "label": "Auto-renewal",
                    "comparable": True,
                    "cells": {
                        "a": {"status": "favorable"},
                        "g": {"status": "absent", "label": "Absent (policy)"},
                    },
                },
            ],
        }
    }

    def test_a_dimension_that_cannot_be_compared_says_so_in_every_cell(self) -> None:
        rows, _ = p.comparison_matrix(self.COMPARISON)
        legal, security = rows
        assert (
            legal["Acme"] == "20 (low)"
            and legal["Globex"] == "55 (medium)"
            and legal["_leader"] == "Acme"
        )
        assert security["Acme"] == security["Globex"] == "Not comparable"
        assert security["_reason"] == "Globex: coverage 0.30"

    def test_only_comparable_dimensions_are_charted(self) -> None:
        rows, _ = p.comparison_matrix(self.COMPARISON)
        data = p.comparison_chart_data(rows)
        assert {d["Dimension"] for d in data} == {"Legal"}
        assert sorted((d["Vendor"], d["Risk score"]) for d in data) == [
            ("Acme", 20.0),
            ("Globex", 55.0),
        ]

    def test_signals_show_not_comparable_and_policy_labels(self) -> None:
        rows = p.signal_rows(self.COMPARISON)
        assert (
            rows[0]["Acme"] == rows[0]["Globex"] == "Not comparable"
            and "Missing in Globex" in rows[0]["Note"]
        )
        assert rows[1]["Globex"] == "Absent (policy)"

    def test_ranking_names_and_a_suppressed_ranking_note(self) -> None:
        assert p.ranking_names(self.COMPARISON) == ["Acme", "Globex"]
        suppressed = {
            "result": {
                **self.COMPARISON["result"],
                "ranking": [],
                "ranking_suppressed_reason": "one vendor is provisional",
            }
        }
        assert p.comparison_matrix(suppressed)[1] == [
            "No overall ranking: one vendor is provisional"
        ]


class TestSmallThings:
    def test_document_counts(self) -> None:
        docs = [
            {"status": s} for s in ["ready", "ready", "failed", "queued", "embedding", "deleted"]
        ]
        assert p.document_counts(docs) == {"ready": 2, "processing": 2, "failed": 1}

    def test_quality_warnings_use_the_fields_the_api_returns(self) -> None:
        detail = {
            "extraction_quality": {"insufficient_text": True, "ocr_needed_pages": [3, 4]},
            "metadata": {"injection_warning": True},
            "error_code": None,
        }
        assert len(p.quality_warnings(detail)) == 3
        assert p.quality_warnings({"extraction_quality": {}, "metadata": {}}) == []

    def test_score_text_and_bar(self) -> None:
        assert p.score_text(None) == "n/a" and p.score_text(41.6) == "42"
        assert "Not scored" in p.score_bar_html(None, "info")
        assert "width:100%" in p.score_bar_html(250, "high")  # clamped

    def test_why_this_answer_describes_steps_sources_and_grounding(self) -> None:
        lines = p.why_this_answer(
            ["retrieving", "generating"],
            [{"document_title": "MSA"}],
            "grounded",
            {"output_tokens": 40},
        )
        assert lines[0].startswith("Steps: searching the documents, then writing the answer")
        assert any("1 passage from MSA" in line for line in lines)
        assert any("matched to a cited passage" in line for line in lines)
        assert p.why_this_answer([], [], "abstained", {})[0].startswith(
            "The documents did not contain enough"
        )

    def test_citation_title_and_page_range(self) -> None:
        assert (
            p.citation_title(
                {"document_title": "MSA", "clause_ref": "6.2", "page_start": 4, "page_end": 5}
            )
            == "MSA · clause 6.2 · pp. 4-5"
        )
        assert (
            p.page_range({"page_start": 4, "page_end": 4}) == "p. 4"
            and p.page_range({"page_start": None}) == ""
        )

    def test_stage_labels_do_not_clash_between_jobs_and_chat(self) -> None:
        assert p.stage_label("validating") == "Checking the file"
        assert p.chat_stage_label("validating") == "Verifying the citations"

    def test_vendor_overview_marks_provisional_and_unassessed(self) -> None:
        vendors = [{"id": "a", "name": "Acme"}, {"id": "g", "name": "Globex"}]
        latest = {
            "a": {"overall_score": 33.0, "overall_severity": "low", "provisional": True},
            "g": None,
        }
        rows = p.vendor_overview(vendors, latest)
        assert rows[0] == {
            "Vendor": "Acme",
            "Overall risk": "33",
            "Severity": "low",
            "Provisional": True,
        }
        assert rows[1]["Overall risk"] == "not assessed"
