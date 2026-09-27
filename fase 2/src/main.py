"""Punto de entrada ASGI."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.common.logger import configure_logger
from src.api.workflows.healthcheck.router import router as healthcheck_router
from src.api.workflows.voice_agents.router import router as voice_agents_router
from src.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger = configure_logger(settings.log_level)
    app.state.logger = logger
    logger.info("API iniciada; versión=%s", settings.app_version)
    try:
        yield
    finally:
        logger.info("API detenida")


def create_app() -> FastAPI:
    app = FastAPI(title="Prototipo de apertura de siniestros", lifespan=lifespan)
    app.include_router(healthcheck_router)
    app.include_router(voice_agents_router)
    return app


app = create_app()
