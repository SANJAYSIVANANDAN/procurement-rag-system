"""The citations panel under an answer, and evidence quotes in findings."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

import streamlit as st

from frontend import presenters

JSON = dict[str, Any]


def render_citations(citations: Sequence[JSON], *, heading: str = "Sources") -> None:
    """One entry per citation: `[n]` badge, document, clause, page and the quoted passage.

    Each entry carries the anchor `cit-<n>` that the superscript markers in the answer link to.
    """
    if not citations:
        return
    st.markdown(f"**{heading}**")
    for citation in citations:
        marker = citation.get("marker")
        anchor = f'<span id="cit-{marker}"></span>' if marker is not None else ""
        badge = presenters.badge_html(f"C{marker}", "#2F5BEA") + " " if marker is not None else ""
        flag = "" if citation.get("validated", True) else " (not verified)"
        st.markdown(
            f"{anchor}{badge}**{escape(presenters.citation_title(citation))}**{flag}",
            unsafe_allow_html=True,
        )
        st.markdown(f"> {escape(citation.get('quote', ''))}")


def render_evidence(evidence: Sequence[JSON]) -> None:
    for item in evidence:
        st.markdown(f"**{escape(presenters.citation_title(item))}**", unsafe_allow_html=True)
        st.markdown(f"> {escape(item.get('quote', ''))}")
