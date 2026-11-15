"""Page 8: compare two to five assessed vendors."""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from frontend import presenters, state
from frontend.api_client import ApiError
from frontend.components import common

JSON = dict[str, Any]
MIN_VENDORS, MAX_VENDORS = 2, 5
SHADE = "background-color:#ECEFF1;color:#546E7A;font-style:italic"


def render() -> None:
    common.page_header("Vendor comparison", "Side by side, only where the evidence allows it.")
    common.disclaimer()
    vendors = state.call(lambda: state.client().all_vendors(max_items=200))
    if vendors is None:
        return
    if len(vendors) < MIN_VENDORS:
        common.empty_state(
            "Comparing needs at least two vendors.", "Create and assess vendors first."
        )
        return
    names = presenters.vendor_names(vendors)
    picked = st.multiselect(
        "Vendors",
        list(names),
        format_func=lambda i: names[i],
        max_selections=MAX_VENDORS,
        key="compare_pick",
    )
    require_fresh = st.checkbox(
        "Require assessments newer than the contracts", value=True, key="compare_fresh"
    )
    if len(picked) < MIN_VENDORS:
        st.caption(f"Choose {MIN_VENDORS} to {MAX_VENDORS} vendors.")
        return
    if not st.button("Compare", type="primary", key="compare_go"):
        return

    try:
        started = state.client().compare_vendors(picked, require_fresh=require_fresh)
    except ApiError as error:
        _explain(error, names)
        return
    job = common.watch_job(started["job"]["id"], "Comparing")
    if job is None or job["status"] != "succeeded":
        return
    comparison = state.call(lambda: state.client().comparison(started["comparison_id"]))
    if comparison:
        show_comparison(comparison)


def _explain(error: ApiError, names: dict[str, str]) -> None:
    """Preconditions: say what is missing, and offer to fix it."""
    if error.status == 401:
        state.call(lambda: (_ for _ in ()).throw(error))
    state.show_error(error)
    missing = [v for v in (error.details or {}).get("vendor_ids", []) if v in names]
    for vendor_id in missing:
        if st.button(f"Assess {names[vendor_id]} now", key=f"assess_now_{vendor_id}"):
            started = state.call(lambda v=vendor_id: state.client().assess_vendor(v))  # type: ignore[misc]
            if started:
                common.watch_job(started["job"]["id"], f"Assessing {names[vendor_id]}")
                st.info("Done. Press Compare again.")


def show_comparison(comparison: JSON) -> None:
    rows, notes = presenters.comparison_matrix(comparison)
    ranking = presenters.ranking_names(comparison)
    if ranking:
        st.markdown("**Lowest to highest overall risk:** " + " , ".join(ranking))
    for note in notes:
        st.warning(note)

    st.subheader("Dimensions")
    vendor_cols = [c for c in rows[0] if not c.startswith("_") and c != "Dimension"] if rows else []
    frame = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    if not frame.empty:
        shaded = [not r["_comparable"] for r in rows]
        styled = frame.style.apply(
            lambda col: [SHADE if shaded[i] else "" for i in range(len(col))], subset=vendor_cols
        )
        st.dataframe(styled, hide_index=True, width="stretch")
        for row in rows:
            if not row["_comparable"] and row["_reason"]:
                st.caption(f"{row['Dimension']}: not compared ({row['_reason']})")

    data = presenters.comparison_chart_data(rows)
    if data:
        chart = (
            alt.Chart(pd.DataFrame(data))
            .mark_bar()
            .encode(
                x=alt.X("Dimension:N", title=None),
                y=alt.Y("Risk score:Q", scale=alt.Scale(domain=[0, 100])),
                color="Vendor:N",
                xOffset="Vendor:N",
            )
            .properties(height=280)
        )
        st.altair_chart(chart, width="stretch")
        st.caption(
            "Only dimensions that every vendor was assessed on with enough evidence are charted."
        )
    else:
        st.info("No dimension could be compared across these vendors.")

    signals = presenters.signal_rows(comparison)
    if signals:
        with st.expander(f"Signals ({len(signals)})"):
            st.dataframe(signals, hide_index=True, width="stretch")

    if comparison.get("narrative"):
        st.subheader("Summary")
        st.write(comparison["narrative"])
    common.disclaimer(comparison.get("disclaimer"))
