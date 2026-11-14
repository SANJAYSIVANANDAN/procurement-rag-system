"""The reranker as its own container, with the real model (ADR-017).

Needs the reranker running: `docker compose --profile rerank up -d reranker`. Opt in with
CIQ_E2E=1 and CIQ_E2E_RERANK=1. The service is only reachable inside the Compose network, so the
requests are made from inside its own container.
"""

import json
import os
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CIQ_E2E") != "1" or os.environ.get("CIQ_E2E_RERANK") != "1",
    reason="set CIQ_E2E=1 and CIQ_E2E_RERANK=1 with the reranker container running",
)

CLIENT = textwrap.dedent(
    """
    import json, sys, time, urllib.error, urllib.request

    path = sys.argv[1]
    payload = sys.stdin.read()  # on stdin: 40 passages do not fit on a Windows command line
    started = time.monotonic()
    try:
        if payload:
            request = urllib.request.Request(
                "http://localhost:8100" + path,
                payload.encode(),
                {"Content-Type": "application/json"},
            )
            response = urllib.request.urlopen(request, timeout=120)
        else:
            response = urllib.request.urlopen("http://localhost:8100" + path, timeout=120)
        status, body = response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode()
    print(json.dumps({"status": status, "seconds": time.monotonic() - started, "body": body}))
    """
)


def call(path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "compose",
            "--profile",
            "rerank",
            "exec",
            "-T",
            "reranker",
            "python",
            "-c",
            CLIENT,
            path,
        ],
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True,
        text=True,
        check=True,
    )
    reply: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    return reply


def test_the_model_is_loaded_and_ready() -> None:
    reply = call("/health/ready")
    assert reply["status"] == 200
    assert json.loads(str(reply["body"]))["model"] == "cross-encoder/ettin-reranker-150m-v1"


def test_it_ranks_the_liability_cap_first_with_raw_scores() -> None:
    documents = [
        "The office kitchen is cleaned every Friday afternoon.",
        "The Supplier's aggregate liability is limited to the fees paid in the prior "
        "twelve months.",
        "Invoices are payable within thirty days of receipt.",
        "Neither party shall be liable for indirect or consequential loss.",
    ]
    reply = call(
        "/v1/rerank",
        {"query": "What is the cap on the supplier's liability?", "documents": documents},
    )
    assert reply["status"] == 200
    results = json.loads(str(reply["body"]))["results"]
    assert [hit["index"] for hit in results][:2] == [1, 3]
    scores = [hit["score"] for hit in results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > 5 and scores[-1] < 0  # raw, unbounded logits, not 0-1 probabilities


def test_a_request_that_cannot_meet_its_deadline_is_abandoned_quickly() -> None:
    chunk = "The Supplier shall provide the Services in accordance with this Agreement. " * 18
    reply = call(
        "/v1/rerank",
        {"query": "liability cap", "documents": [chunk] * 40, "deadline_ms": 500},
    )
    assert reply["status"] == 504
    # without the deadline these 40 passages take well over 20 seconds on 4 CPUs
    assert float(str(reply["seconds"])) < 10
    assert call("/health/ready")["status"] == 200  # and the service carries on


def test_oversized_requests_are_refused() -> None:
    reply = call("/v1/rerank", {"query": "q", "documents": ["a"] * 101})
    assert reply["status"] == 413
