"""Unit tests for outbound provider registry and credential validation.

Covers:
- get_provider() returning correct provider instances
- Unknown providers raising HTTPException 400
- TwilioOutboundProvider credential validation
"""

import pytest
from fastapi import HTTPException

from app.services.outbound.registry import get_provider
from app.services.outbound.twilio import TwilioOutboundProvider
from app.services.outbound.jambonz import JambonzOutboundProvider
from app.services.outbound.mcube import McubeOutboundProvider


class TestGetProvider:
    def test_twilio_returns_twilio_provider_instance(self):
        provider = get_provider("twilio")
        assert isinstance(provider, TwilioOutboundProvider)

    def test_jambonz_returns_jambonz_provider_instance(self):
        provider = get_provider("jambonz")
        assert isinstance(provider, JambonzOutboundProvider)

    def test_mcube_returns_mcube_provider_instance(self):
        provider = get_provider("mcube")
        assert isinstance(provider, McubeOutboundProvider)

    def test_unknown_provider_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            get_provider("vonage")
        assert exc_info.value.status_code == 400
        assert "vonage" in exc_info.value.detail

    def test_empty_string_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            get_provider("")
        assert exc_info.value.status_code == 400

    def test_uppercase_provider_name_raises_400(self):
        """Provider names are case-sensitive — 'Twilio' != 'twilio'."""
        with pytest.raises(HTTPException):
            get_provider("Twilio")

    def test_error_detail_lists_supported_providers(self):
        with pytest.raises(HTTPException) as exc_info:
            get_provider("unknown-provider")
        detail = exc_info.value.detail
        assert "twilio" in detail
        assert "jambonz" in detail
        assert "mcube" in detail


class TestTwilioValidateCredentials:
    """TwilioOutboundProvider.validate_credentials() must fail fast on bad creds."""

    def _provider(self):
        return get_provider("twilio")

    def test_valid_credentials_passes(self):
        """Both account_sid and auth_token present → no exception."""
        self._provider().validate_credentials(
            {
                "provider_credentials": {
                    "account_sid": "AC123456789",
                    "auth_token": "auth-token-xyz",
                }
            }
        )

    def test_missing_account_sid_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            self._provider().validate_credentials(
                {"provider_credentials": {"auth_token": "token"}}
            )
        assert exc_info.value.status_code == 400

    def test_missing_auth_token_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            self._provider().validate_credentials(
                {"provider_credentials": {"account_sid": "AC123"}}
            )
        assert exc_info.value.status_code == 400

    def test_empty_credentials_dict_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            self._provider().validate_credentials({"provider_credentials": {}})
        assert exc_info.value.status_code == 400

    def test_none_credentials_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            self._provider().validate_credentials({"provider_credentials": None})
        assert exc_info.value.status_code == 400

    def test_empty_account_sid_string_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            self._provider().validate_credentials(
                {"provider_credentials": {"account_sid": "", "auth_token": "token"}}
            )
        assert exc_info.value.status_code == 400

    def test_empty_auth_token_string_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            self._provider().validate_credentials(
                {"provider_credentials": {"account_sid": "AC123", "auth_token": ""}}
            )
        assert exc_info.value.status_code == 400
