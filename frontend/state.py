"""Session state and the helpers every page uses (spec 27.1).

`st.session_state` holds the token, the user, the open conversation, the selected documents and a
per-conversation message cache, and nothing else of consequence. Every API call a page makes goes
through `call`, which turns a 401 into a return to the login page and any other error into a
message with its request id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

from frontend.api_client import ApiClient, ApiError, Unauthorized
from frontend.config import api_base_url

_KEYS = (
    "token",
    "user",
    "current_conversation_id",
    "messages",
    "selected_document_ids",
    "analysis_document_id",
    "streaming",
    "pending_regenerate",
    "login_notice",
    "older",
    "pending_question",
    "_pages",
)


def client() -> ApiClient:
    """The API client for this browser session (created once, carrying the token)."""
    existing = st.session_state.get("client")
    if existing is None:
        existing = ApiClient(api_base_url())
        st.session_state["client"] = existing
    existing.token = st.session_state.get("token")
    return existing  # type: ignore[no-any-return,unused-ignore]


def logged_in() -> bool:
    return bool(st.session_state.get("token"))


def login(email: str, password: str) -> None:
    api = client()
    token = api.login(email, password)
    st.session_state["token"] = token
    st.session_state["user"] = api.me()


def logout(notice: str | None = None) -> None:
    for key in _KEYS:
        st.session_state.pop(key, None)
    st.cache_data.clear()
    if notice:
        st.session_state["login_notice"] = notice


def show_error(error: ApiError) -> None:
    st.error(error.message)
    if error.request_id:
        st.caption(f"Reference: {error.request_id}")


def call[T](fn: Callable[[], T], *, show: bool = True) -> T | None:
    """Run one API call. On a 401 return to login; on another error show it and return None."""
    try:
        return fn()
    except Unauthorized:
        logout("Your session ended. Please sign in again.")
        st.rerun()
    except ApiError as error:
        if show:
            show_error(error)
        return None
    return None  # unreachable: st.rerun raises


def messages_cache() -> dict[str, list[dict[str, Any]]]:
    return st.session_state.setdefault("messages", {})  # type: ignore[no-any-return]


def selected_documents() -> list[str]:
    return list(st.session_state.get("selected_document_ids", []))


def set_selected_documents(ids: list[str]) -> None:
    st.session_state["selected_document_ids"] = ids


def open_conversation(conversation_id: str | None) -> None:
    st.session_state["current_conversation_id"] = conversation_id


def current_conversation() -> str | None:
    return st.session_state.get("current_conversation_id")
