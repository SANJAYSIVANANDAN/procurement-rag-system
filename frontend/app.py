"""CovenantIQ UI entry point: `streamlit run frontend/app.py`.

Signed out, the only page is Login. Signed in, the pages are grouped in the sidebar, which also
shows the user, and (on the chat pages) the conversation switcher that `pages/chat.py` adds.
The UI talks only to the HTTP API at `CIQ_API_BASE_URL` (spec 27.4).
"""

import sys
from pathlib import Path

# `streamlit run frontend/app.py` puts frontend/ on the path; the modules import `frontend.x`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from frontend import navigation, state
from frontend.pages import (
    analysis,
    chat,
    compare,
    dashboard,
    documents,
    history,
    login,
    upload,
    vendors,
)

CSS = """
<style>
  [data-testid="stSidebar"] { background: #F5F7FB; }
  div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 10px; }
  h1 { font-weight: 650; letter-spacing: -0.01em; }
  sup a { color: #2F5BEA; }
</style>
"""


def main() -> None:
    st.set_page_config(layout="wide", page_title="CovenantIQ", page_icon=":material/gavel:")
    st.markdown(CSS, unsafe_allow_html=True)

    if not state.logged_in():
        page = st.Page(login.render, title="Sign in", icon=":material/login:", url_path="login")
        st.navigation([page], position="hidden").run()
        return

    pages = {
        "dashboard": st.Page(
            dashboard.render,
            title="Dashboard",
            icon=":material/dashboard:",
            url_path="dashboard",
            default=True,
        ),
        "documents": st.Page(
            documents.render,
            title="Document library",
            icon=":material/folder:",
            url_path="documents",
        ),
        "upload": st.Page(
            upload.render, title="Upload", icon=":material/upload_file:", url_path="upload"
        ),
        "chat": st.Page(
            chat.render, title="Contract chat", icon=":material/chat:", url_path="chat"
        ),
        "history": st.Page(
            history.render,
            title="Conversation history",
            icon=":material/history:",
            url_path="history",
        ),
        "analysis": st.Page(
            analysis.render,
            title="Contract risk analysis",
            icon=":material/policy:",
            url_path="analysis",
        ),
        "vendors": st.Page(
            vendors.render, title="Vendors", icon=":material/business:", url_path="vendors"
        ),
        "compare": st.Page(
            compare.render,
            title="Vendor comparison",
            icon=":material/compare_arrows:",
            url_path="compare",
        ),
    }
    navigation.register(pages)
    with st.sidebar:
        st.markdown("### CovenantIQ")
        user = st.session_state.get("user") or {}
        st.caption(f"Signed in as {user.get('full_name') or user.get('email', '')}")
    nav = st.navigation(
        {
            "Overview": [pages["dashboard"]],
            "Documents": [pages["documents"], pages["upload"]],
            "Chat": [pages["chat"], pages["history"]],
            "Risk": [pages["analysis"], pages["vendors"], pages["compare"]],
        }
    )
    with st.sidebar:
        if st.button("Sign out", key="signout", width="stretch"):
            state.logout()
            st.rerun()
    nav.run()


main()
