"""Integration tests for /v1/insights/config (CRUD).

Auth model:
  - API_KEY="" in tests → verify_global_key_with_org bypasses key check
  - X-Org-ID header (from runner_client default headers) identifies the org
  - runner_client sends X-Org-ID: test_org_id by default
  - For cross-org isolation tests: pass headers={"X-Org-ID": other_org} per-request
"""

import uuid

import pytest

CONFIG_URL = "/insights/config"


# =============================================================================
# POST /v1/insights/config
# =============================================================================


class TestCreateConfig:
    async def test_all_defaults_returns_201(self, runner_client, test_org_id):
        resp = await runner_client.post(CONFIG_URL, json={})
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["org_id"] == test_org_id
        assert data["name"] == "default"
        assert data["stt_provider"] == "deepgram"
        assert data["stt_model"] == "nova-2"
        assert data["llm_provider"] == "anthropic"
        assert data["llm_model"] == "claude-sonnet-4-20250514"
        assert float(data["llm_temperature"]) == pytest.approx(0.1)
        assert data["enable_summary"] is True
        assert data["enable_actionable_insights"] is True
        assert "created_at" in data

    async def test_custom_name_model_temperature(self, runner_client):
        resp = await runner_client.post(
            CONFIG_URL,
            json={
                "name": "my-config",
                "llm_model": "claude-opus-4-6",
                "llm_temperature": 0.5,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "my-config"
        assert data["llm_model"] == "claude-opus-4-6"
        assert float(data["llm_temperature"]) == pytest.approx(0.5)

    async def test_callback_url_and_secret(self, runner_client):
        resp = await runner_client.post(
            CONFIG_URL,
            json={
                "callback_url": "https://example.com/webhook",
                "callback_secret": "sec123",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["callback_url"] == "https://example.com/webhook"
        assert data["callback_secret"] == "sec123"

    async def test_temperature_boundary_zero(self, runner_client):
        resp = await runner_client.post(CONFIG_URL, json={"llm_temperature": 0.0})
        assert resp.status_code == 201
        assert float(resp.json()["llm_temperature"]) == pytest.approx(0.0)

    async def test_temperature_boundary_two(self, runner_client):
        resp = await runner_client.post(CONFIG_URL, json={"llm_temperature": 2.0})
        assert resp.status_code == 201
        assert float(resp.json()["llm_temperature"]) == pytest.approx(2.0)

    async def test_custom_fields_dict_stored_and_returned(self, runner_client):
        """custom_fields_schema (dict) is stored as JSONB and returned."""
        custom = {"monthly_salary": {"type": "number", "required": True}}
        resp = await runner_client.post(
            CONFIG_URL, json={"custom_fields_schema": custom}
        )
        assert resp.status_code == 201
        assert resp.json()["custom_fields_schema"] == custom

    async def test_duplicate_name_same_org_returns_409(self, runner_client):
        """Unique constraint on (org_id, name) — duplicate name returns 409."""
        r1 = await runner_client.post(CONFIG_URL, json={"name": "dupe"})
        r2 = await runner_client.post(CONFIG_URL, json={"name": "dupe"})
        assert r1.status_code == 201
        assert r2.status_code == 409

    async def test_temperature_above_max_returns_422(self, runner_client):
        resp = await runner_client.post(CONFIG_URL, json={"llm_temperature": 2.1})
        assert resp.status_code == 422

    async def test_temperature_negative_returns_422(self, runner_client):
        resp = await runner_client.post(CONFIG_URL, json={"llm_temperature": -0.1})
        assert resp.status_code == 422

    async def test_custom_fields_as_list_returns_422(self, runner_client):
        """custom_fields_schema must be a JSON object (dict), not an array."""
        resp = await runner_client.post(
            CONFIG_URL,
            json={"custom_fields_schema": [{"field": "name", "type": "string"}]},
        )
        assert resp.status_code == 422

    async def test_missing_api_key_returns_401(self, runner_client):
        resp = await runner_client.post(CONFIG_URL, json={}, headers={"X-API-Key": ""})
        assert resp.status_code == 401


# =============================================================================
# GET /v1/insights/config
# =============================================================================


class TestListConfigs:
    async def test_empty_org_returns_empty_list(self, runner_client):
        resp = await runner_client.get(CONFIG_URL)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_all_configs_for_org(self, runner_client):
        await runner_client.post(CONFIG_URL, json={"name": "cfg-1"})
        await runner_client.post(CONFIG_URL, json={"name": "cfg-2"})
        resp = await runner_client.get(CONFIG_URL)
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "cfg-1" in names
        assert "cfg-2" in names

    async def test_does_not_return_other_org_configs(self, runner_client, test_org_id):
        """List as a different org returns empty — configs belong to test org only."""
        await runner_client.post(CONFIG_URL, json={"name": "test-cfg"})
        other_org = str(uuid.uuid4())
        resp = await runner_client.get(CONFIG_URL, headers={"X-Org-ID": other_org})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_missing_api_key_returns_401(self, runner_client):
        resp = await runner_client.get(CONFIG_URL, headers={"X-API-Key": ""})
        assert resp.status_code == 401


# =============================================================================
# GET /v1/insights/config/{config_id}
# =============================================================================


class TestGetConfig:
    async def test_get_existing_returns_200(self, runner_client):
        create = await runner_client.post(CONFIG_URL, json={"name": "fetchable"})
        config_id = create.json()["id"]
        resp = await runner_client.get(f"{CONFIG_URL}/{config_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == config_id
        assert resp.json()["name"] == "fetchable"

    async def test_get_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.get(f"{CONFIG_URL}/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_get_other_org_config_returns_404(self, runner_client):
        """Org isolation: requesting another org's config by ID returns 404."""
        create = await runner_client.post(CONFIG_URL, json={"name": "cross-org"})
        config_id = create.json()["id"]
        other_org = str(uuid.uuid4())
        resp = await runner_client.get(
            f"{CONFIG_URL}/{config_id}", headers={"X-Org-ID": other_org}
        )
        assert resp.status_code == 404

    async def test_invalid_uuid_returns_422(self, runner_client):
        resp = await runner_client.get(f"{CONFIG_URL}/not-a-valid-uuid")
        assert resp.status_code == 422


# =============================================================================
# PUT /v1/insights/config/{config_id}
# =============================================================================


class TestUpdateConfig:
    async def _create(self, runner_client, **kw) -> str:
        r = await runner_client.post(CONFIG_URL, json=kw)
        assert r.status_code == 201
        return r.json()["id"]

    async def test_update_name(self, runner_client):
        config_id = await self._create(runner_client, name="original")
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"name": "updated"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated"

    async def test_update_llm_model(self, runner_client):
        config_id = await self._create(runner_client)
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"llm_model": "gpt-4o"}
        )
        assert resp.status_code == 200
        assert resp.json()["llm_model"] == "gpt-4o"

    async def test_empty_body_returns_unchanged(self, runner_client):
        config_id = await self._create(runner_client, name="no-change")
        resp = await runner_client.put(f"{CONFIG_URL}/{config_id}", json={})
        assert resp.status_code == 200
        assert resp.json()["name"] == "no-change"

    async def test_update_temperature_zero(self, runner_client):
        config_id = await self._create(runner_client)
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"llm_temperature": 0.0}
        )
        assert resp.status_code == 200
        assert float(resp.json()["llm_temperature"]) == pytest.approx(0.0)

    async def test_update_custom_fields(self, runner_client):
        config_id = await self._create(runner_client)
        custom = {"agent_name": {"type": "string", "required": False}}
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"custom_fields_schema": custom}
        )
        assert resp.status_code == 200
        assert resp.json()["custom_fields_schema"] == custom

    async def test_update_allowed_call_outcomes(self, runner_client):
        """allowed_call_outcomes is a TEXT[] column — list update must persist."""
        config_id = await self._create(runner_client)
        new_outcomes = ["sold", "not_interested", "callback"]
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"allowed_call_outcomes": new_outcomes}
        )
        assert resp.status_code == 200
        assert resp.json()["allowed_call_outcomes"] == new_outcomes

    async def test_update_enable_flags(self, runner_client):
        """Boolean enable_* flags should toggle correctly."""
        config_id = await self._create(runner_client)
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}",
            json={"enable_summary": False, "enable_sentiment": False},
        )
        assert resp.status_code == 200
        assert resp.json()["enable_summary"] is False
        assert resp.json()["enable_sentiment"] is False

    async def test_updated_at_changes_on_put(self, runner_client):
        """updated_at timestamp must advance after a PUT."""
        config_id = await self._create(runner_client)
        created_at = (await runner_client.get(f"{CONFIG_URL}/{config_id}")).json()[
            "updated_at"
        ]
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"name": "changed"}
        )
        assert resp.status_code == 200
        assert resp.json()["updated_at"] != created_at

    async def test_custom_fields_roundtrip_after_put(self, runner_client):
        """custom_fields_schema set via PUT must be readable via GET."""
        config_id = await self._create(runner_client)
        custom = {"score": {"type": "integer", "required": True}}
        await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"custom_fields_schema": custom}
        )
        resp = await runner_client.get(f"{CONFIG_URL}/{config_id}")
        assert resp.status_code == 200
        assert resp.json()["custom_fields_schema"] == custom

    async def test_temperature_above_max_returns_422(self, runner_client):
        config_id = await self._create(runner_client)
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}", json={"llm_temperature": 2.1}
        )
        assert resp.status_code == 422

    async def test_nonexistent_config_returns_404(self, runner_client):
        resp = await runner_client.put(
            f"{CONFIG_URL}/{uuid.uuid4()}", json={"name": "ghost"}
        )
        assert resp.status_code == 404

    async def test_update_other_org_config_returns_404(self, runner_client):
        """Org isolation: PUT as a different org returns 404."""
        create = await runner_client.post(CONFIG_URL, json={"name": "other-put"})
        config_id = create.json()["id"]
        other_org = str(uuid.uuid4())
        resp = await runner_client.put(
            f"{CONFIG_URL}/{config_id}",
            json={"name": "hacked"},
            headers={"X-Org-ID": other_org},
        )
        assert resp.status_code == 404


# =============================================================================
# DELETE /v1/insights/config/{config_id}
# =============================================================================


class TestDeleteConfig:
    async def test_delete_existing_returns_204(self, runner_client):
        create = await runner_client.post(CONFIG_URL, json={})
        config_id = create.json()["id"]
        resp = await runner_client.delete(f"{CONFIG_URL}/{config_id}")
        assert resp.status_code == 204
        assert (await runner_client.get(f"{CONFIG_URL}/{config_id}")).status_code == 404

    async def test_delete_nonexistent_returns_404(self, runner_client):
        resp = await runner_client.delete(f"{CONFIG_URL}/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_delete_other_org_config_returns_404(self, runner_client):
        """Org isolation: DELETE as a different org returns 404."""
        create = await runner_client.post(CONFIG_URL, json={})
        config_id = create.json()["id"]
        other_org = str(uuid.uuid4())
        resp = await runner_client.delete(
            f"{CONFIG_URL}/{config_id}", headers={"X-Org-ID": other_org}
        )
        assert resp.status_code == 404

    async def test_delete_referenced_config_returns_409(
        self, runner_client, test_api_key
    ):
        """Config referenced by call_analysis cannot be deleted — FK → 409."""
        create = await runner_client.post(CONFIG_URL, json={})
        config_id = create.json()["id"]
        # Reference this config via an analysis job
        await runner_client.post(
            "/insights/analyse",
            json={
                "audio_url": "https://example.com/recording.mp3",
                "insights_config_id": config_id,
            },
            headers={"X-API-Key": test_api_key},
        )
        resp = await runner_client.delete(f"{CONFIG_URL}/{config_id}")
        assert resp.status_code == 409
