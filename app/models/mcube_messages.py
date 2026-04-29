"""MCube WebSocket protocol message types.

Defines type-safe dataclasses for all MCube WebSocket messages using
the Tagged Union pattern for protocol modeling.
"""

import json
from dataclasses import dataclass
from typing import Union, Literal, Optional

from app.models.mcube_models import McubeStartEvent


# =============================================================================
# Shared Nested Structures
# =============================================================================


@dataclass(frozen=True)
class MediaPayload:
    """Audio media payload for MCube messages.

    Attributes:
        payload: Base64-encoded audio data
        content_type: MIME type (e.g., "audio/x-mulaw")
        sample_rate: Sample rate in Hz
        name: Optional segment name for tracking
    """

    payload: str
    content_type: str
    sample_rate: int
    name: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "MediaPayload":
        """Create from MCube media object.

        Args:
            data: Media object from MCube message

        Returns:
            MediaPayload instance
        """
        return cls(
            payload=data.get("payload", ""),
            content_type=data.get("contentType", "audio/x-mulaw"),
            sample_rate=data.get("sampleRate", 8000),
            name=data.get("name"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        Returns:
            Dictionary with camelCase keys for MCube
        """
        result = {
            "payload": self.payload,
            "contentType": self.content_type,
            "sampleRate": self.sample_rate,
        }
        if self.name:
            result["name"] = self.name
        return result


# =============================================================================
# Incoming Message Types (MCube → Worker)
# =============================================================================


@dataclass(frozen=True)
class McubeStartMessage:
    """MCube start message sent at connection establishment.

    Contains only audio configuration (start). No metadata - worker fetches
    assistant/phone config from call record using call_sid from URL.
    """

    start: McubeStartEvent
    event: Literal["start"] = "start"

    @classmethod
    def from_dict(cls, data: dict) -> "McubeStartMessage":
        """Create from start message JSON.

        Args:
            data: JSON from MCube with event and start (audio config) only.
        """
        if data.get("event") != "start":
            raise ValueError(f"Expected event='start', got '{data.get('event')}'")

        if "start" not in data:
            raise ValueError("Missing 'start' field in start message")

        return cls(start=McubeStartEvent.from_dict(data))


@dataclass(frozen=True)
class McubeMediaMessage:
    """MCube media message with audio payload.

    Sent by MCube during active call with audio data.
    """

    media: MediaPayload
    event: Literal["media"] = "media"

    @classmethod
    def from_dict(cls, data: dict) -> "McubeMediaMessage":
        """Create from media message JSON.

        Args:
            data: Media message JSON from MCube

        Returns:
            McubeMediaMessage instance
        """
        return cls(
            media=MediaPayload.from_dict(data.get("media", {})),
        )


@dataclass(frozen=True)
class McubePlayedStreamMessage:
    """MCube playedStream acknowledgment message.

    Sent by MCube when an audio segment finishes playing.
    """

    stream_id: str
    name: str
    event: Literal["playedStream"] = "playedStream"

    @classmethod
    def from_dict(cls, data: dict) -> "McubePlayedStreamMessage":
        """Create from playedStream message JSON.

        Args:
            data: PlayedStream message JSON from MCube

        Returns:
            McubePlayedStreamMessage instance
        """
        return cls(
            stream_id=data.get("streamId", ""),
            name=data.get("name", ""),
        )


# =============================================================================
# Outgoing Message Types (Worker → MCube)
# =============================================================================


@dataclass(frozen=True)
class McubePlayAudioMessage:
    """MCube playAudio message to play audio.

    Sent by worker to play audio through MCube.
    """

    media: MediaPayload
    event: Literal["playAudio"] = "playAudio"

    def to_json(self) -> str:
        """Serialize to JSON string.

        Returns:
            JSON string ready for WebSocket transmission
        """
        return json.dumps(
            {
                "event": self.event,
                "media": self.media.to_dict(),
            }
        )


@dataclass(frozen=True)
class McubeCheckpointMessage:
    """MCube checkpoint message for audio segment tracking.

    Sent by worker after playAudio to track when segment completes.
    """

    stream_id: str
    name: str
    event: Literal["checkpoint"] = "checkpoint"

    def to_json(self) -> str:
        """Serialize to JSON string.

        Returns:
            JSON string ready for WebSocket transmission
        """
        return json.dumps(
            {
                "event": self.event,
                "streamId": self.stream_id,
                "name": self.name,
            }
        )


@dataclass(frozen=True)
class McubeClearAudioMessage:
    """MCube clearAudio message to clear audio queue.

    Sent by worker during interruptions to stop current audio.
    """

    stream_id: str
    event: Literal["clearAudio"] = "clearAudio"

    def to_json(self) -> str:
        """Serialize to JSON string.

        Returns:
            JSON string ready for WebSocket transmission
        """
        return json.dumps(
            {
                "event": self.event,
                "streamId": self.stream_id,
            }
        )


@dataclass(frozen=True)
class McubeTerminateMessage:
    """MCube terminate message to end call.

    Sent by worker to terminate the MCube call session.
    """

    stream_id: str
    event: Literal["terminate"] = "terminate"

    def to_json(self) -> str:
        """Serialize to JSON string.

        Returns:
            JSON string ready for WebSocket transmission
        """
        return json.dumps(
            {
                "event": self.event,
                "streamId": self.stream_id,
            }
        )


# =============================================================================
# Union Types for Type-Safe Dispatch
# =============================================================================


McubeIncomingMessage = Union[
    McubeStartMessage,
    McubeMediaMessage,
    McubePlayedStreamMessage,
]


McubeOutgoingMessage = Union[
    McubePlayAudioMessage,
    McubeCheckpointMessage,
    McubeClearAudioMessage,
    McubeTerminateMessage,
]


# =============================================================================
# Factory Functions
# =============================================================================


def parse_incoming_message(data: dict) -> McubeIncomingMessage:
    """Parse incoming MCube WebSocket message.

    Factory function that routes to appropriate message type based on event field.

    Args:
        data: Parsed JSON dict from MCube

    Returns:
        Appropriate message type instance

    Raises:
        ValueError: If event type is unknown or parsing fails
    """
    event = data.get("event", "")

    if event == "start":
        return McubeStartMessage.from_dict(data)
    elif event == "media":
        return McubeMediaMessage.from_dict(data)
    elif event == "playedStream":
        return McubePlayedStreamMessage.from_dict(data)
    else:
        raise ValueError(f"Unknown MCube incoming event type: {event}")


def create_play_audio_message(media: MediaPayload) -> McubePlayAudioMessage:
    """Create playAudio message for sending audio to MCube.

    Args:
        media: Audio payload to send

    Returns:
        McubePlayAudioMessage instance
    """
    return McubePlayAudioMessage(media=media)


def create_checkpoint_message(stream_id: str, name: str) -> McubeCheckpointMessage:
    """Create checkpoint message for audio tracking.

    Args:
        stream_id: Stream identifier
        name: Audio segment name

    Returns:
        McubeCheckpointMessage instance
    """
    return McubeCheckpointMessage(stream_id=stream_id, name=name)


def create_clear_audio_message(stream_id: str) -> McubeClearAudioMessage:
    """Create clearAudio message for interruption.

    Args:
        stream_id: Stream identifier

    Returns:
        McubeClearAudioMessage instance
    """
    return McubeClearAudioMessage(stream_id=stream_id)


def create_terminate_message(stream_id: str) -> McubeTerminateMessage:
    """Create terminate message to end call.

    Args:
        stream_id: Stream identifier

    Returns:
        McubeTerminateMessage instance
    """
    return McubeTerminateMessage(stream_id=stream_id)
