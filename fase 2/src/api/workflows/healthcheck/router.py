import logging

from fastapi import APIRouter, Depends

from src.api.common.logger import get_logger
from src.settings import Settings, get_settings

router = APIRouter(tags=["healthcheck"])


@router.get("/")
@router.get("/healthcheck")
async def healthcheck(
    settings: Settings = Depends(get_settings),
    logger: logging.Logger = Depends(get_logger),
) -> dict:
    logger.debug("Healthcheck solicitado")
    return {
        "status": "online",
        "service": "Prototipo de apertura de siniestros",
        "sample_rate": 16000,
    }


@router.get("/version")
async def version(
    settings: Settings = Depends(get_settings),
    logger: logging.Logger = Depends(get_logger),
) -> dict[str, str]:
    logger.debug("Versión solicitada")
    return {"version": settings.app_version}
