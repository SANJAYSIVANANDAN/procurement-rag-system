"""The UI's pages against a running API, with the real client (opt-in).

    CIQ_E2E_UI=1 CIQ_API_BASE_URL=http://localhost:8000/api/v1 uv run pytest tests/e2e

The API may run with the fake model (`CIQ_OFFLINE=1`): the point is that every page works with the
shapes the real API returns. A new account is registered for the run. Uploading needs the worker
stack and is not covered here (the Compose smoke test does that).
"""

import os
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest

from frontend.api_client import ApiClient

pytestmark = pytest.mark.skipif(
    os.environ.get("CIQ_E2E_UI") != "1", reason="set CIQ_E2E_UI=1 with an API running"
)

BASE = os.environ.get("CIQ_API_BASE_URL", "http://localhost:8000/api/v1")
PASSWORD = "correct horse battery staple"


def page(module: str) -> str:
    """The script that runs one page on its own."""
    return "from frontend.pages import " + module + "\n" + module + ".render()\n"


def at_page(module: str, client: ApiClient) -> AppTest:
    app = AppTest.from_string(page(module), default_timeout=60)
    app.session_state["client"] = client
    app.session_state["token"] = client.token
    return app


@pytest.fixture(scope="module")
def client() -> ApiClient:
    api = ApiClient(BASE)
    email = f"ui-{uuid4().hex[:10]}@example.com"
    api.register(email, PASSWORD, "UI Tester")
    api.login(email, PASSWORD)
    return api


def test_every_page_renders_for_a_new_account(client: ApiClient) -> None:
    for module in (
        "dashboard",
        "documents",
        "upload",
        "chat",
        "history",
        "analysis",
        "vendors",
        "compare",
    ):
        app = at_page(module, client).run()
        assert not app.exception, f"{module}: {[e.value for e in app.exception]}"


def test_a_vendor_can_be_created_and_listed_through_the_page(client: ApiClient) -> None:
    app = at_page("vendors", client).run()
    app.text_input[1].set_value("Globex Corporation")  # the create form's name field
    app.button[0].click().run()  # Create
    assert not app.exception
    assert [v["name"] for v in client.all_vendors()] == ["Globex Corporation"]


def test_a_question_is_asked_and_answered_over_real_streaming(client: ApiClient) -> None:
    app = at_page("chat", client).run()
    app.chat_input[0].set_value("Is the vendor's liability capped?").run()
    assert not app.exception, [e.value for e in app.exception]
    conversation = app.session_state["current_conversation_id"]
    cached = app.session_state["messages"][conversation]
    assert [m["role"] for m in cached] == ["user", "assistant"]
    assert cached[1]["status"] == "completed" and cached[1]["content"]
    # the same conversation is there after "refreshing": a new session loads it from the API
    fresh = at_page("history", client).run()
    assert any("Is the vendor's liability capped?"[:60] in m.value for m in fresh.markdown)
    reloaded = client.messages(conversation)["items"]
    assert [m["role"] for m in reloaded] == ["user", "assistant"]
    assert reloaded[1]["content"] == cached[1]["content"]
