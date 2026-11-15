"""Page 2: what needs attention, at a glance. Read from the API, only totalled here."""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from frontend import presenters, state
from frontend.api_client import ApiClient, ApiError
from frontend.components import common

JSON = dict[str, Any]
FINDING_DOCUMENT_LIMIT = 30  # documents whose findings are totalled on the dashboard
VENDOR_LIMIT = 20


@st.cache_data(ttl=30, show_spinner=False)
def _overview(token: str, _api: ApiClient) -> JSON:
    documents = _api.all_documents(max_items=300)
    vendors = _api.all_vendors(max_items=100)
    findings: list[JSON] = []
    ready = [d for d in documents if d["status"] == "ready"][:FINDING_DOCUMENT_LIMIT]
    for document in ready:
        try:
            report = _api.findings(document["id"])
        except ApiError as error:
            if error.status == 404:
                continue  # never analysed
            raise
        findings.extend(presenters.split_findings(report["findings"])[0])
    latest = {v["id"]: _api.latest_assessment(v["id"]) for v in vendors[:VENDOR_LIMIT]}
    return {
        "documents": documents,
        "vendors": vendors,
        "findings": findings,
        "latest": latest,
        "analysed": len(ready),
    }


def render() -> None:
    common.page_header("Dashboard", "Your documents, findings and vendors.")
    data = state.call(lambda: _overview(st.session_state["token"], state.client()))
    if data is None:
        return
    documents, vendors, findings = data["documents"], data["vendors"], data["findings"]
    if not documents and not vendors:
        common.empty_state(
            "Nothing here yet.",
            "Upload a contract to get started, then ask questions or run an analysis.",
        )

    counts = presenters.document_counts(documents)
    urgent = sum(1 for f in findings if f["severity"] in ("critical", "high"))
    cols = st.columns(5)
    cols[0].metric("Documents ready", counts["ready"])
    cols[1].metric("Processing", counts["processing"])
    cols[2].metric("Failed", counts["failed"])
    cols[3].metric("Vendors", len(vendors))
    cols[4].metric(
        "High or critical findings",
        urgent,
        help=f"Across the latest analysis of up to {FINDING_DOCUMENT_LIMIT} ready documents.",
    )

    left, right = st.columns(2)
    with left:
        st.subheader("Findings by severity")
        counts_by_severity = presenters.severity_counts(findings)
        if not any(counts_by_severity.values()):
            common.empty_state("No analysed contracts yet.", "Run a risk analysis from a document.")
        else:
            chart_data = [{"Severity": s, "Findings": n} for s, n in counts_by_severity.items()]
            chart = (
                alt.Chart(pd.DataFrame(chart_data))
                .mark_bar()
                .encode(
                    x=alt.X("Severity:N", sort=presenters.SEVERITY_ORDER),
                    y=alt.Y("Findings:Q"),
                    color=alt.Color(
                        "Severity:N",
                        scale=alt.Scale(
                            domain=presenters.SEVERITY_ORDER,
                            range=[presenters.SEVERITY_COLOR[s] for s in presenters.SEVERITY_ORDER],
                        ),
                        legend=None,
                    ),
                )
                .properties(height=240)
            )
            st.altair_chart(chart, width="stretch")
    with right:
        st.subheader("Vendor risk overview")
        rows = presenters.vendor_overview(vendors[:VENDOR_LIMIT], data["latest"])
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
            if any(r["Provisional"] for r in rows):
                st.caption(
                    "Provisional: assessed with incomplete evidence. Treat the score with care."
                )
        else:
            common.empty_state("No vendors yet.", "Create a vendor and link contracts to it.")

    st.subheader("Recent activity")
    recent = sorted(documents, key=lambda d: d["updated_at"], reverse=True)[:10]
    if recent:
        st.dataframe(
            [
                {
                    "Document": d["title"],
                    "Status": presenters.label(d["status"]),
                    "Type": presenters.label(d["contract_type"]),
                    "Updated": d["updated_at"][:16].replace("T", " "),
                }
                for d in recent
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No documents uploaded yet.")
