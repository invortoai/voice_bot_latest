"""Integration tests for /api-keys endpoints.

Covers:
- GET /api-keys            — list keys (any authenticated user)
- POST /api-keys           — create key (admin only)
- PATCH /api-keys/{id}     — update name / active status / scopes
- DELETE /api-keys/{id}    — delete key (admin only)
- POST /api-keys/{id}/rotate — rotate key (admin only)

Security:
- All endpoints require a valid JWT Bearer token
- Status/scope changes require admin role
- Create/delete/rotate require admin role
- Total key limit (max_api_keys) and active key limit (max_active_api_keys)
- Name uniqueness within org
"""

import hashlib

import psycopg2
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_token(runner_client, test_tenant):
    """Get a valid admin JWT for the test org."""
    resp = await runner_client.post(
        "/auth/login",
        json={
            "email": test_tenant["email"],
            "password_hash": test_tenant["password_hash"],
        },
    )
    assert resp.status_code == 200, f"Login failed: {resp.json()}"
    return resp.json()["access_token"]


@pytest.fixture
async def member_user(test_tenant, pg_container):
    """Create a member-role user in the test org. Yields user info, then deletes."""
    pw_hash = hashlib.sha256(b"member-password-secret").hexdigest()
    conn = psycopg2.connect(pg_container)
    conn.autocommit = True
    user_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO org_users (org_id, email, name, role, is_active, password_hash)
                VALUES (%s, 'member@testorg.com', 'Test Member', 'member', TRUE, %s)
                RETURNING id
                """,
                (test_tenant["org_id"], pw_hash),
            )
            user_id = str(cur.fetchone()[0])
        yield {
            "user_id": user_id,
            "email": "member@testorg.com",
            "password_hash": pw_hash,
        }
    finally:
        if user_id:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM org_users WHERE id = %s", (user_id,))
        conn.close()


@pytest.fixture
async def member_token(runner_client, member_user):
    """Get a valid member JWT for the test org."""
    resp = await runner_client.post(
        "/auth/login",
        json={
            "email": member_user["email"],
            "password_hash": member_user["password_hash"],
        },
    )
    assert resp.status_code == 200, f"Member login failed: {resp.json()}"
    return resp.json()["access_token"]


CREATE_KEY_PAYLOAD = {"name": "my-integration-key", "scopes": ["read", "write"]}


# ---------------------------------------------------------------------------
# List API keys
# ---------------------------------------------------------------------------


class TestListApiKeys:
    async def test_list_requires_jwt(self, runner_client):
        """Missing Authorization header → 401."""
        resp = await runner_client.get("/api-keys")
        assert resp.status_code == 401

    async def test_list_returns_test_key(self, runner_client, admin_token):
        resp = await runner_client.get(
            "/api-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "api_keys" in data
        assert data["total"] >= 1
        names = [k["name"] for k in data["api_keys"]]
        assert "test-key" in names

    async def test_list_does_not_return_key_hash(self, runner_client, admin_token):
        """key_hash is a security secret and must never appear in responses."""
        resp = await runner_client.get(
            "/api-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        for key in resp.json()["api_keys"]:
            assert "key_hash" not in key

    async def test_list_accessible_by_member(self, runner_client, member_token):
        """Members can list keys (read-only access)."""
        resp = await runner_client.get(
            "/api-keys",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 200

    async def test_list_key_contains_expected_fields(self, runner_client, admin_token):
        resp = await runner_client.get(
            "/api-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key = resp.json()["api_keys"][0]
        for field in ["id", "name", "key_prefix", "is_active", "scopes", "created_at"]:
            assert field in key, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Create API keys
# ---------------------------------------------------------------------------


class TestCreateApiKey:
    async def test_create_requires_jwt(self, runner_client):
        resp = await runner_client.post("/api-keys", json=CREATE_KEY_PAYLOAD)
        assert resp.status_code == 401

    async def test_create_requires_admin(self, runner_client, member_token):
        """Members cannot create keys."""
        resp = await runner_client.post(
            "/api-keys",
            json=CREATE_KEY_PAYLOAD,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 403

    async def test_create_returns_201(self, runner_client, admin_token):
        resp = await runner_client.post(
            "/api-keys",
            json=CREATE_KEY_PAYLOAD,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201

    async def test_create_returns_raw_key_once(self, runner_client, admin_token):
        """The raw key value is returned only at creation time."""
        resp = await runner_client.post(
            "/api-keys",
            json=CREATE_KEY_PAYLOAD,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        data = resp.json()
        assert "key" in data
        assert data["key"].startswith("inv_")

    async def test_create_response_has_warning(self, runner_client, admin_token):
        resp = await runner_client.post(
            "/api-keys",
            json=CREATE_KEY_PAYLOAD,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert "warning" in resp.json()

    async def test_create_response_contains_name_and_prefix(
        self, runner_client, admin_token
    ):
        resp = await runner_client.post(
            "/api-keys",
            json=CREATE_KEY_PAYLOAD,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        data = resp.json()
        assert data["name"] == "my-integration-key"
        assert "key_prefix" in data
        assert len(data["key_prefix"]) == 8

    async def test_create_key_appears_in_list(self, runner_client, admin_token):
        await runner_client.post(
            "/api-keys",
            json=CREATE_KEY_PAYLOAD,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        list_resp = await runner_client.get(
            "/api-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        names = [k["name"] for k in list_resp.json()["api_keys"]]
        assert "my-integration-key" in names

    async def test_create_duplicate_name_returns_409(self, runner_client, admin_token):
        """Two keys with the same name (case-insensitive) in one org → 409."""
        await runner_client.post(
            "/api-keys",
            json={"name": "duplicate-key"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        resp = await runner_client.post(
            "/api-keys",
            json={"name": "Duplicate-Key"},  # same name, different case
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 409

    async def test_create_missing_name_returns_422(self, runner_client, admin_token):
        resp = await runner_client.post(
            "/api-keys",
            json={"scopes": ["read"]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 422

    async def test_create_total_key_limit_enforced(
        self, runner_client, admin_token, test_tenant, pg_container
    ):
        """Cannot create more keys than max_api_keys allows."""
        # Set a tight limit: current count is 1 (test-key), set max to 1
        conn = psycopg2.connect(pg_container)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE organizations SET max_api_keys = 1 WHERE id = %s",
                    (test_tenant["org_id"],),
                )
            resp = await runner_client.post(
                "/api-keys",
                json={"name": "over-limit-key"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status_code == 422
            assert "limit" in resp.json()["detail"].lower()
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE organizations SET max_api_keys = 5 WHERE id = %s",
                    (test_tenant["org_id"],),
                )
            conn.close()


# ---------------------------------------------------------------------------
# Update API keys
# ---------------------------------------------------------------------------


class TestUpdateApiKey:
    async def test_update_name(self, runner_client, admin_token):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "rename-me"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/api-keys/{key_id}",
            json={"name": "renamed-key"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed-key"

    async def test_update_nonexistent_returns_404(self, runner_client, admin_token):
        resp = await runner_client.patch(
            "/api-keys/00000000-0000-0000-0000-000000000000",
            json={"name": "ghost"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 404

    async def test_update_empty_body_returns_400(self, runner_client, admin_token):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "patch-empty-test"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]
        resp = await runner_client.patch(
            f"/api-keys/{key_id}",
            json={},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 400

    async def test_deactivate_key(self, runner_client, admin_token):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "deactivate-me"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/api-keys/{key_id}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_member_cannot_change_active_status(
        self, runner_client, admin_token, member_token
    ):
        """Active-status changes require admin role."""
        create = await runner_client.post(
            "/api-keys",
            json={"name": "member-status-test"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/api-keys/{key_id}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 403

    async def test_member_cannot_change_scopes(
        self, runner_client, admin_token, member_token
    ):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "member-scope-test"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/api-keys/{key_id}",
            json={"scopes": ["admin"]},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 403

    async def test_activate_respects_active_limit(
        self, runner_client, admin_token, test_tenant, pg_container
    ):
        """Activating a key that would exceed max_active_api_keys → 409."""
        # Set limit to 1 (test-key already active), then try to activate a second
        conn = psycopg2.connect(pg_container)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE organizations SET max_active_api_keys = 1 WHERE id = %s",
                    (test_tenant["org_id"],),
                )
            # Create an inactive key
            create = await runner_client.post(
                "/api-keys",
                json={"name": "inactive-key"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            key_id = create.json()["id"]
            # Deactivate it first (it's active by default)
            await runner_client.patch(
                f"/api-keys/{key_id}",
                json={"is_active": False},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            # Now try to re-activate — should fail (test-key already uses the 1 slot)
            resp = await runner_client.patch(
                f"/api-keys/{key_id}",
                json={"is_active": True},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status_code == 409
            assert "active key limit" in resp.json()["detail"].lower()
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE organizations SET max_active_api_keys = 5 WHERE id = %s",
                    (test_tenant["org_id"],),
                )
            conn.close()


# ---------------------------------------------------------------------------
# Delete API keys
# ---------------------------------------------------------------------------


class TestDeleteApiKey:
    async def test_delete_requires_admin(
        self, runner_client, admin_token, member_token
    ):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "delete-perm-test"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]

        resp = await runner_client.delete(
            f"/api-keys/{key_id}",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 403

    async def test_delete_returns_204(self, runner_client, admin_token):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "delete-me"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]

        resp = await runner_client.delete(
            f"/api-keys/{key_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 204

    async def test_deleted_key_not_in_list(self, runner_client, admin_token):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "gone-after-delete"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]
        await runner_client.delete(
            f"/api-keys/{key_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        list_resp = await runner_client.get(
            "/api-keys",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        ids = [k["id"] for k in list_resp.json()["api_keys"]]
        assert key_id not in ids

    async def test_delete_nonexistent_returns_404(self, runner_client, admin_token):
        resp = await runner_client.delete(
            "/api-keys/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Rotate API keys
# ---------------------------------------------------------------------------


class TestRotateApiKey:
    async def test_rotate_requires_admin(
        self, runner_client, admin_token, member_token
    ):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "rotate-perm-test"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]

        resp = await runner_client.post(
            f"/api-keys/{key_id}/rotate",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 403

    async def test_rotate_returns_201_with_new_key(self, runner_client, admin_token):
        create = await runner_client.post(
            "/api-keys",
            json={"name": "rotate-me"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        original_prefix = create.json()["key_prefix"]
        key_id = create.json()["id"]

        resp = await runner_client.post(
            f"/api-keys/{key_id}/rotate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "key" in data
        assert data["key"].startswith("inv_")
        # prefix should change after rotation
        assert data["key_prefix"] != original_prefix

    async def test_rotate_preserves_is_active_status(self, runner_client, admin_token):
        """Rotation must preserve the original active status (gap #3 fix)."""
        create = await runner_client.post(
            "/api-keys",
            json={"name": "rotate-inactive"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]
        # Deactivate the key first
        await runner_client.patch(
            f"/api-keys/{key_id}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        rotate_resp = await runner_client.post(
            f"/api-keys/{key_id}/rotate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert rotate_resp.status_code == 201
        # Key should still be inactive after rotation
        assert rotate_resp.json()["is_active"] is False

    async def test_rotated_key_is_usable_for_data_requests(
        self, runner_client, admin_token
    ):
        """The new key returned by rotate should authenticate data-endpoint requests."""
        create = await runner_client.post(
            "/api-keys",
            json={"name": "rotate-and-use"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        key_id = create.json()["id"]
        rotate = await runner_client.post(
            f"/api-keys/{key_id}/rotate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        new_raw_key = rotate.json()["key"]

        # Use the rotated key to hit a data endpoint
        resp = await runner_client.get(
            "/assistants",
            headers={"X-API-Key": new_raw_key},
        )
        assert resp.status_code == 200

    async def test_rotate_nonexistent_returns_404(self, runner_client, admin_token):
        resp = await runner_client.post(
            "/api-keys/00000000-0000-0000-0000-000000000000/rotate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 404
