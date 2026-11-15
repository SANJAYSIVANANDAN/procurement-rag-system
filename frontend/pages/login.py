"""Page 1: sign in or create an account."""

from __future__ import annotations

import streamlit as st

from frontend import state
from frontend.api_client import ApiError
from frontend.components import common

MIN_PASSWORD = 12


def render() -> None:
    common.page_header(
        "CovenantIQ", "Contract and vendor risk review, with sources for every answer."
    )
    notice = st.session_state.pop("login_notice", None)
    if notice:
        st.warning(notice)

    left, centre, _ = st.columns([1, 2, 1])
    with centre:
        sign_in, register = st.tabs(["Sign in", "Create account"])
        with sign_in, st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        if submitted:
            _sign_in(email, password)

        with register, st.form("register_form"):
            name = st.text_input("Full name (optional)", key="register_name")
            new_email = st.text_input("Email", key="register_email")
            new_password = st.text_input(
                "Password",
                type="password",
                key="register_password",
                help=f"At least {MIN_PASSWORD} characters.",
            )
            again = st.text_input("Repeat password", type="password", key="register_again")
            created = st.form_submit_button("Create account", type="primary", width="stretch")
        if created:
            _register(name, new_email, new_password, again)
    del left


def _sign_in(email: str, password: str) -> None:
    if not email or not password:
        st.error("Enter your email and password.")
        return
    try:
        state.login(email.strip(), password)
    except ApiError as error:
        state.show_error(error)
        return
    st.rerun()


def _register(name: str, email: str, password: str, again: str) -> None:
    if not email or not password:
        st.error("Enter an email and a password.")
    elif password != again:
        st.error("The passwords do not match.")
    elif len(password) < MIN_PASSWORD:
        st.error(f"The password needs at least {MIN_PASSWORD} characters.")
    else:
        try:
            state.client().register(email.strip(), password, name.strip() or None)
            state.login(email.strip(), password)
        except ApiError as error:
            state.show_error(error)
            return
        st.rerun()
