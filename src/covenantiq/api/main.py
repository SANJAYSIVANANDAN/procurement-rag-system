import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from covenantiq import __version__
from covenantiq.api.background import declare_broker_topology, sweep_loop
from covenantiq.api.container import Container, build_container
from covenantiq.api.errors import register_exception_handlers
from covenantiq.api.limits import MULTIPART_OVERHEAD_BYTES, BodySizeLimitMiddleware
from covenantiq.api.middleware import RequestContextMiddleware
from covenantiq.api.routers import auth, documents, health, jobs, vendors
from covenantiq.core.config import get_settings
from covenantiq.core.enums import LogFormat
from covenantiq.core.logging import configure_logging, get_logger


def create_app(container: Container | None = None) -> FastAPI:
    """Build the app. Tests pass their own container; otherwise one is built at startup."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format or LogFormat.JSON)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or build_container(settings)
        sweeper: asyncio.Task[None] | None = None
        if owned:
            await declare_broker_topology(app.state.container)
            if settings.sweep_interval_seconds > 0:
                sweeper = asyncio.create_task(
                    sweep_loop(app.state.container, settings.sweep_interval_seconds)
                )
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
            if owned:
                await app.state.container.aclose()

    app = FastAPI(title="CovenantIQ", version=__version__, lifespan=lifespan)
    if container is not None:
        app.state.container = container  # also available when lifespan events are not run
    app.add_middleware(
        BodySizeLimitMiddleware,
        limits={
            ("POST", "/api/v1/documents/upload"): settings.max_upload_mb * 1024 * 1024
            + MULTIPART_OVERHEAD_BYTES
        },
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(jobs.router, prefix="/api/v1")
    app.include_router(vendors.router, prefix="/api/v1")

    get_logger(__name__).info("api.created", env=settings.env.value)
    return app


app = create_app()
