"""Page 5: ask questions about your contracts and watch the answer being written."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import streamlit as st

from frontend import navigation, presenters, state
from frontend.api_client import ApiError
from frontend.components import common
from frontend.components.citations import render_citations
from frontend.components.severity import grounding_badge
from frontend.sse import ReplyState, stream_events

JSON = dict[str, Any]
HISTORY_PAGE = 50
SUGGESTIONS = [
    "Is the vendor's liability capped, and what is excluded from the cap?",
    "How can either party terminate, and with how much notice?",
    "What are the payment terms and any price increase limits?",
]


def render() -> None:
    common.page_header("Contract chat", "Answers cite the passages they come from.")
    documents = state.call(lambda: state.client().all_documents(max_items=300, status="ready"))
    if documents is None:
        return
    conversation_id = state.current_conversation()

    _sidebar(documents, conversation_id)
    conversation_id = state.current_conversation()

    history = _history(conversation_id) if conversation_id else []
    if not history and not state.selected_documents():
        common.empty_state(
            "Pick the contracts to ask about in the sidebar, or leave it empty to ask across all.",
            "Questions about documents that are still processing cannot be answered yet.",
        )
    for message in history:
        _show_message(message)
    if not history:
        st.caption("Try one of these:")
        for suggestion in SUGGESTIONS:
            if st.button(suggestion, key=f"suggest_{abs(hash(suggestion))}"):
                st.session_state["pending_question"] = suggestion
                st.rerun()

    _regenerate_button(history)
    question = st.chat_input(
        "Ask about your contracts", disabled=bool(st.session_state.get("streaming")), key="chat_box"
    )
    question = question or st.session_state.pop("pending_question", None)
    question = question or st.session_state.pop("pending_regenerate", None)
    if question:
        _ask(question, conversation_id)


# ---- sidebar ---------------------------------------------------------------------------------


def _sidebar(documents: list[JSON], conversation_id: str | None) -> None:
    api = state.client()
    with st.sidebar:
        st.markdown("**Conversations**")
        if st.button("New conversation", width="stretch", key="chat_new"):
            state.open_conversation(None)
            st.rerun()
        page = state.call(lambda: api.list_conversations(limit=30), show=False)
        items = page["items"] if page else []
        if items:
            ids = [c["id"] for c in items]
            choice = st.selectbox(
                "Switch conversation",
                ids,
                index=ids.index(conversation_id) if conversation_id in ids else None,
                placeholder="Open an earlier conversation",
                format_func=lambda i: next(
                    (c["title"] or "Untitled" for c in items if c["id"] == i), "Untitled"
                ),
                key="chat_switch",
            )
            if choice and choice != conversation_id:
                state.open_conversation(choice)
                _load_scope(choice)
                st.rerun()

        titles = {d["id"]: d["title"] for d in documents}
        wanted = [i for i in state.selected_documents() if i in titles]
        picked = st.multiselect(
            "Documents",
            list(titles),
            default=wanted,
            format_func=lambda i: titles[i],
            help="Only documents that are ready. Leave empty to search all of them.",
            key="chat_documents",
        )
        if picked != wanted or picked != state.selected_documents():
            state.set_selected_documents(picked)
            if conversation_id:
                state.call(lambda: api.update_conversation(conversation_id, document_ids=picked))


def _load_scope(conversation_id: str) -> None:
    detail = state.call(lambda: state.client().get_conversation(conversation_id), show=False)
    if detail:
        state.set_selected_documents(list(detail["document_ids"]))


# ---- history ---------------------------------------------------------------------------------


def _history(conversation_id: str) -> list[JSON]:
    cache = state.messages_cache()
    if conversation_id not in cache:
        page = state.call(lambda: state.client().messages(conversation_id, limit=HISTORY_PAGE))
        cache[conversation_id] = page["items"] if page else []
        st.session_state.setdefault("older", {})[conversation_id] = (
            page["next_cursor"] if page else None
        )
    older = st.session_state.get("older", {}).get(conversation_id)
    if older and st.button("Load earlier messages", key="chat_older"):
        page = state.call(
            lambda: state.client().messages(
                conversation_id, before_seq=int(older), limit=HISTORY_PAGE
            )
        )
        if page:
            cache[conversation_id] = page["items"] + cache[conversation_id]
            st.session_state["older"][conversation_id] = page["next_cursor"]
            st.rerun()
    return cache[conversation_id]


def _show_message(message: JSON) -> None:
    role = "user" if message["role"] == "user" else "assistant"
    with st.chat_message(role):
        if role == "user":
            st.write(message["content"])
            return
        if message["status"] in ("failed", "interrupted") and not message["content"]:
            st.warning(
                "This answer was not finished"
                + (" (the server restarted)." if message["status"] == "interrupted" else ".")
                + " Use Regenerate to ask again."
            )
            return
        _show_answer(
            message["content"],
            message.get("citations", []),
            message.get("grounding_status"),
            partial=message["status"] in ("failed", "interrupted"),
        )


def _show_answer(
    text: str,
    citations: list[JSON],
    grounding: str | None,
    *,
    stages: list[str] | None = None,
    usage: JSON | None = None,
    partial: bool = False,
) -> None:
    if partial:
        st.caption("Unfinished answer:")
    st.markdown(presenters.markers_to_html(text, citations), unsafe_allow_html=True)
    grounding_badge(grounding)
    render_citations(citations)
    lines = presenters.why_this_answer(stages or [], citations, grounding, usage or {})
    if lines:
        with st.expander("Why this answer?"):
            for line in lines:
                st.markdown(f"- {line}")


def _regenerate_button(history: list[JSON]) -> None:
    last_question = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
    if last_question and st.button(
        "Regenerate", key="chat_regenerate", help="Ask the last question again."
    ):
        st.session_state["pending_regenerate"] = last_question
        st.rerun()


# ---- asking ----------------------------------------------------------------------------------


def _ask(question: str, conversation_id: str | None) -> None:
    api = state.client()
    selected = state.selected_documents()
    if conversation_id is None:
        created = state.call(
            lambda: api.create_conversation(title=question[:60], document_ids=selected)
        )
        if created is None:
            return
        conversation_id = created["id"]
        state.open_conversation(conversation_id)
        state.messages_cache()[conversation_id] = []

    with st.chat_message("user"):
        st.write(question)
    try:
        accepted = api.send_message(conversation_id, question, client_message_id=str(uuid4()))
    except ApiError as error:
        if error.code == "GENERATION_IN_PROGRESS":
            st.warning(
                "A reply is still being written in this conversation. Wait for it to finish."
            )
        else:
            state.show_error(error)
        return

    reply = ReplyState()
    st.session_state["streaming"] = True
    try:
        with st.chat_message("assistant"):
            status_box = st.status("Working on it...", expanded=False)
            slot = st.empty()
            try:
                with slot.container():  # the draft streams here and is replaced by the final text
                    st.write_stream(
                        _tokens(
                            conversation_id, accepted["assistant_message_id"], reply, status_box
                        )
                    )
            except ApiError as error:
                status_box.update(label="Could not follow the answer", state="error")
                state.show_error(error)
                return
            with slot.container():
                _finish(reply, status_box)
    finally:
        st.session_state["streaming"] = False
    _remember(conversation_id, question, accepted, reply)


def _tokens(
    conversation_id: str, message_id: str, reply: ReplyState, status_box: Any
) -> Iterator[str]:
    for event in stream_events(state.client(), conversation_id, message_id):
        text = reply.apply(event)
        if event.kind == "status" and reply.stages:
            status_box.update(label=presenters.chat_stage_label(reply.stages[-1]) + "...")
        if text:
            yield text


def _finish(reply: ReplyState, status_box: Any) -> None:
    if reply.error is not None:
        status_box.update(label="The answer did not finish", state="error")
        retry = reply.error.get("retryable")
        st.warning(
            str(reply.error.get("message") or "Something went wrong.")
            + (" You can regenerate it." if retry else "")
        )
        if reply.draft:
            st.caption("Partial answer:")
            st.markdown(f":gray[{reply.draft}]")
        return
    status_box.update(label="Answer ready", state="complete")
    # the validated text replaces the draft that was streamed
    st.markdown(presenters.markers_to_html(reply.text, reply.citations), unsafe_allow_html=True)
    grounding_badge(reply.grounding)
    render_citations(reply.citations)
    lines = presenters.why_this_answer(reply.stages, reply.citations, reply.grounding, reply.usage)
    if lines:
        with st.expander("Why this answer?"):
            for line in lines:
                st.markdown(f"- {line}")


def _remember(conversation_id: str, question: str, accepted: JSON, reply: ReplyState) -> None:
    """Keep what was just said, so the page redraws it without another call."""
    cache = state.messages_cache().setdefault(conversation_id, [])
    cache.append(
        {
            "id": accepted["user_message_id"],
            "role": "user",
            "content": question,
            "status": "completed",
            "grounding_status": None,
            "citations": [],
        }
    )
    cache.append(
        {
            "id": accepted["assistant_message_id"],
            "role": "assistant",
            "content": reply.text,
            "status": "completed" if reply.final is not None else "failed",
            "grounding_status": reply.grounding,
            "citations": reply.citations,
        }
    )


def open_from_history(conversation_id: str) -> None:
    state.open_conversation(conversation_id)
    _load_scope(conversation_id)
    state.messages_cache().pop(conversation_id, None)
    navigation.go("chat")
