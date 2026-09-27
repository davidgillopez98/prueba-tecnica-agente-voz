import logging
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class ContextInterruptionHandler(FrameProcessor):
    """Procesador que ajusta el contexto conversacional ante interrupciones (barge-in).

    Deepgram Aura WebSocket TTS no emite marcas de tiempo por palabra (word timestamps).
    Por ello, Pipecat vuelca la respuesta completa del LLM en context.messages en cuanto
    termina la inferencia (<500ms), mucho antes de que el usuario termine de escuchar el audio.

    Este procesador cronometra la locución real a partir de BotStartedSpeakingFrame y,
    si se produce una InterruptionFrame, calcula cuántas palabras le dio tiempo a escuchar
    al usuario y trunca el mensaje del asistente en el contexto in-place, añadiendo una
    marca explícita de interrupción.
    """

    def __init__(
        self,
        context: LLMContext,
        logger: logging.Logger,
        words_per_second: float = 3.8,
    ):
        super().__init__()
        self.logger = logger
        self._context = context
        self._words_per_second = words_per_second
        self._bot_speaking = False
        self._speech_start_time = None
        self._llm_response_completed = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._llm_response_completed = False

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm_response_completed = True

        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._speech_start_time = time.monotonic()

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._speech_start_time = None
            self._llm_response_completed = False

        elif isinstance(frame, (CancelFrame, EndFrame)):
            self._bot_speaking = False
            self._speech_start_time = None
            self._llm_response_completed = False

        elif isinstance(frame, InterruptionFrame):
            # Solo actuamos si el bot estaba hablando o si el LLM ya había metido su respuesta al contexto
            if (self._bot_speaking or self._llm_response_completed) and self._context.messages:
                last_msg = self._context.messages[-1]
                # Verificamos que sea un mensaje de texto del asistente (no una llamada a herramienta)
                if (
                    last_msg.get("role") == "assistant"
                    and isinstance(last_msg.get("content"), str)
                    and not last_msg.get("tool_calls")
                ):
                    full_text = last_msg["content"].strip()
                    if full_text:
                        if self._bot_speaking and self._speech_start_time:
                            duration = max(0.0, time.monotonic() - self._speech_start_time)
                            words = full_text.split()
                            spoken_count = max(0, int(duration * self._words_per_second))

                            if spoken_count < len(words):
                                spoken_text = " ".join(words[:spoken_count]).rstrip(",;:.!?")
                                if spoken_text:
                                    new_content = f"{spoken_text}... [interrumpido por el usuario]"
                                else:
                                    new_content = "[el bot fue interrumpido al empezar a hablar]"
                                self.logger.info(
                                    "Bot interrumpido tras %.2fs; palabras estimadas=%s/%s",
                                    duration, spoken_count, len(words),
                                )
                                last_msg["content"] = new_content
                            else:
                                new_content = f"{full_text}... [interrumpido al final]"
                                last_msg["content"] = new_content
                        elif self._llm_response_completed and not self._bot_speaking:
                            self.logger.info("Bot interrumpido antes de comenzar la reproducción de audio")
                            last_msg["content"] = (
                                "[el usuario interrumpió antes de que el asistente pudiera comenzar a hablar]"
                            )

            self._bot_speaking = False
            self._speech_start_time = None
            self._llm_response_completed = False

        await self.push_frame(frame, direction)
