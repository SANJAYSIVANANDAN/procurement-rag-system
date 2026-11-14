"""End-to-end checks of the queue topology, a real worker and the dead-letter cycle.

These need the whole Compose stack (RabbitMQ with its management port, Redis, PostgreSQL, the API
and both workers). Run them with:

    docker compose up -d --build
    CIQ_E2E=1 CIQ_RABBITMQ_URL=... CIQ_REDIS_URL=... uv run pytest tests/e2e/test_celery_rabbitmq.py

Documents are real: the workers run Docling, so the PDFs here have genuine text (or, for the
failure cases, none at all). With CIQ_OFFLINE=1 the workers use the fake embedding provider.
"""

import os
import subprocess
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import redis

from tests.fixtures import documents as fixtures

pytestmark = pytest.mark.skipif(
    os.environ.get("CIQ_E2E") != "1", reason="set CIQ_E2E=1 with the compose stack running"
)

API = os.environ.get("CIQ_E2E_API_URL", "http://localhost:8000") + "/api/v1"
PASSWORD = "correct horse battery staple"
CLI = os.environ.get("CIQ_E2E_CLI", "docker compose exec -T api covenantiq").split()
WORK_QUEUES = [
    "document_processing",
    "embedding_generation",
    "contract_analysis",
    "vendor_analysis",
]


def management() -> httpx.Client:
    broker = urlsplit(os.environ["CIQ_RABBITMQ_URL"])
    base = os.environ.get("CIQ_E2E_RABBITMQ_MGMT_URL", f"http://{broker.hostname}:15672")
    return httpx.Client(
        base_url=base, auth=(broker.username or "", broker.password or ""), timeout=10
    )


def queue_info(name: str) -> dict[str, Any]:
    response = management().get(f"/api/queues/covenantiq/{name}")
    response.raise_for_status()
    info: dict[str, Any] = response.json()
    return info


def wait_for(
    condition: Callable[[], bool], *, timeout: float = 240, what: str = "condition"
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def user() -> dict[str, str]:
    email = f"e2e-{uuid4().hex[:10]}@example.com"
    registered = httpx.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    token = httpx.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


def upload(headers: dict[str, str], pdf: bytes | None = None) -> dict[str, Any]:
    # trailing bytes make each upload unique, so duplicate detection never merges two tests
    content = (pdf if pdf is not None else fixtures.make_msa_pdf()) + uuid4().hex.encode()
    response = httpx.post(
        f"{API}/documents/upload",
        headers=headers,
        files={"file": ("e2e.pdf", content)},
        timeout=30,
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def job_status(headers: dict[str, str], job_id: str) -> dict[str, Any]:
    body: dict[str, Any] = httpx.get(f"{API}/jobs/{job_id}", headers=headers, timeout=30).json()
    return body


@pytest.mark.parametrize("queue", WORK_QUEUES)
def test_work_queues_are_durable_and_dead_letter_to_their_dlq(queue: str) -> None:
    info = queue_info(queue)
    assert info["durable"] is True
    assert info["arguments"]["x-dead-letter-exchange"] == "covenantiq.dlx"
    assert info["arguments"]["x-dead-letter-routing-key"] == f"{queue}.dlq"
    assert queue_info(f"{queue}.dlq")["durable"] is True


def test_an_upload_is_processed_and_embedded_by_workers(user: dict[str, str]) -> None:
    created = upload(user)
    job_id, document_id = created["job"]["id"], created["document"]["id"]

    wait_for(lambda: job_status(user, job_id)["status"] == "succeeded", what="the job to succeed")

    job = job_status(user, job_id)
    assert job["progress"] == 1.0
    assert job["attempt"] == 2  # one claim for extract and chunk, one for embedding
    document = httpx.get(f"{API}/documents/{document_id}", headers=user).json()
    assert document["status"] == "ready"
    assert document["chunk_count"] >= 10
    assert document["extraction_quality"]["insufficient_text"] is False


def test_celery_results_live_in_redis_db1_and_never_in_db0(user: dict[str, str]) -> None:
    created = upload(user)
    wait_for(
        lambda: job_status(user, created["job"]["id"])["status"] == "succeeded",
        what="the job to succeed",
    )
    base = os.environ["CIQ_REDIS_URL"].rsplit("/", 1)[0]
    results_db = redis.Redis.from_url(f"{base}/1")
    sessions_db = redis.Redis.from_url(f"{base}/0")
    assert list(results_db.scan_iter("celery-task-meta-*", count=100))
    assert not list(sessions_db.scan_iter("celery-task-meta-*", count=100))
    assert not list(sessions_db.scan_iter("_kombu*", count=100))


def upload_scan(headers: dict[str, str]) -> str:
    """A scanned PDF has no text layer, so extraction fails permanently (NO_EXTRACTABLE_TEXT)."""
    return str(upload(headers, fixtures.make_scanned_pdf())["job"]["id"])


def dead_letter_targets(queue: str) -> list[str]:
    """Job ids of the messages in a DLQ, read with the operator CLI (queue statistics from the
    management API lag by several seconds, so counts from it cannot be compared exactly)."""
    listed = subprocess.run(  # noqa: S603
        [*CLI, "dlq", "list", queue], capture_output=True, text=True, check=True
    )
    return [
        part.removeprefix("target=")
        for line in listed.stdout.splitlines()
        for part in line.split()
        if part.startswith("target=")
    ]


def test_a_permanently_failing_task_ends_failed_in_postgres_and_in_the_dlq(
    user: dict[str, str],
) -> None:
    job_id = upload_scan(user)

    wait_for(lambda: job_status(user, job_id)["status"] == "failed", what="the job to fail")
    wait_for(
        lambda: dead_letter_targets("document_processing").count(job_id) == 1,
        what="the message to be dead-lettered",
    )
    job = job_status(user, job_id)
    assert job["error"]["code"] == "NO_EXTRACTABLE_TEXT"  # Postgres said so before the DLQ did


def test_dlq_replay_resets_the_job_and_republishes_the_task(user: dict[str, str]) -> None:
    job_id = upload_scan(user)
    wait_for(lambda: job_status(user, job_id)["status"] == "failed", what="the job to fail")
    wait_for(lambda: dead_letter_targets("document_processing").count(job_id) == 1, what="the DLQ")
    attempts_before = job_status(user, job_id)["attempt"]

    # operator commands run inside the api container, which is how they are used in practice
    replay = subprocess.run(  # noqa: S603
        [*CLI, "dlq", "replay", "document_processing", "--job-id", job_id],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "replayed 1" in replay.stdout
    # the worker ran the task again (a new attempt) and, the scan still having no text, it failed
    # again and went back to the DLQ: replaced, not duplicated
    wait_for(
        lambda: (
            job_status(user, job_id)["status"] == "failed"
            and job_status(user, job_id)["attempt"] > attempts_before
        ),
        what="the replayed task to run and fail again",
    )
    wait_for(
        lambda: dead_letter_targets("document_processing").count(job_id) == 1,
        what="the failed task to return to the DLQ",
    )


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "compose", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )


def test_a_job_whose_worker_is_killed_mid_task_is_recovered_and_completes(
    user: dict[str, str],
) -> None:
    """The worker-kill drill (spec milestone 6). RabbitMQ redelivers the dead worker's message at
    once, but a young heartbeat looks alive, so the redelivery is skipped. The sweep reaps jobs
    whose heartbeat is older than 10 minutes; the heartbeat is backdated here rather than waiting
    for it."""
    created = upload(user)
    job_id, document_id = created["job"]["id"], created["document"]["id"]
    wait_for(lambda: job_status(user, job_id)["status"] == "running", what="the job to start")
    compose("kill", "worker-ingest")  # SIGKILL: no chance to clean up
    assert job_status(user, job_id)["status"] == "running"  # nobody is working on it

    compose("start", "worker-ingest")
    time.sleep(20)  # the redelivered message arrives, finds a live-looking job, and is dropped
    assert job_status(user, job_id)["status"] == "running"

    backdate = f"UPDATE document_processing_jobs SET heartbeat_at = now() - interval '11 minutes' WHERE id = '{job_id}'"  # noqa: S608, E501  (a UUID from our own API)
    compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "ciq",
        "-d",
        "covenantiq",
        "-c",
        backdate,
    )
    # The API sweeps every minute and holds a lock while it does, so a manual sweep may find
    # another sweeper at work; either one recovers the job.
    subprocess.run([*CLI, "sweep"], capture_output=True, text=True, check=True)  # noqa: S603

    wait_for(lambda: job_status(user, job_id)["status"] == "succeeded", what="recovery")
    document = httpx.get(f"{API}/documents/{document_id}", headers=user).json()
    assert document["status"] == "ready"
    assert document["chunk_count"] >= 10
