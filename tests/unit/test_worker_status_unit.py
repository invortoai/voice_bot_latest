"""Unit tests for WorkerStatus URL construction (app/services/worker_pool.py).

WorkerStatus computes public WebSocket URLs and health-check URLs for workers.
These tests cover the priority chain:
  1. PUBLIC_WS_URL override (bare or full)
  2. public_ip + suffix / template
  3. private_ip fallback
  4. host fallback
"""

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helper to build a WorkerStatus without importing at module level
# (the module-level singleton is already created; these tests just instantiate new ones).
# ---------------------------------------------------------------------------
def _make_worker(**kwargs):
    from app.services.worker_pool import WorkerStatus

    return WorkerStatus(**kwargs)


class TestGetHealthUrl:
    def test_private_ip_uses_http_on_worker_port(self):
        w = _make_worker(host="pub.example.com", private_ip="10.0.0.1")
        with patch("app.services.worker_pool.base.WORKER_PORT", 8765):
            url = w.get_health_url()
        assert url == "http://10.0.0.1:8765/health"

    def test_no_private_ip_uses_https_with_host(self):
        w = _make_worker(host="pub.example.com:443")
        url = w.get_health_url()
        assert url == "https://pub.example.com:443/health"

    def test_private_ip_takes_precedence_over_host(self):
        w = _make_worker(host="public.example.com", private_ip="192.168.1.5")
        with patch("app.services.worker_pool.base.WORKER_PORT", 8765):
            url = w.get_health_url()
        assert "192.168.1.5" in url
        assert "public.example.com" not in url


class TestGetWsUrlPublicWsUrlOverride:
    def test_full_wss_url_prepended_to_path(self):
        with patch(
            "app.services.worker_pool.base.PUBLIC_WS_URL", "wss://gateway.example.com"
        ):
            w = _make_worker(host="localhost:8765")
            assert w.get_ws_url("/ws") == "wss://gateway.example.com/ws"

    def test_bare_hostname_gets_scheme_prepended(self):
        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", "gateway.example.com"),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "wss"),
        ):
            w = _make_worker(host="localhost:8765")
            url = w.get_ws_url("/ws")
        assert url == "wss://gateway.example.com/ws"

    def test_trailing_slash_on_base_url_is_stripped(self):
        with patch(
            "app.services.worker_pool.base.PUBLIC_WS_URL", "wss://gateway.example.com/"
        ):
            w = _make_worker(host="localhost:8765")
            url = w.get_ws_url("/ws")
        assert url == "wss://gateway.example.com/ws"

    def test_path_without_leading_slash_gets_slash_prepended(self):
        with patch(
            "app.services.worker_pool.base.PUBLIC_WS_URL", "wss://gateway.example.com"
        ):
            w = _make_worker(host="localhost:8765")
            url = w.get_ws_url("ws/jambonz")
        assert url.endswith("/ws/jambonz")

    def test_mcube_path_with_call_id(self):
        with patch(
            "app.services.worker_pool.base.PUBLIC_WS_URL", "wss://gateway.example.com"
        ):
            w = _make_worker(host="localhost:8765")
            url = w.get_ws_url("/ws/mcube/CALL-XYZ")
        assert url == "wss://gateway.example.com/ws/mcube/CALL-XYZ"

    def test_jambonz_path(self):
        with patch(
            "app.services.worker_pool.base.PUBLIC_WS_URL", "wss://gateway.example.com"
        ):
            w = _make_worker(host="localhost:8765")
            url = w.get_ws_url("/ws/jambonz")
        assert url == "wss://gateway.example.com/ws/jambonz"


class TestGetWsUrlPublicIpResolution:
    def test_public_ip_with_sslip_suffix_no_port(self):
        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", ""),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "wss"),
            patch(
                "app.services.worker_pool.base.WORKER_PUBLIC_WS_HOST_SUFFIX",
                ".sslip.io",
            ),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_PORT", 443),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_HOST_TEMPLATE", ""),
        ):
            w = _make_worker(host="10.0.0.1:8765", public_ip="54.1.2.3")
            url = w.get_ws_url("/ws/jambonz")
        assert url == "wss://54.1.2.3.sslip.io/ws/jambonz"
        # No port in URL because WORKER_PUBLIC_WS_PORT == 443
        assert ":443" not in url

    def test_public_ip_with_non_443_port_includes_port(self):
        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", ""),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "wss"),
            patch(
                "app.services.worker_pool.base.WORKER_PUBLIC_WS_HOST_SUFFIX",
                ".sslip.io",
            ),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_PORT", 8765),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_HOST_TEMPLATE", ""),
        ):
            w = _make_worker(host="10.0.0.1:8765", public_ip="54.1.2.3")
            url = w.get_ws_url("/ws")
        assert ":8765" in url

    def test_custom_host_template_is_expanded(self):
        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", ""),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "wss"),
            patch(
                "app.services.worker_pool.base.WORKER_PUBLIC_WS_HOST_TEMPLATE",
                "{public_ip}.custom.example.com",
            ),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_PORT", 443),
        ):
            w = _make_worker(host="localhost:8765", public_ip="1.2.3.4")
            url = w.get_ws_url("/ws")
        assert "1.2.3.4.custom.example.com" in url

    def test_fallback_to_private_ip_when_no_public_ip(self):
        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", ""),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "ws"),
        ):
            w = _make_worker(host="pub.example.com", private_ip="10.0.0.5")
            with patch("app.services.worker_pool.base.WORKER_PORT", 8765):
                url = w.get_ws_url("/ws")
        assert "10.0.0.5" in url

    def test_final_fallback_to_host(self):
        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", ""),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "ws"),
        ):
            w = _make_worker(host="localhost:8765")
            url = w.get_ws_url("/ws")
        assert "localhost:8765" in url
        assert "/ws" in url


class TestWorkerStatusToDict:
    def test_to_dict_has_all_expected_keys(self):
        w = _make_worker(
            host="10.0.0.1:8765",
            instance_id="i-abc123",
            private_ip="10.0.0.1",
            public_ip="54.1.2.3",
        )
        d = w.to_dict()
        for key in [
            "host",
            "instance_id",
            "private_ip",
            "public_ip",
            "is_available",
            "current_call_sid",
            "assigned_at",
            "last_health_check",
        ]:
            assert key in d, f"Missing key: {key}"

    def test_to_dict_default_availability(self):
        w = _make_worker(host="localhost:8765")
        d = w.to_dict()
        assert d["is_available"] is True
        assert d["current_call_sid"] is None
        assert d["assigned_at"] is None

    def test_instance_id_defaults_to_host(self):
        w = _make_worker(host="localhost:8765")
        assert w.instance_id == "localhost:8765"

    def test_explicit_instance_id(self):
        w = _make_worker(host="localhost:8765", instance_id="i-custom-123")
        assert w.instance_id == "i-custom-123"
