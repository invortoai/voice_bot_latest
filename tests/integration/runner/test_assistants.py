"""Integration tests for the /assistants CRUD API endpoints."""

import pytest


ASSISTANT_PAYLOAD = {
    "name": "Test Bot",
    "system_prompt": "You are a helpful test assistant.",
    "llm_provider": "openai",
    "model": "gpt-4.1-nano",
    "llm_settings": {"temperature": 0.7, "max_completion_tokens": 150},
    "voice_provider": "elevenlabs",
    "voice_id": "voice-test-123",
    "greeting_message": "Hello from test!",
    "end_call_phrases": ["goodbye", "bye"],
    "transcriber_provider": "deepgram",
    "transcriber_model": "nova-2",
    "transcriber_language": "en",
    "vad_settings": {"confidence": 0.8, "stop_secs": 1.0},
}


class TestCreateAssistant:
    async def test_create_returns_201_or_200_with_id(self, runner_client):
        resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert "id" in data
        assert data["name"] == "Test Bot"

    async def test_create_persists_all_fields(self, runner_client):
        resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        data = resp.json()
        assert data["llm_provider"] == "openai"
        assert data["model"] == "gpt-4.1-nano"
        assert float(data["llm_settings"]["temperature"]) == pytest.approx(0.7)
        assert data["llm_settings"]["max_completion_tokens"] == 150
        assert data["voice_provider"] == "elevenlabs"
        assert data["greeting_message"] == "Hello from test!"
        assert "goodbye" in (data.get("end_call_phrases") or [])

    async def test_create_with_vad_settings(self, runner_client):
        resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        data = resp.json()
        assert data.get("vad_settings") is not None
        assert data["vad_settings"].get("confidence") == pytest.approx(0.8)

    async def test_create_missing_required_field_returns_422(self, runner_client):
        resp = await runner_client.post("/assistants", json={"name": "Bot only"})
        assert resp.status_code == 422

    async def test_create_temperature_zero(self, runner_client):
        payload = {
            **ASSISTANT_PAYLOAD,
            "llm_settings": {"temperature": 0.0, "max_completion_tokens": 150},
        }
        resp = await runner_client.post("/assistants", json=payload)
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert float(data["llm_settings"]["temperature"]) == pytest.approx(0.0)


class TestListAssistants:
    async def test_list_empty_returns_empty(self, runner_client):
        resp = await runner_client.get("/assistants")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["assistants"] == []

    async def test_list_after_create(self, runner_client):
        await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        resp = await runner_client.get("/assistants")
        data = resp.json()
        assert data["total"] == 1
        assert data["assistants"][0]["name"] == "Test Bot"

    async def test_list_multiple(self, runner_client):
        for i in range(3):
            payload = {**ASSISTANT_PAYLOAD, "name": f"Bot {i}"}
            await runner_client.post("/assistants", json=payload)

        resp = await runner_client.get("/assistants")
        data = resp.json()
        assert data["total"] == 3


class TestGetAssistant:
    async def test_get_existing(self, runner_client):
        create = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        asst_id = create.json()["id"]

        resp = await runner_client.get(f"/assistants/{asst_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == asst_id

    async def test_get_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.get(
            "/assistants/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404


class TestUpdateAssistant:
    async def test_patch_name(self, runner_client):
        create = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        asst_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/assistants/{asst_id}", json={"name": "Updated Bot"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Bot"

    async def test_patch_llm_settings(self, runner_client):
        create = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        asst_id = create.json()["id"]

        resp = await runner_client.patch(
            f"/assistants/{asst_id}",
            json={"llm_settings": {"temperature": 0.0, "max_completion_tokens": 200}},
        )
        assert resp.status_code == 200
        assert float(resp.json()["llm_settings"]["temperature"]) == pytest.approx(0.0)
        assert resp.json()["llm_settings"]["max_completion_tokens"] == 200

    async def test_patch_vad_settings(self, runner_client):
        create = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        asst_id = create.json()["id"]

        new_vad = {"confidence": 0.95, "stop_secs": 2.0}
        resp = await runner_client.patch(
            f"/assistants/{asst_id}", json={"vad_settings": new_vad}
        )
        assert resp.status_code == 200
        assert resp.json()["vad_settings"]["confidence"] == pytest.approx(0.95)

    async def test_patch_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.patch(
            "/assistants/00000000-0000-0000-0000-000000000000",
            json={"name": "Ghost"},
        )
        assert resp.status_code == 404

    async def test_patch_empty_body_returns_400(self, runner_client):
        create = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        asst_id = create.json()["id"]

        resp = await runner_client.patch(f"/assistants/{asst_id}", json={})
        assert resp.status_code == 400


class TestDeleteAssistant:
    async def test_delete_existing(self, runner_client):
        create = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
        asst_id = create.json()["id"]

        resp = await runner_client.delete(f"/assistants/{asst_id}")
        assert resp.status_code == 200

        # Verify it's gone
        get_resp = await runner_client.get(f"/assistants/{asst_id}")
        assert get_resp.status_code == 404

    async def test_delete_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.delete(
            "/assistants/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404
