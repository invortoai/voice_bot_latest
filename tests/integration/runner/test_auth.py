"""Integration tests for POST /auth/login endpoint.

Covers:
- Happy path: valid credentials → JWT returned
- Invalid password → 401 with generic message (no enumeration)
- Unknown email → 401 with generic message
- Inactive user → 401
- Inactive org → 401
- Token payload contains expected claims
"""

import hashlib

import jwt
import psycopg2


class TestLoginHappyPath:
    async def test_valid_credentials_return_200(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        assert resp.status_code == 200

    async def test_response_contains_access_token(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        data = resp.json()
        assert "access_token" in data
        assert data["access_token"]  # non-empty string

    async def test_response_token_type_is_bearer(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        assert resp.json()["token_type"] == "bearer"

    async def test_response_contains_expires_in_minutes(
        self, runner_client, test_tenant
    ):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        data = resp.json()
        assert "expires_in_minutes" in data
        assert isinstance(data["expires_in_minutes"], int)
        assert data["expires_in_minutes"] > 0

    async def test_token_contains_org_id(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        token = resp.json()["access_token"]
        payload = jwt.decode(token, options={"verify_signature": False})
        assert payload["org_id"] == test_tenant["org_id"]

    async def test_token_contains_user_id(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        token = resp.json()["access_token"]
        payload = jwt.decode(token, options={"verify_signature": False})
        assert payload["user_id"] == test_tenant["user_id"]

    async def test_token_contains_admin_role(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        token = resp.json()["access_token"]
        payload = jwt.decode(token, options={"verify_signature": False})
        assert payload["role"] == "admin"

    async def test_email_match_is_case_insensitive(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"].upper(),
                "password_hash": test_tenant["password_hash"],
            },
        )
        assert resp.status_code == 200


class TestLoginFailures:
    async def test_wrong_password_returns_401(self, runner_client, test_tenant):
        wrong_hash = hashlib.sha256(b"definitely-wrong-password").hexdigest()
        resp = await runner_client.post(
            "/auth/login",
            json={"email": test_tenant["email"], "password_hash": wrong_hash},
        )
        assert resp.status_code == 401

    async def test_wrong_password_detail_is_generic(self, runner_client, test_tenant):
        """Error message must not reveal whether the account exists."""
        wrong_hash = hashlib.sha256(b"wrong").hexdigest()
        resp = await runner_client.post(
            "/auth/login",
            json={"email": test_tenant["email"], "password_hash": wrong_hash},
        )
        assert resp.json()["detail"] == "Invalid credentials"

    async def test_unknown_email_returns_401(self, runner_client):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": "nobody@example.com",
                "password_hash": hashlib.sha256(b"whatever").hexdigest(),
            },
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid credentials"

    async def test_inactive_user_returns_401(
        self, runner_client, test_tenant, pg_container
    ):
        """A deactivated user cannot log in even with correct credentials."""
        conn = psycopg2.connect(pg_container)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE org_users SET is_active = FALSE WHERE id = %s",
                    (test_tenant["user_id"],),
                )
            resp = await runner_client.post(
                "/auth/login",
                json={
                    "email": test_tenant["email"],
                    "password_hash": test_tenant["password_hash"],
                },
            )
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Invalid credentials"
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE org_users SET is_active = TRUE WHERE id = %s",
                    (test_tenant["user_id"],),
                )
            conn.close()

    async def test_inactive_org_returns_401(
        self, runner_client, test_tenant, pg_container
    ):
        """Users in a locked-out org cannot log in."""
        conn = psycopg2.connect(pg_container)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE organizations SET is_active = FALSE WHERE id = %s",
                    (test_tenant["org_id"],),
                )
            resp = await runner_client.post(
                "/auth/login",
                json={
                    "email": test_tenant["email"],
                    "password_hash": test_tenant["password_hash"],
                },
            )
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Invalid credentials"
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE organizations SET is_active = TRUE WHERE id = %s",
                    (test_tenant["org_id"],),
                )
            conn.close()

    async def test_missing_email_returns_422(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={"password_hash": test_tenant["password_hash"]},
        )
        assert resp.status_code == 422

    async def test_missing_password_hash_returns_422(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={"email": test_tenant["email"]},
        )
        assert resp.status_code == 422


class TestLoginRefreshToken:
    """Login response includes a refresh token."""

    async def test_response_contains_refresh_token(self, runner_client, test_tenant):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        data = resp.json()
        assert "refresh_token" in data
        assert data["refresh_token"]

    async def test_response_contains_refresh_expires_in_days(
        self, runner_client, test_tenant
    ):
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        data = resp.json()
        assert "refresh_expires_in_days" in data
        assert isinstance(data["refresh_expires_in_days"], int)
        assert data["refresh_expires_in_days"] > 0


class TestRefreshEndpoint:
    """POST /auth/refresh — rotate refresh token."""

    async def _login(self, runner_client, test_tenant) -> dict:
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        return resp.json()

    async def test_valid_refresh_token_returns_200(self, runner_client, test_tenant):
        tokens = await self._login(runner_client, test_tenant)
        resp = await runner_client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 200

    async def test_refresh_response_contains_access_token(
        self, runner_client, test_tenant
    ):
        tokens = await self._login(runner_client, test_tenant)
        resp = await runner_client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        data = resp.json()
        assert "access_token" in data
        assert data["access_token"]

    async def test_refresh_response_contains_new_refresh_token(
        self, runner_client, test_tenant
    ):
        tokens = await self._login(runner_client, test_tenant)
        resp = await runner_client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        data = resp.json()
        assert "refresh_token" in data
        assert data["refresh_token"]

    async def test_refresh_token_is_rotated(self, runner_client, test_tenant):
        """Old refresh token must not be usable after rotation."""
        tokens = await self._login(runner_client, test_tenant)
        old_refresh = tokens["refresh_token"]
        await runner_client.post("/auth/refresh", json={"refresh_token": old_refresh})
        # Using the old token a second time must fail
        resp = await runner_client.post(
            "/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert resp.status_code == 401

    async def test_refresh_response_contains_expires_in_minutes(
        self, runner_client, test_tenant
    ):
        tokens = await self._login(runner_client, test_tenant)
        resp = await runner_client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        data = resp.json()
        assert "expires_in_minutes" in data
        assert isinstance(data["expires_in_minutes"], int)
        assert data["expires_in_minutes"] > 0

    async def test_invalid_refresh_token_returns_401(self, runner_client):
        resp = await runner_client.post(
            "/auth/refresh", json={"refresh_token": "not-a-real-token"}
        )
        assert resp.status_code == 401

    async def test_missing_refresh_token_field_returns_422(self, runner_client):
        resp = await runner_client.post("/auth/refresh", json={})
        assert resp.status_code == 422


class TestLogoutEndpoint:
    """POST /auth/logout — revoke refresh token."""

    async def _login(self, runner_client, test_tenant) -> dict:
        resp = await runner_client.post(
            "/auth/login",
            json={
                "email": test_tenant["email"],
                "password_hash": test_tenant["password_hash"],
            },
        )
        return resp.json()

    async def test_logout_returns_204(self, runner_client, test_tenant):
        tokens = await self._login(runner_client, test_tenant)
        resp = await runner_client.post(
            "/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 204

    async def test_logout_revokes_refresh_token(self, runner_client, test_tenant):
        """After logout the refresh token must no longer be usable."""
        tokens = await self._login(runner_client, test_tenant)
        refresh = tokens["refresh_token"]
        await runner_client.post("/auth/logout", json={"refresh_token": refresh})
        resp = await runner_client.post(
            "/auth/refresh", json={"refresh_token": refresh}
        )
        assert resp.status_code == 401

    async def test_logout_with_invalid_token_still_returns_204(self, runner_client):
        """Logout is best-effort — unknown tokens must not cause an error."""
        resp = await runner_client.post(
            "/auth/logout", json={"refresh_token": "unknown-garbage-token"}
        )
        assert resp.status_code == 204

    async def test_double_logout_returns_204(self, runner_client, test_tenant):
        """Calling logout twice on the same token must not raise."""
        tokens = await self._login(runner_client, test_tenant)
        refresh = tokens["refresh_token"]
        await runner_client.post("/auth/logout", json={"refresh_token": refresh})
        resp = await runner_client.post("/auth/logout", json={"refresh_token": refresh})
        assert resp.status_code == 204
