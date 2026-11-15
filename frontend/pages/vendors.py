"""Page 7: vendors and their risk assessments."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import presenters, state
from frontend.components import common
from frontend.components.dimension_card import render_dimension

JSON = dict[str, Any]


def render() -> None:
    common.page_header("Vendors", "Link contracts to a vendor, then assess its risk.")
    common.disclaimer()
    search = st.text_input("Search vendors", key="vendor_search")
    vendors = state.call(lambda: state.client().all_vendors(max_items=200, q=search or None))
    if vendors is None:
        return

    with (
        st.expander("Create a vendor", expanded=not vendors),
        st.form("vendor_create", clear_on_submit=True),
    ):
        name = st.text_input("Name")
        industry = st.text_input("Industry (optional)")
        website = st.text_input("Website (optional)")
        if st.form_submit_button("Create"):
            if not name.strip():
                st.error("A vendor needs a name.")
            elif state.call(
                lambda: state.client().create_vendor(
                    name.strip(), industry=industry, website=website
                )
            ):
                st.cache_data.clear()
                st.rerun()

    if not vendors:
        common.empty_state(
            "No vendors yet.",
            "Create one above, then set it on your contracts in the document library.",
        )
        return
    ids = [v["id"] for v in vendors]
    names = presenters.vendor_names(vendors)
    chosen = st.selectbox("Vendor", ids, format_func=lambda i: names[i], key="vendor_pick")
    show_vendor(chosen)


def show_vendor(vendor_id: str) -> None:
    detail = state.call(lambda: state.client().get_vendor(vendor_id))
    if detail is None:
        return
    st.subheader(detail["name"])
    if detail.get("industry") or detail.get("website"):
        st.caption(" · ".join(x for x in (detail.get("industry"), detail.get("website")) if x))

    with st.expander("Edit vendor"), st.form(f"vendor_edit_{vendor_id}"):
        name = st.text_input("Name", value=detail["name"])
        industry = st.text_input("Industry", value=detail.get("industry") or "")
        website = st.text_input("Website", value=detail.get("website") or "")
        saved = st.form_submit_button("Save")
    if saved:
        fields = {"name": name, "industry": industry or None, "website": website or None}
        if state.call(lambda: state.client().update_vendor(vendor_id, **fields)) is not None:
            st.cache_data.clear()
            st.rerun()

    st.markdown("**Contracts**")
    documents = detail.get("documents", [])
    if documents:
        st.dataframe(
            [
                {
                    "Title": d["title"],
                    "Status": presenters.label(d["status"]),
                    "Type": presenters.label(d["contract_type"]),
                }
                for d in documents
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        common.empty_state(
            "No contracts linked.", "Set this vendor on a document in the document library."
        )

    if st.button(
        "Assess vendor", type="primary", disabled=not documents, key=f"assess_{vendor_id}"
    ):
        assess(vendor_id)
    show_assessments(vendor_id)


def assess(vendor_id: str) -> None:
    started = state.call(lambda: state.client().assess_vendor(vendor_id))
    if not started:
        return
    waiting = presenters.pending_dependencies_text(started["job"])
    if waiting:
        st.info(waiting + " Contracts are analysed first, then the vendor.")
    common.watch_job(started["job"]["id"], "Assessing vendor")
    st.cache_data.clear()
    st.rerun()


def show_assessments(vendor_id: str) -> None:
    history = state.call(lambda: state.client().assessments(vendor_id), show=False) or []
    if not history:
        common.empty_state("Not assessed yet.", "Choose Assess vendor.")
        return
    labels = {a["id"]: a["assessed_at"][:16].replace("T", " ") for a in history}
    picked = st.selectbox(
        "Assessment",
        list(labels),
        format_func=lambda i: labels[i] + (" (latest)" if i == history[0]["id"] else ""),
        key=f"assessment_pick_{vendor_id}",
    )
    render_assessment(next(a for a in history if a["id"] == picked))
    if len(history) > 1:
        with st.expander("History"):
            st.dataframe(
                [
                    {
                        "Assessed": a["assessed_at"][:16].replace("T", " "),
                        "Overall risk": presenters.score_text(a["overall_score"]),
                        "Severity": a["overall_severity"],
                        "Coverage": f"{a['evidence_coverage']:.0%}",
                        "Provisional": a["provisional"],
                    }
                    for a in history
                ],
                hide_index=True,
                width="stretch",
            )


def render_assessment(assessment: JSON) -> None:
    top, middle, bottom = st.columns([2, 2, 1])
    top.markdown("**Overall risk** (higher is riskier)")
    top.markdown(
        presenters.score_bar_html(assessment["overall_score"], assessment["overall_severity"]),
        unsafe_allow_html=True,
    )
    top.markdown(
        f"### {presenters.score_text(assessment['overall_score'])} / 100  "
        + presenters.severity_badge(assessment["overall_severity"]),
        unsafe_allow_html=True,
    )
    middle.markdown("**Completeness**")
    middle.progress(
        max(0.0, min(1.0, assessment["completeness"])),
        text=f"{assessment['completeness']:.0%} of signals assessed",
    )
    if assessment["provisional"]:
        bottom.markdown(presenters.badge_html("PROVISIONAL", "#F9A825"), unsafe_allow_html=True)
        st.warning(
            "Provisional: too little evidence to rely on this score. "
            "Add contracts or review the gaps below."
        )
    for note in assessment.get("notes", []):
        st.caption(note)
    for dimension in assessment["dimensions"]:
        render_dimension(dimension)
    docs = ", ".join(d["title"] for d in assessment.get("documents_considered", []))
    if docs:
        st.caption(f"Based on: {docs}")
    common.disclaimer(assessment.get("disclaimer"))
