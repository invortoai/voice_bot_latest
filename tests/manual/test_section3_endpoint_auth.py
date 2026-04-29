"""Section 3 — Manual Endpoint Testing

Tests worker and runner endpoints for auth enforcement.
No IS_LOCAL bypasses — all checks enforced in every environment.

Run: .venv/Scripts/python.exe -m pytest tests/manual/test_section3_endpoint_auth.py -v
"""

import pytest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport


# =============================================================================
# 3.1 — DAAI-139: Worker Endpoint Auth
# =============================================================================


class TestWorkerEndpointAuth:
    """Worker management endpoints auth enforcement."""

    @pytest.fixture
    def worker_app(self):
        import app.worker.main as wm

        return wm.app

    # --- Token configured, correct/wrong/missing ---

    async def test_cancel_no_header_returns_403(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post("/cancel")
        assert resp.status_code == 403

    async def test_cancel_wrong_token_returns_403(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/cancel", headers={"X-Worker-Auth": "wrong-token"}
                )
        assert resp.status_code == 403

    async def test_cancel_correct_token_passes(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/cancel", headers={"X-Worker-Auth": "test-secret-token"}
                )
        assert resp.status_code == 200

    async def test_prewarm_no_header_returns_403(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post("/prewarm", json={"call_sid": "test"})
        assert resp.status_code == 403

    async def test_prewarm_correct_token_passes(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/prewarm",
                    json={"call_sid": "test-sid"},
                    headers={"X-Worker-Auth": "test-secret-token"},
                )
        assert resp.status_code == 200

    async def test_delete_prewarm_no_header_returns_403(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.delete("/prewarm/test-sid")
        assert resp.status_code == 403

    # --- Fail-closed: token NOT configured ---

    async def test_cancel_empty_token_returns_503(self, worker_app):
        """Empty WORKER_AUTH_TOKEN must return 503."""
        with patch("app.worker.main.WORKER_AUTH_TOKEN", ""):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post("/cancel")
        assert resp.status_code == 503

    async def test_prewarm_empty_token_returns_503(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", ""):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post("/prewarm", json={"call_sid": "test"})
        assert resp.status_code == 503

    # --- A1: /prewarm/reassign auth ---

    async def test_reassign_no_header_returns_403(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/prewarm/reassign", json={"old_key": "a", "new_key": "b"}
                )
        assert resp.status_code == 403

    async def test_reassign_correct_token_passes(self, worker_app):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "test-secret-token"):
            async with AsyncClient(
                transport=ASGITransport(app=worker_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/prewarm/reassign",
                    json={"old_key": "a", "new_key": "b"},
                    headers={"X-Worker-Auth": "test-secret-token"},
                )
        assert resp.status_code == 200


# =============================================================================
# 3.2 — DAAI-138: Jambonz Webhook Auth
# =============================================================================


class TestJambonzWebhookAuth:
    """Jambonz webhook endpoints auth enforcement."""

    @pytest.fixture
    def runner_app(self):
        from app.main import app

        return app

    async def test_call_no_auth_returns_403(self, runner_app):
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "my-secret"):
            async with AsyncClient(
                transport=ASGITransport(app=runner_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/jambonz/call",
                    json={
                        "callSid": "AUTH-001",
                        "from": "+14155551234",
                        "to": "+15005550020",
                    },
                )
        assert resp.status_code == 403

    async def test_call_wrong_basic_auth_returns_403(self, runner_app):
        import base64

        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "my-secret"):
            basic = base64.b64encode(b"user:wrong-pass").decode()
            async with AsyncClient(
                transport=ASGITransport(app=runner_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/jambonz/call",
                    headers={"Authorization": f"Basic {basic}"},
                    json={
                        "callSid": "AUTH-002",
                        "from": "+14155551234",
                        "to": "+15005550020",
                    },
                )
        assert resp.status_code == 403

    async def test_call_correct_basic_auth_passes(self, runner_app):
        import base64

        with (
            patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "my-secret"),
            patch("app.routes.jambonz.worker_pool") as mock_wp,
            patch("app.routes.jambonz.phone_number_service") as mock_pns,
            patch("app.routes.jambonz.call_service"),
        ):
            mock_pns.get_by_number.return_value = None
            mock_wp.get_and_assign_worker = AsyncMock(return_value=None)
            basic = base64.b64encode(b"user:my-secret").decode()
            async with AsyncClient(
                transport=ASGITransport(app=runner_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/jambonz/call",
                    headers={"Authorization": f"Basic {basic}"},
                    json={
                        "callSid": "AUTH-003",
                        "from": "+14155551234",
                        "to": "+15005550020",
                        "direction": "inbound",
                    },
                )
        assert resp.status_code == 200

    async def test_status_no_auth_returns_403(self, runner_app):
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", "my-secret"):
            async with AsyncClient(
                transport=ASGITransport(app=runner_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/jambonz/status",
                    json={"callSid": "STATUS-001", "callStatus": "completed"},
                )
        assert resp.status_code == 403

    # --- Fail-closed: empty secret ---

    async def test_call_empty_secret_returns_503(self, runner_app):
        """Empty JAMBONZ_WEBHOOK_SECRET must return 503."""
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", ""):
            async with AsyncClient(
                transport=ASGITransport(app=runner_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/jambonz/call",
                    json={
                        "callSid": "FAILOPEN-001",
                        "from": "+1",
                        "to": "+2",
                        "direction": "inbound",
                    },
                )
        assert resp.status_code == 503

    async def test_status_empty_secret_returns_503(self, runner_app):
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", ""):
            async with AsyncClient(
                transport=ASGITransport(app=runner_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/jambonz/status",
                    json={"callSid": "FAILOPEN-002", "callStatus": "completed"},
                )
        assert resp.status_code == 503
