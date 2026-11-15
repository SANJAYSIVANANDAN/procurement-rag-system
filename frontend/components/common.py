"""Small pieces shared by the pages: headers, empty states, the disclaimer, job progress."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import streamlit as st

from frontend import presenters, state

JSON = dict[str, Any]
POLL_SECONDS = 2.0
POLL_LIMIT_SECONDS = 900.0
DONE = ("succeeded", "failed", "cancelled")


def page_header(title: str, subtitle: str | None = None) -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def empty_state(message: str, hint: str | None = None) -> None:
    box = st.container(border=True)
    box.markdown(f"**{message}**")
    if hint:
        box.caption(hint)


def disclaimer(text: str | None = None) -> None:
    st.info(text or presenters.DISCLAIMER, icon=":material/info:")


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def watch_job(
    job_id: str,
    title: str,
    *,
    fetch: Callable[[str], JSON | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JSON | None:
    """Follow a background job with a progress bar until it ends; return its final state.

    Leaving the page stops the watching, not the job. Returns None if the job could not be read.
    """
    read = fetch or (lambda j: state.call(lambda: state.client().job(j), show=False))
    started = time.monotonic()
    with st.status(title, expanded=True) as box:
        bar = st.progress(0.0)
        while True:
            job = read(job_id)
            if job is None:
                box.update(label=f"{title}: status unavailable", state="error")
                return None
            bar.progress(max(0.0, min(1.0, float(job.get("progress") or 0.0))))
            box.update(label=f"{title}: {presenters.stage_label(job.get('stage'), job['status'])}")
            if job["status"] in DONE:
                if job["status"] == "succeeded":
                    bar.progress(1.0)
                    box.update(label=f"{title}: done", state="complete")
                else:
                    reason = (job.get("error") or {}).get("message") or (
                        job.get("error") or {}
                    ).get("code")
                    box.update(label=f"{title}: {job['status']}", state="error")
                    if reason:
                        st.error(str(reason))
                return job
            if time.monotonic() - started > POLL_LIMIT_SECONDS:
                box.update(label=f"{title}: still running, check back later", state="running")
                return job
            sleep(POLL_SECONDS)
