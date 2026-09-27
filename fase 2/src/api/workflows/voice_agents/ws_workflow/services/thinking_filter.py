import logging

from pipecat.frames.frames import Frame, TextFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class StripReasoningProcessor(FrameProcessor):
    """Filtra y elimina cualquier bloque de razonamiento (<think>...</think> o <thought>...</thought>)

    para que el TTS solo reciba la respuesta directa de forma inmediata.
    """

    def __init__(self, logger: logging.Logger):
        super().__init__()
        self.logger = logger
        self._in_thinking = False
        self._buffer = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (TextFrame, LLMTextFrame)):
            text = frame.text
            if not text:
                return

            self._buffer += text

            while self._buffer:
                if self._in_thinking:
                    # Buscamos el cierre de la etiqueta de pensamiento
                    end_think = self._buffer.find("</think>")
                    end_thought = self._buffer.find("</thought>")

                    if end_think != -1:
                        self._in_thinking = False
                        self._buffer = self._buffer[end_think + len("</think>"):]
                        self.logger.debug("Bloque <think> filtrado de la respuesta")
                    elif end_thought != -1:
                        self._in_thinking = False
                        self._buffer = self._buffer[end_thought + len("</thought>"):]
                        self.logger.debug("Bloque <thought> filtrado de la respuesta")
                    else:
                        # Todavía está dentro del bloque de pensamiento, limpiamos buffer y salimos
                        self._buffer = ""
                        return
                else:
                    start_think = self._buffer.find("<think>")
                    start_thought = self._buffer.find("<thought>")

                    # Determinar cuál aparece primero
                    earliest = -1
                    tag_len = 0
                    if start_think != -1 and (start_thought == -1 or start_think < start_thought):
                        earliest = start_think
                        tag_len = len("<think>")
                    elif start_thought != -1:
                        earliest = start_thought
                        tag_len = len("<thought>")

                    if earliest != -1:
                        clean_text = self._buffer[:earliest]
                        self._buffer = self._buffer[earliest + tag_len:]
                        self._in_thinking = True
                        if clean_text:
                            await self.push_frame(type(frame)(text=clean_text), direction)
                    else:
                        clean_text = self._buffer
                        self._buffer = ""
                        if clean_text:
                            await self.push_frame(type(frame)(text=clean_text), direction)
                        break
        else:
            await self.push_frame(frame, direction)
