"""The whole product against a running Compose stack, with real providers (opt-in).

    docker compose up -d
    CIQ_E2E_FLOW=1 CIQ_API_BASE_URL=http://localhost:8000/api/v1 \
        uv run pytest tests/e2e/test_full_flow.py -s

It registers a new account, uploads four synthetic contracts (two vendors, written for the
evaluation dataset, no real data), waits for them to be read and indexed, asks questions over the
live stream, analyses a contract, and assesses both vendors. It spends real tokens on whatever
model and embedding provider the stack is configured with (a few cents with a small model). The
steps depend on each other and run in the order written.
"""

import io
import os
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from docx import Document

from frontend.api_client import ApiClient
from frontend.sse import ReplyState, stream_events

pytestmark = pytest.mark.skipif(
    os.environ.get("CIQ_E2E_FLOW") != "1", reason="set CIQ_E2E_FLOW=1 with the stack running"
)

BASE = os.environ.get("CIQ_API_BASE_URL", "http://localhost:8000/api/v1")
DOCUMENTS = Path(__file__).resolve().parents[2] / "eval" / "datasets" / "v1.0.0" / "documents"
PASSWORD = "correct horse battery staple"
READY_TIMEOUT = 600
JOB_TIMEOUT = 900

CONTRACTS = {  # file, vendor, contract type
    "acme_saas": ("Acme", "saas"),
    "acme_dpa": ("Acme", "dpa"),
    "globex_saas": ("Globex", "saas"),
    "globex_dpa": ("Globex", "dpa"),
}


def to_docx(text: str) -> bytes:
    """The evaluation text format (`# heading`, paragraphs, `@page` markers) as a Word file."""
    document = Document()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("@page"):
            continue
        if line.startswith("# "):
            document.add_heading(line[2:], level=1)
        else:
            document.add_paragraph(line)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def wait_for(read: Any, done: Any, timeout: float, what: str) -> Any:
    started = time.monotonic()
    while True:
        value = read()
        if done(value):
            return value
        if time.monotonic() - started > timeout:
            raise AssertionError(f"{what}: not finished after {timeout:.0f}s ({value})")
        time.sleep(3)


def wait_job(api: ApiClient, job_id: str, what: str) -> dict[str, Any]:
    job = wait_for(
        lambda: api.job(job_id),
        lambda j: j["status"] in ("succeeded", "failed", "cancelled"),
        JOB_TIMEOUT,
        what,
    )
    assert job["status"] == "succeeded", f"{what}: {job}"
    return job  # type: ignore[no-any-return]


def ask(api: ApiClient, conversation: str, question: str) -> ReplyState:
    accepted = api.send_message(conversation, question)
    reply = ReplyState()
    for event in stream_events(api, conversation, accepted["assistant_message_id"]):
        reply.apply(event)
    assert reply.error is None, reply.error
    assert reply.final is not None and reply.status == "completed"
    return reply


class State:
    api: ApiClient
    vendors: ClassVar[dict[str, str]] = {}
    documents: ClassVar[dict[str, str]] = {}
    conversation: str = ""


@pytest.fixture(scope="module")
def state() -> type[State]:
    return State


def test_the_stack_is_ready_and_a_new_account_can_sign_in(state: type[State]) -> None:
    api = ApiClient(BASE, timeout=30)
    ready = api._http.get("/health/ready")
    assert ready.status_code == 200, ready.text
    email = f"flow-{int(time.time())}@example.com"
    api.register(email, PASSWORD, "Flow Tester")
    api.login(email, PASSWORD)
    assert api.me()["email"] == email
    state.api = api


def test_contracts_are_uploaded_read_and_indexed(state: type[State]) -> None:
    api = state.api
    for name in ("Acme", "Globex"):
        state.vendors[name] = api.create_vendor(name)["id"]
    for stem, (vendor, kind) in CONTRACTS.items():
        text = (DOCUMENTS / f"{stem}.txt").read_text(encoding="utf-8")
        uploaded = api.upload_document(
            f"{stem}.docx",
            to_docx(text),
            title=stem.replace("_", " ").title(),
            vendor_id=state.vendors[vendor],
            contract_type=kind,
        )
        assert uploaded["duplicate"] is False
        state.documents[stem] = uploaded["document"]["id"]
    for stem, document_id in state.documents.items():
        status = wait_for(
            lambda d=document_id: api.document_status(d),
            lambda s: s["status"] in ("ready", "failed"),
            READY_TIMEOUT,
            f"reading {stem}",
        )
        assert status["status"] == "ready", f"{stem}: {status}"
        assert status["chunk_count"] > 0, stem


def test_a_question_is_answered_with_validated_citations(state: type[State]) -> None:
    api = state.api
    conversation = api.create_conversation(
        title="Acme liability", document_ids=[state.documents["acme_saas"]]
    )
    state.conversation = conversation["id"]
    reply = ask(
        api, state.conversation, "Is the Supplier's liability capped, and what is excluded?"
    )
    print("\nANSWER:", reply.final)
    print(
        "grounding:", reply.grounding, "citations:", len(reply.citations), "stages:", reply.stages
    )
    assert reply.grounding in ("grounded", "partially_grounded")
    assert reply.citations and all(c["document_title"] for c in reply.citations)
    assert "twelve" in reply.final.lower() or "12" in reply.final  # type: ignore[union-attr]


def test_a_follow_up_uses_the_conversation(state: type[State]) -> None:
    reply = ask(state.api, state.conversation, "And can the Customer terminate early?")
    print("\nFOLLOW-UP:", reply.final)
    assert reply.grounding in ("grounded", "partially_grounded")


def test_a_question_the_documents_cannot_answer_is_not_answered(state: type[State]) -> None:
    reply = ask(state.api, state.conversation, "What is the Supplier's annual revenue in euros?")
    print("\nUNANSWERABLE:", reply.final)
    # declined as out of scope or for lack of evidence, depending on how the model routes it;
    # either way no figure is invented
    assert reply.grounding in ("abstained", "not_applicable", "partially_grounded", "ungrounded")
    assert "€" not in reply.final and not reply.citations  # type: ignore[operator]


def test_history_survives_signing_in_again(state: type[State]) -> None:
    again = ApiClient(BASE, timeout=30)
    again.token = state.api.token  # a refresh keeps nothing but the token: the rest is the server's
    messages = again.messages(state.conversation)["items"]
    assert [m["role"] for m in messages] == ["user", "assistant"] * 3
    assert all(m["status"] == "completed" for m in messages)


def test_a_contract_is_analysed(state: type[State]) -> None:
    api = state.api
    job = api.analyze_document(state.documents["acme_saas"])["job"]
    wait_job(api, job["id"], "contract analysis")
    report = api.findings(state.documents["acme_saas"])
    print("\nFINDINGS:", report["summary"], len(report["findings"]), "findings")
    assert report["findings"], "no findings"
    assert report["disclaimer"]
    for finding in report["findings"]:
        assert finding["title"] and finding["clause_status"]


def test_both_vendors_are_assessed(state: type[State]) -> None:
    api = state.api
    jobs = {name: api.assess_vendor(vid)["job"]["id"] for name, vid in state.vendors.items()}
    for name, job_id in jobs.items():
        wait_job(api, job_id, f"assessment of {name}")
    for name, vendor_id in state.vendors.items():
        latest = api.latest_assessment(vendor_id)
        assert latest is not None
        print(
            f"\n{name}: overall={latest['overall_score']} {latest['overall_severity']}",
            f"provisional={latest['provisional']} completeness={latest['completeness']:.2f}",
        )


def test_every_ui_page_renders_with_the_data_just_created(state: type[State]) -> None:
    """The Streamlit pages against the real API's real data: findings, assessments, history."""
    from streamlit.testing.v1 import AppTest

    def render(module: str, **session: Any) -> AppTest:
        script = "from frontend.pages import " + module + "\n" + module + ".render()\n"
        app = AppTest.from_string(script, default_timeout=120)
        app.session_state["client"] = state.api
        app.session_state["token"] = state.api.token
        for key, value in session.items():
            app.session_state[key] = value
        return app.run()

    pages: dict[str, dict[str, Any]] = {
        "dashboard": {},
        "documents": {},
        "upload": {},
        "chat": {"current_conversation_id": state.conversation},
        "history": {},
        "analysis": {"analysis_document_id": state.documents["acme_saas"]},
        "vendors": {},
    }
    for module, session in pages.items():
        app = render(module, **session)
        assert not app.exception, f"{module}: {[e.value for e in app.exception]}"

    chat = render("chat", current_conversation_id=state.conversation)
    text = " ".join(m.value for m in chat.markdown)
    assert "Sources" in text and "Grounded" in text  # citations and the grounding badge
    analysis = render("analysis", analysis_document_id=state.documents["acme_saas"])
    assert len(analysis.markdown) > 20  # finding cards, not an empty state
    dashboard = render("dashboard")
    assert dashboard.metric[0].value == "4"  # four documents ready
