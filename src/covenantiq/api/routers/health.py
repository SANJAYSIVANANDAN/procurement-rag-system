from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness only: the process is up. Readiness (DB, Redis, broker) arrives with M3+."""
    return {"status": "ok"}
