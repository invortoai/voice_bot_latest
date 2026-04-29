"""Unit tests covering the merge-conflict areas in app/services/worker_pool.py.

Conflict 3 in the merge:
- _send_prewarm: main added _get_worker_url() + config_payload body; security
  added X-Worker-Auth header. Resolution kept both — these tests verify the result.
- _cancel_prewarm: same story.
- send_prewarm_and_wait / _send_prewarm_reassign: also carry X-Worker-Auth now.
- to_safe_dict(): added by security; must not expose infrastructure fields.
- trigger_prewarm_nowait: main added config_payload param — must forward it.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(
    private_ip=None, public_ip=None, host="localhost:8765", instance_id="i-test"
):
    from app.services.worker_pool import WorkerStatus

    return WorkerStatus(
        host=host,
        instance_id=instance_id,
        private_ip=private_ip,
        public_ip=public_ip,
    )


class _ConcretePool:
    """Minimal concrete subclass that satisfies BaseWorkerPool's abstract method."""

    def __new__(cls):
        from app.services.worker_pool import BaseWorkerPool

        class _Pool(BaseWorkerPool):
            async def discover_workers(self):
                pass

        return _Pool()


def _mock_httpx_client(response_json=None):
    """Return (MockClient class, inner async client mock)."""
    mock_response = MagicMock()
    mock_response.json.return_value = response_json or {}
    mock_response.status_code = 200

    inner = AsyncMock()
    inner.post = AsyncMock(return_value=mock_response)
    inner.delete = AsyncMock(return_value=mock_response)

    MockClient = MagicMock()
    MockClient.return_value.__aenter__ = AsyncMock(return_value=inner)
    MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
    return MockClient, inner


# ---------------------------------------------------------------------------
# WorkerStatus.to_safe_dict  (DAAI-143)
# ---------------------------------------------------------------------------


class TestToSafeDict:
    def test_does_not_expose_host(self):
        w = _make_worker(
            host="10.0.0.1:8765", private_ip="10.0.0.1", public_ip="1.2.3.4"
        )
        assert "host" not in w.to_safe_dict()

    def test_does_not_expose_private_ip(self):
        w = _make_worker(private_ip="10.0.0.1")
        assert "private_ip" not in w.to_safe_dict()

    def test_does_not_expose_public_ip(self):
        w = _make_worker(public_ip="54.1.2.3")
        assert "public_ip" not in w.to_safe_dict()

    def test_does_not_expose_instance_id_as_raw_key(self):
        # instance_id is exposed as "worker_id", not "instance_id"
        d = _make_worker(instance_id="i-abc123").to_safe_dict()
        assert "instance_id" not in d
        assert d["worker_id"] == "i-abc123"

    def test_exposes_operational_fields(self):
        w = _make_worker()
        d = w.to_safe_dict()
        for key in (
            "worker_id",
            "is_available",
            "current_call_sid",
            "assigned_at",
            "last_health_check",
        ):
            assert key in d, f"Expected key '{key}' in to_safe_dict()"

    def test_to_dict_still_exposes_all_infrastructure_fields(self):
        """to_dict() must remain unchanged — used internally for logging."""
        w = _make_worker(private_ip="10.0.0.1", public_ip="1.2.3.4")
        d = w.to_dict()
        for key in ("host", "instance_id", "private_ip", "public_ip"):
            assert key in d


# ---------------------------------------------------------------------------
# _get_worker_url  (main's refactor — used by all prewarm methods)
# ---------------------------------------------------------------------------


class TestGetWorkerUrl:
    def test_private_ip_uses_http_with_port(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.5")
        with patch("app.services.worker_pool.base.WORKER_PORT", 8765):
            url = pool._get_worker_url(w, "/prewarm")
        assert url == "http://10.0.0.5:8765/prewarm"

    def test_no_private_ip_uses_https_with_host(self):
        pool = _ConcretePool()
        w = _make_worker(host="worker.example.com")
        url = pool._get_worker_url(w, "/prewarm")
        assert url == "https://worker.example.com/prewarm"

    def test_path_is_appended(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        with patch("app.services.worker_pool.base.WORKER_PORT", 8765):
            assert pool._get_worker_url(w, "/prewarm/CSID-001").endswith(
                "/prewarm/CSID-001"
            )


# ---------------------------------------------------------------------------
# _send_prewarm  — X-Worker-Auth + config_payload body
# ---------------------------------------------------------------------------


class TestSendPrewarm:
    @pytest.mark.asyncio
    async def test_includes_worker_auth_header_when_configured(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", "secret-token"),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._send_prewarm(w, "CSID-001")
        headers = inner.post.call_args.kwargs["headers"]
        assert headers.get("X-Worker-Auth") == "secret-token"

    @pytest.mark.asyncio
    async def test_no_worker_auth_header_when_token_empty(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._send_prewarm(w, "CSID-001")
        headers = inner.post.call_args.kwargs["headers"]
        assert "X-Worker-Auth" not in headers

    @pytest.mark.asyncio
    async def test_config_payload_merged_into_body(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        config = {"assistant_config": {"name": "Bot"}, "provider_name": "jambonz"}
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._send_prewarm(w, "CSID-001", config_payload=config)
        body = inner.post.call_args.kwargs["json"]
        assert body["call_sid"] == "CSID-001"
        assert body["assistant_config"] == {"name": "Bot"}
        assert body["provider_name"] == "jambonz"

    @pytest.mark.asyncio
    async def test_no_config_payload_body_only_has_call_sid(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._send_prewarm(w, "CSID-001")
        body = inner.post.call_args.kwargs["json"]
        assert list(body.keys()) == ["call_sid"]

    @pytest.mark.asyncio
    async def test_api_key_included_when_set(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", "my-api-key"),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._send_prewarm(w, "CSID-001")
        headers = inner.post.call_args.kwargs["headers"]
        assert headers.get("X-API-Key") == "my-api-key"


# ---------------------------------------------------------------------------
# _cancel_prewarm  — X-Worker-Auth + _get_worker_url
# ---------------------------------------------------------------------------


class TestCancelPrewarm:
    @pytest.mark.asyncio
    async def test_includes_worker_auth_header(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", "cancel-token"),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._cancel_prewarm(w, "CSID-001")
        headers = inner.delete.call_args.kwargs["headers"]
        assert headers.get("X-Worker-Auth") == "cancel-token"

    @pytest.mark.asyncio
    async def test_uses_delete_method(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._cancel_prewarm(w, "CSID-001")
        inner.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_url_contains_call_sid(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.5")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._cancel_prewarm(w, "CSID-XYZ")
        url = inner.delete.call_args.args[0]
        assert "CSID-XYZ" in url


# ---------------------------------------------------------------------------
# send_prewarm_and_wait  — X-Worker-Auth + wait=True in body
# ---------------------------------------------------------------------------


class TestSendPrewarmAndWait:
    @pytest.mark.asyncio
    async def test_includes_worker_auth_header(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client({"status": "ready"})
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", "wait-token"),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool.send_prewarm_and_wait(w, "CSID-001", {})
        headers = inner.post.call_args.kwargs["headers"]
        assert headers.get("X-Worker-Auth") == "wait-token"

    @pytest.mark.asyncio
    async def test_body_includes_wait_true(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client({"status": "ready"})
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool.send_prewarm_and_wait(w, "CSID-001", {"provider_name": "twilio"})
        body = inner.post.call_args.kwargs["json"]
        assert body["wait"] is True
        assert body["provider_name"] == "twilio"

    @pytest.mark.asyncio
    async def test_returns_true_when_status_ready(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, _ = _mock_httpx_client({"status": "ready"})
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            result = await pool.send_prewarm_and_wait(w, "CSID-001", {})
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_status_not_ready(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, _ = _mock_httpx_client({"status": "prewarming"})
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            result = await pool.send_prewarm_and_wait(w, "CSID-001", {})
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        with (
            patch(
                "app.services.worker_pool.base.httpx.AsyncClient",
                side_effect=Exception("network error"),
            ),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            result = await pool.send_prewarm_and_wait(w, "CSID-001", {})
        assert result is False


# ---------------------------------------------------------------------------
# _send_prewarm_reassign  — X-Worker-Auth
# ---------------------------------------------------------------------------


class TestSendPrewarmReassign:
    @pytest.mark.asyncio
    async def test_includes_worker_auth_header(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", "reassign-token"),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._send_prewarm_reassign(w, "old-sid", "new-sid")
        headers = inner.post.call_args.kwargs["headers"]
        assert headers.get("X-Worker-Auth") == "reassign-token"

    @pytest.mark.asyncio
    async def test_body_has_old_and_new_keys(self):
        pool = _ConcretePool()
        w = _make_worker(private_ip="10.0.0.1")
        MockClient, inner = _mock_httpx_client()
        with (
            patch("app.services.worker_pool.base.httpx.AsyncClient", MockClient),
            patch("app.services.worker_pool.base.WORKER_AUTH_TOKEN", ""),
            patch("app.services.worker_pool.base.API_KEY", ""),
            patch("app.services.worker_pool.base.WORKER_PORT", 8765),
        ):
            await pool._send_prewarm_reassign(w, "call-id-old", "call-sid-new")
        body = inner.post.call_args.kwargs["json"]
        assert body["old_key"] == "call-id-old"
        assert body["new_key"] == "call-sid-new"


# ---------------------------------------------------------------------------
# trigger_prewarm_nowait  — config_payload forwarded to _send_prewarm
# ---------------------------------------------------------------------------


class TestTriggerPrewarmNowait:
    @pytest.mark.asyncio
    async def test_config_payload_forwarded(self):
        pool = _ConcretePool()
        w = _make_worker()
        captured = {}

        async def fake_send_prewarm(worker, call_sid, config_payload=None):
            captured["config_payload"] = config_payload

        pool._send_prewarm = fake_send_prewarm
        config = {"assistant_config": {"name": "Bot"}}
        pool.trigger_prewarm_nowait(w, "CSID-001", config_payload=config)
        # Let the event loop run the created task
        await asyncio.sleep(0)
        assert captured["config_payload"] == config

    @pytest.mark.asyncio
    async def test_no_config_payload_defaults_to_none(self):
        pool = _ConcretePool()
        w = _make_worker()
        captured = {}

        async def fake_send_prewarm(worker, call_sid, config_payload=None):
            captured["config_payload"] = config_payload

        pool._send_prewarm = fake_send_prewarm
        pool.trigger_prewarm_nowait(w, "CSID-001")
        await asyncio.sleep(0)
        assert captured["config_payload"] is None
