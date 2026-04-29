"""Integration tests for the /phone-numbers CRUD API endpoints."""

import pytest


ASSISTANT_PAYLOAD = {
    "name": "Test Bot",
    "system_prompt": "You are a test assistant.",
}

PHONE_PAYLOAD = {
    "phone_number": "+15005550006",
    "friendly_name": "Test Line",
    "provider": "mcube",
    "provider_credentials": {"token": "mc-test-token"},
    "is_inbound_enabled": True,
    "is_outbound_enabled": True,
    "max_call_duration_seconds": 1800,
}


@pytest.fixture
async def created_assistant(runner_client):
    resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
    return resp.json()


class TestCreatePhoneNumber:
    async def test_create_without_assistant(self, runner_client):
        resp = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert "id" in data
        assert data["phone_number"] == "+15005550006"

    async def test_create_with_assistant(self, runner_client, created_assistant):
        payload = {**PHONE_PAYLOAD, "assistant_id": created_assistant["id"]}
        resp = await runner_client.post("/phone-numbers", json=payload)
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert str(data["assistant_id"]) == created_assistant["id"]

    async def test_create_missing_phone_number_raises_422(self, runner_client):
        resp = await runner_client.post("/phone-numbers", json={"provider": "mcube"})
        assert resp.status_code == 422

    async def test_create_stores_provider_credentials(self, runner_client):
        resp = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
        data = resp.json()
        # Sensitive credential fields are masked in API responses
        assert data["provider_credentials"]["token"] == "***"

    async def test_create_twilio_provider(self, runner_client):
        payload = {
            **PHONE_PAYLOAD,
            "phone_number": "+15005550007",
            "provider": "twilio",
            "provider_credentials": {
                "account_sid": "AC123",
                "auth_token": "token123",
                "sid": "PN123",
            },
        }
        resp = await runner_client.post("/phone-numbers", json=payload)
        assert resp.status_code in (200, 201)
        assert resp.json()["provider"] == "twilio"


class TestListPhoneNumbers:
    async def test_list_empty(self, runner_client):
        resp = await runner_client.get("/phone-numbers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    async def test_list_after_create(self, runner_client):
        await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
        resp = await runner_client.get("/phone-numbers")
        data = resp.json()
        assert data["total"] == 1


class TestGetPhoneNumber:
    async def test_get_existing(self, runner_client):
        create = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
        pn_id = create.json()["id"]

        resp = await runner_client.get(f"/phone-numbers/{pn_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == pn_id

    async def test_get_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.get(
            "/phone-numbers/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404


class TestUpdatePhoneNumber:
    async def test_patch_friendly_name(self, runner_client):
        create = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
        pn_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/phone-numbers/{pn_id}", json={"friendly_name": "Updated Name"}
        )
        assert resp.status_code == 200
        assert resp.json()["friendly_name"] == "Updated Name"

    async def test_patch_assign_assistant(self, runner_client, created_assistant):
        create = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
        pn_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/phone-numbers/{pn_id}",
            json={"assistant_id": created_assistant["id"]},
        )
        assert resp.status_code == 200
        assert str(resp.json()["assistant_id"]) == created_assistant["id"]

    async def test_patch_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.patch(
            "/phone-numbers/00000000-0000-0000-0000-000000000000",
            json={"friendly_name": "Ghost"},
        )
        assert resp.status_code == 404


class TestDeletePhoneNumber:
    async def test_delete_existing(self, runner_client):
        create = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
        pn_id = create.json()["id"]

        resp = await runner_client.delete(f"/phone-numbers/{pn_id}")
        assert resp.status_code == 200

        get_resp = await runner_client.get(f"/phone-numbers/{pn_id}")
        assert get_resp.status_code == 404

    async def test_delete_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.delete(
            "/phone-numbers/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404
