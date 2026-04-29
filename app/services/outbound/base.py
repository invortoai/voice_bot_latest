from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OutboundCallResult:
    """Result of initiating an outbound call with a provider."""

    call_sid: str
    from_number: str


class OutboundProvider(ABC):
    """Abstract base for provider-specific outbound call initiation."""

    @abstractmethod
    def validate_credentials(self, phone_config: dict) -> None:
        """Validate provider credentials before worker assignment.

        Raises HTTPException if credentials are missing or invalid.
        Called before a worker is reserved — fail fast.
        """
        ...

    @abstractmethod
    async def initiate(
        self,
        call_id: str,
        phone_config: dict,
        assistant_config: dict,
        worker,
        to_number: str,
        custom_params: Optional[dict],
    ) -> OutboundCallResult:
        """Initiate an outbound call and return the provider call_sid + from_number.

        Raises HTTPException on failure. Does NOT release workers — the caller handles cleanup.
        """
        ...
