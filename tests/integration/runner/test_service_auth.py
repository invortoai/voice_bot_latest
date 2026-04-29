"""Integration tests for the global service-key authentication path.

verify_global_key_with_org accepts:
  X-API-Key: <API_KEY>  (global service key)
  X-Org-ID:  <org_uuid>

It is used by /assistants, /phone-numbers, and /call/outbound endpoints.
verify_customer_api_key (per-org key only) is used by /calls GET endpoints.

Tests:
  - Service key + valid org → 200
  - Service key + missing X-Org-ID → 401
  - Service key + unknown org → 403
  - When API_KEY is empty, key check is bypassed (dev mode)
  - Wrong service key → 403
  - Correct per-org key still works on customer endpoints (no regression)
  - Missing key → 401
"""

import pytest
from unittest.mock import patch


SERVICE_KEY = "test-internal-service-key-abc123"

ASSISTANT_PAYLOAD = {
    "name": "Service Auth Test Bot",
    "system_prompt": "Test prompt.",
}


# ---------------------------------------------------------------------------
# Service key: happy path (endpoints using verify_global_key_with_org)
# ---------------------------------------------------------------------------


class TestServiceKeyHappyPath:
    async def test_service_key_with_org_id_grants_access(
        self, runner_client, test_org_id
    ):
        """Service key + valid X-Org-ID must succeed on a global-key endpoint."""
        with patch("app.core.auth.API_KEY", SERVICE_KEY):
            resp = await runner_client.get(
                "/assistants",
                headers={
                    "X-API-Key": SERVICE_KEY,
                    "X-Org-ID": test_org_id,
                },
            )
        assert resp.status_code == 200

    async def test_service_key_creates_resource_scoped_to_org(
        self, runner_client, test_org_id
    ):
        """Resources created via service key must be scoped to the supplied org."""
        with patch("app.core.auth.API_KEY", SERVICE_KEY):
            resp = await runner_client.post(
                "/assistants",
                json=ASSISTANT_PAYLOAD,
                headers={
                    "X-API-Key": SERVICE_KEY,
                    "X-Org-ID": test_org_id,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["org_id"] == test_org_id

    async def test_service_key_returns_org_context(self, test_org_id):
        """verify_global_key_with_org must return org_id when key is valid."""
        from app.core.auth import verify_global_key_with_org

        with patch("app.core.auth.API_KEY", SERVICE_KEY):
            ctx = await verify_global_key_with_org(
                x_api_key=SERVICE_KEY,
                x_org_id=test_org_id,
            )
        assert ctx["org_id"] == test_org_id


# ---------------------------------------------------------------------------
# Service key: failure cases
# ---------------------------------------------------------------------------


class TestServiceKeyFailures:
    async def test_service_key_without_org_id_returns_401(self, runner_client):
        """Service key without X-Org-ID header must be rejected.

        runner_client sends X-Org-ID by default; override with empty string
        to simulate a caller that omits the header.
        """
        with patch("app.core.auth.API_KEY", SERVICE_KEY):
            resp = await runner_client.get(
                "/assistants",
                headers={"X-API-Key": SERVICE_KEY, "X-Org-ID": ""},
            )
        assert resp.status_code == 401
        assert "X-Org-ID" in resp.json()["detail"]

    async def test_service_key_with_unknown_org_returns_403(self, runner_client):
        """Service key with a non-existent org UUID must be rejected."""
        with patch("app.core.auth.API_KEY", SERVICE_KEY):
            resp = await runner_client.get(
                "/assistants",
                headers={
                    "X-API-Key": SERVICE_KEY,
                    "X-Org-ID": "00000000-0000-0000-0000-000000000000",
                },
            )
        assert resp.status_code == 403

    async def test_service_key_bypassed_when_api_key_empty(
        self, runner_client, test_org_id
    ):
        """When API_KEY is empty (dev), key validation is skipped entirely."""
        with patch("app.core.auth.API_KEY", ""):
            resp = await runner_client.get(
                "/assistants",
                headers={
                    "X-API-Key": "any-key-is-fine-in-dev",
                    "X-Org-ID": test_org_id,
                },
            )
        # Dev bypass: auth passes, endpoint returns 200
        assert resp.status_code == 200

    async def test_wrong_service_key_returns_403(self, runner_client, test_org_id):
        """A wrong service key is rejected (no per-org fallthrough)."""
        with patch("app.core.auth.API_KEY", SERVICE_KEY):
            resp = await runner_client.get(
                "/assistants",
                headers={
                    "X-API-Key": "wrong-service-key",
                    "X-Org-ID": test_org_id,
                },
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Regression: per-org key still works on customer endpoints
# ---------------------------------------------------------------------------


class TestPerOrgKeyRegression:
    async def test_valid_per_org_key_still_authenticates(
        self, runner_client, test_api_key
    ):
        """Per-org key must still work on verify_customer_api_key endpoints."""
        with patch("app.core.auth.API_KEY", SERVICE_KEY):
            resp = await runner_client.get(
                "/calls",
                headers={"X-API-Key": test_api_key},
            )
        assert resp.status_code == 200

    async def test_missing_key_returns_401(self, pg_container):
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/calls")
        assert resp.status_code == 401
