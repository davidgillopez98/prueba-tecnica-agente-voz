"""Servicios deterministas que intercambian frames con Pipecat real.

Solo se sustituyen proveedores externos y la detección de voz para aceptar
audio sintético. Flow, FlowManager, Pipeline, PipelineWorker, WorkerRunner,
agregadores y transporte WebSocket siguen siendo los de Pipecat.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipecat.frames.frames import (
    Frame,
    FunctionCallFromLLM,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    TTSAudioRawFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.workers.runner import WorkerRunner

from src.api.workflows.voice_agents.ws_workflow import workflow as workflow_module


@dataclass(frozen=True)
class LLMAction:
    """Respuesta textual o llamada a una tool para una inferencia."""

    text: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceScenario:
    transcripts: dict[bytes, str]
    llm_actions: tuple[LLMAction, ...]


class MockVoiceServices:
    """Instala dobles de audio/LLM; mantiene transporte, Flow y tools reales."""

    def __init__(self, monkeypatch, output_dir: Path, scenario: VoiceScenario):
        self.scenario = scenario
        self.output_dir = output_dir
        self.transcripts: list[str] = []
        self.llm_steps: list[LLMAction] = []
        self.spoken: list[str] = []
        self.tool_calls: list[str] = []
        self.navigation_calls: list[str] = []
        self.context_snapshots: list[str] = []
        self.manager = None
        self.stt = None
        self.llm = None
        self.tts = None
        harness = self

        stt_settings = workflow_module.DeepgramSTTService.Settings
        llm_settings = workflow_module.OLLamaLLMService.Settings
        tts_settings = workflow_module.DeepgramTTSService.Settings

        class SyntheticVADAnalyzer:
            def __init__(self, params):
                self.params = params

        class SyntheticInputProcessor(FrameProcessor):
            def __init__(self, *, vad_analyzer):
                super().__init__()

            async def process_frame(self, frame: Frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                await self.push_frame(frame, direction)

        class MockSTT(FrameProcessor):
            Settings = stt_settings

            def __init__(self, *, api_key, settings, sample_rate):
                super().__init__()
                self.api_key = api_key
                self.settings = settings
                self.sample_rate = sample_rate
                harness.stt = self

            async def process_frame(self, frame: Frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                if isinstance(frame, InputAudioRawFrame):
                    transcript = harness.scenario.transcripts[frame.audio]
                    harness.transcripts.append(transcript)
                    await self.push_frame(
                        TranscriptionFrame(
                            text=transcript,
                            user_id="synthetic-user",
                            timestamp="2026-01-01T00:00:00Z",
                            finalized=True,
                        )
                    )
                else:
                    await self.push_frame(frame, direction)

        class MockLLM(LLMService):
            Settings = llm_settings

            def __init__(self, *, base_url, settings):
                super().__init__(settings=settings)
                self.base_url = base_url
                harness.llm = self

            async def process_frame(self, frame: Frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                if not isinstance(frame, LLMContextFrame):
                    await self.push_frame(frame, direction)
                    return

                index = len(harness.llm_steps)
                if index >= len(harness.scenario.llm_actions):
                    raise AssertionError("Pipecat solicitó una inferencia LLM no prevista")
                action = harness.scenario.llm_actions[index]
                harness.llm_steps.append(action)
                harness.context_snapshots.append(str(frame.context.messages))
                await self.push_frame(LLMFullResponseStartFrame())
                if action.text is not None:
                    await self.push_frame(LLMTextFrame(action.text))
                if action.tool is not None:
                    assert action.tool in self._functions, (
                        f"La tool {action.tool} no está registrada en este nodo"
                    )
                    if action.tool in {"confirmar_resumen", "volver_a_comprobacion", "derivar_a_humano"}:
                        harness.navigation_calls.append(action.tool)
                    await self.run_function_calls(
                        [
                            FunctionCallFromLLM(
                                function_name=action.tool,
                                tool_call_id=f"scripted-tool-{index}",
                                arguments=action.arguments,
                                context=frame.context,
                            )
                        ]
                    )
                await self.push_frame(LLMFullResponseEndFrame())

        class MockTTS(FrameProcessor):
            Settings = tts_settings

            def __init__(self, *, api_key, settings, sample_rate, encoding, text_transforms):
                super().__init__()
                self.api_key = api_key
                self.settings = settings
                self.sample_rate = sample_rate
                self.encoding = encoding
                harness.tts = self

            async def process_frame(self, frame: Frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                await self.push_frame(frame, direction)
                if isinstance(frame, (LLMTextFrame, TTSSpeakFrame)):
                    harness.spoken.append(frame.text)
                    # El transporte real agrupa 40 ms de PCM (1280 bytes a
                    # 16 kHz/16 bit); un frame menor quedaría en su buffer.
                    audio = b"VOICE:" + frame.text.encode("utf-8")
                    audio = audio.ljust(self.sample_rate * 2 * 4 // 100, b"\x00")
                    await self.push_frame(
                        TTSAudioRawFrame(
                            audio=audio,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                        )
                    )

        real_tools = workflow_module.FlowTools
        real_manager = workflow_module.FlowManager

        class RecordingFlowManager(real_manager):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                harness.manager = self

        class RecordingFlowTools(real_tools):
            def __init__(self, service, logger):
                super().__init__(service, logger)

            async def recuperar_datos_usuario(self, flow_manager, document_id):
                harness.manager = flow_manager
                harness.tool_calls.append("recuperar_datos_usuario")
                return await super().recuperar_datos_usuario(flow_manager, document_id)

            async def generar_otp(self, flow_manager):
                harness.tool_calls.append("generar_otp")
                return await super().generar_otp(flow_manager)

            async def validar_otp(self, flow_manager, verification_value):
                harness.tool_calls.append("validar_otp")
                return await super().validar_otp(flow_manager, verification_value)

            async def categorizar_incidente(self, flow_manager, **kwargs):
                harness.tool_calls.append("categorizar_incidente")
                return await super().categorizar_incidente(flow_manager, **kwargs)

            async def recuperar_poliza(self, flow_manager):
                harness.tool_calls.append("recuperar_poliza")
                return await super().recuperar_poliza(flow_manager)

            async def crear_parte(self, flow_manager):
                harness.tool_calls.append("crear_parte")
                return await super().crear_parte(flow_manager)

        monkeypatch.setattr(workflow_module, "SileroVADAnalyzer", SyntheticVADAnalyzer)
        monkeypatch.setattr(workflow_module, "VADProcessor", SyntheticInputProcessor)
        monkeypatch.setattr(workflow_module, "DeepgramSTTService", MockSTT)
        monkeypatch.setattr(workflow_module, "OLLamaLLMService", MockLLM)
        monkeypatch.setattr(workflow_module, "DeepgramTTSService", MockTTS)
        monkeypatch.setattr(workflow_module, "FlowTools", RecordingFlowTools)
        monkeypatch.setattr(workflow_module, "FlowManager", RecordingFlowManager)
        # TestClient ejecuta ASGI fuera del hilo principal; las señales solo
        # pueden registrarse en el hilo principal. El runner sigue siendo real.
        monkeypatch.setattr(WorkerRunner, "_setup_sigint", lambda self: None)
