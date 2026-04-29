"""Unit tests for Twilio webhook signature validation (DAAI-137).

No IS_LOCAL bypass — signature validation is enforced in all environments.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException


class TestTwilioSignatureValidation:
    @pytest.mark.asyncio
    async def test_missing_signature_returns_403(self):
        """Request without X-Twilio-Signature header is rejected."""
        from app.utils.twilio_signature import validate_twilio_signature

        request = MagicMock()
        request.headers = {}
        request.url.path = "/twilio/incoming"
        with pytest.raises(HTTPException) as exc:
            await validate_twilio_signature(request, "auth-token")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_403(self):
        """Request with wrong signature is rejected."""
        from app.utils.twilio_signature import validate_twilio_signature

        request = MagicMock()
        request.headers = {"X-Twilio-Signature": "bad-signature"}
        request.url = MagicMock()
        request.url.path = "/twilio/incoming"
        request.form = AsyncMock(return_value={"CallSid": "CA123"})
        with patch("app.utils.twilio_signature.PUBLIC_URL", "https://example.com"):
            with pytest.raises(HTTPException) as exc:
                await validate_twilio_signature(request, "auth-token")
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_signature_passes(self):
        """Request with valid Twilio signature is accepted."""
        from twilio.request_validator import RequestValidator

        from app.utils.twilio_signature import validate_twilio_signature

        auth_token = "test-auth-token"
        url = "https://example.com/twilio/incoming"
        params = {"CallSid": "CA123", "From": "+1234567890"}
        # Generate a real valid signature
        validator = RequestValidator(auth_token)
        valid_sig = validator.compute_signature(url, params)

        request = MagicMock()
        request.headers = {"X-Twilio-Signature": valid_sig}
        request.url = MagicMock()
        request.url.path = "/twilio/incoming"
        request.form = AsyncMock(return_value=params)
        with patch("app.utils.twilio_signature.PUBLIC_URL", "https://example.com"):
            # Should not raise
            await validate_twilio_signature(request, auth_token)
