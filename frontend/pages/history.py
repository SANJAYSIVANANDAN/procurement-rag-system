"""Page 9: earlier conversations."""

from __future__ import annotations

import streamlit as st

from frontend import presenters, state
from frontend.components import common
from frontend.pages.chat import open_from_history


def render() -> None:
    common.page_header(
        "Conversation history", "Open an earlier conversation, rename it or archive it."
    )
    archived = st.toggle("Show archived", key="history_archived")
    page = state.call(lambda: state.client().list_conversations(archived=archived, limit=100))
    if page is None:
        return
    items = page["items"]
    documents = state.call(lambda: state.client().all_documents(max_items=300), show=False) or []
    if not items:
        common.empty_state(
            "No archived conversations." if archived else "No conversations yet.",
            None if archived else "Ask a question in Contract chat to start one.",
        )
        return

    for conversation in items:
        box = st.container(border=True)
        title = conversation["title"] or "Untitled"
        box.markdown(f"**{title}**")
        last = conversation["updated_at"][:16].replace("T", " ")
        box.caption(
            f"Documents: {presenters.message_scope(conversation, documents)} · "
            f"{conversation['last_message_seq']} messages · last activity {last}"
        )
        a, b, c = box.columns([1, 2, 1])
        if a.button("Open", key=f"open_{conversation['id']}"):
            open_from_history(conversation["id"])
        new_title = b.text_input(
            "Rename", value=title, key=f"rename_{conversation['id']}", label_visibility="collapsed"
        )
        if (
            new_title.strip()
            and new_title != title
            and b.button("Save name", key=f"save_{conversation['id']}")
        ):
            if _update(conversation["id"], title=new_title.strip()):
                st.rerun()
        if c.button("Unarchive" if archived else "Archive", key=f"arch_{conversation['id']}"):
            if _update(conversation["id"], archived=not archived):
                st.rerun()


def _update(conversation_id: str, **fields: object) -> bool:
    return (
        state.call(lambda: state.client().update_conversation(conversation_id, **fields))
        is not None
    )
