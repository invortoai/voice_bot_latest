"""Integration tests for POST /calls (customer call-initiation endpoint).

Tests cover:
- Happy path: 202 + request_id + queued status
- Org validation: inactive org (403), no minutes remaining (402)
- Resource validation: assistant/phone not found or inactive, outbound disabled
- Campaign validation: unknown campaign returns 404
- Input validation: duplicate, invalid call_time, bad input_variables, bad callback_url
- Pydantic validation: missing required fields (422)
"""

import uuid

import psycopg2
import pytest

ASSISTANT_PAYLOAD = {
    "name": "Initiate Test Bot",
    "system_prompt": "You are helpful.",
}

PHONE_PAYLOAD = {
    "phone_number": "+917022111000",
    "provider": "twilio",
    "provider_credentials": {"account_sid": "AC123", "auth_token": "tok"},
    "is_outbound_enabled": True,
}


# ---------------------------------------------------------------------------
# Per-test org setup: ensure minutes are set before each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def set_org_minutes(pg_container, test_tenant):
    """Reset org minutes to a generous value before each test.

    The POST /calls handler checks minutes_consumed vs total_minutes_ordered.
    Without this fixture the columns may be NULL → Python TypeError → 500.
    """
    with psycopg2.connect(pg_container) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE organizations
                SET total_minutes_ordered = 9999,
                    minutes_consumed      = 0
                WHERE id = %s
                """,
                (test_tenant["org_id"],),
            )


# ---------------------------------------------------------------------------
# Resource fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def assistant(runner_client):
    resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture
async def phone(runner_client):
    resp = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _call_payload(
    assistant_id, phone_number_id, to_number="+917022999888", **overrides
):
    base = {
        "assistant_id": assistant_id,
        "phone_number_id": phone_number_id,
        "to_number": to_number,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestInitiateCallHappyPath:
    async def test_returns_202(self, runner_client, assistant, phone):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        assert resp.status_code == 202

    async def test_response_contains_uuid_request_id(
        self, runner_client, assistant, phone
    ):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        data = resp.json()
        assert "request_id" in data
        uuid.UUID(data["request_id"])  # raises if not a valid UUID

    async def test_response_status_is_queued(self, runner_client, assistant, phone):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        assert resp.json()["status"] == "queued"

    async def test_response_contains_message_field(
        self, runner_client, assistant, phone
    ):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        assert "message" in resp.json()

    async def test_with_all_optional_fields(self, runner_client, assistant, phone):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                priority=1,
                callback_url="https://example.com/webhook",
                additional_data={"crm_id": "CRM-123"},
                input_variables={
                    "first_name": "Alice",
                    "last_name": "Smith",
                    "product": "Pro plan",
                    "lead_score": "95",
                },
                external_customer_id="EXT-001",
            ),
        )
        assert resp.status_code == 202

    async def test_with_future_call_time(self, runner_client, assistant, phone):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                call_time="2030-01-01T10:00:00Z",
            ),
        )
        assert resp.status_code == 202

    async def test_default_priority_is_five(
        self, runner_client, assistant, phone, pg_container
    ):
        """When priority is omitted, the row should be stored with priority=5."""
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        request_id = resp.json()["request_id"]
        with psycopg2.connect(pg_container) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT priority FROM call_requests WHERE id = %s", (request_id,)
                )
                row = cur.fetchone()
        assert row[0] == 5


# ---------------------------------------------------------------------------
# Org-level validation
# ---------------------------------------------------------------------------


class TestInitiateCallOrgValidation:
    async def test_inactive_org_returns_403(
        self, runner_client, assistant, phone, pg_container, test_tenant
    ):
        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE organizations SET is_active = FALSE WHERE id = %s",
                    (test_tenant["org_id"],),
                )
        try:
            resp = await runner_client.post(
                "/calls",
                json=_call_payload(assistant["id"], phone["id"]),
            )
            assert resp.status_code == 403
        finally:
            # Restore so other tests in this class are not affected
            with psycopg2.connect(pg_container) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE organizations SET is_active = TRUE WHERE id = %s",
                        (test_tenant["org_id"],),
                    )

    async def test_no_minutes_remaining_returns_402(
        self, runner_client, assistant, phone, pg_container, test_tenant
    ):
        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE organizations
                    SET total_minutes_ordered = 100,
                        minutes_consumed      = 100
                    WHERE id = %s
                    """,
                    (test_tenant["org_id"],),
                )
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        assert resp.status_code == 402


# ---------------------------------------------------------------------------
# Resource-level validation
# ---------------------------------------------------------------------------


class TestInitiateCallResourceValidation:
    async def test_unknown_assistant_returns_404(self, runner_client, phone):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(str(uuid.uuid4()), phone["id"]),
        )
        assert resp.status_code == 404

    async def test_inactive_assistant_returns_400(
        self, runner_client, assistant, phone, pg_container
    ):
        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE assistants SET is_active = FALSE WHERE id = %s",
                    (assistant["id"],),
                )
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        assert resp.status_code == 400

    async def test_unknown_phone_returns_404(self, runner_client, assistant):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], str(uuid.uuid4())),
        )
        assert resp.status_code == 404

    async def test_inactive_phone_returns_400(
        self, runner_client, assistant, phone, pg_container
    ):
        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE phone_numbers SET is_active = FALSE WHERE id = %s",
                    (phone["id"],),
                )
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], phone["id"]),
        )
        assert resp.status_code == 400

    async def test_outbound_disabled_phone_returns_400(self, runner_client, assistant):
        # Create a phone with outbound explicitly disabled
        create_resp = await runner_client.post(
            "/phone-numbers",
            json={
                "phone_number": "+917022555777",
                "provider": "twilio",
                "provider_credentials": {"account_sid": "AC123", "auth_token": "tok"},
                "is_outbound_enabled": False,
            },
        )
        no_outbound_phone = create_resp.json()
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(assistant["id"], no_outbound_phone["id"]),
        )
        assert resp.status_code == 400
        assert "disabled" in resp.json()["detail"].lower()

    async def test_unknown_campaign_returns_404(self, runner_client, assistant, phone):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                invorto_campaign_id=str(uuid.uuid4()),
            ),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInitiateCallInputValidation:
    async def test_invalid_call_time_returns_400(self, runner_client, assistant, phone):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                call_time="not-a-valid-datetime",
            ),
        )
        assert resp.status_code == 400

    async def test_duplicate_call_request_returns_409(
        self, runner_client, assistant, phone
    ):
        payload = _call_payload(assistant["id"], phone["id"])
        # First request succeeds
        r1 = await runner_client.post("/calls", json=payload)
        assert r1.status_code == 202
        # Same to_number + org → duplicate
        r2 = await runner_client.post("/calls", json=payload)
        assert r2.status_code == 409

    async def test_duplicate_detail_contains_number(
        self, runner_client, assistant, phone
    ):
        payload = _call_payload(assistant["id"], phone["id"])
        await runner_client.post("/calls", json=payload)
        r2 = await runner_client.post("/calls", json=payload)
        assert "+917022999888" in r2.json()["detail"]

    async def test_non_string_input_variable_returns_422(
        self, runner_client, assistant, phone
    ):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                input_variables={"count": 42},  # integer — must be string
            ),
        )
        assert resp.status_code == 422

    async def test_too_many_input_variables_returns_422(
        self, runner_client, assistant, phone
    ):
        too_many = {f"key_{i}": "value" for i in range(21)}
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                input_variables=too_many,
            ),
        )
        assert resp.status_code == 422

    async def test_input_variable_too_long_returns_422(
        self, runner_client, assistant, phone
    ):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                input_variables={"long_val": "x" * 501},
            ),
        )
        assert resp.status_code == 422

    async def test_input_variable_exactly_500_chars_passes(
        self, runner_client, assistant, phone
    ):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                to_number="+917022888777",
                input_variables={"val": "x" * 500},
            ),
        )
        assert resp.status_code == 202

    async def test_non_https_callback_url_returns_422(
        self, runner_client, assistant, phone
    ):
        resp = await runner_client.post(
            "/calls",
            json=_call_payload(
                assistant["id"],
                phone["id"],
                callback_url="http://not-secure.example.com/webhook",
            ),
        )
        assert resp.status_code == 422

    async def test_missing_required_fields_returns_422(self, runner_client):
        resp = await runner_client.post("/calls", json={})
        assert resp.status_code == 422

    async def test_missing_to_number_returns_422(self, runner_client, assistant, phone):
        resp = await runner_client.post(
            "/calls",
            json={"assistant_id": assistant["id"], "phone_number_id": phone["id"]},
        )
        assert resp.status_code == 422
