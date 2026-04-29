from fastapi import HTTPException

from app.services.outbound.base import OutboundProvider
from app.services.outbound.jambonz import JambonzOutboundProvider
from app.services.outbound.mcube import McubeOutboundProvider
from app.services.outbound.twilio import TwilioOutboundProvider

_PROVIDERS: dict[str, OutboundProvider] = {
    "twilio": TwilioOutboundProvider(),
    "jambonz": JambonzOutboundProvider(),
    "mcube": McubeOutboundProvider(),
}


def get_provider(provider_name: str) -> OutboundProvider:
    """Return the OutboundProvider for the given provider name.

    Raises:
        HTTPException 400: if the provider is not supported.
    """
    provider = _PROVIDERS.get(provider_name)
    if not provider:
        supported = list(_PROVIDERS.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported telephony provider: '{provider_name}'. Supported: {supported}",
        )
    return provider
