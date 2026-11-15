"""One risk dimension of a vendor assessment as a card."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import presenters
from frontend.components.citations import render_evidence

JSON = dict[str, Any]


def render_dimension(dimension: JSON) -> None:
    name = presenters.DIMENSION_LABEL.get(dimension["dimension"], dimension["dimension"])
    box = st.container(border=True)
    box.markdown(
        f"**{name}** {presenters.severity_badge(dimension['severity'])}", unsafe_allow_html=True
    )
    if dimension["status"] != "assessed":
        box.caption(f"{presenters.label(dimension['status'])}: not enough information to score.")
    else:
        box.markdown(
            presenters.score_bar_html(dimension["score"], dimension["severity"]),
            unsafe_allow_html=True,
        )
        box.caption(
            f"Risk score {presenters.score_text(dimension['score'])} of 100 (higher is riskier) · "
            f"evidence coverage {dimension['coverage']:.0%}"
        )
    if dimension.get("reasoning"):
        box.write(dimension["reasoning"])
    if dimension.get("missing_information"):
        with box.expander("Missing information"):
            for item in dimension["missing_information"]:
                st.markdown(f"- **{item['label']}**: {item['reason']}")
    if dimension.get("mitigations"):
        with box.expander("Suggested mitigations"):
            for item in dimension["mitigations"]:
                st.markdown(f"- {item}")
    signals = [s for s in dimension.get("signals", []) if s.get("status") not in (None, "")]
    if signals:
        with box.expander(f"Signals ({len(signals)})"):
            st.dataframe(
                [
                    {
                        "Signal": s["label"],
                        "Status": presenters.label(s["status"]),
                        "Weight": s["weight"],
                        "Gap": presenters.label(s.get("gap")),
                    }
                    for s in signals
                ],
                hide_index=True,
                width="stretch",
            )
    if dimension.get("evidence"):
        with box.expander(f"Evidence ({len(dimension['evidence'])})"):
            render_evidence(dimension["evidence"])
