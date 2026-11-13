import pytest
from fastapi.testclient import TestClient

from covenantiq.services.health_service import ReadinessResult


def client_with(monkeypatch: pytest.MonkeyPatch, result: ReadinessResult) -> TestClient:
    from covenantiq.api.main import create_app

    async def fake_check(*_args: object) -> ReadinessResult:
        return result

    monkeypatch.setattr("covenantiq.api.routers.health.check_readiness", fake_check)
    return TestClient(create_app())


@pytest.mark.usefixtures("valid_env")
def test_ready_when_database_is_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ReadinessResult(
        True, {"database": "ok", "migrations": "ok", "redis": "ok", "rabbitmq": "ok"}
    )
    with client_with(monkeypatch, result) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.usefixtures("valid_env")
def test_not_ready_returns_503_with_coarse_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ReadinessResult(
        False, {"database": "ok", "migrations": "behind", "redis": "ok", "rabbitmq": "ok"}
    )
    with client_with(monkeypatch, result) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "migrations": "behind", "redis": "ok", "rabbitmq": "ok"},
    }
