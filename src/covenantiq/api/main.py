from fastapi import FastAPI

from covenantiq import __version__
from covenantiq.api.routers import health
from covenantiq.core.config import get_settings
from covenantiq.core.enums import LogFormat
from covenantiq.core.logging import configure_logging, get_logger


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format or LogFormat.JSON)

    app = FastAPI(title="CovenantIQ", version=__version__)
    app.include_router(health.router, prefix="/api/v1")

    get_logger(__name__).info("api.created", env=settings.env.value)
    return app


app = create_app()
