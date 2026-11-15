"""Page 6: contract risk analysis."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import presenters, state
from frontend.components import common
from frontend.components.finding_card import render_finding

JSON = dict[str, Any]


def render() -> None:
    common.page_header(
        "Contract risk analysis", "Each rule is checked against the contract, with the evidence."
    )
    common.disclaimer()
    documents = state.call(lambda: state.client().all_documents(max_items=300, status="ready"))
    if documents is None:
        return
    if not documents:
        common.empty_state(
            "No documents are ready.", "Upload a contract and wait for it to be read."
        )
        return

    titles = {d["id"]: d["title"] for d in documents}
    ids = list(titles)
    wanted = st.session_state.get("analysis_document_id")
    document_id = st.selectbox(
        "Contract",
        ids,
        index=ids.index(wanted) if wanted in ids else 0,
        format_func=lambda i: titles[i],
        key="analysis_doc",
    )
    st.session_state["analysis_document_id"] = document_id

    c1, c2 = st.columns([1, 3])
    force = c2.checkbox("Re-run even if a recent analysis exists", key="analysis_force")
    if c1.button("Run analysis", type="primary", key="analysis_run"):
        started = state.call(lambda: state.client().analyze_document(document_id, force=force))
        if started:
            common.watch_job(started["job"]["id"], "Analysing")
            st.rerun()

    report = state.call(lambda: state.client().findings(document_id), show=False)
    if report is None:
        common.empty_state(
            "This contract has not been analysed yet.",
            "Choose Run analysis. It takes a few minutes.",
        )
        return
    show_report(titles[document_id], report)


def show_report(title: str, report: JSON) -> None:
    findings = report["findings"]
    rated, obligations = presenters.split_findings(findings)
    if report.get("analyzed_at"):
        when = report["analyzed_at"][:16].replace("T", " ")
        st.caption(f"Analysed {when} · ruleset {report.get('ruleset_version')}")
    for note in report.get("notes", []):
        st.warning(note)

    counts = presenters.severity_counts(rated)
    cols = st.columns(len(counts))
    for col, (severity, n) in zip(cols, counts.items(), strict=True):
        col.markdown(presenters.severity_badge(severity), unsafe_allow_html=True)
        col.metric(severity.capitalize(), n, label_visibility="collapsed")

    f1, f2 = st.columns(2)
    statuses = f1.multiselect(
        "Status",
        presenters.STATUS_FILTERS,
        format_func=lambda s: presenters.CLAUSE_STATUS_LABEL[s],
        key="analysis_status",
    )
    severities = f2.multiselect("Severity", presenters.SEVERITY_ORDER, key="analysis_severity")
    shown = presenters.filter_findings(rated, statuses=statuses, severities=severities)

    if not shown:
        common.empty_state("No findings match these filters.")
    for severity, categories in presenters.group_findings(shown):
        st.subheader(severity.capitalize())
        for category, items in categories:
            st.markdown(f"**{presenters.label(category)}**")
            for finding in items:
                render_finding(finding)

    if obligations:
        st.subheader("Obligations and deadlines")
        st.dataframe(presenters.obligation_rows(obligations), hide_index=True, width="stretch")

    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button(
        "Export CSV",
        presenters.findings_csv(rated),
        file_name="findings.csv",
        mime="text/csv",
        key="export_csv",
    )
    d2.download_button(
        "Export Markdown",
        presenters.findings_markdown(title, report),
        file_name="findings.md",
        mime="text/markdown",
        key="export_md",
    )
    common.disclaimer(report.get("disclaimer"))
