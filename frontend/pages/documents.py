"""Page 3: the document library."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import navigation, presenters, state
from frontend.components import common

JSON = dict[str, Any]
CONTRACT_TYPES = [
    "unknown",
    "msa",
    "saas",
    "dpa",
    "sow",
    "nda",
    "license",
    "supply",
    "services",
    "other",
]
STATUSES = ["ready", "failed", "uploaded", "queued", "extracting", "chunking", "embedding"]


def render() -> None:
    common.page_header("Document library", "Everything you have uploaded.")
    vendors = state.call(lambda: state.client().all_vendors(max_items=200)) or []
    names = presenters.vendor_names(vendors)

    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    search = f1.text_input("Search by title", key="doc_search")
    status = f2.selectbox("Status", ["any", *STATUSES], key="doc_status")
    vendor = f3.selectbox(
        "Vendor",
        ["any", *names],
        format_func=lambda v: "any" if v == "any" else names[v],
        key="doc_vendor",
    )
    kind = f4.selectbox("Type", ["any", *CONTRACT_TYPES], key="doc_type")

    documents = state.call(
        lambda: state.client().all_documents(
            max_items=300,
            status=None if status == "any" else status,
            vendor_id=None if vendor == "any" else vendor,
            q=search or None,
        )
    )
    if documents is None:
        return
    if kind != "any":
        documents = [d for d in documents if d["contract_type"] == kind]
    if not documents:
        common.empty_state("No documents match.", "Change the filters, or upload a contract.")
        return

    table = st.dataframe(
        [
            {
                "Title": d["title"],
                "Status": presenters.label(d["status"]),
                "Type": presenters.label(d["contract_type"]),
                "Vendor": names.get(d["vendor_id"] or "", ""),
                "Pages": d["page_count"],
                "Uploaded": d["created_at"][:10],
            }
            for d in documents
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="doc_table",
    )
    picked = table.selection.rows if table and table.selection else []
    if not picked:
        st.caption("Select a row to see its details and actions.")
        return
    _detail(documents[picked[0]]["id"], vendors, names)


def _delete(document_id: str) -> bool:
    state.client().delete_document(document_id)
    return True


def _detail(document_id: str, vendors: list[JSON], names: dict[str, str]) -> None:
    detail = state.call(lambda: state.client().get_document(document_id))
    if detail is None:
        return
    st.subheader(detail["title"])
    st.markdown(
        presenters.badge_html(
            presenters.label(detail["status"]),
            presenters.DOCUMENT_STATUS_COLOR.get(detail["status"], "#2F5BEA"),
        ),
        unsafe_allow_html=True,
    )
    for warning in presenters.quality_warnings(detail):
        st.warning(warning)
    st.caption(
        f"{detail['original_filename']} · {detail['size_bytes'] / 1024:.0f} KB · "
        f"{detail['page_count'] or '?'} pages · {detail['chunk_count']} passages"
    )

    ready = detail["status"] == "ready"
    a, b, c = st.columns(3)
    if a.button("Run risk analysis", disabled=not ready, width="stretch", key="doc_analyze"):
        st.session_state["analysis_document_id"] = document_id
        navigation.go("analysis")
    if b.button("Open chat", disabled=not ready, width="stretch", key="doc_chat"):
        state.set_selected_documents([document_id])
        state.open_conversation(None)
        navigation.go("chat")
    confirm = c.checkbox("Confirm delete", key=f"doc_confirm_{document_id}")
    if c.button("Delete", disabled=not confirm, width="stretch", key="doc_delete"):
        if state.call(lambda: _delete(document_id)):
            st.cache_data.clear()
            st.success("Deleted.")
            st.rerun()

    with st.expander("Edit details"), st.form(f"doc_edit_{document_id}"):
        title = st.text_input("Title", value=detail["title"])
        vendor_ids = ["", *names]
        vendor = st.selectbox(
            "Vendor",
            vendor_ids,
            index=vendor_ids.index(detail["vendor_id"]) if detail["vendor_id"] in vendor_ids else 0,
            format_func=lambda v: "none" if not v else names[v],
        )
        kind = st.selectbox(
            "Contract type", CONTRACT_TYPES, index=CONTRACT_TYPES.index(detail["contract_type"])
        )
        if st.form_submit_button("Save"):
            fields = {"title": title, "contract_type": kind, "vendor_id": vendor or None}
            if state.call(lambda: state.client().update_document(document_id, **fields)):
                st.cache_data.clear()
                st.success("Saved.")
                st.rerun()
    del vendors
