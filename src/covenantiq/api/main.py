from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from covenantiq import __version__
from covenantiq.api.container import Container, build_container
from covenantiq.api.errors import register_exception_handlers
from covenantiq.api.middleware import RequestContextMiddleware
from covenantiq.api.routers import auth, health
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
        try:
            yield
        finally:
            if owned:
                await app.state.container.aclose()

    app = FastAPI(title="CovenantIQ", version=__version__, lifespan=lifespan)
    if container is not None:
        app.state.container = container  # also available when lifespan events are not run
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")

    get_logger(__name__).info("api.created", env=settings.env.value)
    return app


app = create_app()
