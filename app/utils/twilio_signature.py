"""Twilio webhook signature validation.

Validates the X-Twilio-Signature header to ensure requests originate from
Twilio and have not been tampered with.  Enforced in all environments.
"""

from fastapi import HTTPException, Request, status
from loguru import logger
from twilio.request_validator import RequestValidator

from app.config import PUBLIC_URL


async def validate_twilio_signature(request: Request, auth_token: str) -> None:
    """Validate X-Twilio-Signature on an incoming webhook request.

    Args:
        request: The incoming FastAPI request.
        auth_token: The Twilio auth token for the phone number account.

    Raises:
        HTTPException: 403 if signature is missing or invalid.
    """
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing Twilio signature",
        )

    # Reconstruct the URL Twilio used to compute the signature.
    # If we're behind a reverse proxy/ngrok, use PUBLIC_URL as the base.
    if PUBLIC_URL:
        url = PUBLIC_URL.rstrip("/") + request.url.path
    else:
        url = str(request.url)

    form_data = await request.form()
    params = {k: str(v) for k, v in form_data.items()}

    validator = RequestValidator(auth_token)
    if not validator.validate(url, params, signature):
        logger.warning(f"Twilio signature validation failed for {request.url.path}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Twilio signature",
        )
