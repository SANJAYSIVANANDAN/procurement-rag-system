from fastapi import APIRouter, Response, status

from covenantiq.api.deps import ContainerDep
from covenantiq.services.health_service import check_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness only: the process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(container: ContainerDep, response: Response) -> dict[str, object]:
    """Readiness: PostgreSQL reachable, schema at the Alembic head, Redis and RabbitMQ up."""
    result = await check_readiness(
        container.engine, container.redis, container.settings.rabbitmq_url.get_secret_value()
    )
    if not result.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if result.ready else "not_ready", "checks": result.checks}
