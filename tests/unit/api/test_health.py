import pytest
from fastapi.testclient import TestClient


@pytest.mark.usefixtures("valid_env")
def test_live_endpoint() -> None:
    from covenantiq.api.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.usefixtures("valid_env")
def test_unknown_route_is_404() -> None:
    from covenantiq.api.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/nope").status_code == 404
