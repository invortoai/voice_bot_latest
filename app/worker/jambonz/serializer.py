import base64
import json
from typing import Optional

from loguru import logger
from pydantic import BaseModel, ConfigDict

from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartInterruptionFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class JambonzFrameSerializer(FrameSerializer):
    """Serializer for a Jambonz-style websocket audio protocol.

    Notes:
    - Incoming audio is expected as raw PCM bytes in websocket *binary* messages.
    - Outgoing audio is sent as raw PCM bytes in websocket *binary* messages.
    - Control messages (disconnect/killAudio) are sent as websocket *text* messages
      containing JSON (because FrameSerializer only supports str|bytes).
    """

    class InputParams(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        audio_in_sample_rate: Optional[int] = None
        audio_out_sample_rate: Optional[int] = None
        stt_sample_rate: Optional[int] = None
        # Jambonz listen websocket audio is commonly linear16, but some SIP legs are PCMU.
        # Allow the app to force encoding if needed.
        audio_in_encoding: str = "linear16"  # "linear16" | "mulaw"
        audio_out_encoding: str = "linear16"  # "linear16" | "mulaw"
        # If true, send bidirectional audio back as *binary frames* (raw L16 PCM),
        # as required by Jambonz when bidirectionalAudio.streaming=true.
        audio_out_streaming: bool = False
        # How much audio (ms) to buffer before emitting a `playAudio` message.
        # For Jambonz bidirectional `listen`, sending real-time 20ms frames tends
        # to behave best (matches telephony pacing).
        audio_out_buffer_ms: int = 20
        auto_hang_up: bool = True

    def __init__(self, params: InputParams):
        self._params = params
        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()
        self._play_buffer = bytearray()

    async def serialize(self, frame: Frame) -> str | bytes | None:
        # Control frames
        if isinstance(frame, (EndFrame, CancelFrame)):
            if self._params.auto_hang_up:
                return json.dumps({"type": "disconnect"})
            return None

        # Interruption: ask the far-end to stop playing pending audio
        if isinstance(frame, StartInterruptionFrame):
            self._play_buffer = bytearray()
            return json.dumps({"type": "killAudio"})

        # Audio frames
        if isinstance(frame, OutputAudioRawFrame):
            audio = frame.audio

            # Outbound: Pipecat audio is PCM s16le. Convert to requested encoding if needed.
            if (self._params.audio_out_encoding or "").lower() in (
                "mulaw",
                "pcmu",
                "ulaw",
            ):
                # Convert PCM at frame.sample_rate to 8kHz μ-law by default.
                out_rate = (
                    self._params.audio_out_sample_rate or frame.sample_rate or 8000
                )
                converted = await pcm_to_ulaw(
                    audio, frame.sample_rate, out_rate, self._output_resampler
                )
                audio = converted if converted else b""

            out_rate = self._params.audio_out_sample_rate or frame.sample_rate or 8000

            # Streaming mode: send raw audio bytes as binary frames.
            if self._params.audio_out_streaming:
                # No buffering needed; transport already paces audio.
                return audio

            # Non-streaming mode: send JSON `playAudio` messages (base64), which
            # Jambonz buffers and plays once received in full.
            self._play_buffer.extend(audio)

            # Emit buffered audio in larger chunks to reduce overhead / artifacts.
            buffer_ms = max(20, int(self._params.audio_out_buffer_ms or 20))
            bytes_per_ms = int((out_rate * 2) / 1000)  # 16-bit mono PCM
            threshold = max(320, bytes_per_ms * buffer_ms)
            if len(self._play_buffer) < threshold:
                return None

            payload_bytes = bytes(self._play_buffer[:threshold])
            self._play_buffer = self._play_buffer[threshold:]

            audio_b64 = base64.b64encode(payload_bytes).decode("ascii")

            audio_content_type = "raw"
            if (self._params.audio_out_encoding or "").lower() in (
                "mulaw",
                "pcmu",
                "ulaw",
            ):
                # Documented by Jambonz as a supported payload type in some deployments.
                audio_content_type = "mulaw"

            logger.debug(
                f"Jambonz playAudio: bytes={len(payload_bytes)} rate={out_rate} type={audio_content_type}"
            )

            return json.dumps(
                {
                    "type": "playAudio",
                    "data": {
                        "audioContent": audio_b64,
                        "audioContentType": audio_content_type,
                        # Some Jambonz deployments validate this as a number.
                        "sampleRate": out_rate,
                    },
                }
            )

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        # Audio in
        if isinstance(data, (bytes, bytearray)):
            sample_rate = self._params.audio_in_sample_rate or 8000
            audio_bytes = bytes(data)

            # Inbound: if μ-law, decode to PCM for pipecat/STT.
            if (self._params.audio_in_encoding or "").lower() in (
                "mulaw",
                "pcmu",
                "ulaw",
            ):
                in_rate = self._params.audio_in_sample_rate or 8000
                out_rate = self._params.stt_sample_rate or in_rate
                decoded = await ulaw_to_pcm(
                    audio_bytes, in_rate, out_rate, self._input_resampler
                )
                audio_bytes = decoded if decoded else b""
                sample_rate = out_rate

            return InputAudioRawFrame(
                audio=audio_bytes,
                num_channels=1,
                sample_rate=sample_rate,
            )

        # Optional control messages inbound from Jambonz-style client
        try:
            msg = json.loads(data)
        except Exception:
            return None

        msg_type = msg.get("type")
        if msg_type == "disconnect":
            return EndFrame()
        # Jambonz emits `playDone` after a playAudio finishes; we don't need to act on it.
        if msg_type == "playDone":
            return None

        return None
