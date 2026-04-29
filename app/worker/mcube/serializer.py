import base64
import json
import uuid
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


class McubeFrameSerializer(FrameSerializer):
    """Serializer for MCube WebSocket audio protocol.

    Handles:
    - Incoming: media, playedStream events
    - Outgoing: playAudio, checkpoint, clearAudio, terminate events

    Audio formats:
    - mulaw: audio/x-mulaw @ 8kHz
    - linear16: audio/x-l16 @ 8kHz or 16kHz
    """

    class InputParams(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        audio_in_sample_rate: Optional[int] = 8000
        audio_out_sample_rate: Optional[int] = 8000
        stt_sample_rate: Optional[int] = None
        audio_in_encoding: str = "mulaw"  # "mulaw" or "linear16"
        audio_out_encoding: str = "mulaw"  # "mulaw" or "linear16"
        stream_id: Optional[str] = None
        auto_hang_up: bool = True

    def __init__(self, params: InputParams):
        self._params = params
        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()
        self._audio_segment_names: set[str] = set()  # Track in-flight audio segments

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Convert Pipecat frames to MCube messages using typed message classes."""
        from app.models.mcube_messages import (
            create_terminate_message,
            create_clear_audio_message,
            create_play_audio_message,
            create_checkpoint_message,
            MediaPayload,
        )

        # Control frames - terminate call
        if isinstance(frame, (EndFrame, CancelFrame)):
            if self._params.auto_hang_up and self._params.stream_id:
                logger.debug(
                    f"Sending terminate event: stream_id={self._params.stream_id}"
                )
                msg = create_terminate_message(self._params.stream_id)
                return msg.to_json()
            return None

        # Interruption - clear audio queue
        if isinstance(frame, StartInterruptionFrame):
            if self._params.stream_id:
                logger.debug(
                    f"Sending clearAudio event: stream_id={self._params.stream_id}"
                )
                msg = create_clear_audio_message(self._params.stream_id)
                return msg.to_json()
            return None

        # Audio frames - send as playAudio + checkpoint
        if isinstance(frame, OutputAudioRawFrame):
            audio = frame.audio
            out_rate = self._params.audio_out_sample_rate or frame.sample_rate or 8000

            # Convert to target encoding
            if self._params.audio_out_encoding == "mulaw":
                converted = await pcm_to_ulaw(
                    audio, frame.sample_rate, out_rate, self._output_resampler
                )
                audio = converted if converted else b""
                content_type = "audio/x-mulaw"
            else:  # linear16
                # Linear16 is raw PCM - might need resampling
                if frame.sample_rate != out_rate:
                    # TODO: Add resampling for linear16 if sample rates differ
                    logger.warning(
                        f"Sample rate mismatch: frame={frame.sample_rate}, "
                        f"target={out_rate}. Resampling not implemented for linear16."
                    )
                content_type = "audio/x-l16"

            if not audio:
                return None

            # Generate unique name for this audio segment
            segment_name = str(uuid.uuid4())
            self._audio_segment_names.add(segment_name)

            # Encode to base64
            audio_b64 = base64.b64encode(audio).decode("ascii")

            # Create playAudio message using typed dataclass
            media = MediaPayload(
                payload=audio_b64,
                content_type=content_type,
                sample_rate=out_rate,
                name=segment_name,
            )
            play_audio_msg = create_play_audio_message(media)

            # Create checkpoint message using typed dataclass
            checkpoint_msg = create_checkpoint_message(
                stream_id=self._params.stream_id,
                name=segment_name,
            )

            # Return both messages separated by newline
            # Transport will send them as separate text messages
            return f"{play_audio_msg.to_json()}\n{checkpoint_msg.to_json()}"

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Convert MCube messages to Pipecat frames using typed message classes.

        Note: The initial "start" event is handled in main.py BEFORE creating the pipeline,
        similar to how Jambonz handles initial metadata. This method only handles
        ongoing audio and control messages during the active call.
        """
        from app.models.mcube_messages import (
            parse_incoming_message,
            McubeMediaMessage,
            McubePlayedStreamMessage,
        )

        # Binary audio data
        if isinstance(data, (bytes, bytearray)):
            sample_rate = self._params.audio_in_sample_rate or 8000
            audio_bytes = bytes(data)

            # Decode if mulaw
            if self._params.audio_in_encoding == "mulaw":
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

        # JSON control messages - parse using typed message classes
        try:
            msg_dict = json.loads(data)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON message: {data[:100]}")
            return None

        try:
            # Parse to typed message using factory function
            message = parse_incoming_message(msg_dict)
        except ValueError as e:
            # Includes "start" event (handled in main.py) and unknown events
            logger.debug(f"Skipping message during deserialize: {e}")
            return None

        # media event - audio payload in base64
        if isinstance(message, McubeMediaMessage):
            payload_b64 = message.media.payload

            try:
                audio_bytes = base64.b64decode(payload_b64)
                sample_rate = self._params.audio_in_sample_rate or 8000

                # Decode if mulaw
                if self._params.audio_in_encoding == "mulaw":
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
            except Exception as e:
                logger.error(f"Failed to decode media payload: {e}")
                return None

        # playedStream event - acknowledgment that audio finished playing
        # Sent by MCube when the audio segment we sent has finished playing
        if isinstance(message, McubePlayedStreamMessage):
            name = message.name
            if name:
                self._audio_segment_names.discard(name)
            # This is informational only - no action needed
            return None

        # All other message types are handled elsewhere
        return None
