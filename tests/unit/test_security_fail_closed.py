"""Comprehensive tests for all fail-closed security guards.

Covers every loophole found during the security audit:
  Loophole #2 — Jambonz webhook auth fails open when secret not configured
  Loophole #5 — Twilio signature validation skipped when auth_token missing
  A1 — /prewarm/reassign endpoint missing auth dependency

No IS_LOCAL bypasses exist — all checks are enforced in every environment.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException


# =============================================================================
# Loophole #2 — Jambonz webhook auth fail-closed
# =============================================================================


class TestJambonzWebhookFailClosed:
    """_verify_jambonz_webhook must reject when secret is empty or auth is wrong."""

    def test_empty_secret_raises_503(self):
        """Empty JAMBONZ_WEBHOOK_SECRET = 503."""
        from app.routes.jambonz import _verify_jambonz_webhook

        request = MagicMock()
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", ""):
            with pytest.raises(HTTPException) as exc:
                _verify_jambonz_webhook(request)
            assert exc.value.status_code == 503

    def test_wrong_password_returns_403(self):
        import base64
        from app.routes.jambonz import _verify_jambonz_webhook

        request = MagicMock()
        request.headers = {
            "Authorization": "Basic " + base64.b64encode(b"user:wrong-pass").decode()
        }
        request.client = MagicMock(host="1.2.3.4")
        request.method = "POST"
        request.url = MagicMock(path="/jambonz/call")
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "correct-secret"):
            with pytest.raises(HTTPException) as exc:
                _verify_jambonz_webhook(request)
            assert exc.value.status_code == 403

    def test_correct_password_passes(self):
        import base64
        from app.routes.jambonz import _verify_jambonz_webhook

        request = MagicMock()
        request.headers = {
            "Authorization": "Basic " + base64.b64encode(b"user:my-secret").decode()
        }
        request.client = MagicMock(host="1.2.3.4")
        request.method = "POST"
        request.url = MagicMock(path="/jambonz/call")
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "my-secret"):
            _verify_jambonz_webhook(request)

    def test_missing_auth_header_returns_403(self):
        from app.routes.jambonz import _verify_jambonz_webhook

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock(host="1.2.3.4")
        request.method = "POST"
        request.url = MagicMock(path="/jambonz/call")
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "configured-secret"):
            with pytest.raises(HTTPException) as exc:
                _verify_jambonz_webhook(request)
            assert exc.value.status_code == 403

    def test_bearer_instead_of_basic_returns_403(self):
        from app.routes.jambonz import _verify_jambonz_webhook

        request = MagicMock()
        request.headers = {"Authorization": "Bearer some-token"}
        request.client = MagicMock(host="1.2.3.4")
        request.method = "POST"
        request.url = MagicMock(path="/jambonz/call")
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "my-secret"):
            with pytest.raises(HTTPException) as exc:
                _verify_jambonz_webhook(request)
            assert exc.value.status_code == 403

    def test_malformed_base64_returns_403(self):
        from app.routes.jambonz import _verify_jambonz_webhook

        request = MagicMock()
        request.headers = {"Authorization": "Basic not-valid-base64!!!"}
        request.client = MagicMock(host="1.2.3.4")
        request.method = "POST"
        request.url = MagicMock(path="/jambonz/call")
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "my-secret"):
            with pytest.raises(HTTPException) as exc:
                _verify_jambonz_webhook(request)
            assert exc.value.status_code == 403


# =============================================================================
# Loophole #5 — Twilio auth fail-closed
#
# Tests call _verify_twilio_webhook directly — the extracted helper function
# that all 3 Twilio endpoints use.
# =============================================================================


class TestTwilioAuthFailClosed:
    """Tests that _verify_twilio_webhook rejects when auth_token is missing."""

    @pytest.mark.asyncio
    async def test_none_auth_token_returns_403(self):
        """No auth_token (phone number not in DB or missing credentials) = 403."""
        from app.routes.twilio import _verify_twilio_webhook

        request = MagicMock()
        with pytest.raises(HTTPException) as exc:
            await _verify_twilio_webhook(request, None)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_auth_token_returns_403(self):
        """Empty string auth_token = 403."""
        from app.routes.twilio import _verify_twilio_webhook

        request = MagicMock()
        with pytest.raises(HTTPException) as exc:
            await _verify_twilio_webhook(request, "")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_auth_token_calls_signature_validation(self):
        """When auth_token is present, validate_twilio_signature is called."""
        from app.routes.twilio import _verify_twilio_webhook

        request = MagicMock()
        with patch(
            "app.routes.twilio.validate_twilio_signature", new_callable=AsyncMock
        ) as mock_validate:
            await _verify_twilio_webhook(request, "real-token")
            mock_validate.assert_called_once_with(request, "real-token")


# =============================================================================
# A1 — /prewarm/reassign must have auth dependency
# =============================================================================


class TestPrewarmReassignAuth:
    """Verify /prewarm/reassign has the verify_worker_auth dependency."""

    def test_reassign_endpoint_has_auth_dependency(self):
        """The /prewarm/reassign route must include verify_worker_auth."""
        import app.worker.main as wm

        reassign_route = None
        for route in wm.app.routes:
            if hasattr(route, "path") and route.path == "/prewarm/reassign":
                reassign_route = route
                break

        assert reassign_route is not None, "/prewarm/reassign route not found"

        dep_callables = [d.call for d in reassign_route.dependant.dependencies]
        assert wm.verify_worker_auth in dep_callables, (
            "/prewarm/reassign is missing Depends(verify_worker_auth)"
        )

    def test_all_management_endpoints_have_auth(self):
        """ALL management endpoints must have verify_worker_auth."""
        import app.worker.main as wm

        protected_paths = {
            "/prewarm",
            "/prewarm/reassign",
            "/prewarm/{call_sid}",
            "/cancel",
        }

        for route in wm.app.routes:
            path = getattr(route, "path", "")
            if path in protected_paths:
                dep_callables = [d.call for d in route.dependant.dependencies]
                assert wm.verify_worker_auth in dep_callables, (
                    f"{path} is missing Depends(verify_worker_auth)"
                )
