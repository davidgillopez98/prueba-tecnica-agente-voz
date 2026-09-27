import logging

from fastapi import APIRouter, Depends, WebSocket

from src.api.common.logger import get_logger
from src.api.workflows.voice_agents.ws_workflow.workflow import VoiceAgentWorkflow
from src.settings import Settings, get_settings

router = APIRouter(tags=["voice_agents"])


@router.websocket("/ws")
async def voice_agent(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),
    logger: logging.Logger = Depends(get_logger),
) -> None:
    if not settings.deepgram_api_key:
        await websocket.close(code=1013, reason="Canal de voz opcional no configurado; use demo.py")
        return
    logger.info("Nueva conexión WebSocket de voz")
    workflow = VoiceAgentWorkflow(logger=logger, db_path=settings.claims_db_path)
    try:
        await workflow.run(
            websocket,
            deepgram_api_key=settings.deepgram_api_key,
            llm_model=settings.llm_model,
            ollama_endpoint=settings.ollama_endpoint,
        )
    finally:
        logger.info("Conexión WebSocket de voz finalizada")
