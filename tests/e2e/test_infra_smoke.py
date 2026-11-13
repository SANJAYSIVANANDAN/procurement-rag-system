"""Smoke tests against a running `docker compose up` stack. Opt in with CIQ_E2E=1."""

import os

import httpx
import pytest
import redis
from kombu import Connection

pytestmark = pytest.mark.skipif(
    os.environ.get("CIQ_E2E") != "1", reason="set CIQ_E2E=1 with the compose stack running"
)

API_URL = os.environ.get("CIQ_E2E_API_URL", "http://localhost:8000")


def test_api_is_live() -> None:
    response = httpx.get(f"{API_URL}/api/v1/health/live", timeout=5)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_is_ready_and_schema_is_at_head() -> None:
    response = httpx.get(f"{API_URL}/api/v1/health/ready", timeout=5)
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "migrations": "ok"}


def test_rabbitmq_accepts_connections() -> None:
    with Connection(os.environ["CIQ_RABBITMQ_URL"], connect_timeout=5) as connection:
        connection.ensure_connection(max_retries=2)
        assert connection.connected


def test_redis_answers_ping() -> None:
    client = redis.Redis.from_url(os.environ["CIQ_REDIS_URL"], socket_timeout=5)
    assert client.ping() is True
