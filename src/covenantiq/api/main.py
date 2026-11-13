from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from covenantiq import __version__
from covenantiq.api.routers import health
from covenantiq.core.config import get_settings
from covenantiq.core.enums import LogFormat
from covenantiq.core.logging import configure_logging, get_logger
from covenantiq.db.session import create_engine, create_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_engine(get_settings())
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format or LogFormat.JSON)

    app = FastAPI(title="CovenantIQ", version=__version__, lifespan=lifespan)
    app.include_router(health.router, prefix="/api/v1")

    get_logger(__name__).info("api.created", env=settings.env.value)
    return app


app = create_app()
