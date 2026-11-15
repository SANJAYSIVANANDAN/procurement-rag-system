"""Moving between pages: `app.py` registers the page objects each run, a page asks by name."""

from __future__ import annotations

import streamlit as st

_KEY = "_pages"


def register(pages: dict[str, st.Page]) -> None:
    st.session_state[_KEY] = pages


def go(name: str) -> None:
    """Switch to a registered page; does nothing if it is not registered (a test, or logged out)."""
    page = st.session_state.get(_KEY, {}).get(name)
    if page is not None:
        st.switch_page(page)
