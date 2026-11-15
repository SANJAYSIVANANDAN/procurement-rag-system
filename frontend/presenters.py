"""Turning API data into what the pages show. Pure functions, no Streamlit, no I/O (spec 27.4).

Colours and labels live here and nowhere else. Scores, statuses and what can be compared always
come from the API; this module only arranges and words them.
"""

from __future__ import annotations

import csv
import html
import io
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

JSON = dict[str, Any]

# ---- severity and status --------------------------------------------------------------------

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
SEVERITY_COLOR = {
    "critical": "#C62828",
    "high": "#EF6C00",
    "medium": "#F9A825",
    "low": "#1E88E5",
    "info": "#78909C",
}
CLAUSE_STATUS_LABEL = {
    "absent": "Absent",
    "problematic": "Problematic",
    "ambiguous": "Ambiguous",
    "insufficient_evidence": "Insufficient evidence",
    "acceptable": "Acceptable",
}
CLAUSE_STATUS_COLOR = {
    "absent": "#8E24AA",
    "problematic": "#C62828",
    "ambiguous": "#F9A825",
    "insufficient_evidence": "#78909C",
    "acceptable": "#2E7D32",
}
GROUNDING_LABEL = {
    "grounded": ("Grounded", "#2E7D32"),
    "partially_grounded": ("Partially grounded", "#F9A825"),
    "abstained": ("Not enough evidence", "#78909C"),
    "ungrounded": ("Not supported by the documents", "#C62828"),
}
DOCUMENT_STATUS_COLOR = {
    "ready": "#2E7D32",
    "failed": "#C62828",
    "deleted": "#78909C",
}
IN_PROGRESS_DOCUMENT = {"uploaded", "queued", "extracting", "chunking", "embedding"}
JOB_STAGE_LABEL = {
    "queued": "Waiting in the queue",
    "validating": "Checking the file",
    "extracting": "Reading the document",
    "chunking": "Splitting into clauses",
    "embedding": "Indexing",
    "finalizing": "Finishing",
}
CHAT_STAGE_LABEL = {
    "understanding": "Understanding the question",
    "loading_context": "Loading the conversation",
    "classifying": "Understanding the question",
    "retrieving": "Searching the documents",
    "reranking": "Ranking the passages",
    "checking_evidence": "Checking the evidence",
    "generating": "Writing the answer",
    "validating": "Verifying the citations",
    "saving": "Saving",
}
DIMENSION_LABEL = {
    "legal": "Legal",
    "data_security": "Data security",
    "operational": "Operational",
    "financial_commercial": "Financial and commercial",
    "compliance": "Compliance",
    "dependency": "Dependency",
}
DISCLAIMER = "Automated analysis for decision support. Not legal advice."


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def label(value: str | None) -> str:
    return (value or "").replace("_", " ").capitalize()


def stage_label(stage: str | None, status: str | None = None) -> str:
    """A background job's stage in plain words."""
    if stage and stage in JOB_STAGE_LABEL:
        return JOB_STAGE_LABEL[stage]
    return label(stage or status) or "Working"


def chat_stage_label(stage: str | None) -> str:
    """A stage reported while a chat reply is written."""
    return CHAT_STAGE_LABEL.get(stage or "", label(stage) or "Working")


def badge_html(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:#fff;border-radius:10px;padding:1px 9px;'
        f'font-size:0.78rem;font-weight:600;white-space:nowrap">{html.escape(text)}</span>'
    )


def severity_badge(severity: str) -> str:
    return badge_html(severity.upper(), SEVERITY_COLOR.get(severity, SEVERITY_COLOR["info"]))


def status_badge(status: str) -> str:
    return badge_html(
        CLAUSE_STATUS_LABEL.get(status, label(status)),
        CLAUSE_STATUS_COLOR.get(status, SEVERITY_COLOR["info"]),
    )


def grounding_badge(grounding: str | None) -> str | None:
    if grounding not in GROUNDING_LABEL:
        return None
    text, color = GROUNDING_LABEL[grounding]
    return badge_html(text, color)


def page_range(item: JSON) -> str:
    start, end = item.get("page_start"), item.get("page_end")
    if start is None:
        return ""
    return f"p. {start}" if end in (None, start) else f"pp. {start}-{end}"


# ---- chat -----------------------------------------------------------------------------------

_MARKER = re.compile(r"\[(C\d+(?:\s*,\s*C\d+)*)\]")


def markers_to_html(text: str, citations: Sequence[JSON] = ()) -> str:
    """Escape the text and turn `[C1]` / `[C1, C3]` into small superscript badges.

    Each number links to the citation entry `cit-<n>` when that citation exists. The text is
    escaped first, so nothing a document or a model wrote can become markup.
    """
    known = {c.get("marker") for c in citations}

    def one(number: int) -> str:
        if number in known:
            return f'<a href="#cit-{number}" style="text-decoration:none">{number}</a>'
        return str(number)

    def badge(match: re.Match[str]) -> str:
        numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
        inner = ", ".join(one(n) for n in numbers)
        return (
            '<sup style="background:#E3EAFD;color:#2F5BEA;border-radius:6px;padding:0 4px;'
            f'font-weight:600">{inner}</sup>'
        )

    return _MARKER.sub(badge, html.escape(text, quote=False))


def citation_title(citation: JSON) -> str:
    parts = [citation.get("document_title", "Document")]
    if citation.get("clause_ref"):
        parts.append(f"clause {citation['clause_ref']}")
    pages = page_range(citation)
    if pages:
        parts.append(pages)
    return " · ".join(parts)


def why_this_answer(
    stages: Sequence[str], citations: Sequence[JSON], grounding: str | None, usage: JSON
) -> list[str]:
    """A plain-language account of how a reply was made, from what the stream reported."""
    lines = []
    if stages:
        lines.append("Steps: " + ", then ".join(chat_stage_label(s).lower() for s in stages) + ".")
    if citations:
        documents = sorted({c.get("document_title", "") for c in citations})
        lines.append(
            f"The answer cites {len(citations)} passage{'s' if len(citations) != 1 else ''} "
            f"from {', '.join(documents)}."
        )
    text = {
        "grounded": "Every claim was matched to a cited passage.",
        "partially_grounded": (
            "Some claims could not be matched to a cited passage; the answer says which."
        ),
        "abstained": (
            "The documents did not contain enough to answer reliably, so no answer was written."
        ),
        "ungrounded": "The claims could not be matched to the documents.",
    }.get(grounding or "")
    if text:
        lines.append(text)
    if usage.get("output_tokens"):
        lines.append(f"Model output: {usage['output_tokens']} tokens.")
    return lines


def message_scope(conversation: JSON, documents: Sequence[JSON]) -> str:
    titles = {d["id"]: d["title"] for d in documents}
    names = [titles.get(i, "a document") for i in conversation.get("document_ids", [])]
    return ", ".join(names) if names else "all your documents"


# ---- findings -------------------------------------------------------------------------------

STATUS_FILTERS = ["absent", "problematic", "ambiguous", "insufficient_evidence", "acceptable"]


def split_findings(findings: Iterable[JSON]) -> tuple[list[JSON], list[JSON]]:
    """(rated findings, obligations): obligations are listed, not rated."""
    rated: list[JSON] = []
    obligations: list[JSON] = []
    for finding in findings:
        (obligations if finding.get("details", {}).get("kind") == "obligation" else rated).append(
            finding
        )
    return rated, obligations


def filter_findings(
    findings: Iterable[JSON], *, statuses: Sequence[str] = (), severities: Sequence[str] = ()
) -> list[JSON]:
    return [
        f
        for f in findings
        if (not statuses or f["clause_status"] in statuses)
        and (not severities or f["severity"] in severities)
    ]


def group_findings(findings: Iterable[JSON]) -> list[tuple[str, list[tuple[str, list[JSON]]]]]:
    """Severity (worst first) > category > findings, each level in a stable order."""
    by_severity: dict[str, dict[str, list[JSON]]] = {}
    for finding in sorted(findings, key=lambda f: (f["rule_id"], f["id"])):
        by_severity.setdefault(finding["severity"], {}).setdefault(
            finding["risk_category"], []
        ).append(finding)
    return [
        (severity, sorted(categories.items()))
        for severity, categories in sorted(
            by_severity.items(), key=lambda item: severity_rank(item[0])
        )
    ]


def severity_counts(findings: Iterable[JSON]) -> dict[str, int]:
    counts = Counter(f["severity"] for f in findings)
    return {s: counts.get(s, 0) for s in SEVERITY_ORDER}


def findings_csv(findings: Iterable[JSON]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "rule",
            "category",
            "status",
            "severity",
            "title",
            "explanation",
            "impact",
            "action",
            "clause",
            "page",
            "confidence",
            "evidence",
        ]
    )
    for f in findings:
        evidence = " | ".join(e.get("quote", "") for e in f.get("evidence", []))
        writer.writerow(
            [
                f["rule_id"],
                f["risk_category"],
                f["clause_status"],
                f["severity"],
                f["title"],
                f["explanation"],
                f.get("business_impact") or "",
                f.get("recommended_action") or "",
                f.get("clause_ref") or "",
                f.get("page_number") or "",
                f"{f['confidence']:.2f}",
                evidence,
            ]
        )
    return out.getvalue()


def findings_markdown(document_title: str, report: JSON) -> str:
    lines = [f"# Contract risk analysis: {document_title}", ""]
    if report.get("analyzed_at"):
        lines += [
            f"Analysed {report['analyzed_at']} (ruleset {report.get('ruleset_version')}).",
            "",
        ]
    rated, obligations = split_findings(report.get("findings", []))
    for severity, categories in group_findings(rated):
        lines += [f"## {severity.capitalize()}", ""]
        for category, items in categories:
            lines += [f"### {label(category)}", ""]
            for f in items:
                lines.append(
                    f"- **{f['title']}** "
                    f"({CLAUSE_STATUS_LABEL.get(f['clause_status'], f['clause_status'])})"
                )
                lines.append(f"  - {f['explanation']}")
                if f.get("business_impact"):
                    lines.append(f"  - Impact: {f['business_impact']}")
                if f.get("recommended_action"):
                    lines.append(f"  - Recommended: {f['recommended_action']}")
                for e in f.get("evidence", []):
                    lines.append(f'  - Evidence ({citation_title(e)}): "{e.get("quote", "")}"')
            lines.append("")
    if obligations:
        lines += ["## Obligations and deadlines", ""]
        for row in obligation_rows(obligations):
            lines.append(
                f"- {row['Party']}: {row['Obligation']} ({row['Deadline']}) [{row['Clause']}]"
            )
        lines.append("")
    lines += ["---", report.get("disclaimer") or DISCLAIMER, ""]
    return "\n".join(lines)


def obligation_rows(obligations: Iterable[JSON]) -> list[JSON]:
    rows = []
    for f in obligations:
        d = f.get("details", {})
        rows.append(
            {
                "Party": label(d.get("party")),
                "Obligation": d.get("action") or f["title"],
                "Deadline": d.get("deadline_text") or "none stated",
                "Trigger": d.get("trigger") or "",
                "Clause": f.get("clause_ref") or "",
                "Page": f.get("page_number"),
            }
        )
    return rows


# ---- documents, dashboard -------------------------------------------------------------------


def document_counts(documents: Iterable[JSON]) -> dict[str, int]:
    counts = {"ready": 0, "processing": 0, "failed": 0}
    for d in documents:
        status = d["status"]
        if status == "ready":
            counts["ready"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status in IN_PROGRESS_DOCUMENT:
            counts["processing"] += 1
    return counts


def quality_warnings(detail: JSON) -> list[str]:
    """Plain wording for the extraction-quality flags of a document."""
    quality = detail.get("extraction_quality") or {}
    metadata = detail.get("metadata") or {}
    warnings = []
    if quality.get("insufficient_text"):
        warnings.append(
            "Little readable text was found: this may be a scan, and answers may be poor."
        )
    pages = quality.get("ocr_needed_pages") or []
    if pages:
        warnings.append(f"{len(pages)} page(s) look like images and could not be read.")
    if metadata.get("injection_warning") or quality.get("injection_flagged_blocks"):
        warnings.append("The document contains text that looks like instructions to an AI system.")
    if detail.get("error_code"):
        warnings.append(f"Processing failed ({detail['error_code']}).")
    return warnings


def vendor_names(vendors: Iterable[JSON]) -> dict[str, str]:
    return {v["id"]: v["name"] for v in vendors}


# ---- vendors and comparison -----------------------------------------------------------------


def score_text(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.0f}"


def score_bar_html(score: float | None, severity: str) -> str:
    """A bar for a 0-100 risk score (higher means more risk); empty when there is no score."""
    if score is None:
        return '<div style="color:#78909C">Not scored</div>'
    width = max(0.0, min(100.0, score))
    color = SEVERITY_COLOR.get(severity, SEVERITY_COLOR["info"])
    return (
        '<div style="background:#ECEFF1;border-radius:6px;height:12px;width:100%">'
        f'<div style="background:{color};width:{width:.0f}%;height:12px;'
        'border-radius:6px"></div></div>'
    )


def comparison_matrix(comparison: JSON) -> tuple[list[JSON], list[str]]:
    """(rows, notes) for the dimension matrix of a finished comparison.

    A row is `{Dimension, <vendor name>: text ...}` plus `_comparable`, `_reason`, `_leader`, and
    `_cells` (vendor name to score or None, for charts). Cells of a dimension that cannot be
    compared say so; nothing is ranked or charted for them.
    """
    result = comparison.get("result") or {}
    names = {v["id"]: v["name"] for v in result.get("vendors", [])}
    rows: list[JSON] = []
    for dim in result.get("dimensions", []):
        row: JSON = {"Dimension": DIMENSION_LABEL.get(dim["dimension"], label(dim["dimension"]))}
        scores: dict[str, float | None] = {}
        for vendor_id, name in names.items():
            cell = dim["cells"].get(vendor_id)
            if not dim["comparable"]:
                row[name] = "Not comparable"
            elif cell is None or cell.get("score") is None:
                row[name] = "n/a"
            else:
                row[name] = f"{cell['score']:.0f} ({cell['severity']})"
            scores[name] = None if cell is None else cell.get("score")
        row["_comparable"] = dim["comparable"]
        row["_reason"] = dim.get("reason", "")
        row["_leader"] = names.get(dim.get("leader") or "", "")
        row["_cells"] = scores
        rows.append(row)
    notes = []
    if result.get("ranking_suppressed_reason"):
        notes.append(f"No overall ranking: {result['ranking_suppressed_reason']}")
    return rows, notes


def comparison_chart_data(rows: Iterable[JSON]) -> list[JSON]:
    """Long-format data for a chart: comparable dimensions only, one point per vendor."""
    data = []
    for row in rows:
        if not row["_comparable"]:
            continue
        for vendor, score in row["_cells"].items():
            if score is not None:
                data.append({"Dimension": row["Dimension"], "Vendor": vendor, "Risk score": score})
    return data


def signal_rows(comparison: JSON) -> list[JSON]:
    result = comparison.get("result") or {}
    names = {v["id"]: v["name"] for v in result.get("vendors", [])}
    rows = []
    for signal in result.get("signals", []):
        row: JSON = {"Signal": signal.get("label") or label(signal.get("signal_id"))}
        cells = signal.get("cells", {})
        for vendor_id, name in names.items():
            cell = cells.get(vendor_id)
            if not signal.get("comparable", True):
                row[name] = "Not comparable"
            elif isinstance(cell, dict):
                row[name] = cell.get("label") or label(cell.get("status"))
            else:
                row[name] = "n/a"
        row["Note"] = signal.get("reason", "")
        rows.append(row)
    return rows


def ranking_names(comparison: JSON) -> list[str]:
    result = comparison.get("result") or {}
    names = {v["id"]: v["name"] for v in result.get("vendors", [])}
    return [names.get(i, i) for i in result.get("ranking", [])]


def vendor_overview(vendors: Sequence[JSON], latest: dict[str, JSON | None]) -> list[JSON]:
    rows = []
    for vendor in vendors:
        assessment = latest.get(vendor["id"])
        rows.append(
            {
                "Vendor": vendor["name"],
                "Overall risk": score_text(assessment["overall_score"])
                if assessment
                else "not assessed",
                "Severity": assessment["overall_severity"] if assessment else "",
                "Provisional": bool(assessment and assessment["provisional"]),
            }
        )
    return rows


def pending_dependencies_text(job: JSON) -> str:
    pending = job.get("pending_dependencies") or []
    if not pending:
        return ""
    count = len(pending)
    return f"Waiting for {count} contract analys{'es' if count != 1 else 'is'} to finish first."
