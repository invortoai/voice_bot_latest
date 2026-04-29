"""Unit tests covering the merge-conflict areas in app/worker/main.py.

Conflict areas:
1. Config imports — IS_LOCAL, OPENAI_API_KEY, WORKER_AUTH_TOKEN,
   WORKER_PUBLIC_WS_SCHEME, WS_ALLOWED_ORIGINS all present after merge.
2. verify_worker_auth — security PR dependency; must block in prod,
   skip in local/dev or when WORKER_AUTH_TOKEN is unconfigured.
3. _check_ws_origin — security PR function; blocks wrong browser origins,
   allows server-to-server (no Origin header) and matching origins.

NOTE: app.worker.main requires pipecat stubs (added to conftest).
      The module is imported at module scope here so sys.modules has it
      before any patch() call references it by dotted path.
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

# Pre-import so `patch("app.worker.main.X")` can resolve the module.
import app.worker.main as _worker_main  # noqa: F401  (side-effect: populates sys.modules)


# ---------------------------------------------------------------------------
# verify_worker_auth
# ---------------------------------------------------------------------------


class TestVerifyWorkerAuth:
    @pytest.mark.asyncio
    async def test_no_token_configured_returns_503(self):
        """Empty WORKER_AUTH_TOKEN raises 503 (fail-closed)."""
        with patch("app.worker.main.WORKER_AUTH_TOKEN", ""):
            from app.worker.main import verify_worker_auth

            with pytest.raises(HTTPException) as exc:
                await verify_worker_auth(x_worker_auth=None)
            assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_missing_header_returns_403(self):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "secret"):
            from app.worker.main import verify_worker_auth

            with pytest.raises(HTTPException) as exc:
                await verify_worker_auth(x_worker_auth=None)
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_token_returns_403(self):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "correct-secret"):
            from app.worker.main import verify_worker_auth

            with pytest.raises(HTTPException) as exc:
                await verify_worker_auth(x_worker_auth="wrong-secret")
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_correct_token_passes(self):
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "correct-secret"):
            from app.worker.main import verify_worker_auth

            await verify_worker_auth(x_worker_auth="correct-secret")

    @pytest.mark.asyncio
    async def test_near_match_token_still_rejected(self):
        """One-character-short token must still return 403 (timing-safe comparison)."""
        with patch("app.worker.main.WORKER_AUTH_TOKEN", "secret"):
            from app.worker.main import verify_worker_auth

            with pytest.raises(HTTPException) as exc:
                await verify_worker_auth(x_worker_auth="secre")
            assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# _check_ws_origin
# ---------------------------------------------------------------------------


def _make_websocket(origin=None):
    ws = MagicMock()
    ws.headers = {"origin": origin} if origin is not None else {}
    return ws


class TestCheckWsOrigin:
    def test_empty_allowed_origins_allows_all(self):
        """Empty WS_ALLOWED_ORIGINS allows all connections (opt-in enforcement)."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", []):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket("https://any.com")) is True

    def test_empty_allowed_origins_allows_server_to_server(self):
        """Empty WS_ALLOWED_ORIGINS allows server-to-server (no Origin header)."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", []):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket(origin=None)) is True

    def test_configured_origins_rejects_unknown(self):
        """When WS_ALLOWED_ORIGINS is configured, unknown origins are rejected."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://app.invorto.ai"]):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket("https://evil.com")) is False

    def test_no_origin_header_allows_server_to_server(self):
        """Twilio/Jambonz connect server-to-server — they send no Origin."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://app.invorto.ai"]):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket(origin=None)) is True

    def test_matching_origin_is_allowed(self):
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://app.invorto.ai"]):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket("https://app.invorto.ai")) is True

    def test_wrong_origin_is_blocked(self):
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://app.invorto.ai"]):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket("https://evil.com")) is False

    def test_origin_with_path_is_rejected(self):
        """Browser Origin headers never include paths. An origin with a path
        does not match via exact comparison and should be rejected."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://app.invorto.ai"]):
            from app.worker.main import _check_ws_origin

            assert (
                _check_ws_origin(_make_websocket("https://app.invorto.ai/path"))
                is False
            )

    def test_prefix_bypass_is_blocked(self):
        """Attacker registers app.invorto.ai.evil.com — must NOT match allowed origin.
        Old code used startswith() which would pass; new code uses exact match."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://app.invorto.ai"]):
            from app.worker.main import _check_ws_origin

            assert (
                _check_ws_origin(_make_websocket("https://app.invorto.ai.evil.com"))
                is False
            )

    def test_case_insensitive_match(self):
        """Origin comparison should be case-insensitive."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://App.Invorto.AI"]):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket("https://app.invorto.ai")) is True

    def test_trailing_slash_normalization(self):
        """Trailing slash differences should not cause rejection."""
        with patch("app.worker.main.WS_ALLOWED_ORIGINS", ["https://app.invorto.ai/"]):
            from app.worker.main import _check_ws_origin

            assert _check_ws_origin(_make_websocket("https://app.invorto.ai")) is True

    def test_multiple_allowed_origins_any_match_passes(self):
        with patch(
            "app.worker.main.WS_ALLOWED_ORIGINS",
            ["https://app.invorto.ai", "https://staging.invorto.ai"],
        ):
            from app.worker.main import _check_ws_origin

            assert (
                _check_ws_origin(_make_websocket("https://staging.invorto.ai")) is True
            )


# ---------------------------------------------------------------------------
# Config imports — all vars from both merge branches must be present
# ---------------------------------------------------------------------------


class TestConfigImports:
    def test_all_conflict_config_vars_importable(self):
        """IS_LOCAL, OPENAI_API_KEY, WORKER_AUTH_TOKEN, WORKER_PUBLIC_WS_SCHEME,
        and WS_ALLOWED_ORIGINS must all be importable from app.worker.main after
        the conflict resolution that combined both branches' config imports."""
        import app.worker.main as wm

        for attr in (
            "IS_LOCAL",
            "OPENAI_API_KEY",
            "WORKER_AUTH_TOKEN",
            "WORKER_PUBLIC_WS_SCHEME",
            "WS_ALLOWED_ORIGINS",
        ):
            assert hasattr(wm, attr), f"app.worker.main missing: {attr}"
