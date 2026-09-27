import logging

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer


class AudioFrameSerializer(FrameSerializer):
    """Serializer for transmitting raw PCM audio frames directly over WebSocket."""

    def __init__(
        self,
        sample_rate: int = 16000,
        num_channels: int = 1,
        *,
        logger: logging.Logger,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.logger = logger
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.logger.debug(
            "Serializador de audio inicializado; %s Hz, %s canal(es)",
            sample_rate,
            num_channels,
        )

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            return InputAudioRawFrame(
                audio=data,
                sample_rate=self.sample_rate,
                num_channels=self.num_channels,
            )
        return None
