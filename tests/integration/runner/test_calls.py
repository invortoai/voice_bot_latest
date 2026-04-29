"""Integration tests for /calls and /calls/{id} HTTP endpoints.

Tests cover:
- GET /calls list with filtering and pagination
- GET /calls/{id} lookup by UUID and by call SID
- 404 handling for unknown calls
"""

import pytest


ASSISTANT_PAYLOAD = {
    "name": "Test Bot",
    "system_prompt": "You are a test assistant.",
}

PHONE_PAYLOAD = {
    "phone_number": "+15005550010",
    "provider": "twilio",
    "provider_credentials": {"account_sid": "AC123", "auth_token": "token123"},
}


@pytest.fixture
async def created_assistant(runner_client):
    resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
    return resp.json()


@pytest.fixture
async def created_phone(runner_client):
    resp = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
    return resp.json()


def _seed_call(org_id=None, **overrides):
    """Directly seed a call record via service layer to avoid webhook complexity.

    Pass org_id so the call is visible to the authenticated test client, which
    filters all /calls responses by the test org.
    """
    from app.services import call_service

    defaults = dict(
        call_sid="CA-DEFAULT",
        direction="inbound",
        from_number="+14155551234",
        to_number="+15005550010",
        provider="twilio",
        status="initiated",
    )
    defaults.update(overrides)
    return call_service.create(org_id=org_id, **defaults)


class TestListCallsEndpoint:
    async def test_returns_200_with_empty_list(self, runner_client):
        resp = await runner_client.get("/calls")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["calls"] == []

    async def test_includes_pagination_metadata(self, runner_client):
        resp = await runner_client.get("/calls?limit=25&offset=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 25
        assert data["offset"] == 5

    async def test_returns_seeded_calls(self, runner_client, test_org_id):
        _seed_call(org_id=test_org_id, call_sid="CA-LIST-001")
        _seed_call(org_id=test_org_id, call_sid="CA-LIST-002")
        resp = await runner_client.get("/calls")
        data = resp.json()
        assert data["total"] == 2
        sids = {c["call_sid"] for c in data["calls"]}
        assert {"CA-LIST-001", "CA-LIST-002"} == sids

    async def test_filter_by_direction_inbound(self, runner_client, test_org_id):
        _seed_call(org_id=test_org_id, call_sid="CA-DIR-IN-01", direction="inbound")
        _seed_call(org_id=test_org_id, call_sid="CA-DIR-OUT-01", direction="outbound")
        resp = await runner_client.get("/calls?direction=inbound")
        calls = resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["direction"] == "inbound"

    async def test_filter_by_direction_outbound(self, runner_client, test_org_id):
        _seed_call(org_id=test_org_id, call_sid="CA-DIR-IN-02", direction="inbound")
        _seed_call(org_id=test_org_id, call_sid="CA-DIR-OUT-02", direction="outbound")
        resp = await runner_client.get("/calls?direction=outbound")
        calls = resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["direction"] == "outbound"

    async def test_filter_by_status_initiated(self, runner_client, test_org_id):
        _seed_call(org_id=test_org_id, call_sid="CA-ST-INIT", status="initiated")
        _seed_call(org_id=test_org_id, call_sid="CA-ST-COMP", status="completed")
        resp = await runner_client.get("/calls?status=initiated")
        calls = resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["status"] == "initiated"

    async def test_filter_by_status_completed(self, runner_client, test_org_id):
        _seed_call(org_id=test_org_id, call_sid="CA-ST-INIT2", status="initiated")
        _seed_call(org_id=test_org_id, call_sid="CA-ST-COMP2", status="completed")
        resp = await runner_client.get("/calls?status=completed")
        calls = resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["status"] == "completed"

    async def test_filter_by_phone_number_id(
        self, runner_client, created_phone, test_org_id
    ):
        from app.services import call_service

        call_service.create(
            call_sid="CA-PN-FILTER",
            org_id=test_org_id,
            direction="inbound",
            from_number="+1111",
            to_number="+2222",
            phone_number_id=created_phone["id"],
            provider="twilio",
        )
        _seed_call(org_id=test_org_id, call_sid="CA-NO-PN")

        resp = await runner_client.get(f"/calls?phone_number_id={created_phone['id']}")
        calls = resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["call_sid"] == "CA-PN-FILTER"

    async def test_filter_by_assistant_id(
        self, runner_client, created_assistant, test_org_id
    ):
        from app.services import call_service

        call_service.create(
            call_sid="CA-ASST-FILTER",
            org_id=test_org_id,
            direction="inbound",
            from_number="+1111",
            to_number="+2222",
            assistant_id=created_assistant["id"],
            provider="twilio",
        )
        _seed_call(org_id=test_org_id, call_sid="CA-NO-ASST")

        resp = await runner_client.get(f"/calls?assistant_id={created_assistant['id']}")
        calls = resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["call_sid"] == "CA-ASST-FILTER"

    async def test_limit_parameter_respected(self, runner_client, test_org_id):
        for i in range(5):
            _seed_call(org_id=test_org_id, call_sid=f"CA-LIMIT-{i:03d}")
        resp = await runner_client.get("/calls?limit=2")
        data = resp.json()
        assert len(data["calls"]) == 2

    async def test_no_filter_returns_all_calls(self, runner_client, test_org_id):
        for i in range(3):
            _seed_call(org_id=test_org_id, call_sid=f"CA-ALL-{i:03d}")
        resp = await runner_client.get("/calls")
        assert resp.json()["total"] == 3


class TestGetCallByIdEndpoint:
    async def test_get_by_uuid(self, runner_client, test_org_id):
        import uuid

        call_id = str(uuid.uuid4())
        from app.services import call_service

        call_service.create(
            call_sid="CA-GET-UUID",
            org_id=test_org_id,
            direction="inbound",
            from_number="+1111",
            to_number="+2222",
            provider="twilio",
            call_id=call_id,
        )
        resp = await runner_client.get(f"/calls/{call_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == call_id
        assert resp.json()["call_sid"] == "CA-GET-UUID"

    async def test_get_by_call_sid(self, runner_client, test_org_id):
        _seed_call(org_id=test_org_id, call_sid="CA-GET-BY-SID")
        resp = await runner_client.get("/calls/CA-GET-BY-SID")
        assert resp.status_code == 200
        assert resp.json()["call_sid"] == "CA-GET-BY-SID"

    async def test_get_nonexistent_uuid_returns_404(self, runner_client):
        resp = await runner_client.get("/calls/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    async def test_get_nonexistent_sid_returns_404(self, runner_client):
        resp = await runner_client.get("/calls/CA-DOES-NOT-EXIST")
        assert resp.status_code == 404

    async def test_get_returns_all_call_fields(self, runner_client, test_org_id):
        """Response should include the core fields from the calls table."""
        _seed_call(org_id=test_org_id, call_sid="CA-FIELDS-CHECK")
        resp = await runner_client.get("/calls/CA-FIELDS-CHECK")
        data = resp.json()
        for field in [
            "id",
            "call_sid",
            "direction",
            "from_number",
            "to_number",
            "status",
            "provider",
        ]:
            assert field in data, f"Missing field: {field}"
