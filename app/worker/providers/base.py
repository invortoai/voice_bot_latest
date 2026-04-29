from abc import ABC, abstractmethod
from typing import Optional, Tuple

from fastapi import WebSocket


class WorkerProvider(ABC):
    """Abstract base for telephony provider-specific WebSocket handling.

    Adding a new provider:
    1. Create app/worker/providers/<name>.py implementing WorkerProvider
    2. Add 4 lines to main.py (accept + _handle_call)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier used in logging (e.g. 'twilio', 'jambonz', 'mcube')."""

    @abstractmethod
    async def parse_initial_message(
        self, websocket: WebSocket, path_call_sid: Optional[str] = None
    ) -> dict:
        """Receive and parse the initial WebSocket message(s).

        Returns a call_info dict with provider-specific metadata.
        Raise ValueError for invalid/unexpected message formats.
        """

    @abstractmethod
    def extract_call_sid(
        self, call_info: dict, path_call_sid: Optional[str] = None
    ) -> str:
        """Extract the call_sid from call_info or the URL path parameter."""

    @abstractmethod
    def build_custom_params(self, call_sid: str, call_info: dict) -> dict:
        """Build the custom_params dict for AssistantConfig when no call record exists.

        Must include at least: call_sid, call_type, caller, called, to_number.
        """

    @abstractmethod
    async def config_fallback(
        self, call_sid: str, call_info: dict
    ) -> Tuple[Optional[dict], Optional[dict]]:
        """Provider-specific config lookup when the call record is not found.

        Returns (phone_config, assistant_config). Return (None, None) if unsupported.
        """

    @abstractmethod
    def get_audio_params(self, call_info: dict) -> Tuple[int, int, str]:
        """Return (in_sample_rate, out_sample_rate, encoding) for this call.

        Called before create_transport so STT/TTS services can be configured.
        """

    @abstractmethod
    def create_transport(
        self,
        websocket: WebSocket,
        vad_analyzer,
        call_info: dict,
        config,
        in_sample_rate: int,
        out_sample_rate: int,
    ):
        """Create and return the Pipecat transport for this provider."""
