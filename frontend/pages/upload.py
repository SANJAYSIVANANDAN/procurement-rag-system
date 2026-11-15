"""Page 4: upload contracts and watch them being read."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import streamlit as st

from frontend import presenters, state
from frontend.components import common
from frontend.pages.documents import CONTRACT_TYPES

JSON = dict[str, Any]
POLL_SECONDS = 2.0
POLL_LIMIT_SECONDS = 900.0
NEW_VENDOR = "__new__"


def render() -> None:
    common.page_header(
        "Upload", "PDF or Word contracts. Reading takes a minute or two for a long one."
    )
    vendors = state.call(lambda: state.client().all_vendors(max_items=200)) or []
    names = presenters.vendor_names(vendors)

    files = st.file_uploader(
        "Contracts", type=["pdf", "docx"], accept_multiple_files=True, key="upload_files"
    )
    c1, c2 = st.columns(2)
    choice = c1.selectbox(
        "Vendor",
        ["", *names, NEW_VENDOR],
        format_func=lambda v: (
            "no vendor" if not v else "Create a new vendor..." if v == NEW_VENDOR else names[v]
        ),
        key="upload_vendor",
    )
    kind = c2.selectbox("Contract type", CONTRACT_TYPES, key="upload_type")
    new_name = (
        st.text_input("New vendor name", key="upload_new_vendor") if choice == NEW_VENDOR else ""
    )

    if not st.button("Upload", type="primary", disabled=not files, key="upload_go"):
        if not files:
            common.empty_state("Choose one or more files above.")
        return

    vendor_id = choice or None
    if choice == NEW_VENDOR:
        if not new_name.strip():
            st.error("Enter the new vendor's name.")
            return
        created = state.call(lambda: state.client().create_vendor(new_name.strip()))
        if created is None:
            return
        vendor_id = created["id"]
        st.success(f"Vendor {created['name']} created.")

    pending: list[tuple[str, str]] = []
    for file in files or []:
        outcome = state.call(
            lambda f=file: state.client().upload_document(  # type: ignore[misc]
                f.name,
                f.getvalue(),
                vendor_id=vendor_id,
                contract_type=kind if kind != "unknown" else None,
            )
        )
        if outcome is None:
            continue
        document = outcome["document"]
        if outcome["duplicate"]:
            st.info(f"{file.name}: already uploaded, using the existing copy.")
        pending.append((document["id"], document["title"]))
    st.cache_data.clear()
    if pending:
        follow(pending)


def follow(
    pending: list[tuple[str, str]],
    *,
    fetch: Callable[[str], JSON | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, JSON]:
    """Poll each document's status every two seconds until all are ready or failed."""
    read = fetch or (lambda d: state.call(lambda: state.client().document_status(d), show=False))
    slots = {}
    for document_id, title in pending:
        box = st.container(border=True)
        box.markdown(f"**{title}**")
        slots[document_id] = (box.empty(), box.progress(0.0))
    final: dict[str, JSON] = {}
    started = time.monotonic()
    while len(final) < len(pending) and time.monotonic() - started < POLL_LIMIT_SECONDS:
        for document_id, _title in pending:
            if document_id in final:
                continue
            status = read(document_id)
            text, bar = slots[document_id]
            if status is None:
                text.warning("Status unavailable.")
                continue
            bar.progress(max(0.0, min(1.0, float(status.get("progress") or 0.0))))
            if status["status"] == "ready":
                bar.progress(1.0)
                text.success(
                    f"Ready: {status.get('page_count') or '?'} pages, "
                    f"{status['chunk_count']} passages."
                )
                final[document_id] = status
            elif status["status"] == "failed":
                reason = (status.get("error") or {}).get("message") or "Processing failed."
                text.error(reason)
                final[document_id] = status
            else:
                text.info(presenters.stage_label(status.get("stage"), status["status"]) + "...")
        if len(final) < len(pending):
            sleep(POLL_SECONDS)
    return final
