"""Severity and status badges, as Streamlit calls."""

from __future__ import annotations

import streamlit as st

from frontend import presenters


def severity_badge(severity: str) -> None:
    st.markdown(presenters.severity_badge(severity), unsafe_allow_html=True)


def status_badge(status: str) -> None:
    st.markdown(presenters.status_badge(status), unsafe_allow_html=True)


def grounding_badge(grounding: str | None) -> None:
    markup = presenters.grounding_badge(grounding)
    if markup:
        st.markdown(markup, unsafe_allow_html=True)
