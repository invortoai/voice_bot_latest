"""Unit tests for API key authentication (app/core/auth.py)."""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# 1. verify_api_key  (global infra key for /workers)
# ---------------------------------------------------------------------------


class TestVerifyApiKey:
    """Tests for the verify_api_key dependency function."""

    @pytest.mark.asyncio
    async def test_empty_api_key_config_disables_auth(self):
        """When API_KEY env var is empty in local dev, any request passes without a key."""
        with patch("app.core.auth.API_KEY", ""), patch("app.core.auth.IS_LOCAL", True):
            from app.core.auth import verify_api_key

            result = await verify_api_key(api_key=None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_api_key_config_ignores_supplied_key(self):
        """When auth is disabled in local dev, a supplied key is accepted silently."""
        with patch("app.core.auth.API_KEY", ""), patch("app.core.auth.IS_LOCAL", True):
            from app.core.auth import verify_api_key

            result = await verify_api_key(api_key="whatever")
        assert result == ""

    @pytest.mark.asyncio
    async def test_missing_key_returns_401_when_auth_enabled(self):
        """Missing X-API-Key header → 401 Unauthorized."""
        with patch("app.core.auth.API_KEY", "secret-key"):
            from app.core.auth import verify_api_key

            with pytest.raises(HTTPException) as exc_info:
                await verify_api_key(api_key=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_returns_403(self):
        """Supplying a key that does not match configured key → 403 Forbidden."""
        with patch("app.core.auth.API_KEY", "secret-key"):
            from app.core.auth import verify_api_key

            with pytest.raises(HTTPException) as exc_info:
                await verify_api_key(api_key="wrong-key")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_correct_key_returns_key(self):
        """Exact key match → returns the key string."""
        with patch("app.core.auth.API_KEY", "secret-key"):
            from app.core.auth import verify_api_key

            result = await verify_api_key(api_key="secret-key")
        assert result == "secret-key"

    @pytest.mark.asyncio
    async def test_key_is_case_sensitive(self):
        """API key comparison must be case-sensitive."""
        with patch("app.core.auth.API_KEY", "Secret-Key"):
            from app.core.auth import verify_api_key

            with pytest.raises(HTTPException) as exc_info:
                await verify_api_key(api_key="secret-key")
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# 2. verify_org_api_key  (per-org key, DB lookup)
# ---------------------------------------------------------------------------


class TestVerifyOrgApiKey:
    """Unit tests for verify_org_api_key — DB is mocked via get_cursor."""

    def _make_cursor(self, row):
        """Return a mock cursor whose fetchone() returns *row*."""
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = row
        return cur

    @pytest.mark.asyncio
    async def test_missing_header_returns_401(self):
        from app.core.auth import verify_org_api_key

        with pytest.raises(HTTPException) as exc_info:
            await verify_org_api_key(x_api_key=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_key_returns_403(self):
        """Hash not found in DB → 403."""
        from app.core.auth import verify_org_api_key

        with patch("app.core.auth.get_cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(
                return_value=self._make_cursor(None)  # no row found
            )
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(HTTPException) as exc_info:
                await verify_org_api_key(x_api_key="inv_fake-key-value")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_inactive_key_returns_403(self):
        """Found but is_active=False → 403."""
        import uuid

        from app.core.auth import verify_org_api_key

        row = {
            "id": uuid.uuid4(),
            "org_id": uuid.uuid4(),
            "key_prefix": "inv_test",
            "is_active": False,
            "expires_at": None,
            "scopes": [],
        }
        with patch("app.core.auth.get_cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(
                return_value=self._make_cursor(row)
            )
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(HTTPException) as exc_info:
                await verify_org_api_key(x_api_key="inv_fake-key-value")
        assert exc_info.value.status_code == 403
        assert "inactive" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_expired_key_returns_403(self):
        """Key with expires_at in the past → 403."""
        import uuid

        from app.core.auth import verify_org_api_key

        row = {
            "id": uuid.uuid4(),
            "org_id": uuid.uuid4(),
            "key_prefix": "inv_test",
            "is_active": True,
            "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "scopes": [],
        }
        with patch("app.core.auth.get_cursor") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(
                return_value=self._make_cursor(row)
            )
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(HTTPException) as exc_info:
                await verify_org_api_key(x_api_key="inv_fake-key-value")
        assert exc_info.value.status_code == 403
        assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_valid_key_returns_org_context(self):
        """Active, non-expired key → returns org_id / key_id / scopes dict."""
        import uuid

        from app.core.auth import verify_org_api_key

        org_id = uuid.uuid4()
        key_id = uuid.uuid4()
        row = {
            "id": key_id,
            "org_id": org_id,
            "key_prefix": "inv_test",
            "is_active": True,
            "expires_at": None,
            "scopes": ["read"],
        }

        # First cursor call (SELECT) returns row; second (UPDATE last_used_at) ignored
        cursors = [self._make_cursor(row), self._make_cursor(None)]
        call_count = [0]

        def side_effect():
            ctx = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            cursor = cursors[min(idx, len(cursors) - 1)]
            ctx.__enter__ = MagicMock(return_value=cursor)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("app.core.auth.get_cursor", side_effect=side_effect):
            result = await verify_org_api_key(x_api_key="inv_fake-key-value")

        assert result["org_id"] == str(org_id)
        assert result["key_id"] == str(key_id)
        assert result["scopes"] == ["read"]


# ---------------------------------------------------------------------------
# 3. JWT helpers: create_access_token / verify_jwt_token
# ---------------------------------------------------------------------------


class TestCreateAccessToken:
    def test_returns_string(self):
        with patch("app.core.auth.JWT_SECRET_KEY", "test-secret"):
            from app.core.auth import create_access_token

            token = create_access_token(
                {"org_id": "org-1", "user_id": "user-1", "role": "admin"}
            )
        assert isinstance(token, str)
        assert len(token) > 0

    def test_token_contains_payload_fields(self):
        import jwt as pyjwt

        with patch("app.core.auth.JWT_SECRET_KEY", "test-secret"):
            from app.core.auth import create_access_token

            token = create_access_token(
                {
                    "org_id": "org-1",
                    "user_id": "user-1",
                    "role": "admin",
                    "email": "a@b.com",
                }
            )
        payload = pyjwt.decode(token, "test-secret", algorithms=["HS256"])
        assert payload["org_id"] == "org-1"
        assert payload["user_id"] == "user-1"
        assert payload["role"] == "admin"
        assert "exp" in payload

    def test_custom_expiry(self):
        import jwt as pyjwt

        with patch("app.core.auth.JWT_SECRET_KEY", "test-secret"):
            from app.core.auth import create_access_token

            token = create_access_token({"org_id": "x"}, expires_in_minutes=60)
        payload = pyjwt.decode(token, "test-secret", algorithms=["HS256"])
        remaining = payload["exp"] - datetime.now(timezone.utc).timestamp()
        # Should expire in roughly 60 minutes (within a 5-minute tolerance)
        assert 3300 < remaining < 3700


class TestVerifyJwtToken:
    @pytest.mark.asyncio
    async def test_missing_bearer_returns_401(self):
        from app.core.auth import verify_jwt_token

        with pytest.raises(HTTPException) as exc_info:
            await verify_jwt_token(credentials=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self):
        from fastapi.security import HTTPAuthorizationCredentials

        from app.core.auth import verify_jwt_token

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not.a.jwt")
        with patch("app.core.auth.JWT_SECRET_KEY", "secret"):
            with pytest.raises(HTTPException) as exc_info:
                await verify_jwt_token(credentials=creds)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self):
        import jwt as pyjwt
        from fastapi.security import HTTPAuthorizationCredentials

        from app.core.auth import verify_jwt_token

        expired_token = pyjwt.encode(
            {
                "org_id": "org-1",
                "user_id": "user-1",
                "role": "admin",
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            "secret",
            algorithm="HS256",
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token)
        with patch("app.core.auth.JWT_SECRET_KEY", "secret"):
            with pytest.raises(HTTPException) as exc_info:
                await verify_jwt_token(credentials=creds)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_jwt_secret_returns_500(self):
        from fastapi.security import HTTPAuthorizationCredentials

        from app.core.auth import verify_jwt_token

        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="any.token.value"
        )
        with patch("app.core.auth.JWT_SECRET_KEY", ""):
            with pytest.raises(HTTPException) as exc_info:
                await verify_jwt_token(credentials=creds)
        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# 4. Key generation helpers
# ---------------------------------------------------------------------------


class TestGenerateApiKey:
    def test_returns_three_values(self):
        from app.core.auth import generate_api_key

        result = generate_api_key()
        assert len(result) == 3

    def test_raw_key_starts_with_prefix(self):
        with patch("app.core.auth.API_KEY_PREFIX", "inv_"):
            from app.core.auth import generate_api_key

            raw_key, _, _ = generate_api_key()
        assert raw_key.startswith("inv_")

    def test_key_prefix_is_first_8_chars(self):
        from app.core.auth import generate_api_key

        raw_key, key_prefix, _ = generate_api_key()
        assert key_prefix == raw_key[:8]

    def test_key_hash_is_sha256_of_raw(self):
        from app.core.auth import generate_api_key

        raw_key, _, key_hash = generate_api_key()
        expected = hashlib.sha256(raw_key.encode()).hexdigest()
        assert key_hash == expected

    def test_each_call_generates_unique_key(self):
        from app.core.auth import generate_api_key

        keys = {generate_api_key()[0] for _ in range(10)}
        assert len(keys) == 10


class TestHashApiKey:
    def test_produces_sha256_hex(self):
        from app.core.auth import hash_api_key

        result = hash_api_key("test-key")
        assert result == hashlib.sha256(b"test-key").hexdigest()

    def test_same_input_same_output(self):
        from app.core.auth import hash_api_key

        assert hash_api_key("key") == hash_api_key("key")

    def test_different_inputs_different_outputs(self):
        from app.core.auth import hash_api_key

        assert hash_api_key("key-a") != hash_api_key("key-b")


# ---------------------------------------------------------------------------
# 5. Refresh token helpers
# ---------------------------------------------------------------------------


class TestCreateRefreshToken:
    def _make_cursor_ctx(self):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        return cur

    def test_returns_non_empty_string(self):
        from app.core.auth import create_refresh_token

        cur = self._make_cursor_ctx()
        with patch("app.core.auth.get_cursor", return_value=cur):
            token = create_refresh_token("user-1", "org-1")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_each_call_returns_unique_token(self):
        from app.core.auth import create_refresh_token

        cur = self._make_cursor_ctx()
        with patch("app.core.auth.get_cursor", return_value=cur):
            token_a = create_refresh_token("user-1", "org-1")
            token_b = create_refresh_token("user-1", "org-1")
        assert token_a != token_b

    def test_inserts_hash_not_raw_token(self):
        """Verifies that the token hash (not the raw token) is persisted."""
        from app.core.auth import create_refresh_token

        cur = self._make_cursor_ctx()
        with patch("app.core.auth.get_cursor", return_value=cur):
            raw_token = create_refresh_token("user-1", "org-1")

        execute_call = cur.execute.call_args
        # Second positional arg is the tuple of params
        params = execute_call[0][1]
        stored_hash = params[2]
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        assert stored_hash == expected_hash


class TestVerifyAndRotateRefreshToken:
    def _active_row(self, user_id="user-1", org_id="org-1"):
        return {
            "id": "rt-id-1",
            "user_id": user_id,
            "org_id": org_id,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "revoked_at": None,
            "email": "admin@test.com",
            "role": "admin",
            "is_active": True,
            "org_is_active": True,
        }

    def _make_cursor_ctx(self, row):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = row
        return cur

    @pytest.mark.asyncio
    async def test_missing_token_raises_401(self):
        from app.core.auth import verify_and_rotate_refresh_token

        with pytest.raises(HTTPException) as exc_info:
            await verify_and_rotate_refresh_token("")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_token_raises_401(self):
        from app.core.auth import verify_and_rotate_refresh_token

        cur = self._make_cursor_ctx(None)
        with patch("app.core.auth.get_cursor", return_value=cur):
            with pytest.raises(HTTPException) as exc_info:
                await verify_and_rotate_refresh_token("unknown-token")
        assert exc_info.value.status_code == 401
        assert "invalid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_revoked_token_raises_401(self):
        from app.core.auth import verify_and_rotate_refresh_token

        row = self._active_row()
        row["revoked_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
        cur = self._make_cursor_ctx(row)
        with patch("app.core.auth.get_cursor", return_value=cur):
            with pytest.raises(HTTPException) as exc_info:
                await verify_and_rotate_refresh_token("some-token")
        assert exc_info.value.status_code == 401
        assert "revoked" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_expired_token_raises_401(self):
        from app.core.auth import verify_and_rotate_refresh_token

        row = self._active_row()
        row["expires_at"] = datetime.now(timezone.utc) - timedelta(days=1)
        cur = self._make_cursor_ctx(row)
        with patch("app.core.auth.get_cursor", return_value=cur):
            with pytest.raises(HTTPException) as exc_info:
                await verify_and_rotate_refresh_token("some-token")
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_inactive_user_raises_401(self):
        from app.core.auth import verify_and_rotate_refresh_token

        row = self._active_row()
        row["is_active"] = False
        cur = self._make_cursor_ctx(row)
        with patch("app.core.auth.get_cursor", return_value=cur):
            with pytest.raises(HTTPException) as exc_info:
                await verify_and_rotate_refresh_token("some-token")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_returns_new_pair(self):
        from app.core.auth import verify_and_rotate_refresh_token

        row = self._active_row()
        cursors = [
            self._make_cursor_ctx(row),  # SELECT
            self._make_cursor_ctx(None),  # UPDATE revoked_at
            self._make_cursor_ctx(None),  # INSERT new refresh token
        ]
        call_count = [0]

        def side_effect():
            idx = call_count[0]
            call_count[0] += 1
            return cursors[min(idx, len(cursors) - 1)]

        with (
            patch("app.core.auth.get_cursor", side_effect=side_effect),
            patch("app.core.auth.JWT_SECRET_KEY", "test-secret"),
        ):
            result = await verify_and_rotate_refresh_token("valid-token")

        assert "access_token" in result
        assert "refresh_token" in result
        assert result["access_token"] != result["refresh_token"]


class TestRevokeRefreshToken:
    def _make_cursor_ctx(self):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        return cur

    def test_empty_token_is_noop(self):
        from app.core.auth import revoke_refresh_token

        with patch("app.core.auth.get_cursor") as mock_ctx:
            revoke_refresh_token("")
            mock_ctx.assert_not_called()

    def test_valid_token_calls_update(self):
        from app.core.auth import revoke_refresh_token

        cur = self._make_cursor_ctx()
        with patch("app.core.auth.get_cursor", return_value=cur):
            revoke_refresh_token("some-raw-token")
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "revoked_at" in sql.lower()

    def test_db_error_does_not_raise(self):
        """revoke_refresh_token is best-effort — DB errors must be swallowed."""
        from app.core.auth import revoke_refresh_token

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute.side_effect = Exception("db down")
        with patch("app.core.auth.get_cursor", return_value=cur):
            revoke_refresh_token("some-token")  # must not raise
