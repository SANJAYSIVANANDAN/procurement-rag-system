"""Streamlit AppTest smoke tests of the pages, with the API client replaced by a fake.

A page is run on its own (`AppTest.from_function`) with a fake client in the session, so what is
tested is what the page does with what the API says, never a real network call.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from streamlit.testing.v1 import AppTest

from frontend.api_client import ApiError, Unauthorized

APP = Path(__file__).resolve().parents[3] / "frontend" / "app.py"
CID = "00000000-0000-0000-0000-0000000000c1"
DOC = "00000000-0000-0000-0000-0000000000d1"
MID_USER = "00000000-0000-0000-0000-00000000aa01"
MID_BOT = "00000000-0000-0000-0000-00000000aa02"
NEW_MID = "00000000-0000-0000-0000-00000000aa03"

CITATION = {
    "marker": 1,
    "document_id": DOC,
    "document_title": "Acme MSA",
    "page_start": 14,
    "page_end": 14,
    "clause_ref": "12.2",
    "quote": "shall not exceed the fees paid in the preceding twelve months",
    "validated": True,
}


class FakeClient:
    """Just the calls the pages make, answering from canned data and recording what was sent."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.sent: list[dict[str, Any]] = []
        self.login_error: ApiError | None = None
        self.unauthorized = False
        self.stream: list[tuple[str, dict[str, Any]]] = []
        self.history: list[dict[str, Any]] = []

    def _check(self) -> None:
        if self.unauthorized and self.token:  # a signed-out page would not be rendered at all
            raise Unauthorized(401, "TOKEN_EXPIRED", "Token expired.")

    def login(self, email: str, password: str) -> str:
        if self.login_error:
            raise self.login_error
        self.token = "tok"
        return "tok"

    def me(self) -> dict[str, Any]:
        return {"id": "u1", "email": "a@b.c", "full_name": "Ada"}

    def all_documents(self, **_: Any) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "id": DOC,
                "title": "Acme MSA",
                "status": "ready",
                "contract_type": "msa",
                "vendor_id": None,
                "page_count": 20,
                "created_at": "2026-11-01T10:00:00Z",
                "updated_at": "2026-11-02T10:00:00Z",
            }
        ]

    def all_vendors(self, **_: Any) -> list[dict[str, Any]]:
        return []

    def findings(self, document_id: str, **_: Any) -> dict[str, Any]:
        finding = {
            "id": "f1",
            "rule_id": "LIAB-001",
            "risk_category": "liability",
            "clause_status": "problematic",
            "severity": "high",
            "title": "Cap is too low",
            "explanation": "x",
            "details": {},
        }
        return {"document_id": document_id, "findings": [finding]}

    def latest_assessment(self, vendor_id: str) -> None:
        return None

    def list_conversations(self, **_: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": CID,
                    "title": "Liability",
                    "document_ids": [DOC],
                    "last_message_seq": 2,
                    "updated_at": "2026-11-02T10:00:00Z",
                    "archived": False,
                }
            ],
            "next_cursor": None,
        }

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return {"id": conversation_id, "document_ids": [DOC]}

    def messages(self, conversation_id: str, **_: Any) -> dict[str, Any]:
        self._check()
        return {"items": self.history, "next_cursor": None}

    def create_conversation(self, **fields: Any) -> dict[str, Any]:
        return {"id": CID, "title": fields.get("title")}

    def update_conversation(self, conversation_id: str, **fields: Any) -> dict[str, Any]:
        return {"id": conversation_id}

    def send_message(self, conversation_id: str, content: str, **kw: Any) -> dict[str, Any]:
        self.sent.append({"content": content, **kw})
        return {
            "user_message_id": MID_USER,
            "assistant_message_id": NEW_MID,
            "stream_url": "x",
            "status": "pending",
        }

    @contextmanager
    def open_stream(
        self, conversation_id: str, message_id: str, last_event_id: str | None = None
    ) -> Iterator[Any]:
        events = [
            SimpleNamespace(
                event=kind,
                id=f"{i}-0",
                data=json.dumps({"event": kind, "message_id": message_id, **data}),
            )
            for i, (kind, data) in enumerate(self.stream, start=1)
        ]
        yield SimpleNamespace(iter_sse=lambda: iter(events))


def run_login() -> None:
    from frontend.pages import login

    login.render()


def run_chat() -> None:
    from frontend.pages import chat

    chat.render()


def run_history() -> None:
    from frontend.pages import history

    history.render()


def make(script: Any, fake: FakeClient, *, token: bool = True) -> AppTest:
    at = AppTest.from_function(script, default_timeout=20)
    at.session_state["client"] = fake
    if token:
        at.session_state["token"] = "tok"
    return at


ANSWER_EVENTS = [
    ("message_start", {"conversation_id": CID, "seq": 4}),
    ("status", {"stage": "retrieving"}),
    ("token", {"text": "The cap is fees [C1].", "index": 0}),
    ("citation", {"citation": {k: v for k, v in CITATION.items() if k != "validated"}}),
    (
        "message_complete",
        {
            "status": "completed",
            "grounding_status": "grounded",
            "content": "The cap is twelve months of fees [C1].",
            "usage": {"output_tokens": 12},
            "timings_ms": {},
        },
    ),
]


class TestLogin:
    def test_the_page_offers_sign_in_and_registration(self) -> None:
        at = make(run_login, FakeClient(), token=False).run()
        assert not at.exception
        assert [t.label for t in at.tabs] == ["Sign in", "Create account"]
        assert at.title[0].value == "CovenantIQ"

    def test_empty_fields_are_refused_before_any_call(self) -> None:
        fake = FakeClient()
        at = make(run_login, fake, token=False).run()
        at.button[0].click().run()
        assert at.error[0].value == "Enter your email and password."
        assert fake.token is None

    def test_a_wrong_password_shows_the_api_message_and_its_reference(self) -> None:
        fake = FakeClient()
        fake.login_error = ApiError(
            401, "INVALID_CREDENTIALS", "Wrong email or password.", request_id="req-9"
        )
        at = make(run_login, fake, token=False).run()
        at.text_input(key="login_email").set_value("a@b.c")
        at.text_input(key="login_password").set_value("nope")
        at.button[0].click().run()
        assert at.error[0].value == "Wrong email or password."
        assert any("req-9" in c.value for c in at.caption)
        assert "token" not in at.session_state

    def test_signing_in_stores_the_token_and_the_user(self) -> None:
        fake = FakeClient()
        at = make(run_login, fake, token=False).run()
        at.text_input(key="login_email").set_value("a@b.c")
        at.text_input(key="login_password").set_value("correct horse battery")
        at.button[0].click().run()
        assert at.session_state["token"] == "tok" and at.session_state["user"]["full_name"] == "Ada"

    def test_registration_checks_the_passwords_match(self) -> None:
        at = make(run_login, FakeClient(), token=False).run()
        at.text_input(key="register_email").set_value("new@b.c")
        at.text_input(key="register_password").set_value("correct horse battery")
        at.text_input(key="register_again").set_value("something else entirely")
        at.button[1].click().run()
        assert at.error[0].value == "The passwords do not match."


class TestChat:
    def history(self) -> list[dict[str, Any]]:
        return [
            {
                "id": MID_USER,
                "seq": 1,
                "role": "user",
                "content": "Is liability capped?",
                "status": "completed",
                "grounding_status": None,
                "citations": [],
            },
            {
                "id": MID_BOT,
                "seq": 2,
                "role": "assistant",
                "content": "Yes, at twelve months of fees [C1].",
                "status": "completed",
                "grounding_status": "grounded",
                "citations": [CITATION],
            },
        ]

    def test_history_is_shown_with_markers_citations_and_the_grounding_badge(self) -> None:
        fake = FakeClient()
        fake.history = self.history()
        at = make(run_chat, fake)
        at.session_state["current_conversation_id"] = CID
        at.run()
        assert not at.exception
        text = " ".join(m.value for m in at.markdown)
        assert 'href="#cit-1"' in text and "<sup" in text
        assert "Grounded" in text and "Acme MSA · clause 12.2 · p. 14" in text
        assert "preceding twelve months" in text
        assert [e.label for e in at.expander] == ["Why this answer?"]

    def test_a_new_conversation_starts_empty_with_suggestions(self) -> None:
        at = make(run_chat, FakeClient()).run()
        assert not at.exception
        assert any("Try one of these" in c.value for c in at.caption)
        assert any(b.label.startswith("Is the vendor's liability capped") for b in at.button)

    def test_asking_creates_a_conversation_sends_the_question_and_shows_the_final_text(
        self,
    ) -> None:
        fake = FakeClient()
        fake.stream = ANSWER_EVENTS
        at = make(run_chat, fake).run()

        at.chat_input[0].set_value("Is liability capped?").run()

        assert not at.exception
        assert [s["content"] for s in fake.sent] == ["Is liability capped?"]
        assert fake.sent[0]["client_message_id"]  # idempotent resubmission is possible
        assert at.session_state["current_conversation_id"] == CID
        text = " ".join(m.value for m in at.markdown)
        assert "twelve months of fees" in text  # the validated final text, not only the draft
        assert "Acme MSA · clause 12.2 · p. 14" in text
        cached = at.session_state["messages"][CID]
        assert [m["role"] for m in cached] == ["user", "assistant"]
        assert (
            cached[1]["content"] == "The cap is twelve months of fees [C1]."
            and cached[1]["status"] == "completed"
        )
        assert at.session_state["streaming"] is False

    def test_a_stream_that_fails_shows_the_partial_text_and_offers_regenerate(self) -> None:
        fake = FakeClient()
        fake.stream = [
            ("message_start", {"conversation_id": CID, "seq": 4}),
            ("token", {"text": "The cap is", "index": 0}),
            (
                "error",
                {
                    "code": "INTERRUPTED",
                    "message": "The server restarted before the answer was finished.",
                    "retryable": True,
                },
            ),
        ]
        at = make(run_chat, fake).run()

        at.chat_input[0].set_value("Is liability capped?").run()

        assert not at.exception
        assert any("You can regenerate it." in w.value for w in at.warning)
        assert any("The cap is" in m.value for m in at.markdown)
        assert at.session_state["messages"][CID][1]["status"] == "failed"

    def test_a_second_question_while_one_is_written_is_explained_not_failed(self) -> None:
        fake = FakeClient()

        def busy(*a: Any, **k: Any) -> dict[str, Any]:
            raise ApiError(409, "GENERATION_IN_PROGRESS", "A reply is already being written.")

        fake.send_message = busy  # type: ignore[method-assign]
        at = make(run_chat, fake).run()
        at.chat_input[0].set_value("again?").run()
        assert any("still being written" in w.value for w in at.warning)
        assert not at.exception

    def test_an_expired_token_returns_to_the_login_page(self) -> None:
        fake = FakeClient()
        fake.unauthorized = True
        at = make(run_chat, fake).run()
        assert "token" not in at.session_state
        assert "sign in again" in at.session_state["login_notice"]

    def test_the_input_is_disabled_while_a_reply_is_streaming(self) -> None:
        at = make(run_chat, FakeClient())
        at.session_state["streaming"] = True
        at.run()
        assert at.chat_input[0].disabled


class TestHistoryPage:
    def test_conversations_are_listed_with_their_documents(self) -> None:
        at = make(run_history, FakeClient()).run()
        assert not at.exception
        text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
        assert "Liability" in text and "Acme MSA" in text and "2 messages" in text
        assert at.button(key=f"open_{CID}") is not None


class TestWholeApp:
    def app(self, fake: FakeClient, *, token: bool) -> AppTest:
        at = AppTest.from_file(str(APP), default_timeout=30)
        at.session_state["client"] = fake
        if token:
            at.session_state["token"] = "tok"
            at.session_state["user"] = {"email": "a@b.c", "full_name": "Ada"}
        return at

    def test_signed_out_there_is_only_the_login_page(self) -> None:
        at = self.app(FakeClient(), token=False).run()
        assert not at.exception
        assert at.title[0].value == "CovenantIQ" and [t.label for t in at.tabs] == [
            "Sign in",
            "Create account",
        ]
        assert not at.sidebar.button

    def test_signed_in_the_sidebar_shows_the_user_and_the_dashboard_opens(self) -> None:
        fake = FakeClient()
        at = self.app(fake, token=True).run()
        assert not at.exception, [e.value for e in at.exception]
        assert any("Signed in as Ada" in c.value for c in at.sidebar.caption)
        assert at.title[0].value == "Dashboard"
        assert [m.label for m in at.metric][:2] == ["Documents ready", "Processing"]
        assert at.metric[-1].value == "1"  # the one high finding of the one analysed document

    def test_the_sidebar_has_a_sign_out_button(self) -> None:
        at = self.app(FakeClient(), token=True).run()
        assert at.sidebar.button(key="signout").label == "Sign out"

    def test_logging_out_forgets_the_user_and_leaves_a_notice(self) -> None:
        def script() -> None:
            import streamlit as st

            from frontend import state

            st.session_state["token"] = "tok"
            st.session_state["user"] = {"email": "a@b.c"}
            st.session_state["messages"] = {"c": []}
            st.session_state["current_conversation_id"] = "c"
            state.logout("Your session ended.")
            left = [k for k in ("token", "user", "messages") if k in st.session_state]
            st.text(str(left))

        at = AppTest.from_function(script, default_timeout=20).run()
        assert not at.exception
        assert at.text[0].value == "[]"  # nothing of the session is left
        assert at.session_state["login_notice"] == "Your session ended."
