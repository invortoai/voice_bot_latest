"""Unit tests for audio URL fetch security — SSRF and size limits (DAAI-164, DAAI-165)."""

import pytest
from unittest.mock import patch

from app.worker.pipeline import _validate_audio_url


class TestAudioUrlSsrf:
    def test_localhost_blocked_in_production(self):
        with patch("app.worker.pipeline.IS_LOCAL", False):
            with pytest.raises(ValueError, match="localhost"):
                _validate_audio_url("http://localhost/audio.mp3")

    def test_localhost_allowed_in_local(self):
        with patch("app.worker.pipeline.IS_LOCAL", True):
            result = _validate_audio_url("http://localhost/audio.mp3")
            # Should return pinned addresses (not raise) in local dev
            assert isinstance(result, list)
            assert len(result) > 0
            assert all(isinstance(entry, tuple) and len(entry) == 2 for entry in result)

    def test_metadata_endpoint_blocked(self):
        """AWS metadata endpoint (169.254.169.254) is always blocked.
        In production it's caught by is_private; the link-local and explicit
        169.254.169.254 checks provide defense-in-depth."""
        with patch("app.worker.pipeline.IS_LOCAL", False):
            with pytest.raises(ValueError, match="private|link-local|metadata"):
                _validate_audio_url("http://169.254.169.254/latest/meta-data/audio.mp3")

    def test_private_ip_blocked_in_production(self):
        with patch("app.worker.pipeline.IS_LOCAL", False):
            with pytest.raises(ValueError, match="private|loopback"):
                _validate_audio_url("http://10.0.0.5/greeting.mp3")

    def test_ftp_scheme_blocked(self):
        with pytest.raises(ValueError, match="HTTP"):
            _validate_audio_url("ftp://files.example.com/greeting.mp3")

    def test_no_hostname_blocked(self):
        with pytest.raises(ValueError, match="hostname"):
            _validate_audio_url("http:///path/audio.mp3")

    def test_zero_ip_blocked_in_production(self):
        with patch("app.worker.pipeline.IS_LOCAL", False):
            with pytest.raises(ValueError, match="localhost"):
                _validate_audio_url("http://0.0.0.0/audio.mp3")

    def test_loopback_ip_blocked_in_production(self):
        with patch("app.worker.pipeline.IS_LOCAL", False):
            with pytest.raises(ValueError, match="loopback|private|localhost"):
                _validate_audio_url("http://127.0.0.1/audio.mp3")

    def test_link_local_ip_blocked(self):
        """Link-local addresses (169.254.x.x) are always blocked.
        In production, is_private catches them first. Either way they're rejected."""
        with patch("app.worker.pipeline.IS_LOCAL", False):
            with pytest.raises(ValueError, match="private|link-local"):
                _validate_audio_url("http://169.254.1.1/audio.mp3")

    def test_returns_pinned_addresses_for_safe_url(self):
        """Safe public URLs should return a list of (family, ip_str) tuples
        that can be fed to the pinned DNS resolver."""
        with patch("app.worker.pipeline.IS_LOCAL", False):
            # example.com is IANA-reserved and always resolves
            result = _validate_audio_url("https://example.com/audio.mp3")
            assert isinstance(result, list)
            assert len(result) > 0
            for family, ip_str in result:
                assert isinstance(family, int)
                assert isinstance(ip_str, str)


class TestPinnedResolver:
    """Tests for the _PinnedResolver that prevents DNS rebinding."""

    @pytest.mark.asyncio
    async def test_resolver_returns_pinned_ips(self):
        """Resolver must return the exact IPs we validated, not re-resolve DNS."""
        import socket

        # Import after conftest stubs are in place — _PinnedResolver inherits
        # from the stubbed AbstractResolver (a real empty class, not MagicMock).
        from app.worker.pipeline import _PinnedResolver

        resolver = _PinnedResolver("example.com", [(socket.AF_INET, "93.184.216.34")])
        results = await resolver.resolve("example.com", port=443)
        assert len(results) == 1
        assert results[0]["host"] == "93.184.216.34"
        assert results[0]["hostname"] == "example.com"

    @pytest.mark.asyncio
    async def test_resolver_close_is_noop(self):
        """close() must not raise."""
        import socket
        from app.worker.pipeline import _PinnedResolver

        resolver = _PinnedResolver("x", [(socket.AF_INET, "1.2.3.4")])
        await resolver.close()
