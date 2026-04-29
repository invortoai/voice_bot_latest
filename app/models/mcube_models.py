"""Shared dataclass models for MCube integration.

These models provide type-safe data structures used across runner and worker
components for MCube telephony integration.

Note: This file contains nested structures and metadata classes. For complete
WebSocket protocol message types (start, media, playedStream, playAudio, etc.),
see app/models/mcube_messages.py which builds upon these base structures.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Literal


@dataclass(frozen=True)
class CallMetadata:
    """Call metadata passed from runner to worker via WebSocket.

    This is sent in the initial WebSocket message from runner to worker
    and contains essential call information for pipeline setup.

    Attributes:
        call_sid: Unique call identifier
        caller: Caller phone number (E.164 format)
        called: Called phone number (E.164 format)
        call_type: Direction of call ("inbound" or "outbound")
        assistant_id: Optional assistant ID for configuration lookup
    """

    call_sid: str
    caller: str
    called: str
    call_type: Literal["inbound", "outbound"]
    assistant_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        Returns:
            Dictionary with all non-None fields
        """
        result = {
            "call_sid": self.call_sid,
            "caller": self.caller,
            "called": self.called,
            "call_type": self.call_type,
        }
        if self.assistant_id:
            result["assistant_id"] = self.assistant_id
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "CallMetadata":
        """Create from dictionary (received from WebSocket).

        Args:
            data: Dictionary containing call metadata fields

        Returns:
            CallMetadata instance with extracted fields
        """
        return cls(
            call_sid=data.get("call_sid", ""),
            caller=data.get("caller", ""),
            called=data.get("called", ""),
            call_type=data.get("call_type", "inbound"),
            assistant_id=data.get("assistant_id"),
        )


@dataclass(frozen=True)
class MediaFormat:
    """Audio media format configuration.

    Attributes:
        encoding: MIME type encoding (e.g., "audio/x-mulaw", "audio/x-l16")
        sample_rate: Audio sample rate in Hz (e.g., 8000, 16000)
    """

    encoding: str
    sample_rate: int

    @property
    def encoding_type(self) -> Literal["mulaw", "linear16"]:
        """Get simplified encoding type.

        Returns:
            "mulaw" if encoding contains "mulaw", otherwise "linear16"
        """
        if "mulaw" in self.encoding.lower():
            return "mulaw"
        return "linear16"


@dataclass(frozen=True)
class McubeStartEvent:
    """MCube WebSocket start event structure.

    Sent by MCube at the beginning of a WebSocket connection
    to provide audio configuration and call identifiers.

    Attributes:
        call_id: MCube call identifier
        stream_id: Optional stream identifier
        tracks: Audio tracks (typically ["inbound", "outbound"])
        media_format: Audio format configuration
    """

    call_id: str
    stream_id: Optional[str] = None
    tracks: List[str] = field(default_factory=lambda: ["inbound", "outbound"])
    media_format: Optional[MediaFormat] = None

    @classmethod
    def from_dict(cls, data: dict) -> "McubeStartEvent":
        """Create from MCube start event JSON.

        Args:
            data: Complete call_info dict containing "start" nested object

        Returns:
            McubeStartEvent instance parsed from start data
        """
        start_data = data.get("start", {})
        media_format_dict = start_data.get("mediaFormat", {})

        media_format = (
            MediaFormat(
                encoding=media_format_dict.get("encoding", "audio/x-mulaw"),
                sample_rate=media_format_dict.get("sampleRate", 8000),
            )
            if media_format_dict
            else None
        )

        return cls(
            call_id=start_data.get("callId", ""),
            stream_id=start_data.get("streamId"),
            tracks=start_data.get("tracks", ["inbound", "outbound"]),
            media_format=media_format,
        )


@dataclass
class McubeProviderMetadata:
    """Metadata stored in database for MCube calls.

    Stored in calls.provider_metadata JSONB field.
    Mutable because it accumulates data throughout call lifecycle.

    Attributes:
        group_name: MCube call group name
        agent_name: Name of the agent handling the call
        start_time: Call start timestamp
        end_time: Call end timestamp
        recording_url: URL to call recording
        disconnected_by: Who disconnected the call (Customer/Agent)
    """

    group_name: Optional[str] = None
    agent_name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    recording_url: Optional[str] = None
    disconnected_by: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dict, excluding None values.

        Returns:
            Dictionary with only non-None fields
        """
        return {
            k: v
            for k, v in {
                "group_name": self.group_name,
                "agent_name": self.agent_name,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "recording_url": self.recording_url,
                "disconnected_by": self.disconnected_by,
            }.items()
            if v is not None
        }

    def update_from_payload(self, payload) -> None:
        """Update fields from MCube webhook payload.

        Args:
            payload: McubeWebhookRequest instance
        """
        if payload.groupname:
            self.group_name = payload.groupname
        if payload.agentname:
            self.agent_name = payload.agentname
        if payload.starttime:
            self.start_time = payload.starttime
        if payload.endtime:
            self.end_time = payload.endtime
        if payload.filename:
            self.recording_url = payload.filename
        if payload.disconnectedby:
            self.disconnected_by = payload.disconnectedby

    @classmethod
    def from_payload(cls, payload) -> "McubeProviderMetadata":
        """Create from MCube webhook payload.

        Args:
            payload: McubeWebhookRequest instance

        Returns:
            McubeProviderMetadata instance with fields from payload
        """
        return cls(
            group_name=payload.groupname,
            agent_name=payload.agentname,
            start_time=payload.starttime,
            end_time=payload.endtime,
            recording_url=payload.filename,
            disconnected_by=payload.disconnectedby,
        )
