"""One contract finding as a card."""

from __future__ import annotations

from html import escape
from typing import Any

import streamlit as st

from frontend import presenters
from frontend.components.citations import render_evidence

JSON = dict[str, Any]


def render_finding(finding: JSON) -> None:
    box = st.container(border=True)
    head = (
        f"{presenters.severity_badge(finding['severity'])} "
        f"{presenters.status_badge(finding['clause_status'])} "
        f"**{escape(finding['title'])}**"
    )
    box.markdown(head, unsafe_allow_html=True)
    where = []
    if finding.get("clause_ref"):
        where.append(f"clause {finding['clause_ref']}")
    if finding.get("page_number") is not None:
        where.append(f"page {finding['page_number']}")
    if where:
        box.caption(" · ".join(where) + f" · rule {finding['rule_id']}")
    box.write(finding["explanation"])
    if finding.get("business_impact"):
        box.markdown(f"**Business impact.** {finding['business_impact']}")
    if finding.get("recommended_action"):
        box.markdown(f"**Recommended action.** {finding['recommended_action']}")
    confidence = float(finding.get("confidence") or 0.0)
    box.progress(max(0.0, min(1.0, confidence)), text=f"Confidence {confidence:.0%}")
    if finding.get("evidence"):
        with box.expander(f"Evidence ({len(finding['evidence'])})"):
            render_evidence(finding["evidence"])
    elif finding["clause_status"] == "absent":
        box.caption("Nothing in the document addresses this; no passage to show.")
