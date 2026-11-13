from fastapi import APIRouter, Request, Response, status

from covenantiq.services.health_service import check_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness only: the process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    """Readiness: PostgreSQL reachable and the schema at the Alembic head."""
    result = await check_readiness(request.app.state.engine)
    if not result.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if result.ready else "not_ready", "checks": result.checks}
