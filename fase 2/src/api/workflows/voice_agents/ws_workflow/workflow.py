"""Pipeline Pipecat para una sesión WebSocket de voz."""

import logging
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.flows.config import FlowConfig
from pipecat.flows.flow import Flow
from pipecat.flows.manager import FlowManager
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.text.transforms.strip_markdown import strip_markdown
from pipecat.workers.runner import WorkerRunner

from src.api.workflows.voice_agents.ws_workflow.schemas import (
    SAMPLE_RATE,
    VoiceAgentConfig,
)
from src.api.workflows.voice_agents.ws_workflow.claims import ClaimsService
from src.api.workflows.voice_agents.ws_workflow.mocks.services import MockInsuranceServices
from src.api.workflows.voice_agents.ws_workflow.mocks.repository import ClaimRepository
from src.api.workflows.voice_agents.ws_workflow.services.interruption_handler import (
    ContextInterruptionHandler,
)
from src.api.workflows.voice_agents.ws_workflow.services.serializer import (
    AudioFrameSerializer,
)
from src.api.workflows.voice_agents.ws_workflow.services.thinking_filter import (
    StripReasoningProcessor,
)
from src.api.workflows.voice_agents.ws_workflow.tools import FlowTools

FLOW_FILE = Path(__file__).with_name("flow.json")
GREETING = "Hola, soy tu asistente de IA para la apertura de siniestros. Para empezar, dime tu DNI sintético."
SYSTEM_INSTRUCTION = (
    "Eres un asistente de IA para apertura de siniestros ficticios. "
    "Evita incluir emoticonos, emojis o formato markdown en tus respuestas. "
    "La identidad, cobertura y alta se deciden exclusivamente mediante herramientas. "
    "Si el usuario pide hablar con una persona, llama a derivar_a_humano. "
    "No repitas códigos OTP ni identificadores internos de usuario o póliza. "
    "Tras el ACK del guardado, comunica el identificador completo del parte. "
    "No incluyas etiquetas <think> o <thought>. Da la respuesta final de inmediato."
)


class VoiceAgentWorkflow:
    """Construye y ejecuta una sesión independiente del agente."""

    def __init__(self, logger: logging.Logger, db_path: Path = Path("claims.sqlite3")):
        self.logger = logger
        self.db_path = db_path

    async def run(
        self,
        websocket: WebSocket,
        *,
        deepgram_api_key: str,
        llm_model: str,
        ollama_endpoint: str,
    ) -> None:
        logger = self.logger
        await websocket.accept()
        logger.info("WebSocket aceptado; modelo=%s, frecuencia=%s Hz", llm_model, SAMPLE_RATE)

        config = VoiceAgentConfig(
            deepgram_api_key=deepgram_api_key,
            llm_model=llm_model,
            ollama_endpoint=ollama_endpoint,
        )
        logger.debug("Creando transporte, VAD, STT, LLM y TTS")
        service = ClaimsService(MockInsuranceServices(), ClaimRepository(self.db_path), logger)
        tools = FlowTools(service, logger=logger)

        vad = SileroVADAnalyzer(
            params=VADParams(
                confidence=0.7,
                start_secs=0.2,
                stop_secs=0.6,
                min_volume=0.4,
            )
        )
        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                serializer=AudioFrameSerializer(
                    sample_rate=SAMPLE_RATE, num_channels=1, logger=logger
                ),
            ),
        )
        stt = DeepgramSTTService(
            api_key=config.deepgram_api_key,
            settings=DeepgramSTTService.Settings(model="nova-3", language="es"),
            sample_rate=SAMPLE_RATE,
        )
        llm = OLLamaLLMService(
            base_url=config.ollama_endpoint,
            settings=OLLamaLLMService.Settings(
                model=config.llm_model,
                system_instruction=SYSTEM_INSTRUCTION,
            ),
        )
        tts = DeepgramTTSService(
            api_key=config.deepgram_api_key,
            settings=DeepgramTTSService.Settings(voice="aura-2-diana-es", speed=1.5),
            sample_rate=SAMPLE_RATE,
            encoding="linear16",
            text_transforms=[("*", strip_markdown)],
        )
        context = LLMContext()
        context_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.8)]
                ),
            ),
        )
        pipeline = Pipeline(
            [
                transport.input(),
                VADProcessor(vad_analyzer=vad),
                stt,
                context_aggregator.user(),
                llm,
                StripReasoningProcessor(logger=logger),
                tts,
                transport.output(),
                context_aggregator.assistant(),
                ContextInterruptionHandler(
                    context=context, words_per_second=3.8, logger=logger
                ),
            ]
        )
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=SAMPLE_RATE,
                audio_out_sample_rate=SAMPLE_RATE,
            ),
        )
        logger.debug("Pipeline Pipecat creado")
        flow = Flow(
            FlowConfig.from_file(str(FLOW_FILE)),
            handlers={
                "recuperar_datos_usuario": tools.recuperar_datos_usuario,
                "generar_otp": tools.generar_otp,
                "validar_otp": tools.validar_otp,
                "categorizar_incidente": tools.categorizar_incidente,
                "recuperar_poliza": tools.recuperar_poliza,
                "crear_parte": tools.crear_parte,
            },
        )
        logger.info("Flujo conversacional cargado desde %s", FLOW_FILE)
        flow_manager = FlowManager(
            llm=llm,
            context_aggregator=context_aggregator,
            worker=worker,
            transport=transport,
        )

        @transport.event_handler("on_client_connected")
        async def on_client_connected(_transport, _client):
            logger.info("Cliente conectado. Inicializando flujo conversacional")
            flow_manager.state["welcome_message"] = GREETING
            await flow_manager.initialize(flow.initial_node)
            logger.info("Flujo conversacional inicializado")

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport, _client):
            logger.info("Cliente desconectado")
            await worker.cancel()

        # Uvicorn debe conservar el control de SIGINT para que Ctrl+C pare
        # el servidor completo y no solo el worker de Pipecat.
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        try:
            logger.info("Ejecutando pipeline de voz")
            await runner.run()
        except WebSocketDisconnect:
            logger.info("WebSocket cerrado por el cliente")
        except Exception:
            logger.exception("Error en la sesión de Pipecat")
            raise
        finally:
            logger.info("Sesión de Pipecat finalizada")
